#!/bin/bash
# B12 Memory System - UserPromptSubmit Memory Retrieval Hook (v2 — Decay-Aware)
# Searches memory DB on every user message and injects relevant context
# Uses FTS5 keyword search + Ebbinghaus decay scoring + strength boost
#
# Fires on: every user prompt
# Output: additionalContext with relevant memories (max 3)
# Side effect: boosts strength of retrieved memories (spaced repetition)
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
# Sanitize: strip any SQL-dangerous chars (defense-in-depth over keyword regex)
FTS_QUERY=$(echo "$KEYWORDS" | sed "s/['\";(){}]//g" | sed 's/ / OR /g')

# Search memory DB via FTS5 + Ebbinghaus decay + importance scoring
# Combined score = 0.3*decay + 0.3*importance + 0.4*relevance(FTS5 rank)
RESULTS=$(sqlite3 "$DB_PATH" "
  SELECT '[' || m.memory_type || '] ' || replace(substr(m.content, 1, 300), char(10), ' ')
  FROM memories m
  JOIN memory_fts f ON m.id = f.rowid
  WHERE f.memory_fts MATCH '${FTS_QUERY}'
    AND m.deleted_at IS NULL
    AND m.valid_until IS NULL
    AND m.memory_type != 'session_summary'
    AND m.tags NOT LIKE '%session-summary%'
  ORDER BY (
    0.3 * COALESCE(exp(-((julianday('now') - julianday(datetime(COALESCE(m.last_accessed_at, m.created_at), 'unixepoch')))) / COALESCE(m.strength, 1.0)), 0.5)
    + 0.3 * COALESCE(json_extract(m.metadata, '$.importance_score'), 1.0) / 2.0
    + 0.4 * (1.0 / (1.0 + abs(f.rank)))
  ) DESC
  LIMIT 3
" 2>/dev/null)

# Boost strength of retrieved memories (spaced repetition effect)
# H1 fix: use same combined scoring as SELECT to ensure we boost the DISPLAYED memories
if [ -n "$RESULTS" ]; then
  sqlite3 "$DB_PATH" "
    WITH top3 AS (
      SELECT m.id FROM memories m
      JOIN memory_fts f ON m.id = f.rowid
      WHERE f.memory_fts MATCH '${FTS_QUERY}'
        AND m.deleted_at IS NULL
        AND m.valid_until IS NULL
        AND m.memory_type != 'session_summary'
        AND m.tags NOT LIKE '%session-summary%'
      ORDER BY (
        0.3 * COALESCE(exp(-((julianday('now') - julianday(datetime(COALESCE(m.last_accessed_at, m.created_at), 'unixepoch')))) / COALESCE(m.strength, 1.0)), 0.5)
        + 0.3 * COALESCE(json_extract(m.metadata, '\$.importance_score'), 1.0) / 2.0
        + 0.4 * (1.0 / (1.0 + abs(f.rank)))
      ) DESC
      LIMIT 3
    )
    UPDATE memories
    SET strength = min(COALESCE(strength, 1.0) + 0.3, 5.0),
        last_accessed_at = unixepoch('now')
    WHERE id IN (SELECT id FROM top3)
  " 2>/dev/null
fi

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
