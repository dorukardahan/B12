#!/bin/bash
# B12 Codex CLI — Notify Hook (agent-turn-complete)
#
# Triggered by Codex CLI after every agent turn. Uses debouncing to
# avoid processing mid-session — only processes when the session appears
# to have ended (no new turns for 2+ minutes).
#
# Config in ~/.codex/config.toml:
#   notify = ["/path/to/b12-codex-notify.sh"]
#
# Codex passes a JSON payload as $1 (first argument) with:
#   type, thread-id, turn-id, input-messages, last-assistant-message

# pipefail: surface a real exit from the upstream command in any pipe chain.
set -o pipefail 2>/dev/null || true

# Central data directory
B12_BASE="${B12_DATA_DIR:-$HOME/.B12}"
B12_SCRIPTS="${B12_HOOK_DIR:-$HOME/.B12/hooks}/scripts"
STATE_DIR="$B12_BASE/memory-logs"
DEBOUNCE_FILE="$STATE_DIR/codex-notify-debounce.json"
CODEX_SESSIONS="${CODEX_HOME:-$HOME/.codex}/sessions"
DEBUG_LOG="$STATE_DIR/codex-notify-debug.log"

mkdir -p "$STATE_DIR"

# ── Extract thread ID ──
# Codex passes JSON as $1 (argv). Fallback: try stdin.
THREAD_ID=""
PAYLOAD=""

# Try $1 first (Codex standard)
if [ -n "$1" ]; then
  PAYLOAD="$1"
  THREAD_ID=$(echo "$PAYLOAD" | jq -r '.["thread-id"] // empty' 2>/dev/null)
fi

# Fallback: try remaining args
if [ -z "$THREAD_ID" ]; then
  for arg in "$@"; do
    tid=$(echo "$arg" | jq -r '.["thread-id"] // empty' 2>/dev/null)
    [ -n "$tid" ] && THREAD_ID="$tid" && break
  done
fi

# Fallback: try stdin (if not a terminal)
if [ -z "$THREAD_ID" ] && [ ! -t 0 ]; then
  PAYLOAD=$(cat)
  THREAD_ID=$(echo "$PAYLOAD" | jq -r '.["thread-id"] // empty' 2>/dev/null)
fi

# Debug: log every invocation
{
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] args=$# thread=$THREAD_ID"
} >> "$DEBUG_LOG" 2>/dev/null

# If we still don't have a thread ID, exit silently
[ -z "$THREAD_ID" ] && exit 0

NOW=$(date +%s)

# ── Debounce logic ──
# Record this turn's timestamp. A background check will process
# the session if no new turns arrive within DEBOUNCE_SECONDS.
DEBOUNCE_SECONDS=120

# Update last-seen timestamp for this thread
python3 - "$DEBOUNCE_FILE" "$THREAD_ID" "$NOW" << 'PYEOF' 2>/dev/null
import json, sys

debounce_file = sys.argv[1]
thread_id = sys.argv[2]
now = int(sys.argv[3])

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
PYEOF

# ── Background deferred processing ──
# Fork a background process that waits DEBOUNCE_SECONDS, then checks
# if this was the last turn. If so, process the session.
(
  sleep "$DEBOUNCE_SECONDS"

  # Re-read state to check if a newer turn arrived since we started
  LAST_SEEN=$(python3 - "$DEBOUNCE_FILE" "$THREAD_ID" << 'PYEOF' 2>/dev/null
import json, sys
try:
    state = json.load(open(sys.argv[1]))
    print(int(state.get(sys.argv[2], 0)))
except: print(0)
PYEOF
)

  # If a newer turn arrived after us (LAST_SEEN > our NOW), skip
  # Only the last turn's background process should fire
  if [ "$LAST_SEEN" -gt "$NOW" ]; then
    exit 0
  fi

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
    python3 - "$DEBOUNCE_FILE" "$THREAD_ID" << 'PYEOF' 2>/dev/null
import json, sys
try:
    state = json.load(open(sys.argv[1]))
    state.pop(sys.argv[2], None)
    json.dump(state, open(sys.argv[1], 'w'))
except: pass
PYEOF
  fi
) &

exit 0
