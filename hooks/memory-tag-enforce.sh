#!/bin/bash
# B12 Memory System - PreToolUse Tag Enforcement Hook (v1)
# Ensures every memory_store call has proper scope tags (proj:*, user:*)
# If missing, auto-injects based on CWD and setup detection
#
# Fires on: mcp__B12__memory_store (PreToolUse)
# Output: updatedInput with corrected tags, or silent allow if compliant

# ── Self-timeout watchdog ─────────────────────────────────────
( sleep 5 && kill -TERM $$ 2>/dev/null ) &
_WATCHDOG=$!
trap "kill $_WATCHDOG 2>/dev/null; wait $_WATCHDOG 2>/dev/null" EXIT

INPUT=$(cat)
CWD=$(echo "$INPUT" | jq -r '.cwd // ""')
TAGS_RAW=$(echo "$INPUT" | jq -r '.tool_input.tags // empty' 2>/dev/null)

# Central data directory
B12_BASE="${B12_DATA_DIR:-$HOME/.B12}"

PROJECT_NAME=$(basename "$CWD" 2>/dev/null || echo "unknown")

# Setup detection (set B12_WORK_PATTERN env var to match your work dirs)
_WORK_PAT="${B12_WORK_PATTERN:-}"
_WORK_PAT_LOWER=$(echo "$_WORK_PAT" | tr '[:upper:]' '[:lower:]')
if [ -n "$_WORK_PAT" ] && { [[ "$B12_BASE" == *"$_WORK_PAT"* ]] || [[ "$CWD" == *"/$_WORK_PAT"* ]] || [[ "$CWD" == *"/${_WORK_PAT_LOWER}"* ]]; }; then
  SETUP="work"
else
  SETUP="personal"
fi

# Normalize tags to a single string for checking
# tags can be: string ("a,b,c"), JSON array (["a","b"]), or empty
TAGS_STR=""
if echo "$INPUT" | jq -e '.tool_input.tags | type == "array"' > /dev/null 2>&1; then
  TAGS_STR=$(echo "$INPUT" | jq -r '.tool_input.tags | join(",")' 2>/dev/null)
elif [ -n "$TAGS_RAW" ] && [ "$TAGS_RAW" != "null" ]; then
  TAGS_STR="$TAGS_RAW"
fi

# Also check metadata.tags
META_TAGS=$(echo "$INPUT" | jq -r '.tool_input.metadata.tags // empty' 2>/dev/null)
if [ -n "$META_TAGS" ] && [ "$META_TAGS" != "null" ]; then
  if [ -n "$TAGS_STR" ]; then
    TAGS_STR="${TAGS_STR},${META_TAGS}"
  else
    TAGS_STR="$META_TAGS"
  fi
fi

# Check compliance
HAS_PROJ=false
HAS_USER=false
if echo "$TAGS_STR" | grep -q "proj:"; then
  HAS_PROJ=true
fi
if echo "$TAGS_STR" | grep -q "user:"; then
  HAS_USER=true
fi

# If both present, allow silently
if $HAS_PROJ && $HAS_USER; then
  exit 0
fi

# Build missing tags
MISSING=""
if ! $HAS_PROJ; then
  MISSING="proj:${PROJECT_NAME}"
fi
if ! $HAS_USER; then
  if [ -n "$MISSING" ]; then
    MISSING="${MISSING},user:${SETUP}"
  else
    MISSING="user:${SETUP}"
  fi
fi

# Determine updated tags value
if echo "$INPUT" | jq -e '.tool_input.tags | type == "array"' > /dev/null 2>&1; then
  # Tags is an array — append missing tags as array elements
  IFS=',' read -ra PARTS <<< "$MISSING"
  ADDITIONS=$(printf '%s\n' "${PARTS[@]}" | jq -R . | jq -s .)
  UPDATED_TAGS=$(echo "$INPUT" | jq --argjson add "$ADDITIONS" '.tool_input.tags + $add')

  cat <<EOF
{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "allow",
    "permissionDecisionReason": "Auto-injected scope tags: ${MISSING}",
    "updatedInput": {
      "tags": ${UPDATED_TAGS}
    }
  }
}
EOF
elif [ -n "$TAGS_STR" ]; then
  # Tags is a non-empty string — append
  NEW_TAGS="${TAGS_STR},${MISSING}"

  cat <<EOF
{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "allow",
    "permissionDecisionReason": "Auto-injected scope tags: ${MISSING}",
    "updatedInput": {
      "tags": "${NEW_TAGS}"
    }
  }
}
EOF
else
  # No tags at all — create new string
  cat <<EOF
{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "allow",
    "permissionDecisionReason": "Auto-injected scope tags: ${MISSING}",
    "updatedInput": {
      "tags": "${MISSING}"
    }
  }
}
EOF
fi

exit 0
