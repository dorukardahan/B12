#!/bin/bash
# B12 Memory System - PreCompact Hook
# Extracts key context before compaction and stages it for post-compact recovery
#
# Fires on: auto, manual
# Side effect: Creates staging file in ~/.claude/memory-staging/
#
# Install: Copy to ~/.claude/hooks/ and chmod +x

INPUT=$(cat)
TRANSCRIPT_PATH=$(echo "$INPUT" | jq -r '.transcript_path // ""')
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // "unknown"')
CWD=$(echo "$INPUT" | jq -r '.cwd // ""')

PROJECT_NAME=$(basename "$CWD" 2>/dev/null || echo "unknown")
STAGING_DIR="$HOME/.claude/memory-staging"
mkdir -p "$STAGING_DIR"

# Extract a summary from the last portion of the transcript
# Focus on assistant messages which contain the actual work/decisions
if [ -f "$TRANSCRIPT_PATH" ]; then
  SUMMARY=$(tail -100 "$TRANSCRIPT_PATH" 2>/dev/null | \
    python3 -c "
import sys, json
messages = []
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        obj = json.loads(line)
        if obj.get('type') == 'assistant':
            content = obj.get('message', {}).get('content', [])
            for block in content:
                if isinstance(block, dict) and block.get('type') == 'text':
                    text = block['text'][:500]
                    messages.append(text)
    except (json.JSONDecodeError, KeyError, TypeError):
        continue

recent = messages[-5:] if messages else []
print('\n---\n'.join(recent))
" 2>/dev/null)

  if [ -n "$SUMMARY" ]; then
    STAGE_FILE="$STAGING_DIR/precompact-${SESSION_ID}.txt"
    echo "Project: $PROJECT_NAME" > "$STAGE_FILE"
    echo "Time: $(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$STAGE_FILE"
    echo "---" >> "$STAGE_FILE"
    echo "$SUMMARY" >> "$STAGE_FILE"
  fi
fi

# Clean up old staging files (older than 1 hour)
find "$STAGING_DIR" -name "precompact-*.txt" -mmin +60 -delete 2>/dev/null

exit 0
