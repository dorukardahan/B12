#!/bin/bash
# B12 Memory System - PostToolUse Feedback Hook (v1)
# Tracks memory tool usage patterns for quality improvement
#
# Fires on: mcp__memory__memory_store, mcp__memory__memory_search
# Output: Appends to feedback.jsonl — no additionalContext
#
# Install: Copy to ~/.claude/hooks/ and chmod +x
# Settings matcher: "mcp__memory__memory_store|mcp__memory__memory_search"

INPUT=$(cat)
TOOL_NAME=$(echo "$INPUT" | jq -r '.tool_name // ""')
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // ""')
CWD=$(echo "$INPUT" | jq -r '.cwd // ""')

PROJECT_NAME=$(basename "$CWD" 2>/dev/null || echo "unknown")
FEEDBACK_DIR="$HOME/.claude/memory-logs"
FEEDBACK_FILE="$FEEDBACK_DIR/feedback.jsonl"
TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

mkdir -p "$FEEDBACK_DIR" 2>/dev/null

if [ "$TOOL_NAME" = "mcp__memory__memory_store" ]; then
  # Track what was stored — extract metadata quality indicators
  HAS_METADATA=$(echo "$INPUT" | jq -r 'if .tool_input.metadata then "true" else "false" end' 2>/dev/null)
  HAS_TAGS=$(echo "$INPUT" | jq -r 'if .tool_input.tags then "true" else "false" end' 2>/dev/null)
  CONTENT_LEN=$(echo "$INPUT" | jq -r '.tool_input.content // "" | length' 2>/dev/null)

  echo "{\"ts\":\"$TIMESTAMP\",\"action\":\"store\",\"project\":\"$PROJECT_NAME\",\"session\":\"$SESSION_ID\",\"has_metadata\":$HAS_METADATA,\"has_tags\":$HAS_TAGS,\"content_length\":$CONTENT_LEN}" >> "$FEEDBACK_FILE"

elif [ "$TOOL_NAME" = "mcp__memory__memory_search" ]; then
  # Track search patterns — detect empty results
  QUERY=$(echo "$INPUT" | jq -r '.tool_input.query // ""' 2>/dev/null)
  QUERY_LEN=${#QUERY}
  # Check if tool_output contains results (look for "No results" or empty array)
  RESULT_SNIPPET=$(echo "$INPUT" | jq -r '.tool_output // "" | tostring | .[0:200]' 2>/dev/null)
  IS_EMPTY="false"
  if echo "$RESULT_SNIPPET" | grep -qi "no results\|no memories\|\[\]"; then
    IS_EMPTY="true"
  fi

  echo "{\"ts\":\"$TIMESTAMP\",\"action\":\"search\",\"project\":\"$PROJECT_NAME\",\"session\":\"$SESSION_ID\",\"query_length\":$QUERY_LEN,\"empty_result\":$IS_EMPTY}" >> "$FEEDBACK_FILE"
fi

# Always output empty JSON (PostToolUse doesn't need additionalContext)
echo '{}'
exit 0
