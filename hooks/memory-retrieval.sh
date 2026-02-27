#!/bin/bash
# B12 Memory System - UserPromptSubmit Memory Retrieval Hook (v6)
# Searches memory DB on every user message and injects relevant context
#
# v6 changes (2026-02-18) — Phase 1 latency reduction:
# - Embedding daemon: persistent SentenceTransformer over Unix socket
# - Daemon-first search/rerank (~50ms each) with cold fallback
# - Merged cold fallback: single Python process for both ops (was 2)
# - Pure bash keyword extraction (saves ~150ms Python spawn)
# - Smart re-rank skip: high FTS scores bypass reranking
# - Latency tracking in feedback.jsonl (millisecond precision via perl)
#
# v5 changes (2026-02-18) — Phase 0 retrieval quality:
# - FTS5 AND logic for 3+ keywords (precision boost)
# - Filter out 'progress' type micro-memories (noise reduction)
# - Fix Ebbinghaus decay: remove *3 softening (align with ebbinghaus.py)
# - FTS5 sanitization: strip *, ^, :, NEAR operators
# - busy_timeout on strength boost UPDATE
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
# - Ebbinghaus decay: exp(-t/S) (v5: removed *3 softening)
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

# ── Latency tracking (Phase 1) ───────────────────────────────
_START_MS=$(perl -MTime::HiRes=time -e 'printf "%d", time()*1000' 2>/dev/null || echo 0)

# Skip trivial messages (short, greetings, confirmations)
PROMPT_LEN=${#PROMPT}
if [ "$PROMPT_LEN" -lt 15 ]; then
  exit 0
fi

# Skip common non-searchable patterns
PROMPT_LOWER=$(echo "$PROMPT" | tr '[:upper:]' '[:lower:]')
case "$PROMPT_LOWER" in
  evet*|hayır*|tamam*|ok*|yes*|no*|devam*|anladım*|güzel*|teşekkür*|thanks*|merhaba*|hey*|hi\ *|hello*|peki*|hadi*|tamamdır*|oldu*|anlaşıldı*|süper*|harika*)
    exit 0
    ;;
  # Imperative commands — skip if short (<=3 words, likely bare command)
  # Longer prompts like "push notification implementation" should NOT be skipped
  commit*|push*|pull*|merge*|rebase*|deploy*|install*|build*|run\ *|start\ *|stop\ *|restart*|kill\ *)
    _WORD_COUNT_RAW=$(echo "$PROMPT" | wc -w | tr -d ' ')
    [ "$_WORD_COUNT_RAW" -le 3 ] && exit 0
    ;;
  # Turkish imperative commands (short only)
  *commit\'le*|*push\'la*|*başlat*|*çalıştır*)
    _WORD_COUNT_RAW=$(echo "$PROMPT" | wc -w | tr -d ' ')
    [ "$_WORD_COUNT_RAW" -le 4 ] && exit 0
    ;;
esac

# Skip slash commands (e.g., /commit, /help — but NOT file paths like /Users/...)
if [[ "$PROMPT" =~ ^/[a-zA-Z][a-zA-Z0-9_-]*($|[[:space:]]) ]]; then
  exit 0
fi

if [ "$(uname)" = "Darwin" ]; then
  DB_PATH="$HOME/Library/Application Support/mcp-memory/sqlite_vec.db"
elif [ -d "$HOME/AppData" ]; then
  DB_PATH="$HOME/AppData/Local/mcp-memory/sqlite_vec.db"
else
  DB_PATH="$HOME/.local/share/mcp-memory/sqlite_vec.db"
fi
if [ ! -f "$DB_PATH" ]; then
  exit 0
fi

B12_BASE="${B12_DATA_DIR:-$HOME/.B12}"
FEEDBACK_DIR="$B12_BASE/memory-logs"

# ── Embedding daemon helpers (Phase 1) ───────────────────────
_UID=$(id -u 2>/dev/null || echo $$)
# Hardcode /tmp/ — macOS TMPDIR varies per session, causing mismatch with daemon
EMBED_SOCK="/tmp/b12-embed-${_UID}.sock"
EMBED_PID="/tmp/b12-embed-${_UID}.pid"

