#!/bin/bash
# B12 Codex CLI — turn-scoped Stop hook.
# Codex defines Stop as "right before Codex ends its turn".  Session summary
# extraction belongs to memory-codex-session-end.sh, not this per-turn path.
#
# Output is always empty and failures are fail-open: a Stop hook must never
# block Codex or trigger a hidden continuation/subagent loop.

set -o pipefail 2>/dev/null || true

B12_BASE="${B12_DATA_DIR:-$HOME/.B12}"
STATE_DIR="$B12_BASE/state"
LOG_DIR="$B12_BASE/memory-logs"
ERR_LOG="$LOG_DIR/codex-hook-errors.log"
mkdir -p "$STATE_DIR" "$LOG_DIR" 2>/dev/null || true
exec 2>>"${ERR_LOG:-/dev/null}"

INPUT=""
if [ ! -t 0 ]; then
  INPUT=$(cat)
fi

SID=$(printf '%s' "$INPUT" | jq -r '.session_id // empty' 2>/dev/null)
TURN_ID=$(printf '%s' "$INPUT" | jq -r '.turn_id // empty' 2>/dev/null)
LAST=$(printf '%s' "$INPUT" | jq -r '
  (.last_assistant_message // "") as $a
  | (.response // "") as $b
  | (.lastAssistantMessage // "") as $c
  | if ($a|length) > 0 then $a
    elif ($b|length) > 0 then $b
    else $c end
' 2>/dev/null)

[ -z "$SID" ] && exit 0

# If an active /goal is in flight, append this turn's progress so the next
# SessionStart can re-prime against it. Best-effort and intentionally cheap.
ACTIVE_GOAL_FILE="$STATE_DIR/active-codex-goal-${SID}.txt"
if [ -f "$ACTIVE_GOAL_FILE" ] && [ -n "$LAST" ]; then
  PROG_FILE="$STATE_DIR/codex-goal-progress-${SID}.log"
  {
    printf '\n[%s] turn=%s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "${TURN_ID:-?}"
    printf '%s\n' "${LAST:0:600}"
  } >> "$PROG_FILE" 2>/dev/null || true
fi

exit 0
