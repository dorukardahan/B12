#!/bin/bash
# B12 Memory System - UserPromptSubmit Memory Retrieval Hook (v1)
# Searches memory DB on every user message and injects relevant context
# Uses FTS5 keyword search (fast, no embedding model needed)
#
# Fires on: every user prompt
# Output: additionalContext with relevant memories (max 3)
# Performance target: <200ms

INPUT=$(cat)
PROMPT=$(echo "$INPUT" | jq -r '.prompt // ""')
CWD=$(echo "$INPUT" | jq -r '.cwd // ""')

# Skip trivial messages (short, greetings, confirmations)
PROMPT_LEN=${#PROMPT}
if [ "$PROMPT_LEN" -lt 15 ]; then
  exit 0
fi

# Skip common non-searchable patterns
PROMPT_LOWER=$(echo "$PROMPT" | tr '[:upper:]' '[:lower:]')
case "$PROMPT_LOWER" in
  evet*|hayır*|tamam*|ok*|yes*|no*|devam*|anladım*|güzel*|teşekkür*|thanks*|merhaba*|hey*|hi\ *|hello*|peki*|hadi*)
    exit 0
    ;;
esac

# Skip slash commands
if [[ "$PROMPT" == /* ]]; then
  exit 0
fi

DB_PATH="$HOME/Library/Application Support/mcp-memory/sqlite_vec.db"
if [ ! -f "$DB_PATH" ]; then
  exit 0
fi

PROJECT_NAME=$(basename "$CWD" 2>/dev/null || echo "unknown")

# Extract keywords: alphanumeric words 3+ chars, skip common stop words
KEYWORDS=$(echo "$PROMPT" | tr '[:upper:]' '[:lower:]' | \
  grep -oE '[a-zA-Z0-9_.-]{3,}' | \
  grep -vE '^(the|and|for|are|but|not|you|all|can|had|her|was|one|our|out|has|his|how|its|let|may|new|now|old|see|way|who|did|get|got|him|say|she|too|use|bir|bir|ile|için|var|ben|sen|nasıl|neden|ama|gibi|daha|çok|bana|sana|olan|olarak|bunu|şimdi|lütfen|this|that|with|from|have|will|been|they|what|when|which|would|could|should|about|there|their|these|where|some|than|them|then|into|also|just|like|only|come|made|after|back|over|such|take|other|than|most|make|know|long|here|many|some|help|want|need|look|does)$' | \
  head -8 | \
  tr '\n' ' ' | \
  sed 's/ *$//')

# Need at least 2 keywords for meaningful search
WORD_COUNT=$(echo "$KEYWORDS" | wc -w | tr -d ' ')
if [ "$WORD_COUNT" -lt 2 ]; then
  exit 0
fi

# Build FTS5 query: OR between keywords for broader matching
FTS_QUERY=$(echo "$KEYWORDS" | sed 's/ / OR /g')

# Search memory DB via FTS5 — exclude session summaries, limit to 3
RESULTS=$(sqlite3 "$DB_PATH" "
  SELECT '[' || m.memory_type || '] ' || replace(substr(m.content, 1, 300), char(10), ' ')
  FROM memories m
  JOIN memory_fts f ON m.id = f.rowid
  WHERE f.memory_fts MATCH '${FTS_QUERY}'
    AND m.deleted_at IS NULL
    AND m.memory_type != 'session_summary'
    AND m.tags NOT LIKE '%session-summary%'
  ORDER BY f.rank
  LIMIT 3
" 2>/dev/null)

# No results — exit silently
if [ -z "$RESULTS" ]; then
  exit 0
fi

# Escape for JSON
ESCAPED=$(echo "$RESULTS" | jq -Rs '.' | sed 's/^"//;s/"$//')

cat <<EOF
{
  "hookSpecificOutput": {
    "hookEventName": "UserPromptSubmit",
    "additionalContext": "Memory retrieval (auto, keywords: ${KEYWORDS}):\\n${ESCAPED}"
  }
}
EOF

exit 0
