#!/bin/bash
# B12 Memory System - SessionEnd Hook
# Logs session metadata for analytics and cleanup
#
# Fires on: clear, logout, prompt_input_exit, other
# Side effect: Appends to ~/.claude/memory-logs/sessions.jsonl
#
# Install: Copy to ~/.claude/hooks/ and chmod +x

INPUT=$(cat)
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // "unknown"')
REASON=$(echo "$INPUT" | jq -r '.reason // "other"')
CWD=$(echo "$INPUT" | jq -r '.cwd // ""')

PROJECT_NAME=$(basename "$CWD" 2>/dev/null || echo "unknown")

# Clean up any remaining staging files for this session
STAGING_DIR="$HOME/.claude/memory-staging"
rm -f "$STAGING_DIR/precompact-${SESSION_ID}.txt" 2>/dev/null

# Log session end for analytics (append-only log)
LOG_DIR="$HOME/.claude/memory-logs"
mkdir -p "$LOG_DIR"
echo "{\"session\":\"$SESSION_ID\",\"project\":\"$PROJECT_NAME\",\"reason\":\"$REASON\",\"time\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}" >> "$LOG_DIR/sessions.jsonl"

# Keep log file reasonable (last 1000 entries)
if [ -f "$LOG_DIR/sessions.jsonl" ]; then
  LINE_COUNT=$(wc -l < "$LOG_DIR/sessions.jsonl")
  if [ "$LINE_COUNT" -gt 1000 ]; then
    tail -500 "$LOG_DIR/sessions.jsonl" > "$LOG_DIR/sessions.jsonl.tmp"
    mv "$LOG_DIR/sessions.jsonl.tmp" "$LOG_DIR/sessions.jsonl"
  fi
fi

exit 0
