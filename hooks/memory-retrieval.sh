#!/bin/bash
# B12 Memory System - UserPromptSubmit Memory Retrieval Hook (v4)
# Searches memory DB on every user message and injects relevant context
#
# v4 changes (2026-02-09):
# - Query-adaptive search mode: keyword-first + smart vector re-rank
#   - Attribute/preference queries → skip re-rank (BM25 wins)
#   - Negation/adversarial queries → always re-rank (vector filters better)
#   - Few FTS5 results (< 2) → fallback to re-rank
#   - Default → re-rank (hybrid wins on most query types)
# - Benchmark-validated: adaptive beats pure keyword (+2.2pp overall)
#   and approaches hybrid quality while saving ~200ms on 20% of queries
#
# v3 changes (2026-02-09):
# - Phrase-aware FTS5 queries (bigram detection for compound terms)
# - Softened Ebbinghaus decay: exp(-t/(S*3))
# - Project hierarchy awareness (walks up to find parent project)
# - Vector re-rank via Python helper (3s timeout, fallback to FTS5-only)
# - Up to 5 results (was 3), with adaptive hint when few results found
# - Feedback logging to feedback.jsonl
#
# Fires on: every user prompt
# Output: additionalContext with relevant memories
# Performance target: <500ms (re-rank skipped on attribute queries → ~50ms)

# ── Self-timeout watchdog ─────────────────────────────────────
# Kills this script if it exceeds max runtime. Prevents orphan processes.
( sleep 10 && kill -TERM $$ 2>/dev/null ) &
_WATCHDOG=$!
trap "kill $_WATCHDOG 2>/dev/null; wait $_WATCHDOG 2>/dev/null" EXIT

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

# Skip slash commands (e.g., /commit, /help — but NOT file paths like /Users/...)
if [[ "$PROMPT" =~ ^/[a-zA-Z][a-zA-Z0-9_-]*($|[[:space:]]) ]]; then
  exit 0
fi

DB_PATH="$HOME/Library/Application Support/mcp-memory/sqlite_vec.db"
if [ ! -f "$DB_PATH" ]; then
  exit 0
fi

B12_BASE="${B12_DATA_DIR:-$HOME/.claude}"
FEEDBACK_DIR="$B12_BASE/memory-logs"

# ── Project detection (with hierarchy) ─────────────────────────
PROJECT_NAME=$(basename "$CWD" 2>/dev/null || echo "unknown")
PARENT_PROJECT=""
_dir="$CWD"
while [ "$_dir" != "/" ] && [ "$_dir" != "$HOME" ]; do
  if [ -d "$_dir/.git" ] || [ -f "$_dir/package.json" ] || [ -f "$_dir/Cargo.toml" ] || [ -f "$_dir/go.mod" ] || [ -f "$_dir/pyproject.toml" ]; then
    _root_name=$(basename "$_dir" 2>/dev/null)
    if [ "$_root_name" != "$PROJECT_NAME" ]; then
      PARENT_PROJECT="$_root_name"
    fi
    break
  fi
  _dir=$(dirname "$_dir")
done

# ── Query classification (adaptive search mode) ───────────────
# Determines whether to use vector re-ranking or keep FTS5 order
# Based on LoCoMo benchmark findings:
#   - Negation/adversarial → always re-rank (hybrid +18pp)
#   - Attribute/preference → skip re-rank (keyword +4.7pp)
#   - Default → re-rank (hybrid wins on most types)
QUERY_MODE="hybrid"  # default: do vector re-rank

# Check negation first (highest priority → force hybrid)
if echo "$PROMPT_LOWER" | grep -qE '\b(never|nobody|no one|nothing|nowhere)\b|n'\''t\b| not '; then
  QUERY_MODE="hybrid"
# Check attribute/preference patterns (→ keyword, skip re-rank)
elif echo "$PROMPT_LOWER" | grep -qE '\b(favorite|favourite|favori|likes?|enjoys?|prefers?|hobbi|hobies|hobby|hobbies|interests?|passionate?|obsess|fond)\b'; then
  QUERY_MODE="keyword"
elif echo "$PROMPT_LOWER" | grep -qE '\b(loves?|hates?|dislikes?)\b'; then
  QUERY_MODE="keyword"
elif echo "$PROMPT_LOWER" | grep -qE '(think about|feel about|opinion|views? on|attitude|in common|relationship (with|between)|tell me about|describe)'; then
  QUERY_MODE="keyword"
fi