daemon_alive() {
  [ -S "$EMBED_SOCK" ] && [ -f "$EMBED_PID" ] && \
    kill -0 "$(cat "$EMBED_PID" 2>/dev/null)" 2>/dev/null
}

daemon_request() {
  printf '%s\n' "$1" | nc -U "$EMBED_SOCK" -w 4 2>/dev/null
}

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

# ── Keyword extraction (pure bash — no Python spawn) ──────────
# Saves ~150ms by avoiding Python startup. Uses sed + case for stopwords.
# Note: sed [:alnum:] handles Turkish chars (ı,ş,ç,ö,ü,ğ) with UTF-8 locale.
KEYWORDS=""
_kw_count=0
for _w in $(echo "$PROMPT_LOWER" | sed 's/[^[:alnum:]_]/ /g'); do
  [ ${#_w} -lt 3 ] && continue
  case "$_w" in
    bir|ile|için|var|ben|sen|nasıl|neden|ama|gibi|daha|çok|bana|sana|olan|olarak|bunu|şimdi|lütfen|yapıyoruz|yapalım|bunun|burada|benim|onun|şey|the|and|for|are|but|not|you|all|can|had|was|one|has|how|its|may|new|now|see|who|did|get|him|she|too|use|this|that|with|from|have|will|been|they|what|when|which|would|could|should|about|there|their|these|where|some|than|them|then|into|also|just|like|only|make|know|here|help|want|need|look|does) continue ;;
    *) KEYWORDS="${KEYWORDS}${_w} "; _kw_count=$((_kw_count + 1)); [ $_kw_count -ge 10 ] && break ;;
  esac
done
KEYWORDS=$(echo "$KEYWORDS" | sed 's/ *$//')

# Need at least 1 keyword for meaningful search
WORD_COUNT=$(echo "$KEYWORDS" | wc -w | tr -d ' ')
if [ "$WORD_COUNT" -lt 1 ]; then
  exit 0
fi

# ── Query alias expansion ──────────────────────────────────────
# Expand common abbreviations: db→database, k8s→kubernetes, etc.
_ALIAS_FILE="${B12_HOOK_DIR:-$HOME/.B12/hooks}/scripts/query_aliases.json"
_ALIAS_MAP=""
if [ -f "$_ALIAS_FILE" ]; then
  _ALIAS_MAP=$(cat "$_ALIAS_FILE" 2>/dev/null)
fi

# expand_kw: returns "kw" or "(kw OR alias1 OR alias2)" if aliases exist
expand_kw() {
  local kw="$1"
  if [ -z "$_ALIAS_MAP" ]; then
    printf '%s' "$kw"
    return
  fi
  local aliases
  aliases=$(echo "$_ALIAS_MAP" | jq -r --arg k "$kw" '.[$k] // [] | .[]' 2>/dev/null)
  if [ -z "$aliases" ]; then
    printf '%s' "$kw"
  else
    local parts="$kw"
    while IFS= read -r a; do
      # Sanitize aliases (same rules as user keywords — defense in depth)
      a=$(echo "$a" | sed "s/['\";(){}*^:\\\\]//g" | sed 's/--//g')
      [ -z "$a" ] && continue
      # Quote multi-word aliases for FTS5 phrase matching
      if echo "$a" | grep -q ' '; then
        parts="$parts OR \"$a\""
      else
        parts="$parts OR $a"
      fi
    done <<< "$aliases"
    printf '(%s)' "$parts"
  fi
}

# ── Phrase detection (bigrams) ─────────────────────────────────
SAFE_KEYWORDS=$(echo "$KEYWORDS" | sed "s/['\";(){}*^:\\\\]//g" | sed 's/--//g' | sed 's|/\*||g' | sed 's|\*/||g' | sed 's/\bNEAR\b//gI')
KEYWORD_ARRAY=($SAFE_KEYWORDS)

