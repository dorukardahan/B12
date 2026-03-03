#!/bin/bash
# B12 Memory System - Proactive Surfacing Hook (v1)
# Surfaces relevant memories when user reads/edits files or encounters errors.
# Fires on: PreToolUse (Read/Edit/Write), PostToolUse (Bash with errors)
# Output: JSON with additionalContext (surfaced memories) or empty {}
# Performance target: <500ms surfacing engine, <5s total hook

# ── Self-timeout watchdog ─────────────────────────────────────
( sleep 5 && kill -TERM $$ 2>/dev/null ) &
_WATCHDOG=$!
trap "kill $_WATCHDOG 2>/dev/null; wait $_WATCHDOG 2>/dev/null" EXIT

INPUT=$(cat)
TOOL_NAME=$(echo "$INPUT" | jq -r '.tool_name // ""')
HOOK_EVENT=$(echo "$INPUT" | jq -r '.hook_event_name // ""')

B12_BASE="${B12_DATA_DIR:-$HOME/.B12}"
B12_SCRIPTS="${B12_HOOK_DIR:-$HOME/.B12/hooks}/scripts"
STATE_FILE="$B12_BASE/surfacing-state.json"

# Determine trigger type and context based on hook event
TRIGGER_TYPE=""
TRIGGER_CONTEXT=""

if [ "$HOOK_EVENT" = "PreToolUse" ]; then
  case "$TOOL_NAME" in
    Read|Edit|Write)
      TRIGGER_TYPE="file"
      TRIGGER_CONTEXT=$(echo "$INPUT" | jq -r '.tool_input.file_path // ""' 2>/dev/null)
      ;;
  esac
elif [ "$HOOK_EVENT" = "PostToolUse" ]; then
  if [ "$TOOL_NAME" = "Bash" ]; then
    # Check if the tool result contains an error
    TOOL_RESULT=$(echo "$INPUT" | jq -r '.tool_result // ""' 2>/dev/null)
    # Look for error indicators in bash output
    if echo "$TOOL_RESULT" | grep -qiE '(error|failed|exception|traceback|errno|permission denied|not found|command not found)' 2>/dev/null; then
      TRIGGER_TYPE="error"
      TRIGGER_CONTEXT=$(echo "$TOOL_RESULT" | head -5 | head -c 500)
    fi
  fi
fi

# No trigger → pass through
if [ -z "$TRIGGER_TYPE" ] || [ -z "$TRIGGER_CONTEXT" ] || [ "$TRIGGER_CONTEXT" = "null" ]; then
  echo '{}'
  exit 0
fi

# ── Invoke surfacing engine ───────────────────────────────────
SURFACING_SCRIPT="$B12_SCRIPTS/surfacing_engine.py"
if [ ! -f "$SURFACING_SCRIPT" ]; then
  echo '{}'
  exit 0
fi

# Run surfacing engine via Python heredoc
RESULT=$(python3 - "$TRIGGER_TYPE" "$TRIGGER_CONTEXT" "$STATE_FILE" << 'PYEOF'
import sys, os

trigger_type = sys.argv[1]
trigger_context = sys.argv[2]
state_path = sys.argv[3]

# Import surfacing engine
_hook_dir = os.environ.get('B12_HOOK_DIR', os.path.expanduser('~/.B12/hooks'))
sys.path.insert(0, os.path.join(_hook_dir, 'scripts'))

try:
    from surfacing_engine import surface, format_for_context

    result = surface(
        trigger_type=trigger_type,
        context=trigger_context,
        state_path=state_path,
    )

    if result.surfaced:
        context_text = format_for_context(result)
        if context_text:
            # Output the context text (hook will wrap in JSON)
            print(context_text)
        else:
            print("")
    else:
        # Increment tool call counter (for rate limiting)
        from surfacing_engine import _increment_tool_calls
        _increment_tool_calls(state_path)
        print("")
except Exception as e:
    import traceback
    sys.stderr.write(f"B12 surfacing error: {e}\n{traceback.format_exc()}\n")
    print("")
PYEOF
)

# If surfacing returned context, inject it
if [ -n "$RESULT" ] && [ "$RESULT" != "" ]; then
  # Escape for JSON
  ESCAPED=$(echo "$RESULT" | jq -Rs '.' 2>/dev/null | sed 's/^"//;s/"$//')
  cat <<SURF_EOF
{
  "additionalContext": "${ESCAPED}"
}
SURF_EOF
else
  echo '{}'
fi

exit 0