# ── Keyword extraction ─────────────────────────────────────────
KEYWORDS=$(echo "$PROMPT" | tr '[:upper:]' '[:lower:]' | \
  grep -oE '[a-zA-Z0-9_.-]{3,}' | \
  grep -vE '^(the|and|for|are|but|not|you|all|can|had|her|was|one|our|out|has|his|how|its|let|may|new|now|old|see|way|who|did|get|got|him|say|she|too|use|bir|ile|için|var|ben|sen|nasıl|neden|ama|gibi|daha|çok|bana|sana|olan|olarak|bunu|şimdi|lütfen|this|that|with|from|have|will|been|they|what|when|which|would|could|should|about|there|their|these|where|some|than|them|then|into|also|just|like|only|come|made|after|back|over|such|take|other|most|make|know|long|here|many|help|want|need|look|does)$' | \
  head -10 | \
  tr '\n' ' ' | \
  sed 's/ *$//')

# Need at least 1 keyword for meaningful search
WORD_COUNT=$(echo "$KEYWORDS" | wc -w | tr -d ' ')
if [ "$WORD_COUNT" -lt 1 ]; then
  exit 0
fi

# ── Phrase detection (bigrams) ─────────────────────────────────
SAFE_KEYWORDS=$(echo "$KEYWORDS" | sed "s/['\";(){}]//g")
KEYWORD_ARRAY=($SAFE_KEYWORDS)

FTS_PARTS=""
PREV=""
for kw in "${KEYWORD_ARRAY[@]}"; do
  if [ -n "$PREV" ]; then
    BIGRAM="${PREV} ${kw}"
    if echo "$PROMPT_LOWER" | grep -q "${BIGRAM}"; then
      FTS_PARTS="${FTS_PARTS} OR NEAR(${PREV} ${kw}, 2)"
    fi
  fi
  if [ -z "$FTS_PARTS" ]; then
    FTS_PARTS="${kw}"
  else
    FTS_PARTS="${FTS_PARTS} OR ${kw}"
  fi
  PREV="$kw"
done

