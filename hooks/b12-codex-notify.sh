#!/bin/bash
# B12 Codex CLI — Notify Hook (agent-turn-complete)
#
# Triggered by Codex CLI after every agent turn. Uses debouncing to
# avoid processing mid-session — only processes when the session appears
# to have ended (no new turns for 2+ minutes).
#
# Config in ~/.codex/config.toml:
#   notify = ["bash", "-lc", "~/.claude/hooks/b12-codex-notify.sh"]
#
# The hook receives a JSON payload on argv (not stdin) with:
#   type, thread-id, turn-id, input-messages, last-assistant-message

# Central data directory
B12_BASE="${B12_DATA_DIR:-$HOME/.claude}"
B12_SCRIPTS="${B12_HOOK_DIR:-$HOME/.claude/hooks}/scripts"
STATE_DIR="$B12_BASE/memory-logs"
DEBOUNCE_FILE="$STATE_DIR/codex-notify-debounce.json"
CODEX_SESSIONS="${CODEX_HOME:-$HOME/.codex}/sessions"

mkdir -p "$STATE_DIR"

# ── Extract thread ID from environment or args ──
# Codex passes notify payload differently depending on version.
# Try parsing JSON from stdin first, then args.
THREAD_ID=""
if [ -t 0 ]; then
  # No stdin, check args
  for arg in "$@"; do
    # Try to extract thread-id from JSON arg
    tid=$(echo "$arg" | jq -r '.["thread-id"] // empty' 2>/dev/null)
    [ -n "$tid" ] && THREAD_ID="$tid" && break
  done
else
  INPUT=$(cat)
  THREAD_ID=$(echo "$INPUT" | jq -r '.["thread-id"] // empty' 2>/dev/null)
fi

# If we still don't have a thread ID, exit silently
[ -z "$THREAD_ID" ] && exit 0

NOW=$(date +%s)

# ── Debounce logic ──
# Record this turn's timestamp. A background check will process
# the session if no new turns arrive within DEBOUNCE_SECONDS.
DEBOUNCE_SECONDS=120

# Update last-seen timestamp for this thread
python3 -c "
import json, sys, os

debounce_file = '$DEBOUNCE_FILE'
thread_id = '$THREAD_ID'
now = $NOW

try:
    with open(debounce_file, 'r') as f:
        state = json.load(f)
except (FileNotFoundError, json.JSONDecodeError):
    state = {}

state[thread_id] = now

# Clean old entries (older than 1 hour)
state = {k: v for k, v in state.items() if now - v < 3600}

with open(debounce_file, 'w') as f:
    json.dump(state, f)
" 2>/dev/null

# ── Background deferred processing ──
# Fork a background process that waits DEBOUNCE_SECONDS, then checks
# if this was the last turn. If so, process the session.
(
  sleep "$DEBOUNCE_SECONDS"

  # Re-read state to check if a newer turn arrived
  LAST_SEEN=$(python3 -c "
import json
try:
    state = json.load(open('$DEBOUNCE_FILE'))
    print(state.get('$THREAD_ID', 0))
except: print(0)
" 2>/dev/null)

  CURRENT=$(date +%s)
  ELAPSED=$((CURRENT - LAST_SEEN))

  # Only process if no new turns in the debounce window
  if [ "$ELAPSED" -ge "$DEBOUNCE_SECONDS" ]; then
    # Find the rollout file for this thread
    ROLLOUT=$(find "$CODEX_SESSIONS" -name "rollout-*${THREAD_ID}*.jsonl" -type f 2>/dev/null | head -1)

    if [ -n "$ROLLOUT" ] && [ -f "$ROLLOUT" ]; then
      # Use b12-venv Python if available, otherwise system Python
      VENV_PYTHON="$HOME/.local/b12-venv/bin/python3"
      if [ -x "$VENV_PYTHON" ]; then
        PY="$VENV_PYTHON"
      else
        PY="python3"
      fi

      "$PY" "$B12_SCRIPTS/codex_session_end.py" "$ROLLOUT" \
        >> "$STATE_DIR/codex-session-end.log" 2>&1
    fi

    # Remove this thread from debounce state
    python3 -c "
import json
try:
    state = json.load(open('$DEBOUNCE_FILE'))
    state.pop('$THREAD_ID', None)
    json.dump(state, open('$DEBOUNCE_FILE', 'w'))
except: pass
" 2>/dev/null
  fi
) &

exit 0
