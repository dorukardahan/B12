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

# Build complete updatedInput preserving ALL original fields.
# updatedInput REPLACES the entire tool_input — we must include everything.
ORIGINAL=$(echo "$INPUT" | jq '.tool_input')

# Determine where tags live and update them in place
if echo "$INPUT" | jq -e '.tool_input.tags | type == "array"' > /dev/null 2>&1; then
  # Tags is a top-level array — append missing tags as array elements
  IFS=',' read -ra PARTS <<< "$MISSING"
  ADDITIONS=$(printf '%s\n' "${PARTS[@]}" | jq -R . | jq -s .)
  UPDATED=$(echo "$ORIGINAL" | jq --argjson add "$ADDITIONS" '.tags = (.tags + $add)')

elif echo "$INPUT" | jq -e '.tool_input.tags // empty | length > 0' > /dev/null 2>&1; then
  # Tags is a top-level non-empty string — append
  NEW_TAGS="${TAGS_STR},${MISSING}"
  UPDATED=$(echo "$ORIGINAL" | jq --arg t "$NEW_TAGS" '.tags = $t')

elif [ -n "$META_TAGS" ] && [ "$META_TAGS" != "null" ]; then
  # Tags only in metadata.tags — update there
  NEW_TAGS="${META_TAGS},${MISSING}"
  UPDATED=$(echo "$ORIGINAL" | jq --arg t "$NEW_TAGS" '.metadata.tags = $t')

elif echo "$INPUT" | jq -e '.tool_input.metadata' > /dev/null 2>&1; then
  # Has metadata but no tags — inject into metadata.tags
  UPDATED=$(echo "$ORIGINAL" | jq --arg t "$MISSING" '.metadata.tags = $t')

else
  # No metadata at all — inject top-level tags
  UPDATED=$(echo "$ORIGINAL" | jq --arg t "$MISSING" '.tags = $t')
fi

# Emit the complete updatedInput (jq ensures valid JSON)
echo "$UPDATED" | jq '{
  hookSpecificOutput: {
    hookEventName: "PreToolUse",
    permissionDecision: "allow",
    permissionDecisionReason: ("Auto-injected scope tags: " + $reason),
    updatedInput: .
  }
}' --arg reason "$MISSING"

exit 0
