#!/bin/bash
# B12 Codex CLI — Stop hook (Plan §2 CX1).
#
# Replaces the legacy `notify = [...]` debounce hook with the formal
# Stop event surface (hooks GA on 2026-05-14). Wire input shape:
# codex-rs/hooks/src/schema.rs:450
#   {session_id, turn_id, transcript_path, cwd, hook_event_name,
#    model, permission_mode, stop_hook_active, last_assistant_message}
#
# Field defense (doc-vs-wire — Claude Code PR #39 surfaced a parallel
# `last_assistant_message` vs `response` rename mid-release on the
# Claude side). The jq fallback `(.a // "") as $x | (.b // "") as $y |
# if ($x|length)>0 then $x else $y end` ensures we keep working through
# any near-term Codex 0.13x rename.
#
# Output: empty (no `{"decision": "block"}`) — never blocks. Background-
# forks codex_session_end.py against the active rollout so the heavy
# extraction work doesn't gate the model's perception of turn-end.
#
# Fail-open guard wrapper — issue #22008 critical: a Stop hook that
# returns non-zero or panics can spawn a hidden memory_consolidation
# subagent that burns a 5-hour Pro quota.

{
  set -o pipefail 2>/dev/null || true

  B12_BASE="${B12_DATA_DIR:-$HOME/.B12}"
  B12_SCRIPTS="${B12_HOOK_DIR:-$HOME/.B12/hooks}/scripts"
  STATE_DIR="$B12_BASE/state"
  LOG_DIR="$B12_BASE/memory-logs"
  ERR_LOG="$LOG_DIR/codex-hook-errors.log"
  SESS_LOG="$LOG_DIR/codex-session-end.log"
  mkdir -p "$STATE_DIR" "$LOG_DIR" 2>/dev/null || true

  INPUT=""
  if [ ! -t 0 ]; then
    INPUT=$(cat)
  fi

  SID=$(printf '%s' "$INPUT" | jq -r '.session_id // empty' 2>/dev/null)
  TURN_ID=$(printf '%s' "$INPUT" | jq -r '.turn_id // empty' 2>/dev/null)
  TRANSCRIPT=$(printf '%s' "$INPUT" | jq -r '.transcript_path // empty' 2>/dev/null)

  # Doc-vs-wire defense for last_assistant_message.
  LAST=$(printf '%s' "$INPUT" | jq -r '
    (.last_assistant_message // "") as $a
    | (.response // "") as $b
    | (.lastAssistantMessage // "") as $c
    | if ($a|length) > 0 then $a
      elif ($b|length) > 0 then $b
      else $c end
  ' 2>/dev/null)

  [ -z "$SID" ] && exit 0

  # If an active /goal is in flight, append turn progress so the next
  # SessionStart can re-prime against it. Best-effort.
  ACTIVE_GOAL_FILE="$STATE_DIR/active-codex-goal-${SID}.txt"
  if [ -f "$ACTIVE_GOAL_FILE" ] && [ -n "$LAST" ]; then
    PROG_FILE="$STATE_DIR/codex-goal-progress-${SID}.log"
    {
      printf '\n[%s] turn=%s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "${TURN_ID:-?}"
      printf '%s\n' "${LAST:0:600}"
    } >> "$PROG_FILE" 2>/dev/null || true
  fi

  # Debounce. Codex review PR #41 round 2 caught the bug: Stop fires
  # at EVERY turn-end (not session-end), but codex_session_end.py
  # marks any session with <3 messages as "processed" on first run —
  # so an early turn (turn 1 or 2) gets permanently marked processed,
  # and the rich extraction at real session-end never happens.
  #
  # Pattern (mirrors the legacy b12-codex-notify.sh debounce):
  #   1. Stamp this turn's timestamp in a per-session debounce file.
  #   2. Fork a background sleeper that wakes after DEBOUNCE_SECONDS
  #      and only proceeds if no newer turn stamped over its NOW value.
  #   3. Last-turn's background sleeper wins; earlier ones noop.
  DEBOUNCE_FILE="$LOG_DIR/codex-stop-debounce.json"
  DEBOUNCE_SECONDS=120
  NOW=$(date +%s)

  python3 - "$DEBOUNCE_FILE" "$SID" "$NOW" << 'PYEOF' 2>/dev/null
import json, sys
debounce_file, sess_id, now = sys.argv[1], sys.argv[2], int(sys.argv[3])
try:
    with open(debounce_file, 'r') as f:
        state = json.load(f)
except (FileNotFoundError, json.JSONDecodeError):
    state = {}
state[sess_id] = now
# Drop entries older than 1h to keep the file bounded.
state = {k: v for k, v in state.items() if now - v < 3600}
with open(debounce_file, 'w') as f:
    json.dump(state, f)
PYEOF

  # Locate the rollout file. Same fallback the legacy notify hook uses.
  ROLLOUT=""
  if [ -n "$TRANSCRIPT" ] && [ -f "$TRANSCRIPT" ]; then
    ROLLOUT="$TRANSCRIPT"
  else
    SESS_ROOT="${CODEX_HOME:-$HOME/.codex}/sessions"
    if [ -d "$SESS_ROOT" ]; then
      ROLLOUT=$(find "$SESS_ROOT" -name "rollout-*${SID}*.jsonl" -type f 2>/dev/null | head -1)
    fi
  fi

  # Background fork the debounce sleeper. Returns immediately so Codex's
  # turn-end is never blocked. Inherits the fail-open guard via its own
  # subshell + redirect.
  if [ -n "$ROLLOUT" ] && [ -f "$ROLLOUT" ]; then
    VENV_PY="$HOME/.local/b12-venv/bin/python3"
    if [ -x "$VENV_PY" ]; then PY="$VENV_PY"; else PY="python3"; fi
    (
      {
        sleep "$DEBOUNCE_SECONDS"

        # Re-read state — did a newer turn stamp over ours?
        LAST_SEEN=$(python3 - "$DEBOUNCE_FILE" "$SID" << 'INNER_PYEOF' 2>/dev/null
import json, sys
try:
    state = json.load(open(sys.argv[1]))
    print(int(state.get(sys.argv[2], 0)))
except Exception:
    print(0)
INNER_PYEOF
)
        # If a newer turn arrived after us, abort — last turn's sleeper
        # is the one that will fire.
        if [ "${LAST_SEEN:-0}" -gt "${NOW:-0}" ]; then
          exit 0
        fi

        "$PY" "$B12_SCRIPTS/codex_session_end.py" "$ROLLOUT" \
          >> "$SESS_LOG" 2>&1

        # Cleanup our entry so the file does not accrue completed sessions.
        python3 - "$DEBOUNCE_FILE" "$SID" << 'INNER2_PYEOF' 2>/dev/null
import json, sys
try:
    state = json.load(open(sys.argv[1]))
    state.pop(sys.argv[2], None)
    json.dump(state, open(sys.argv[1], 'w'))
except Exception:
    pass
INNER2_PYEOF
      } || true
    ) &
    disown 2>/dev/null || true
  fi

} 2>>"${ERR_LOG:-/dev/null}" || true
exit 0
