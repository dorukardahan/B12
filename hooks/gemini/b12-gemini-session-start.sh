#!/bin/bash
# B12 Gemini CLI — SessionStart Hook Adapter
#
# Converts Gemini CLI SessionStart event to Claude Code hook format,
# calls the shared memory-session-start.sh, and converts output back.
#
# Gemini CLI input (stdin):
#   { "session_id": "...", "transcript_path": "...", "cwd": "...",
#     "hook_event_name": "SessionStart", "timestamp": "...",
#     "source": "startup|resume|clear" }
#
# Gemini CLI output (stdout):
#   { "hookSpecificOutput": { "hookEventName": "SessionStart",
#     "additionalContext": "..." } }
#
# Config in ~/.gemini/settings.json:
#   "hooks": { "SessionStart": [{ "hooks": [{ "type": "command",
#     "command": "~/.B12/hooks/gemini/b12-gemini-session-start.sh" }] }] }

set -euo pipefail

# All logs to stderr — stdout is reserved for JSON output
exec 3>&2

B12_HOOK_DIR="${B12_HOOK_DIR:-$HOME/.B12/hooks}"
B12_HOOK="$B12_HOOK_DIR/memory-session-start.sh"

# Read Gemini CLI input
INPUT=$(cat)

# Check B12 hook exists
if [ ! -f "$B12_HOOK" ]; then
  echo '{}' # No-op if B12 hooks not installed
  exit 0
fi

# ── Transform Gemini input to Claude Code format ──
# Claude Code SessionStart expects:
#   { "source": "startup|resume|compact", "cwd": "..." }
#
# Gemini CLI provides these fields directly, but uses "clear" instead of "compact"
SOURCE=$(echo "$INPUT" | jq -r '.source // "startup"')
CWD=$(echo "$INPUT" | jq -r '.cwd // ""')

# Map Gemini "clear" to Claude "compact" (closest semantic match)
case "$SOURCE" in
  clear) SOURCE="compact" ;;
esac

# Build Claude Code format input
CLAUDE_INPUT=$(jq -n \
  --arg source "$SOURCE" \
  --arg cwd "$CWD" \
  '{ "source": $source, "cwd": $cwd }')

# ── Call B12 hook ──
RESULT=$(echo "$CLAUDE_INPUT" | bash "$B12_HOOK" 2>&3) || true

# ── Transform output back to Gemini format ──
# Claude Code hook returns:
#   { "hookSpecificOutput": { "hookEventName": "SessionStart",
#     "additionalContext": "..." } }
#
# Gemini CLI uses the same format, so pass through directly
if [ -n "$RESULT" ] && echo "$RESULT" | jq empty 2>/dev/null; then
  echo "$RESULT"
else
  echo '{}'
fi

exit 0
