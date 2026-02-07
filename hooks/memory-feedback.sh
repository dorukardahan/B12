#!/bin/bash
# B12 Memory System - PostToolUse Feedback Hook (v2 — Scope Tracking)
# Tracks memory tool usage patterns for quality improvement
#
# Fires on: mcp__memory__memory_store, mcp__memory__memory_search,
#           mcp__memory__memory_quality, mcp__memory__memory_update
# Output: Appends to feedback.jsonl — no additionalContext
#
# v2 changes:
# - Tracks scope compliance (has_proj_tag, has_scope metadata)
# - Tracks memory_quality and memory_update calls
# - Log rotation (max 5000 lines)

INPUT=$(cat)
TOOL_NAME=$(echo "$INPUT" | jq -r '.tool_name // ""')
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // ""')
CWD=$(echo "$INPUT" | jq -r '.cwd // ""')

# Central data directory — override with B12_DATA_DIR env var for custom setups
B12_BASE="${B12_DATA_DIR:-$HOME/.claude}"

PROJECT_NAME=$(basename "$CWD" 2>/dev/null || echo "unknown")
FEEDBACK_DIR="$B12_BASE/memory-logs"
FEEDBACK_FILE="$FEEDBACK_DIR/feedback.jsonl"
TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

mkdir -p "$FEEDBACK_DIR" 2>/dev/null

if [ "$TOOL_NAME" = "mcp__memory__memory_store" ]; then
  # Track store quality — metadata, tags, scope compliance
  HAS_METADATA=$(echo "$INPUT" | jq -r 'if .tool_input.metadata then "true" else "false" end' 2>/dev/null)
  HAS_TAGS=$(echo "$INPUT" | jq -r 'if .tool_input.tags then "true" else "false" end' 2>/dev/null)
  CONTENT_LEN=$(echo "$INPUT" | jq -r '.tool_input.content // "" | length' 2>/dev/null)

  # Scope compliance checks
  TAGS_STR=$(echo "$INPUT" | jq -r '.tool_input.tags // ""' 2>/dev/null)
  HAS_PROJ_TAG="false"
  if echo "$TAGS_STR" | grep -q "proj:"; then
    HAS_PROJ_TAG="true"
  fi
  HAS_SCOPE="false"
  SCOPE_VAL=$(echo "$INPUT" | jq -r '.tool_input.metadata.scope // ""' 2>/dev/null)
  if [ -n "$SCOPE_VAL" ] && [ "$SCOPE_VAL" != "null" ]; then
    HAS_SCOPE="true"
  fi

  echo "{\"ts\":\"$TIMESTAMP\",\"action\":\"store\",\"project\":\"$PROJECT_NAME\",\"session\":\"$SESSION_ID\",\"has_metadata\":$HAS_METADATA,\"has_tags\":$HAS_TAGS,\"content_length\":$CONTENT_LEN,\"has_proj_tag\":$HAS_PROJ_TAG,\"has_scope\":$HAS_SCOPE}" >> "$FEEDBACK_FILE"

elif [ "$TOOL_NAME" = "mcp__memory__memory_search" ]; then
  # Track search patterns — detect empty results
  QUERY=$(echo "$INPUT" | jq -r '.tool_input.query // ""' 2>/dev/null)
  QUERY_LEN=${#QUERY}
  RESULT_SNIPPET=$(echo "$INPUT" | jq -r '.tool_output // "" | tostring | .[0:200]' 2>/dev/null)
  IS_EMPTY="false"
  if echo "$RESULT_SNIPPET" | grep -qi "no results\|no memories\|\[\]"; then
    IS_EMPTY="true"
  fi

  echo "{\"ts\":\"$TIMESTAMP\",\"action\":\"search\",\"project\":\"$PROJECT_NAME\",\"session\":\"$SESSION_ID\",\"query_length\":$QUERY_LEN,\"empty_result\":$IS_EMPTY}" >> "$FEEDBACK_FILE"

elif [ "$TOOL_NAME" = "mcp__memory__memory_quality" ]; then
  MEMORY_HASH=$(echo "$INPUT" | jq -r '.tool_input.content_hash // ""' 2>/dev/null)
  echo "{\"ts\":\"$TIMESTAMP\",\"action\":\"quality\",\"project\":\"$PROJECT_NAME\",\"session\":\"$SESSION_ID\",\"memory_hash\":\"$MEMORY_HASH\"}" >> "$FEEDBACK_FILE"

elif [ "$TOOL_NAME" = "mcp__memory__memory_update" ]; then
  echo "{\"ts\":\"$TIMESTAMP\",\"action\":\"update\",\"project\":\"$PROJECT_NAME\",\"session\":\"$SESSION_ID\"}" >> "$FEEDBACK_FILE"
fi

# Keep feedback file reasonable (max 5000 lines)
if [ -f "$FEEDBACK_FILE" ]; then
  LINE_COUNT=$(wc -l < "$FEEDBACK_FILE" 2>/dev/null || echo "0")
  if [ "$LINE_COUNT" -gt 5000 ]; then
    tail -2500 "$FEEDBACK_FILE" > "$FEEDBACK_FILE.tmp"
    mv "$FEEDBACK_FILE.tmp" "$FEEDBACK_FILE"
  fi
fi

# Always output empty JSON (PostToolUse doesn't need additionalContext)
echo '{}'
exit 0