if [ "$WORD_COUNT" -ge 3 ]; then
  if [ "$WORD_COUNT" -ge 4 ]; then
    # Relaxed AND: require N-1 of N keywords (cap at 5 keywords for combinations)
    # Each keyword gets alias expansion: db → (db OR database)
    # Pre-compute expanded forms to avoid O(N^2) jq spawns
    _COMBO_KWS=("${KEYWORD_ARRAY[@]:0:5}")
    _COMBO_COUNT=${#_COMBO_KWS[@]}
    _EXPANDED=()
    for (( _i=0; _i<_COMBO_COUNT; _i++ )); do
      _EXPANDED+=("$(expand_kw "${_COMBO_KWS[$_i]}")")
    done
    FTS_PARTS=""
    for (( _skip=0; _skip<_COMBO_COUNT; _skip++ )); do
      _COMBO=""
      for (( _j=0; _j<_COMBO_COUNT; _j++ )); do
        [ "$_j" -eq "$_skip" ] && continue
        if [ -z "$_COMBO" ]; then
          _COMBO="${_EXPANDED[$_j]}"
        else
          _COMBO="${_COMBO} AND ${_EXPANDED[$_j]}"
        fi
      done
      if [ -z "$FTS_PARTS" ]; then
        FTS_PARTS="(${_COMBO})"
      else
        FTS_PARTS="${FTS_PARTS} OR (${_COMBO})"
      fi
    done
  else
    # 3 keywords: strict AND with alias expansion
    FTS_PARTS=""
    for kw in "${KEYWORD_ARRAY[@]}"; do
      _expanded=$(expand_kw "$kw")
      if [ -z "$FTS_PARTS" ]; then
        FTS_PARTS="$_expanded"
      else
        FTS_PARTS="${FTS_PARTS} AND $_expanded"
      fi
    done
  fi
  # Add bigrams as OR boost for recall
  PREV=""
  for kw in "${KEYWORD_ARRAY[@]}"; do
    if [ -n "$PREV" ]; then
      BIGRAM="${PREV} ${kw}"
      if echo "$PROMPT_LOWER" | grep -q "${BIGRAM}"; then
        FTS_PARTS="${FTS_PARTS} OR NEAR(${PREV} ${kw}, 2)"
      fi
    fi
    PREV="$kw"
  done
else
  # 1-2 keywords: keep OR with alias expansion (need recall)
  FTS_PARTS=""
  for kw in "${KEYWORD_ARRAY[@]}"; do
    _expanded=$(expand_kw "$kw")
    if [ -z "$FTS_PARTS" ]; then
      FTS_PARTS="$_expanded"
    else
      FTS_PARTS="${FTS_PARTS} OR $_expanded"
    fi
  done
fi

# ── FTS5 search with softened Ebbinghaus decay ─────────────────
RESULTS=$(sqlite3 "$DB_PATH" "
  WITH scored AS (
    SELECT m.id,
           '[' || m.memory_type || '] ' || replace(substr(m.content, 1, 300), char(10), ' ') as display,
           (
             0.3 * max(COALESCE(exp(-((julianday('now') - julianday(datetime(COALESCE(m.last_accessed_at, m.created_at), 'unixepoch')))) / COALESCE(m.strength, 1.0)), 0.5), 0.01)
             + 0.3 * min(COALESCE(json_extract(m.metadata, '$.importance_score'), 1.0) / 2.0, 1.0)
             + 0.4 * (1.0 / (1.0 + abs(f.rank)))
           ) as score
    FROM memories m
    JOIN memory_fts f ON m.id = f.rowid
    WHERE f.memory_fts MATCH '${FTS_PARTS}'
      AND m.deleted_at IS NULL
      AND m.valid_until IS NULL
      AND m.memory_type NOT IN ('session_summary', 'progress')
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

# ── Determine needed operations ───────────────────────────────
VENV_PYTHON="$HOME/.local/b12-venv/bin/python3"
RERANK_DONE=false
SKIP_REASON=""
SEARCH_SOURCE="fts5"  # Track for feedback log
_FTS_COUNT="$RESULT_COUNT"

# Semantic search: always try via daemon (parallel path, not fallback)
# Cold fallback only when daemon is down AND FTS returned 0
NEEDS_COLD_SEMANTIC=false
[ "$RESULT_COUNT" -eq 0 ] && NEEDS_COLD_SEMANTIC=true

# Query-adaptive re-rank decision
SHOULD_RERANK=false
if [ "$RESULT_COUNT" -gt 0 ]; then
  if [ "$QUERY_MODE" = "hybrid" ]; then
    SHOULD_RERANK=true
  elif [ "$RESULT_COUNT" -lt 2 ]; then
    SHOULD_RERANK=true
    SKIP_REASON="fallback"
  else
    SKIP_REASON="attribute_query"
  fi
fi

# Smart re-rank skip: if top FTS score is very high, reranking won't help
if [ "$SHOULD_RERANK" = true ] && [ "$RESULT_COUNT" -ge 3 ]; then
  TOP_SCORE=$(echo "$RESULTS" | head -1 | awk -F'|' '{print $3}')
  if [ -n "$TOP_SCORE" ] && echo "$TOP_SCORE" | awk '{exit ($1 > 0.8) ? 0 : 1}' 2>/dev/null; then
    SHOULD_RERANK=false
    SKIP_REASON="high_fts_score"
  fi
fi

# ── Daemon path: parallel semantic + rerank (Phase 1 + v11) ───
if daemon_alive; then
  # Run semantic search (~50ms) as parallel retrieval path (skip in keyword-only mode)
  if [ "$QUERY_MODE" != "keyword" ]; then
  _REQ=$(jq -nc --arg q "$PROMPT" --arg db "$DB_PATH" '{op:"semantic_search",query:$q,db_path:$db,limit:5}')
  _RESP=$(daemon_request "$_REQ")
  if echo "$_RESP" | jq -e '.ok == true' >/dev/null 2>&1; then
    _SEM_DATA=$(echo "$_RESP" | jq -r '.results[] | "\(.id)|\(.display)"')
    _EXISTING_IDS=",${RESULT_IDS}"
    _SEM_ADDED=0
    while IFS='|' read -r _id _display; do
      if [ -n "$_id" ]; then
        # Deduplicate: skip if already in FTS results (rerank handles scoring)
        if echo "$_EXISTING_IDS" | grep -q ",${_id},"; then
          continue
        fi
        RESULT_IDS="${RESULT_IDS}${_id},"
        RESULT_DISPLAY="${RESULT_DISPLAY}${_display}\n"
        RESULT_COUNT=$((RESULT_COUNT + 1))
        _SEM_ADDED=$((_SEM_ADDED + 1))
      fi
    done <<< "$_SEM_DATA"
    NEEDS_COLD_SEMANTIC=false
    if [ "$_FTS_COUNT" -gt 0 ]; then
      SEARCH_SOURCE="fts5+daemon_semantic"
    else
      SEARCH_SOURCE="daemon_semantic"
    fi
    # If semantic added new results to FTS pool, enable reranking for merged pool
    if [ "$_SEM_ADDED" -gt 0 ] && [ "$_FTS_COUNT" -gt 0 ] && [ "$SHOULD_RERANK" = false ]; then
      SHOULD_RERANK=true
      SKIP_REASON=""
    fi
  fi
  fi  # end QUERY_MODE != keyword

  if [ "$SHOULD_RERANK" = true ] && [ "$RESULT_COUNT" -gt 0 ]; then
    _IDS_JSON=$(echo "$RESULT_IDS" | sed 's/,$//' | tr ',' '\n' | grep -E '^[0-9]+$' | head -10 | jq -Rn '[inputs | tonumber]')
    _REQ=$(jq -nc --arg q "$PROMPT" --arg db "$DB_PATH" --argjson ids "$_IDS_JSON" '{op:"rerank",query:$q,db_path:$db,ids:$ids}')
    _RESP=$(daemon_request "$_REQ")
    if echo "$_RESP" | jq -e '.ok == true' >/dev/null 2>&1; then
      RERANK_IDS=$(echo "$_RESP" | jq -r '.ranked_ids | map(tostring) | join(",")')
      if [ -n "$RERANK_IDS" ]; then
        RESULT_DISPLAY=$(sqlite3 "$DB_PATH" "
          SELECT '[' || m.memory_type || '] ' || replace(substr(m.content, 1, 300), char(10), ' ')
          FROM memories m
          WHERE m.id IN (${RERANK_IDS})
          ORDER BY CASE m.id
            $(echo "$RERANK_IDS" | tr ',' '\n' | awk '{printf "WHEN %s THEN %d\n", $1, NR}')
          END
          LIMIT 5
        " 2>/dev/null)
        RESULT_IDS="${RERANK_IDS},"
        RERANK_DONE=true
        SHOULD_RERANK=false
        SEARCH_SOURCE="${SEARCH_SOURCE}+daemon_rerank"
      fi
    fi
  fi
fi

# ── Cold fallback: single Python process for remaining ops ────
# Loads model ONCE for both semantic search and rerank (saves ~4.5s vs two heredocs)
# Cold semantic only fires when daemon is down AND FTS returned 0 (too slow otherwise)
if { [ "$NEEDS_COLD_SEMANTIC" = true ] || [ "$SHOULD_RERANK" = true ]; } && [ -x "$VENV_PYTHON" ]; then
  _ID_LIST=$(echo "$RESULT_IDS" | sed 's/,$//')

  _COLD_OUTPUT=$("$VENV_PYTHON" - "$DB_PATH" "$PROMPT" "$NEEDS_COLD_SEMANTIC" "$SHOULD_RERANK" "$_ID_LIST" 2>/dev/null << 'COLDEOF'
import sys, struct, sqlite3, signal
signal.alarm(3)  # Fail fast if daemon is down — cold model load takes 5-12s anyway

db_path = sys.argv[1]
query = sys.argv[2]
needs_semantic = sys.argv[3] == 'true'
should_rerank = sys.argv[4] == 'true'
id_list_str = sys.argv[5] if len(sys.argv) > 5 else ''

try:
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer('paraphrase-multilingual-MiniLM-L12-v2')
    q_emb = model.encode([query], normalize_embeddings=True)[0]

    if needs_semantic:
        import sqlite_vec
        conn = sqlite3.connect(db_path)
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        rows = conn.execute("""
            SELECT m.id,
                   '[' || m.memory_type || '] ' || replace(substr(m.content, 1, 300), char(10), ' '),
                   e.content_embedding
            FROM memories m
            JOIN memory_embeddings e ON m.id = e.rowid
            WHERE m.deleted_at IS NULL
              AND m.valid_until IS NULL
              AND m.memory_type NOT IN ('session_summary', 'progress')
              AND m.tags NOT LIKE '%session-summary%'
        """).fetchall()

        scores = []
        for row_id, display, emb_bytes in rows:
            if not emb_bytes:
                continue
            dim = len(emb_bytes) // 4
            stored = struct.unpack(f'{dim}f', emb_bytes)
            cos_sim = sum(a*b for a,b in zip(q_emb, stored))
            if cos_sim > 0.3:
                scores.append((row_id, display, cos_sim))
        scores.sort(key=lambda x: x[2], reverse=True)
        for row_id, display, sim in scores[:5]:
            print(f"SEM:{row_id}|{display}|{sim:.3f}")
        conn.close()
        # Semantic results are already cosine-sorted — skip rerank

    elif should_rerank and id_list_str:
        ids = [int(x) for x in id_list_str.split(',') if x.strip()]
        if ids:
            import sqlite_vec
            conn = sqlite3.connect(db_path)
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
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
            print("RERANK:" + ','.join(str(s[0]) for s in scores))
except Exception:
    pass
COLDEOF
  )

  # Parse cold fallback output (prefixed lines)
  while IFS= read -r _line; do
    case "$_line" in
      SEM:*)
        _data="${_line#SEM:}"
        IFS='|' read -r _id _display _score <<< "$_data"
        if [ -n "$_id" ]; then
          RESULT_IDS="${RESULT_IDS}${_id},"
          RESULT_DISPLAY="${RESULT_DISPLAY}${_display}\n"
          RESULT_COUNT=$((RESULT_COUNT + 1))
        fi
        SEARCH_SOURCE="cold_semantic"
        ;;
      RERANK:*)
        _rerank_ids="${_line#RERANK:}"
        if [ -n "$_rerank_ids" ]; then
          RERANK_IDS=$(echo "$_rerank_ids" | sed 's/,$//')
          RESULT_DISPLAY=$(sqlite3 "$DB_PATH" "
            SELECT '[' || m.memory_type || '] ' || replace(substr(m.content, 1, 300), char(10), ' ')
            FROM memories m
            WHERE m.id IN (${RERANK_IDS})
            ORDER BY CASE m.id
              $(echo "$RERANK_IDS" | tr ',' '\n' | awk '{printf "WHEN %s THEN %d\n", $1, NR}')
            END
            LIMIT 5
          " 2>/dev/null)
          RESULT_IDS="${RERANK_IDS},"
          RERANK_DONE=true
          SEARCH_SOURCE="${SEARCH_SOURCE:-fts5}+cold_rerank"
        fi
        ;;
    esac
  done <<< "$_COLD_OUTPUT"
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
  BOOST_IDS=$(echo "$RESULT_IDS" | tr ',' '\n' | grep -E '^[0-9]+$' | head -5 | tr '\n' ',' | sed 's/,$//')
  sqlite3 "$DB_PATH" "
    PRAGMA busy_timeout=10000;
    UPDATE memories
    SET strength = min(COALESCE(strength, 1.0) + 0.2, 5.0),
        last_accessed_at = unixepoch('now'),
        metadata = json_set(COALESCE(metadata, '{}'),
          '\$.access_count',
          COALESCE(json_extract(metadata, '\$.access_count'), 0) + 1)
    WHERE id IN (${BOOST_IDS})
  " >/dev/null 2>&1
fi

# ── Graph-aware context expansion (Phase 2) ──────────────────
# 1-hop expansion from top-3 results via memory_graph edges (~2ms SQLite)
GRAPH_EXPANDED=""
if [ "$RESULT_COUNT" -gt 0 ]; then
  TOP_3_IDS=$(echo "$RESULT_IDS" | tr ',' '\n' | grep -E '^[0-9]+$' | head -3 | tr '\n' ',' | sed 's/,$//')
  ALL_IDS=$(echo "$RESULT_IDS" | tr ',' '\n' | grep -E '^[0-9]+$' | tr '\n' ',' | sed 's/,$//')
  if [ -n "$TOP_3_IDS" ]; then
    GRAPH_EXPANDED=$(sqlite3 "$DB_PATH" "
      SELECT DISTINCT m2.id,
             '[' || m2.memory_type || '] ' || replace(substr(m2.content, 1, 200), char(10), ' ')
      FROM (
        SELECT mg.target_hash AS neighbor_hash, mg.similarity
        FROM memory_graph mg
        JOIN memories m ON m.content_hash = mg.source_hash
        WHERE m.id IN (${TOP_3_IDS})
          AND mg.relationship_type IN ('related', 'supports')
          AND mg.similarity > 0.6
        UNION
        SELECT mg.source_hash AS neighbor_hash, mg.similarity
        FROM memory_graph mg
        JOIN memories m ON m.content_hash = mg.target_hash
        WHERE m.id IN (${TOP_3_IDS})
          AND mg.relationship_type IN ('related', 'supports')
          AND mg.similarity > 0.6
      ) edges
      JOIN memories m2 ON m2.content_hash = edges.neighbor_hash
      WHERE m2.id NOT IN (${ALL_IDS})
        AND m2.deleted_at IS NULL
      ORDER BY edges.similarity DESC
      LIMIT 2
    " 2>/dev/null)
  fi
fi

# ── Feedback logging ───────────────────────────────────────────
if [ -d "$FEEDBACK_DIR" ] || mkdir -p "$FEEDBACK_DIR" 2>/dev/null; then
  FEEDBACK_FILE="$FEEDBACK_DIR/feedback.jsonl"
  PROJ="${PARENT_PROJECT:-$PROJECT_NAME}"
  _END_MS=$(perl -MTime::HiRes=time -e 'printf "%d", time()*1000' 2>/dev/null || echo 0)
  _LATENCY_MS=0
  [ "$_START_MS" -gt 0 ] && [ "$_END_MS" -gt 0 ] && _LATENCY_MS=$((_END_MS - _START_MS))
  jq -nc \
    --arg q "$(echo "$PROMPT" | head -c 100)" \
    --arg kw "$SAFE_KEYWORDS" \
    --argjson rc "$RESULT_COUNT" \
    --arg rr "$RERANK_DONE" \
    --arg qm "$QUERY_MODE" \
    --arg sr "$SKIP_REASON" \
    --arg ss "$SEARCH_SOURCE" \
    --argjson lat "$_LATENCY_MS" \
    --arg proj "$PROJ" \
    '{ts: (now|floor), type: "hook_retrieval", query: $q, keywords: $kw, result_count: $rc, reranked: ($rr == "true"), query_mode: $qm, skip_reason: $sr, search_source: $ss, latency_ms: $lat, project: $proj}' \
    >> "$FEEDBACK_FILE" 2>/dev/null
fi

# ── Output ─────────────────────────────────────────────────────
if [ -z "$RESULT_DISPLAY" ] || [ "$RESULT_COUNT" -eq 0 ]; then
  exit 0
fi

# Build context with adaptive hint
HINT=""
if [ "$RESULT_COUNT" -lt 2 ]; then
  HINT=$'\n(Few results — consider using memory_search for broader semantic context)'
fi

# ── Contradiction warnings (Phase 2) ─────────────────────────
# Check if any returned memories have 'contradicts' edges (~2ms SQLite)
CONTRADICTION_WARN=""
if [ "$RESULT_COUNT" -gt 0 ]; then
  _CONTRA_IDS=$(echo "$RESULT_IDS" | tr ',' '\n' | grep -E '^[0-9]+$' | head -5 | tr '\n' ',' | sed 's/,$//')
  if [ -n "$_CONTRA_IDS" ]; then
    _CONTRA_HITS=$(sqlite3 "$DB_PATH" "
      SELECT DISTINCT m.id || ' ' || m2.id
      FROM memory_graph mg
      JOIN memories m ON m.content_hash = mg.source_hash
      JOIN memories m2 ON m2.content_hash = mg.target_hash
      WHERE m.id IN (${_CONTRA_IDS})
        AND mg.relationship_type = 'contradicts'
        AND m2.deleted_at IS NULL
      LIMIT 3
    " 2>/dev/null)
    if [ -n "$_CONTRA_HITS" ]; then
      CONTRADICTION_WARN=$'\n[Note: Potential contradictions detected — verify which is current]'
      while read -r _src_id _tgt_id; do
        [ -n "$_src_id" ] && [ -n "$_tgt_id" ] && \
          CONTRADICTION_WARN="${CONTRADICTION_WARN}"$'\n'"  Memory #${_src_id} may conflict with #${_tgt_id}"
      done <<< "$_CONTRA_HITS"
    fi
  fi
fi

# Use jq to construct valid JSON (handles all control chars and Unicode safely)
{
  printf '%s' "Memory retrieval (auto, ${QUERY_MODE}, keywords: ${SAFE_KEYWORDS}):"
  printf '\n'
  printf '%b' "$RESULT_DISPLAY"
  if [ -n "$GRAPH_EXPANDED" ]; then
    printf '\nRelated (graph):\n'
    printf '%s\n' "$GRAPH_EXPANDED"
  fi
  printf '%s' "$HINT"
  printf '%s' "$CONTRADICTION_WARN"
} | jq -Rs '{hookSpecificOutput:{hookEventName:"UserPromptSubmit",additionalContext:.}}'

exit 0