# ── FTS5 search with softened Ebbinghaus decay ─────────────────
RESULTS=$(sqlite3 "$DB_PATH" "
  WITH scored AS (
    SELECT m.id,
           '[' || m.memory_type || '] ' || replace(substr(m.content, 1, 300), char(10), ' ') as display,
           (
             0.3 * COALESCE(exp(-((julianday('now') - julianday(datetime(COALESCE(m.last_accessed_at, m.created_at), 'unixepoch')))) / (COALESCE(m.strength, 1.0) * 3.0)), 0.5)
             + 0.3 * COALESCE(json_extract(m.metadata, '$.importance_score'), 1.0) / 2.0
             + 0.4 * (1.0 / (1.0 + abs(f.rank)))
           ) as score
    FROM memories m
    JOIN memory_fts f ON m.id = f.rowid
    WHERE f.memory_fts MATCH '${FTS_PARTS}'
      AND m.deleted_at IS NULL
      AND m.valid_until IS NULL
      AND m.memory_type != 'session_summary'
      AND m.tags NOT LIKE '%session-summary%'
    ORDER BY score DESC
    LIMIT 10
  )
  SELECT id, display, score FROM scored
" 2>/dev/null)

# Parse results into arrays
RESULT_IDS=""
RESULT_DISPLAY=""
RESULT_COUNT=0
while IFS='|' read -r id display score; do
  if [ -n "$id" ]; then
    RESULT_IDS="${RESULT_IDS}${id},"
    RESULT_DISPLAY="${RESULT_DISPLAY}${display}\n"
    RESULT_COUNT=$((RESULT_COUNT + 1))
  fi
done <<< "$RESULTS"

# ── Query-adaptive vector re-rank ────────────────────────────
# Decision tree:
#   QUERY_MODE=keyword AND results >= 2 → skip re-rank (save ~200ms)
#   QUERY_MODE=keyword AND results < 2  → fallback to re-rank
#   QUERY_MODE=hybrid                   → always re-rank
VENV_PYTHON="$HOME/.local/pipx/venvs/mcp-memory-service/bin/python3"
RERANK_DONE=false
SKIP_REASON=""

SHOULD_RERANK=false
if [ "$RESULT_COUNT" -gt 0 ] && [ -x "$VENV_PYTHON" ]; then
  if [ "$QUERY_MODE" = "hybrid" ]; then
    SHOULD_RERANK=true
  elif [ "$RESULT_COUNT" -lt 2 ]; then
    # Keyword mode but few results → fallback to re-rank
    SHOULD_RERANK=true
    SKIP_REASON="fallback"
  else
    SKIP_REASON="attribute_query"
  fi
fi

if [ "$SHOULD_RERANK" = true ]; then
  ID_LIST=$(echo "$RESULT_IDS" | sed 's/,$//')

  RERANKED=$(timeout 3 "$VENV_PYTHON" - "$DB_PATH" "$ID_LIST" "$PROMPT" 2>/dev/null << 'PYEOF'
import sys, struct, sqlite3

db_path, id_list, query = sys.argv[1], sys.argv[2], sys.argv[3]
ids = [int(x) for x in id_list.split(',') if x.strip()]
if not ids:
    sys.exit(0)

try:
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer('paraphrase-multilingual-MiniLM-L12-v2')
    q_emb = model.encode([query], normalize_embeddings=True)[0]

    conn = sqlite3.connect(db_path)
    scores = []
    for mem_id in ids:
        row = conn.execute('SELECT content_embedding FROM memory_embeddings WHERE rowid = ?', (mem_id,)).fetchone()
        if row and row[0]:
            dim = len(row[0]) // 4
            stored = struct.unpack(f'{dim}f', row[0])
            cos_sim = sum(a*b for a,b in zip(q_emb, stored))
            scores.append((mem_id, max(0.0, cos_sim)))
        else:
            scores.append((mem_id, 0.0))
    conn.close()

    scores.sort(key=lambda x: x[1], reverse=True)
    print(','.join(str(s[0]) for s in scores))
except Exception:
    print(','.join(str(i) for i in ids))
PYEOF
  )

  if [ -n "$RERANKED" ] && [ "$RERANKED" != "$ID_LIST" ]; then
    RERANK_IDS=$(echo "$RERANKED" | sed 's/,$//')
    RESULT_DISPLAY=$(sqlite3 "$DB_PATH" "
      SELECT '[' || m.memory_type || '] ' || replace(substr(m.content, 1, 300), char(10), ' ')
      FROM memories m
      WHERE m.id IN (${RERANK_IDS})
      ORDER BY CASE m.id
        $(echo "$RERANK_IDS" | tr ',' '\n' | awk '{printf \"WHEN %s THEN %d\\n\", \$1, NR}')
      END
      LIMIT 5
    " 2>/dev/null)
    RESULT_IDS="${RERANK_IDS},"
    RERANK_DONE=true
  fi
fi

# Trim to top 5
if [ "$RERANK_DONE" = false ] && [ "$RESULT_COUNT" -gt 5 ]; then
  RESULT_DISPLAY=$(echo -e "$RESULT_DISPLAY" | head -5)
  RESULT_IDS=$(echo "$RESULT_IDS" | tr ',' '\n' | head -5 | tr '\n' ',' | sed 's/,$//')
  RESULT_IDS="${RESULT_IDS},"
  RESULT_COUNT=5
fi

# ── Boost strength (spaced repetition) ─────────────────────────
if [ "$RESULT_COUNT" -gt 0 ]; then
  BOOST_IDS=$(echo "$RESULT_IDS" | sed 's/,$//' | head -c 200)
  sqlite3 "$DB_PATH" "
    UPDATE memories
    SET strength = min(COALESCE(strength, 1.0) + 0.3, 5.0),
        last_accessed_at = unixepoch('now')
    WHERE id IN (${BOOST_IDS})
  " 2>/dev/null
fi

# ── Feedback logging ───────────────────────────────────────────
if [ -d "$FEEDBACK_DIR" ] || mkdir -p "$FEEDBACK_DIR" 2>/dev/null; then
  FEEDBACK_FILE="$FEEDBACK_DIR/feedback.jsonl"
  PROJ="${PARENT_PROJECT:-$PROJECT_NAME}"
  printf '{"ts":%d,"type":"hook_retrieval","query":"%s","keywords":"%s","result_count":%d,"reranked":%s,"query_mode":"%s","skip_reason":"%s","project":"%s"}\n' \
    "$(date +%s)" \
    "$(echo "$PROMPT" | head -c 100 | sed 's/"/\\"/g' | tr '\n' ' ')" \
    "$(echo "$SAFE_KEYWORDS" | sed 's/"/\\"/g')" \
    "$RESULT_COUNT" \
    "$RERANK_DONE" \
    "$QUERY_MODE" \
    "$SKIP_REASON" \
    "$PROJ" \
    >> "$FEEDBACK_FILE" 2>/dev/null
fi

# ── Output ─────────────────────────────────────────────────────
if [ -z "$RESULT_DISPLAY" ] || [ "$RESULT_COUNT" -eq 0 ]; then
  exit 0
fi

# Build context with adaptive hint
HINT=""
if [ "$RESULT_COUNT" -lt 2 ]; then
  HINT="\\n(Few results — consider using memory_search for broader semantic context)"
fi

ESCAPED=$(echo -e "$RESULT_DISPLAY" | jq -Rs '.' | sed 's/^"//;s/"$//')

cat <<EOF
{
  "hookSpecificOutput": {
    "hookEventName": "UserPromptSubmit",
    "additionalContext": "Memory retrieval (auto, ${QUERY_MODE}, keywords: ${SAFE_KEYWORDS}):\\n${ESCAPED}${HINT}"
  }
}
EOF

exit 0
