#!/bin/bash
# B12 Memory System - PostToolUse Feedback Hook (v3 — Retrieval Feedback)
# Tracks memory tool usage patterns for quality improvement
#
# Fires on: mcp__B12__memory_store, mcp__B12__memory_search,
#           mcp__B12__memory_quality, mcp__B12__memory_update
# Output: Appends to feedback.jsonl — no additionalContext
#
# v3 changes (2026-02-08):
# - Search entries now include: query_text, result_count, search_seq
# - Per-session search sequence tracking via temp files
# - Enables retrieval relevance analysis in weekly digest
# v2 changes:
# - Tracks scope compliance (has_proj_tag, has_scope metadata)
# - Tracks memory_quality and memory_update calls
# - Log rotation (max 5000 lines)

# ── Self-timeout watchdog ─────────────────────────────────────
( sleep 5 && kill -TERM $$ 2>/dev/null ) &
_WATCHDOG=$!
trap "kill $_WATCHDOG 2>/dev/null; wait $_WATCHDOG 2>/dev/null" EXIT

INPUT=$(cat)
TOOL_NAME=$(echo "$INPUT" | jq -r '.tool_name // ""')
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // ""')
CWD=$(echo "$INPUT" | jq -r '.cwd // ""')

# Central data directory — override with B12_DATA_DIR env var for custom setups
B12_BASE="${B12_DATA_DIR:-$HOME/.B12}"

PROJECT_NAME=$(basename "$CWD" 2>/dev/null || echo "unknown")
FEEDBACK_DIR="$B12_BASE/memory-logs"
FEEDBACK_FILE="$FEEDBACK_DIR/feedback.jsonl"
TIMESTAMP=$(date +%s)

mkdir -p "$FEEDBACK_DIR" 2>/dev/null

if [ "$TOOL_NAME" = "mcp__B12__memory_store" ]; then
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

  jq -nc --argjson ts "$TIMESTAMP" --arg action "store" --arg project "$PROJECT_NAME" --arg session "$SESSION_ID" \
    --argjson has_metadata "$HAS_METADATA" --argjson has_tags "$HAS_TAGS" --argjson content_length "$CONTENT_LEN" \
    --argjson has_proj_tag "$HAS_PROJ_TAG" --argjson has_scope "$HAS_SCOPE" \
    '{ts: $ts, action: $action, project: $project, session: $session, has_metadata: $has_metadata, has_tags: $has_tags, content_length: $content_length, has_proj_tag: $has_proj_tag, has_scope: $has_scope}' >> "$FEEDBACK_FILE"

elif [ "$TOOL_NAME" = "mcp__B12__memory_search" ]; then
  # Track search patterns — query text, result count, sequence, empty detection
  QUERY=$(echo "$INPUT" | jq -r '.tool_input.query // ""' 2>/dev/null)
  QUERY_LEN=${#QUERY}
  # Truncated query text for analysis (first 120 chars, sanitized for JSON)
  QUERY_TEXT=$(printf '%s' "$QUERY" | head -c 120 | jq -Rs '.' 2>/dev/null | sed 's/^"//;s/"$//')
  RESULT_SNIPPET=$(echo "$INPUT" | jq -r '.tool_output // "" | tostring | .[0:300]' 2>/dev/null)

  # Parse result count from MCP output: "Found N memories"
  RESULT_COUNT=$(echo "$RESULT_SNIPPET" | grep -oE 'Found [0-9]+ memor' | grep -oE '[0-9]+' | head -1)
  RESULT_COUNT=${RESULT_COUNT:-0}

  IS_EMPTY="false"
  if [ "$RESULT_COUNT" -eq 0 ] 2>/dev/null || echo "$RESULT_SNIPPET" | grep -qi "no results\|no memories\|\[\]"; then
    IS_EMPTY="true"
  fi

  # Per-session search sequence tracking
  SESSION_SEARCH_FILE="$FEEDBACK_DIR/.search-seq-${SESSION_ID}"
  SEARCH_SEQ=$(cat "$SESSION_SEARCH_FILE" 2>/dev/null || echo "0")
  SEARCH_SEQ=$((SEARCH_SEQ + 1))
  echo "$SEARCH_SEQ" > "${SESSION_SEARCH_FILE}.tmp" && mv "${SESSION_SEARCH_FILE}.tmp" "$SESSION_SEARCH_FILE"

  jq -nc --argjson ts "$TIMESTAMP" --arg action "search" --arg project "$PROJECT_NAME" --arg session "$SESSION_ID" \
    --argjson query_length "$QUERY_LEN" --arg query_text "$QUERY_TEXT" --argjson result_count "$RESULT_COUNT" \
    --argjson search_seq "$SEARCH_SEQ" --argjson empty_result "$IS_EMPTY" \
    '{ts: $ts, action: $action, project: $project, session: $session, query_length: $query_length, query_text: $query_text, result_count: $result_count, search_seq: $search_seq, empty_result: $empty_result}' >> "$FEEDBACK_FILE"

elif [ "$TOOL_NAME" = "mcp__B12__memory_quality" ]; then
  MEMORY_HASH=$(echo "$INPUT" | jq -r '.tool_input.content_hash // ""' 2>/dev/null)
  jq -nc --argjson ts "$TIMESTAMP" --arg action "quality" --arg project "$PROJECT_NAME" --arg session "$SESSION_ID" \
    --arg memory_hash "$MEMORY_HASH" \
    '{ts: $ts, action: $action, project: $project, session: $session, memory_hash: $memory_hash}' >> "$FEEDBACK_FILE"

elif [ "$TOOL_NAME" = "mcp__B12__memory_update" ]; then
  jq -nc --argjson ts "$TIMESTAMP" --arg action "update" --arg project "$PROJECT_NAME" --arg session "$SESSION_ID" \
    '{ts: $ts, action: $action, project: $project, session: $session}' >> "$FEEDBACK_FILE"
fi

# Keep feedback file reasonable (max 5000 lines)
if [ -f "$FEEDBACK_FILE" ]; then
  LINE_COUNT=$(wc -l < "$FEEDBACK_FILE" 2>/dev/null || echo "0")
  if [ "$LINE_COUNT" -gt 5000 ]; then
    tail -2500 "$FEEDBACK_FILE" > "$FEEDBACK_FILE.tmp"
    mv "$FEEDBACK_FILE.tmp" "$FEEDBACK_FILE"
  fi
fi

# Clean up stale search sequence temp files (older than 4 hours)
find "$FEEDBACK_DIR" -name ".search-seq-*" -mmin +240 -delete 2>/dev/null || true

# Always output empty JSON (PostToolUse doesn't need additionalContext)
echo '{}'
exit 0
