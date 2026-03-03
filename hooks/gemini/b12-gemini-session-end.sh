#!/bin/bash
# B12 Gemini CLI — SessionEnd Hook Adapter
#
# Converts Gemini CLI SessionEnd event to Claude Code hook format,
# calls the shared memory-session-end.sh for session summary extraction.
#
# Gemini CLI input (stdin):
#   { "session_id": "...", "transcript_path": "...", "cwd": "...",
#     "hook_event_name": "SessionEnd", "timestamp": "...",
#     "reason": "exit|clear|logout|prompt_input_exit|other" }
#
# Gemini CLI output (stdout):
#   { "systemMessage": "..." }
#
# NOTE: Gemini CLI does NOT wait for SessionEnd hooks to complete and
# ignores all flow-control fields. We fork processing to background
# to avoid blocking the CLI exit.
#
# Config in ~/.gemini/settings.json:
#   "hooks": { "SessionEnd": [{ "hooks": [{ "type": "command",
#     "command": "~/.B12/hooks/gemini/b12-gemini-session-end.sh" }] }] }

set -euo pipefail

exec 3>&2

B12_HOOK_DIR="${B12_HOOK_DIR:-$HOME/.B12/hooks}"
B12_HOOK="$B12_HOOK_DIR/memory-session-end.sh"
B12_BASE="${B12_DATA_DIR:-$HOME/.B12}"
LOG_DIR="$B12_BASE/memory-logs"

mkdir -p "$LOG_DIR"

# Read Gemini CLI input
INPUT=$(cat)

# Check B12 hook exists
if [ ! -f "$B12_HOOK" ]; then
  echo '{}'
  exit 0
fi

# ── Extract Gemini fields ──
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // "gemini-unknown"')
REASON=$(echo "$INPUT" | jq -r '.reason // "other"')
CWD=$(echo "$INPUT" | jq -r '.cwd // ""')
TRANSCRIPT_PATH=$(echo "$INPUT" | jq -r '.transcript_path // ""')

# ── Locate Gemini session transcript ──
# Gemini CLI stores session data in ~/.gemini/history/ as JSON files.
# If transcript_path is provided by Gemini, use it directly.
# Otherwise, try to find the most recent session file.
if [ -z "$TRANSCRIPT_PATH" ] || [ ! -f "$TRANSCRIPT_PATH" ]; then
  GEMINI_HISTORY="$HOME/.gemini/history"
  if [ -d "$GEMINI_HISTORY" ]; then
    # Find most recent .json file modified in the last 30 minutes
    TRANSCRIPT_PATH=$(find "$GEMINI_HISTORY" -name "*.json" -mmin -30 -type f 2>/dev/null \
      | sort -t/ -k$(echo "$GEMINI_HISTORY" | tr -cd '/' | wc -c | tr -d ' ') -r \
      | head -1)
  fi
fi

# ── Convert Gemini transcript to Claude Code JSONL format ──
# Claude Code SessionEnd expects a JSONL transcript with:
#   {"type":"human","message":{"content":"..."}}
#   {"type":"assistant","message":{"content":[{"type":"text","text":"..."},{"type":"tool_use","name":"...","input":{}}]}}
#
# Gemini stores history differently. We convert on-the-fly.
CONVERTED_TRANSCRIPT=""

if [ -n "$TRANSCRIPT_PATH" ] && [ -f "$TRANSCRIPT_PATH" ]; then
  CONVERTED_TRANSCRIPT=$(mktemp /tmp/b12-gemini-transcript-XXXXXX.jsonl)

  set +e
  python3 - "$TRANSCRIPT_PATH" "$CONVERTED_TRANSCRIPT" 2>&3 << 'PYEOF'
import sys, json, os

src_path = sys.argv[1]
dst_path = sys.argv[2]

try:
    with open(src_path, 'r') as f:
        data = json.load(f)
except (json.JSONDecodeError, FileNotFoundError):
    # Try JSONL format (one object per line)
    try:
        data = []
        with open(src_path, 'r') as f:
            for line in f:
                line = line.strip()
                if line:
                    data.append(json.loads(line))
    except Exception:
        sys.exit(0)

lines = []

# Handle both array-of-messages and nested structures
messages = data if isinstance(data, list) else data.get('messages', data.get('turns', []))

for msg in messages:
    if not isinstance(msg, dict):
        continue

    role = msg.get('role', msg.get('author', '')).lower()
    content = msg.get('content', msg.get('text', msg.get('parts', '')))

    # Normalize content to string
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                parts.append(part.get('text', ''))
            elif isinstance(part, str):
                parts.append(part)
        content = '\n'.join(parts)
    elif not isinstance(content, str):
        content = str(content) if content else ''

    if not content.strip():
        continue

    if role in ('user', 'human'):
        lines.append(json.dumps({
            'type': 'human',
            'message': {'content': content}
        }))
    elif role in ('model', 'assistant'):
        lines.append(json.dumps({
            'type': 'assistant',
            'message': {'content': [{'type': 'text', 'text': content}]}
        }))

with open(dst_path, 'w') as f:
    f.write('\n'.join(lines) + '\n')

PYEOF
  set -e
fi

# ── Build Claude Code format input ──
# Claude Code SessionEnd expects:
#   { "session_id": "...", "reason": "...", "cwd": "...", "transcript_path": "..." }
EFFECTIVE_TRANSCRIPT="${CONVERTED_TRANSCRIPT:-$TRANSCRIPT_PATH}"

CLAUDE_INPUT=$(jq -n \
  --arg session_id "$SESSION_ID" \
  --arg reason "$REASON" \
  --arg cwd "$CWD" \
  --arg transcript_path "${EFFECTIVE_TRANSCRIPT:-}" \
  '{ "session_id": $session_id, "reason": $reason, "cwd": $cwd, "transcript_path": $transcript_path }')

# ── Call B12 hook in background ──
# Gemini CLI doesn't wait for SessionEnd, so we fork to avoid losing work
(
  echo "$CLAUDE_INPUT" | bash "$B12_HOOK" > /dev/null 2>&1

  # Clean up converted transcript
  [ -n "$CONVERTED_TRANSCRIPT" ] && [ -f "$CONVERTED_TRANSCRIPT" ] && rm -f "$CONVERTED_TRANSCRIPT"

  # Log completion
  echo "{\"event\":\"gemini_session_end\",\"session\":\"$SESSION_ID\",\"time\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}" \
    >> "$LOG_DIR/gemini-hooks.jsonl" 2>/dev/null
) &
disown 2>/dev/null

# Return immediately so Gemini CLI can exit
echo '{"systemMessage": "B12: session saved"}'

exit 0
