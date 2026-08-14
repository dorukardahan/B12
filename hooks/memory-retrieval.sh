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

# Shared helpers (b12_sync_watchdog, b12_async_fork). Soft sync cap from
# S3 (P-SPEED): once exceeded, emit empty `{}` + log to sync-cap-hits.jsonl
# instead of blocking the user's prompt with a slow retrieval.
_B12_HOOK_DIR="${B12_HOOK_DIR:-$HOME/.B12/hooks}"
# shellcheck disable=SC1091
. "$_B12_HOOK_DIR/_b12_common.sh"

# ── Self-timeout watchdog ─────────────────────────────────────
# S3 hard cap: B12_SYNC_CAP_S overrides for benchmarks; default 1.5s
# (FSRS update + graph expansion + contradiction join finish here;
# the heavy Python boost path is moved to disown'd background below).
b12_sync_watchdog "${B12_SYNC_CAP_S:-1.5}" memory-retrieval

INPUT=$(cat)
PROMPT=$(echo "$INPUT" | jq -r '.prompt // ""')
CWD=$(echo "$INPUT" | jq -r '.cwd // ""')
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // ""')
SESSION_ID12="${SESSION_ID:0:12}"

# ── Latency tracking (Phase 1) ───────────────────────────────
_START_MS=$(perl -MTime::HiRes=time -e 'printf "%d", time()*1000' 2>/dev/null || echo 0)

# ── Token / dedup state (T1 + T2 + T3) ───────────────────────
# Lockstep with scripts/b12_token_budget.py — same session id truncation,
# same state-dir layout. Defaults: 800-token per-turn cap, 80K cumulative,
# 500-entry LRU dedup ledger.
B12_BASE_FOR_STATE="${B12_DATA_DIR:-$HOME/.B12}"
B12_STATE_DIR="$B12_BASE_FOR_STATE/state"
B12_TOK_STATE="$B12_STATE_DIR/session-tok-${SESSION_ID12}.txt"
B12_DEDUP_LEDGER="$B12_STATE_DIR/session-injected-${SESSION_ID12}.txt"
B12_TOK_PER_TURN="${B12_MAX_INJECT_TOKENS:-800}"   # T1
B12_TOK_SESSION_MAX="${B12_MAX_SESSION_TOKENS:-80000}"  # T2
B12_TOK_PER_TURN_CHARS=$(( B12_TOK_PER_TURN * 4 ))  # char proxy
mkdir -p "$B12_STATE_DIR" 2>/dev/null

# T2 pre-check — skip retrieval entirely if cumulative budget exhausted.
_TOK_USED=0
if [ -f "$B12_TOK_STATE" ]; then
  _TOK_USED=$(cat "$B12_TOK_STATE" 2>/dev/null | tr -cd '0-9')
  [ -z "$_TOK_USED" ] && _TOK_USED=0
fi
if [ "$_TOK_USED" -ge "$B12_TOK_SESSION_MAX" ]; then
  # Best-effort log line — never blocks
  echo "{\"ts\":$(date +%s),\"session_id\":\"${SESSION_ID12}\",\"reason\":\"cumulative_cap\",\"used\":${_TOK_USED},\"ceiling\":${B12_TOK_SESSION_MAX}}" \
    >> "$B12_BASE_FOR_STATE/memory-logs/token-budget-skips.jsonl" 2>/dev/null
  exit 0
fi

# Q2 (P-LONGSESSION) turn counter bump — happens BEFORE any prompt-trivial
# skip so the counter tracks actual UserPromptSubmit fires, not just
# fires that produced retrieval work. The expensive re-surface decision
# still lives later in the script behind the same early-exit guards.
_Q2_TURN_FILE="$B12_STATE_DIR/session-turn-counter-${SESSION_ID12}.txt"
_Q2_TURN=0
if [ -f "$_Q2_TURN_FILE" ]; then
  _Q2_TURN=$(cat "$_Q2_TURN_FILE" 2>/dev/null | tr -cd '0-9')
  [ -z "$_Q2_TURN" ] && _Q2_TURN=0
fi
_Q2_TURN=$((_Q2_TURN + 1))
echo "$_Q2_TURN" > "${_Q2_TURN_FILE}.tmp" 2>/dev/null && \
  mv "${_Q2_TURN_FILE}.tmp" "$_Q2_TURN_FILE" 2>/dev/null

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

# ── Recall-verb detection (v8 — forensic-driven, EN+TR) ───────
# When the user uses an explicit recall verb, the retrieval path
# widens (limit 8 not 5), forces hybrid rerank (no skip), and the
# output additionalContext gets a directive prefix so the model
# knows the user explicitly asked for memory recall.
# Closes the 83% Claude Code underutilization gap from the audit
# at internal design notes section 6.
RECALL_VERB_HIT=false
if echo "$PROMPT_LOWER" | grep -qE '\b(remember|recall|last time|previously|earlier|prior|before|said|told|mentioned|stored|saved)\b'; then
  RECALL_VERB_HIT=true
fi
# Turkish recall verbs (Unicode-aware — works under UTF-8 locale)
if [ "$RECALL_VERB_HIT" = false ]; then
  if echo "$PROMPT_LOWER" | grep -qiE 'hatırla|hatırlıyor|geçen sefer|daha önce|önceki|demiştik|söylemiştim|söylemişti|kaydetmiştik|nerden geldiğini hatırlamadığım'; then
    RECALL_VERB_HIT=true
  fi
fi

DB_PATH="$(b12_get_db_path)"   # P3: cached resolver (avoids a python3 spawn every prompt)
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

# Recall verb overrides everything else — always force hybrid so the
# rerank step does not get skipped on a high-FTS-score top hit.
if [ "$RECALL_VERB_HIT" = true ]; then
  QUERY_MODE="hybrid"
# Check negation first (highest priority → force hybrid)
elif echo "$PROMPT_LOWER" | grep -qE '\b(never|nobody|no one|nothing|nowhere)\b|n'\''t\b| not '; then
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
      a=$(echo "$a" | sed "s/['\";(){}*^:\\\\]//g" | sed 's/--//g' | sed -E 's/(^|[[:space:]])(AND|OR|NOT|NEAR)([[:space:]]|$)/\1\3/gI')
      a=$(echo "$a" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
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
SAFE_KEYWORDS=$(echo "$KEYWORDS" | sed "s/['\";(){}*^:\\\\]//g" | sed 's/--//g' | sed 's|/\*||g' | sed 's|\*/||g' | sed -E 's/(^|[[:space:]])NEAR([[:space:]]|$)/ /gI')
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

# ── FTS5 search with FSRS power-law retention ──────────────────
# v8: importance+strength both feed effective stability (eff_stab = S*(1+4*imp));
#     4-term weights 0.25/0.25/0.40/0.10 match MCP _unified_score on its DEFAULTS.
# NOTE: alpha (4.0) and the weights are compiled-in literals here. The MCP scorer's
# B12_AGING_ALPHA / B12_WEIGHT_* env overrides are NOT honored by this hook (it has
# never read B12_WEIGHT_* — weights were always hardcoded). So those env knobs tune
# `memory_search` only; this UserPromptSubmit ranking always uses the defaults. Parity
# with _unified_score holds on the default config (the common case); a deployment that
# overrides them deliberately accepts that the two surfaces diverge.
RESULTS=$(sqlite3 "$DB_PATH" "
  WITH base AS (
    SELECT m.id AS id,
           m.memory_type AS memory_type,
           m.content AS content,
           m.strength AS strength,
           f.rank AS rank,
           (julianday('now') - julianday(datetime(COALESCE(m.last_accessed_at, m.created_at), 'unixepoch'))) AS age_days,
           max(min(CASE
               WHEN json_valid(m.metadata) AND json_type(m.metadata, '$.importance_score') IN ('integer','real')
               THEN (CASE WHEN json_extract(m.metadata, '$.importance_score') >= 1.0
                          THEN json_extract(m.metadata, '$.importance_score') / 2.0
                          ELSE json_extract(m.metadata, '$.importance_score') END)
               ELSE 0.50 END, 1.0), 0.0) AS imp_norm
    FROM memories m
    JOIN memory_fts f ON m.id = f.rowid
    WHERE f.memory_fts MATCH '${FTS_PARTS}'
      AND m.deleted_at IS NULL
      AND (m.valid_until IS NULL OR m.valid_until > datetime('now'))
      AND m.memory_type NOT IN ('session_summary', 'progress')
      AND (m.tags IS NULL OR m.tags NOT LIKE '%session-summary%')
  ),
  scored AS (
    SELECT id,
           '[' || memory_type || '] ' || replace(substr(content, 1, 300), char(10), ' ') as display,
           (
             0.25 * max(1.0 / (1.0 + age_days / (9.0 * COALESCE(strength, 1.0) * (1.0 + 4.0 * imp_norm))), 0.01)
             + 0.25 * imp_norm
             -- BM25 relevance: FTS5 rank is negative (more-negative = better match),
             -- so relevance must INCREASE with abs(rank). The old 1/(1+abs(rank))
             -- DECREASED with match strength (best matches scored lowest) — the same
             -- inversion fixed in the MCP _unified_score (v11.0.0) but never in this
             -- hook SQL. Parity with b12_mcp_server.py: min(abs(rank)/20, 1.0). (audit #3)
             + 0.40 * min(abs(rank) / 20.0, 1.0)
             + 0.10 * min(COALESCE(strength, 1.0) / 5.0, 1.0)
           ) as score
    FROM base
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

# Smart re-rank skip: if top FTS score is very high, reranking won't help.
# Recall-verb queries bypass the skip — the user explicitly asked for memory.
if [ "$SHOULD_RERANK" = true ] && [ "$RESULT_COUNT" -ge 3 ] && [ "$RECALL_VERB_HIT" = false ]; then
  TOP_SCORE=$(echo "$RESULTS" | head -1 | awk -F'|' '{print $3}')
  if [ -n "$TOP_SCORE" ] && echo "$TOP_SCORE" | awk '{exit ($1 > 0.8) ? 0 : 1}' 2>/dev/null; then
    SHOULD_RERANK=false
    SKIP_REASON="high_fts_score"
  fi
fi

# ── T3 dedup: load already-injected IDs for this session ──────
# Comma-bracketed for substring tests later (`,${id},`).
_DEDUP_IDS=","
if [ -f "$B12_DEDUP_LEDGER" ]; then
  while IFS= read -r _did; do
    _did="${_did//[^0-9]/}"
    [ -n "$_did" ] && _DEDUP_IDS="${_DEDUP_IDS}${_did},"
  done < "$B12_DEDUP_LEDGER"
fi

# Strip dedup IDs from FTS pool before scoring continues.
if [ "$_DEDUP_IDS" != "," ] && [ "$RESULT_COUNT" -gt 0 ]; then
  _FILTERED_IDS=""
  _FILTERED_DISPLAY=""
  _FILTERED_COUNT=0
  _OLD_DISPLAY_LINES=$(printf '%b' "$RESULT_DISPLAY")
  _idx=0
  while IFS= read -r _line; do
    _idx=$((_idx + 1))
    [ -z "$_line" ] && continue
  done <<< "$_OLD_DISPLAY_LINES"
  # Rebuild from RESULT_IDS while honoring dedup.
  _idx=0
  _OLD_IDS=$(echo "$RESULT_IDS" | tr ',' '\n')
  while IFS= read -r _cand_id; do
    _idx=$((_idx + 1))
    [ -z "$_cand_id" ] && continue
    if echo "$_DEDUP_IDS" | grep -q ",${_cand_id},"; then
      continue
    fi
    _line=$(printf '%b' "$RESULT_DISPLAY" | sed -n "${_idx}p")
    _FILTERED_IDS="${_FILTERED_IDS}${_cand_id},"
    _FILTERED_DISPLAY="${_FILTERED_DISPLAY}${_line}\n"
    _FILTERED_COUNT=$((_FILTERED_COUNT + 1))
  done <<< "$_OLD_IDS"
  RESULT_IDS="$_FILTERED_IDS"
  RESULT_DISPLAY="$_FILTERED_DISPLAY"
  RESULT_COUNT="$_FILTERED_COUNT"
  _FTS_COUNT="$RESULT_COUNT"
  # When dedup empties the pool there's nothing left to rerank — clear the
  # flag so the cold-fallback semantic loader doesn't fire just because
  # SHOULD_RERANK was decided BEFORE the dedup filter ran.
  if [ "$RESULT_COUNT" -eq 0 ]; then
    NEEDS_COLD_SEMANTIC=true
    SHOULD_RERANK=false
    SKIP_REASON="dedup_emptied"
  fi
fi

# ── Daemon path: parallel semantic + rerank (Phase 1 + v11) ───
# The daemon may have self-healed after a Homebrew Python upgrade (or exited
# on idle/RSS). Start the replacement asynchronously; this request keeps the
# existing fail-soft FTS/cold-fallback behavior while the model warms.
b12_ensure_embed_daemon 2>/dev/null || true
if daemon_alive; then
  # S2: prefer the `recall` op — single round-trip, threshold + dedup pushed
  # into the daemon. Falls back to legacy semantic_search if the daemon is
  # too old to know `recall`.
  if [ "$QUERY_MODE" != "keyword" ]; then
  _SKIP_IDS_JSON=$(echo "$_DEDUP_IDS" | tr ',' '\n' | grep -E '^[0-9]+$' | jq -Rn '[inputs | tonumber]')
  _REQ=$(jq -nc --arg q "$PROMPT" --arg db "$DB_PATH" --argjson skip "$_SKIP_IDS_JSON" \
    '{op:"recall",query:$q,db_path:$db,limit:5,threshold:0.55,skip_ids:$skip}')
  _RESP=$(daemon_request "$_REQ")
  _USED_RECALL=false
  _DAEMON_ANSWERED=false
  if echo "$_RESP" | jq -e '.ok == true' >/dev/null 2>&1; then
    _USED_RECALL=true
    _DAEMON_ANSWERED=true
    _SEM_DATA=$(echo "$_RESP" | jq -r '.results[] | "\(.id)|\(.display)"')
  else
    # Older daemon: fall back to semantic_search.
    _REQ=$(jq -nc --arg q "$PROMPT" --arg db "$DB_PATH" '{op:"semantic_search",query:$q,db_path:$db,limit:5}')
    _RESP=$(daemon_request "$_REQ")
    if echo "$_RESP" | jq -e '.ok == true' >/dev/null 2>&1; then
      _DAEMON_ANSWERED=true
      _SEM_DATA=$(echo "$_RESP" | jq -r '.results[] | "\(.id)|\(.display)"')
    else
      _SEM_DATA=""
    fi
  fi
  # If the daemon answered at all (recall or legacy semantic_search), trust
  # the result — don't fire the 2-3s cold fallback just because we got
  # back zero hits. Cold fallback exists for daemon-down emergencies, not
  # for "no semantic match above threshold" — which is a legitimate signal.
  if [ "$_DAEMON_ANSWERED" = true ]; then
    NEEDS_COLD_SEMANTIC=false
  fi
  if [ -n "$_SEM_DATA" ]; then
    _EXISTING_IDS=",${RESULT_IDS}"
    _SEM_ADDED=0
    while IFS='|' read -r _id _display; do
      if [ -n "$_id" ]; then
        # Deduplicate: skip if already in FTS results, OR in T3 ledger
        if echo "$_EXISTING_IDS" | grep -q ",${_id},"; then
          continue
        fi
        if echo "$_DEDUP_IDS" | grep -q ",${_id},"; then
          continue
        fi
        RESULT_IDS="${RESULT_IDS}${_id},"
        # Q4 4-field format: when the recall op returned source_session +
        # importance + project + preview, replace the bare `[type] preview`
        # with `[type|src:sid12|imp:0.85|proj:B12] preview` so the LLM can
        # tell where the memory came from at a glance.
        if [ "$_USED_RECALL" = true ]; then
          _META=$(echo "$_RESP" | jq -r --argjson id "$_id" \
            '.results[] | select(.id==$id) | "\(.memory_type)|\(.source_session)|\(.importance)|\(.project)|\(.preview)"' 2>/dev/null)
          if [ -n "$_META" ] && [ "$_META" != "||||" ]; then
            IFS='|' read -r _mt _ssid _imp _proj _prev <<< "$_META"
            _ssid_clean="${_ssid:-?}"
            _proj_clean="${_proj:-?}"
            _imp_clean="${_imp:-0.50}"
            _display="[${_mt}|src:${_ssid_clean}|imp:${_imp_clean}|proj:${_proj_clean}] ${_prev}"
          fi
        fi
        RESULT_DISPLAY="${RESULT_DISPLAY}${_display}\n"
        RESULT_COUNT=$((RESULT_COUNT + 1))
        _SEM_ADDED=$((_SEM_ADDED + 1))
      fi
    done <<< "$_SEM_DATA"
    NEEDS_COLD_SEMANTIC=false
    if [ "$_USED_RECALL" = true ]; then
      _SEM_LABEL="daemon_recall"
    else
      _SEM_LABEL="daemon_semantic"
    fi
    if [ "$_FTS_COUNT" -gt 0 ]; then
      SEARCH_SOURCE="fts5+${_SEM_LABEL}"
    else
      SEARCH_SOURCE="${_SEM_LABEL}"
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
    import os as _os
    from sentence_transformers import SentenceTransformer
    _model_name = _os.environ.get('MCP_EMBEDDING_MODEL', 'BAAI/bge-m3')
    model = SentenceTransformer(_model_name)
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
              AND (m.valid_until IS NULL OR m.valid_until > datetime('now'))
              AND m.memory_type NOT IN ('session_summary', 'progress')
              AND (m.tags IS NULL OR m.tags NOT LIKE '%session-summary%')
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

# Trim to top 5 (top 8 when the user used a recall verb — explicit ask).
DISPLAY_LIMIT=5
[ "$RECALL_VERB_HIT" = true ] && DISPLAY_LIMIT=8
if [ "$RERANK_DONE" = false ] && [ "$RESULT_COUNT" -gt "$DISPLAY_LIMIT" ]; then
  RESULT_DISPLAY=$(echo -e "$RESULT_DISPLAY" | head -"$DISPLAY_LIMIT")
  RESULT_IDS=$(echo "$RESULT_IDS" | tr ',' '\n' | head -"$DISPLAY_LIMIT" | tr '\n' ',' | sed 's/,$//')
  RESULT_IDS="${RESULT_IDS},"
  RESULT_COUNT="$DISPLAY_LIMIT"
fi

# ── Boost strength via FSRS (spaced repetition) ────────────────
# S3 (P-SPEED): Python startup + DB UPDATE = ~80-120ms. The injection's
# already decided at this point, so backgrounding the strength boost
# keeps the foreground under the 200ms cap while the FSRS update
# still completes for the next session's recall.
if [ "$RESULT_COUNT" -gt 0 ]; then
  BOOST_IDS=$(echo "$RESULT_IDS" | tr ',' '\n' | grep -E '^[0-9]+$' | head -5 | tr '\n' ',' | sed 's/,$//')
  B12_SCRIPTS="${B12_HOOK_DIR:-$HOME/.B12/hooks}/scripts"
  {
  python3 - "$DB_PATH" "$BOOST_IDS" "$B12_SCRIPTS" << 'FSRS_EOF' >/dev/null 2>&1
import sys, os, sqlite3, json
db_path, boost_ids_str, scripts_dir = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path.insert(0, scripts_dir)
try:
    from b12_scheduler import review_memory, FSRS_AVAILABLE
except ImportError:
    FSRS_AVAILABLE = False

ids = [int(x) for x in boost_ids_str.split(",") if x.strip().isdigit()]
if not ids:
    sys.exit(0)

conn = sqlite3.connect(db_path, timeout=10)
conn.execute("PRAGMA busy_timeout=10000")

for mid in ids:
    row = conn.execute(
        "SELECT strength, difficulty, due_date, metadata FROM memories WHERE id = ?", (mid,)
    ).fetchone()
    if not row:
        continue
    strength, difficulty, due_date, meta_str = row
    strength = strength or 1.0
    difficulty = difficulty or 5.0
    access_count = 0
    try:
        if meta_str and json.loads(meta_str):
            access_count = json.loads(meta_str).get("access_count", 0)
    except (json.JSONDecodeError, TypeError):
        pass

    if FSRS_AVAILABLE:
        result = review_memory(
            stability=strength, difficulty=difficulty,
            due_date=due_date, rating="good", access_count=access_count
        )
        new_strength = result["stability"]
        new_difficulty = result["difficulty"]
        new_due = result["due_date"]
    else:
        new_strength = min(strength + 0.2, 5.0)
        new_difficulty = difficulty
        new_due = due_date

    # Update memory with new FSRS values
    meta = {}
    try:
        meta = json.loads(meta_str) if meta_str else {}
    except (json.JSONDecodeError, TypeError):
        pass
    meta["access_count"] = access_count + 1

    conn.execute(
        """UPDATE memories
           SET strength = ?, difficulty = ?, due_date = ?,
               last_accessed_at = unixepoch('now'),
               metadata = ?
           WHERE id = ?""",
        (new_strength, new_difficulty, new_due, json.dumps(meta, ensure_ascii=False), mid)
    )

conn.commit()
conn.close()
FSRS_EOF
  } >/dev/null 2>&1 &
  disown
fi

# ── Graph-aware context expansion (Phase 2) ──────────────────
# 1-hop expansion from top-3 results via memory_graph edges (~2ms SQLite)
GRAPH_EXPANDED=""
if [ "$RESULT_COUNT" -gt 0 ]; then
  TOP_3_IDS=$(echo "$RESULT_IDS" | tr ',' '\n' | grep -E '^[0-9]+$' | head -3 | tr '\n' ',' | sed 's/,$//')
  ALL_IDS=$(echo "$RESULT_IDS" | tr ',' '\n' | grep -E '^[0-9]+$' | tr '\n' ',' | sed 's/,$//')
  if [ -n "$TOP_3_IDS" ]; then
    # Graph-expanded rows carry the same Q4 4-field surface format as the
    # primary results (`[type|src:sid12|imp:X|proj:Y] preview`). Without
    # this, the model sees a mixed-format inject where top-3 hits have the
    # 4-field anchor and the graph block reverts to legacy `[mtype] preview`,
    # which (a) defeats the format consistency Codex flagged in round-3
    # and (b) breaks T1's `^\[[^]]*\|src:` line counter on the graph rows.
    # Single-column SELECT — the printf at line 1010 emits the value
    # verbatim, so an extra `id|` prefix would clutter the inject.
    GRAPH_EXPANDED=$(sqlite3 "$DB_PATH" "
      SELECT DISTINCT
             '[' || m2.memory_type ||
             '|src:' || COALESCE(SUBSTR(json_extract(m2.metadata, '\$.source_session'), 1, 12), '?') ||
             '|imp:' || COALESCE(
               CASE WHEN json_extract(m2.metadata, '\$.importance_score') IS NOT NULL
                    THEN printf('%.2f', json_extract(m2.metadata, '\$.importance_score'))
               END,
               '0.50'
             ) ||
             '|proj:' || COALESCE(json_extract(m2.metadata, '\$.project'), '?') ||
             '] ' || replace(substr(m2.content, 1, 200), char(10), ' ')
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
    --arg rv "$RECALL_VERB_HIT" \
    '{ts: (now|floor), type: "hook_retrieval", query: $q, keywords: $kw, result_count: $rc, reranked: ($rr == "true"), query_mode: $qm, skip_reason: $sr, search_source: $ss, latency_ms: $lat, project: $proj, recall_verb_hit: ($rv == "true")}' \
    >> "$FEEDBACK_FILE" 2>/dev/null
fi

# ── Q2 long-session re-surface (P-LONGSESSION) ───────────────
# Bump the turn counter once per UserPromptSubmit. On every Nth turn
# (default 20, override via B12_RESURFACE_EVERY_N) ask
# scripts/b12_long_session.py for a small batch of THIS session's
# early-captured high-importance memories so they don't fade out of
# the model's effective working window. The Python module is small
# enough that the import cost (~30ms) is comfortably inside the S3
# sync cap.
_RESURFACE_BLOCK=""
# Counter was already bumped at the top of the hook; here we just *peek*
# at the value and decide whether to fire. The peek path lets short
# greeting prompts still advance the turn counter (correct
# UserPromptSubmit accounting) without firing the expensive Python
# pick_resurface_ids on them.
_RESURFACE_EVERY_N="${B12_RESURFACE_EVERY_N:-20}"
_RESURFACE_FIRE=false
if [ "$_RESURFACE_EVERY_N" -gt 0 ] 2>/dev/null && \
   [ "$_Q2_TURN" -ge "$_RESURFACE_EVERY_N" ] && \
   [ $((_Q2_TURN % _RESURFACE_EVERY_N)) -eq 0 ]; then
  _RESURFACE_FIRE=true
fi
if [ "$_RESURFACE_FIRE" = true ] && [ -n "$VENV_PYTHON" ] && [ -x "$VENV_PYTHON" ]; then
  # Phase E: same-session re-surface PLUS cross-session anchors. The
  # python heredoc returns both lists in one process so we don't pay
  # ~30ms of Python startup twice. Cross-session is opt-in only when
  # we have a project name (PARENT_PROJECT or PROJECT_NAME) — the
  # filter requires a concrete project string for scope discipline.
  _RESURFACE_PROJ="${PARENT_PROJECT:-$PROJECT_NAME}"
  _RESURFACE_OUT=$("$VENV_PYTHON" - "$SESSION_ID" "$DB_PATH" "$_Q2_TURN" "$_DEDUP_IDS" "$_RESURFACE_PROJ" << 'PYEOF' 2>/dev/null
import os, sys, json
_hook_dir = os.environ.get('B12_HOOK_DIR', os.path.expanduser('~/.B12/hooks'))
sys.path.insert(0, os.path.join(_hook_dir, 'scripts'))
try:
    from b12_long_session import pick_resurface_ids, pick_cross_session_ids
except ImportError:
    sys.exit(0)
sid = sys.argv[1]
db_path = sys.argv[2]
try:
    turn = int(sys.argv[3])
except (TypeError, ValueError):
    turn = 0
skip_raw = sys.argv[4] if len(sys.argv) > 4 else ''
skip_ids = [int(x) for x in skip_raw.split(',') if x.strip().isdigit()]
project = sys.argv[5] if len(sys.argv) > 5 else ''

hits = pick_resurface_ids(db_path, sid, limit=3, skip_ids=skip_ids)
already = list(skip_ids) + [int(h['id']) for h in hits]
cross = []
if project:
    cross = pick_cross_session_ids(
        db_path, sid, project=project, limit=2, skip_ids=already,
    )
print(json.dumps({"fired": True, "turn": turn, "hits": hits, "cross": cross}, ensure_ascii=False))
PYEOF
  )
  if [ -n "$_RESURFACE_OUT" ]; then
    _RESURFACE_TURN=$(echo "$_RESURFACE_OUT" | jq -r '.turn // ""' 2>/dev/null)
    _RESURFACE_HITS_JSON=$(echo "$_RESURFACE_OUT" | jq -c '.hits // []' 2>/dev/null)
    _RESURFACE_HIT_COUNT=$(echo "$_RESURFACE_HITS_JSON" | jq -r 'length' 2>/dev/null)
    _RESURFACE_CROSS_JSON=$(echo "$_RESURFACE_OUT" | jq -c '.cross // []' 2>/dev/null)
    _RESURFACE_CROSS_COUNT=$(echo "$_RESURFACE_CROSS_JSON" | jq -r 'length' 2>/dev/null)
    if [ "${_RESURFACE_HIT_COUNT:-0}" -gt 0 ] || [ "${_RESURFACE_CROSS_COUNT:-0}" -gt 0 ]; then
      _RESURFACE_BODY=""
      if [ "${_RESURFACE_HIT_COUNT:-0}" -gt 0 ]; then
        _RESURFACE_BODY=$(echo "$_RESURFACE_HITS_JSON" | \
          jq -r '.[] | "[\(.memory_type)|src:\(.source_session)|imp:\(.importance|tostring)|proj:\(.project)] \(.preview)"')
      fi
      _RESURFACE_CROSS_BODY=""
      if [ "${_RESURFACE_CROSS_COUNT:-0}" -gt 0 ]; then
        _RESURFACE_CROSS_BODY=$(echo "$_RESURFACE_CROSS_JSON" | \
          jq -r '.[] | "[\(.memory_type)|src:\(.source_session)|imp:\(.importance|tostring)|proj:\(.project)|cross-session] \(.preview)"')
      fi
      if [ -n "$_RESURFACE_BODY" ] && [ -n "$_RESURFACE_CROSS_BODY" ]; then
        _RESURFACE_BLOCK=$'\n[long-session re-surface (turn '"${_RESURFACE_TURN}"$', same-session high-importance + cross-session anchors)]\n'"${_RESURFACE_BODY}"$'\n'"${_RESURFACE_CROSS_BODY}"$'\n'
      elif [ -n "$_RESURFACE_BODY" ]; then
        _RESURFACE_BLOCK=$'\n[long-session re-surface (turn '"${_RESURFACE_TURN}"$', high-importance from this session)]\n'"${_RESURFACE_BODY}"$'\n'
      else
        _RESURFACE_BLOCK=$'\n[long-session re-surface (turn '"${_RESURFACE_TURN}"$', cross-session anchors for project '"${_RESURFACE_PROJ}"')]\n'"${_RESURFACE_CROSS_BODY}"$'\n'
      fi
      _RESURFACE_IDS=$( {
        echo "$_RESURFACE_HITS_JSON" | jq -r '.[].id' 2>/dev/null
        echo "$_RESURFACE_CROSS_JSON" | jq -r '.[].id' 2>/dev/null
      } )
    fi
  fi
fi

# ── Q2 topic-shift re-surface (P-BURNIN / Phase C) ───────────
# Orthogonal trigger to the periodic-N counter above: when consecutive
# user prompts drift in topic (cosine < 0.55 against the previous
# prompt's embedding), the session has likely topic-shifted and the
# model can benefit from a fresh injection of THIS session's
# high-importance memories that may have slid out of the effective
# working window. Stricter filter than periodic (importance >= 0.8,
# older than session midpoint). Skipped if the periodic re-surface
# already fired this turn — no point firing twice.
#
# Default off if B12_TOPICSHIFT_DISABLE=1 is set. Daemon-route only —
# falls through silently if daemon down (this is best-effort signal).
_TOPICSHIFT_BLOCK=""
if [ "${B12_TOPICSHIFT_DISABLE:-0}" != "1" ] && \
   [ -z "$_RESURFACE_BLOCK" ] && \
   [ -n "$VENV_PYTHON" ] && [ -x "$VENV_PYTHON" ] && \
   [ -S "$EMBED_SOCK" ] && [ -n "$PROMPT" ]; then
  _TS_THRESHOLD="${B12_TOPICSHIFT_COSINE:-0.55}"
  _TOPICSHIFT_OUT=$(B12_TS_PROMPT="$PROMPT" "$VENV_PYTHON" - "$SESSION_ID" "$DB_PATH" "$EMBED_SOCK" "$_TS_THRESHOLD" "$_DEDUP_IDS" << 'PYEOF' 2>/dev/null
import os, sys, json, socket
_hook_dir = os.environ.get('B12_HOOK_DIR', os.path.expanduser('~/.B12/hooks'))
sys.path.insert(0, os.path.join(_hook_dir, 'scripts'))
try:
    from b12_long_session import topic_shift_check, pick_topic_shift_ids
except ImportError:
    sys.exit(0)

sid = sys.argv[1]
db_path = sys.argv[2]
sock_path = sys.argv[3]
try:
    threshold = float(sys.argv[4])
except (TypeError, ValueError):
    threshold = 0.55
skip_raw = sys.argv[5] if len(sys.argv) > 5 else ''
skip_ids = [int(x) for x in skip_raw.split(',') if x.strip().isdigit()]

# Prompt comes through env, not stdin — `python3 - << PYEOF` already
# uses stdin for the script body, so reading argv-shaped data via env
# avoids the stdin collision.
prompt = (os.environ.get("B12_TS_PROMPT", "") or "").strip()
if not prompt or len(prompt) < 4:
    sys.exit(0)

# Embed the current prompt via the daemon's encode_batch op.
try:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(2.0)
    s.connect(sock_path)
    req = json.dumps({"op": "encode_batch", "texts": [prompt]}) + "\n"
    s.sendall(req.encode("utf-8"))
    chunks = []
    while True:
        c = s.recv(65536)
        if not c:
            break
        chunks.append(c)
    s.close()
    resp = json.loads(b"".join(chunks).decode("utf-8", errors="replace"))
except Exception:
    sys.exit(0)

if not resp.get("ok") or not resp.get("embeddings"):
    sys.exit(0)
cur_emb_b64 = resp["embeddings"][0]

shifted, cos = topic_shift_check(sid, cur_emb_b64, threshold=threshold)
if not shifted:
    sys.exit(0)

hits = pick_topic_shift_ids(db_path, sid, limit=3, skip_ids=skip_ids)
print(json.dumps({"shifted": True, "cosine": cos, "threshold": threshold, "hits": hits}, ensure_ascii=False))
PYEOF
  )
  if [ -n "$_TOPICSHIFT_OUT" ]; then
    _TS_COS=$(echo "$_TOPICSHIFT_OUT" | jq -r '.cosine // ""' 2>/dev/null)
    _TS_HITS_JSON=$(echo "$_TOPICSHIFT_OUT" | jq -c '.hits // []' 2>/dev/null)
    _TS_HIT_COUNT=$(echo "$_TS_HITS_JSON" | jq -r 'length' 2>/dev/null)
    if [ "${_TS_HIT_COUNT:-0}" -gt 0 ]; then
      _TS_BODY=$(echo "$_TS_HITS_JSON" | \
        jq -r '.[] | "[\(.memory_type)|src:\(.source_session)|imp:\(.importance|tostring)|proj:\(.project)] \(.preview)"')
      _TOPICSHIFT_BLOCK=$'\n[topic-shift re-surface (cos='"${_TS_COS}"$' < threshold, high-importance older memories from this session)]\n'"${_TS_BODY}"$'\n'
      # Don't write ledger yet — T1/T2 may trim. Same pattern as periodic.
      _RESURFACE_IDS=$(echo "$_TS_HITS_JSON" | jq -r '.[].id')
    fi
  fi
fi

# Merge periodic + topic-shift blocks (only one can be set; the
# guard above skips topic-shift when periodic already fired).
if [ -n "$_TOPICSHIFT_BLOCK" ]; then
  _RESURFACE_BLOCK="$_TOPICSHIFT_BLOCK"
fi

# ── Output ─────────────────────────────────────────────────────
# Re-surface block alone is enough to inject even if regular retrieval
# came up empty — the model still benefits from the periodic anchor.
if [ -z "$RESULT_DISPLAY" ] || [ "$RESULT_COUNT" -eq 0 ]; then
  if [ -z "$_RESURFACE_BLOCK" ]; then
    exit 0
  fi
fi

# ── Q4 4-field surface format ────────────────────────────────
# Rewrite RESULT_DISPLAY so every line carries `[type|src:sid12|imp:X|proj:Y]`
# regardless of whether it came from FTS5, rerank, daemon recall, or graph.
# Source-of-truth is a single SQLite roundtrip; falls back to the original
# RESULT_DISPLAY on any error so we never lose an inject because of a
# format issue.
# Honor the DISPLAY_LIMIT cap (5 default, 8 on recall-verb) when picking
# the Q4 ID set — the FTS-only trim earlier already capped RESULT_IDS at
# DISPLAY_LIMIT, so using `head -10` here would surface rows the FTS pass
# deliberately skipped (prompt-size + recall-quality drift).
_FINAL_IDS=$(echo "$RESULT_IDS" | tr ',' '\n' | grep -E '^[0-9]+$' | head -"$DISPLAY_LIMIT" | tr '\n' ',' | sed 's/,$//')
if [ -n "$_FINAL_IDS" ]; then
  # Build ORDER BY CASE map in bash so we don't have to fight nested
  # shell-quoting inside an awk-inside-sqlite3-inside-bash sandwich.
  _Q4_ORDER=""
  _idx=0
  for _qid in $(echo "$_FINAL_IDS" | tr ',' ' '); do
    _idx=$((_idx + 1))
    _Q4_ORDER="${_Q4_ORDER} WHEN ${_qid} THEN ${_idx}"
  done
  # NOTE on importance fallback: `printf('%.2f', NULL)` in SQLite returns
  # the string `'0.00'`, which the outer COALESCE then treats as a non-NULL
  # match — so legacy memories without `importance_score` in metadata would
  # surface as `imp:0.00` instead of the neutral `imp:0.50` default. Guard
  # with CASE before printf so the COALESCE actually applies.
  _Q4_ROWS=$(sqlite3 "$DB_PATH" "
    SELECT m.id,
           m.memory_type,
           COALESCE(SUBSTR(json_extract(m.metadata, '\$.source_session'), 1, 12), '?'),
           COALESCE(
             CASE WHEN json_extract(m.metadata, '\$.importance_score') IS NOT NULL
                  THEN printf('%.2f', json_extract(m.metadata, '\$.importance_score'))
             END,
             '0.50'
           ),
           COALESCE(json_extract(m.metadata, '\$.project'), '?'),
           SUBSTR(replace(replace(m.content, char(10), ' '), char(9), ' '), 1, 80)
    FROM memories m
    WHERE m.id IN (${_FINAL_IDS})
    ORDER BY CASE m.id${_Q4_ORDER} END
  " 2>/dev/null)
  if [ -n "$_Q4_ROWS" ]; then
    RESULT_DISPLAY=""
    while IFS='|' read -r _mid _mt _ssid _imp _proj _prev; do
      [ -z "$_mid" ] && continue
      RESULT_DISPLAY="${RESULT_DISPLAY}[${_mt}|src:${_ssid}|imp:${_imp}|proj:${_proj}] ${_prev}\n"
    done <<< "$_Q4_ROWS"
    _Q4_REFORMAT_OK=true
  fi
fi
_Q4_REFORMAT_OK="${_Q4_REFORMAT_OK:-false}"

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
    # Surface threshold: legacy 0.71-0.79 edges (pre-v12.3) over-emit
    # cross-domain false positives like SQLite vs PostgreSQL. Default 0.85.
    # Codex review PR #57 P2: coerce + validate the env var BEFORE
    # interpolating into SQL. Malformed values (e.g. "abc", "0,85") cause
    # sqlite3 parse failures that, with stderr suppressed, silently kill
    # the contradiction warning. Fall back to default on any parse error.
    _CONTRA_SURFACE_RAW="${B12_CONTRA_SURFACE_THRESHOLD:-0.85}"
    _CONTRA_SURFACE_THRESHOLD=$(awk -v v="$_CONTRA_SURFACE_RAW" 'BEGIN{
      if (v+0 == v && v+0 >= 0 && v+0 <= 1) { printf("%.4f", v+0) } else { print "0.85" }
    }')
    _CONTRA_HITS=$(sqlite3 "$DB_PATH" "
      SELECT DISTINCT m.id || ' ' || m2.id
      FROM memory_graph mg
      JOIN memories m ON m.content_hash = mg.source_hash
      JOIN memories m2 ON m2.content_hash = mg.target_hash
      WHERE m.id IN (${_CONTRA_IDS})
        AND mg.relationship_type = 'contradicts'
        AND mg.similarity >= ${_CONTRA_SURFACE_THRESHOLD}
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

# Build the candidate context string first so we can apply T1/T2 caps
# BEFORE handing it back to Claude Code.
_CANDIDATE_CONTEXT=$({
  if [ "$RECALL_VERB_HIT" = true ]; then
    printf '%s' "Memory retrieval (auto, ${QUERY_MODE}, recall-verb HIT, keywords: ${SAFE_KEYWORDS}):"
    printf '\n[directive] user used a recall verb — surface these memories aggressively and follow up with memory_search if the top hits do not cover the user'\''s intent.\n'
  else
    printf '%s' "Memory retrieval (auto, ${QUERY_MODE}, keywords: ${SAFE_KEYWORDS}):"
    printf '\n'
  fi
  printf '%b' "$RESULT_DISPLAY"
  if [ -n "$GRAPH_EXPANDED" ]; then
    printf '\nRelated (graph):\n'
    printf '%s\n' "$GRAPH_EXPANDED"
  fi
  if [ -n "$_RESURFACE_BLOCK" ]; then
    printf '%b' "$_RESURFACE_BLOCK"
  fi
  printf '%s' "$HINT"
  printf '%s' "$CONTRADICTION_WARN"
})

# ── T1 per-turn char cap (≈800 tokens at chars/4) ─────────────
# Trim from the tail; track how many memory lines survived so the T3
# dedup ledger does not over-record IDs that the model never saw.
_CAND_LEN=${#_CANDIDATE_CONTEXT}
_CAND_LEN_BEFORE_TRIM=0
_SURVIVED_MEMORY_LINES=""
if [ "$_CAND_LEN" -gt "$B12_TOK_PER_TURN_CHARS" ]; then
  _CAND_LEN_BEFORE_TRIM="$_CAND_LEN"
  _CANDIDATE_CONTEXT="${_CANDIDATE_CONTEXT:0:$B12_TOK_PER_TURN_CHARS}"
  # Count ONLY memory rows that fit before the cut. Two formats are
  # possible:
  #   Q4 reformat succeeded → `[type|src:sid|imp:X|proj:Y] preview`
  #   Q4 reformat fell back → legacy `[mtype] preview`
  # Headers (`[directive]`, `[Note: ...]`, `[long-session re-surface (...)]`,
  # `[trimmed: ...]`) also start with `[`, so we use an awk pattern that
  # picks the right row format and excludes the known header tokens.
  if [ "$_Q4_REFORMAT_OK" = true ]; then
    _SURVIVED_MEMORY_LINES=$(printf '%s' "$_CANDIDATE_CONTEXT" | grep -cE '^\[[^]]*\|src:' 2>/dev/null || echo 0)
  else
    _SURVIVED_MEMORY_LINES=$(printf '%s' "$_CANDIDATE_CONTEXT" | awk '
      /^\[[a-z_]+\] / && !/^\[(directive|Note|long-session|trimmed)/ { c++ }
      END { print c+0 }' 2>/dev/null || echo 0)
  fi
  _CANDIDATE_CONTEXT="${_CANDIDATE_CONTEXT}"$'\n[trimmed: per-turn token cap hit]'
  _CAND_LEN=${#_CANDIDATE_CONTEXT}
fi

# ── T2 cumulative cap (~80K per session) ──────────────────────
# Char-based proxy: chars / 4 ≈ tokens (R10 — no real tokenizer in hooks).
_CAND_TOK=$(( (_CAND_LEN + 3) / 4 ))
_WOULD_BE=$(( _TOK_USED + _CAND_TOK ))
if [ "$_WOULD_BE" -gt "$B12_TOK_SESSION_MAX" ]; then
  echo "{\"ts\":$(date +%s),\"session_id\":\"${SESSION_ID12}\",\"reason\":\"would_exceed_cumulative\",\"requested_tokens\":${_CAND_TOK},\"used\":${_TOK_USED},\"ceiling\":${B12_TOK_SESSION_MAX}}" \
    >> "$B12_BASE_FOR_STATE/memory-logs/token-budget-skips.jsonl" 2>/dev/null
  exit 0
fi

# Update cumulative counter atomically (write-then-rename).
echo "$_WOULD_BE" > "${B12_TOK_STATE}.tmp" 2>/dev/null && mv "${B12_TOK_STATE}.tmp" "$B12_TOK_STATE" 2>/dev/null

# ── T3 dedup ledger: prepend only IDs the model actually saw ──
# When T1 trimmed mid-output, _SURVIVED_MEMORY_LINES is the count of
# completely-rendered RESULT_DISPLAY lines. Use that as the cap so we
# don't burn a dedup slot on a memory that never made it through the
# token cap. When trim fired but 0 lines survived (header-only output),
# record 0 IDs — the model saw none of them.
if [ "$RESULT_COUNT" -gt 0 ]; then
  if [ -n "$_SURVIVED_MEMORY_LINES" ] && [ "$_CAND_LEN_BEFORE_TRIM" -gt 0 ]; then
    _DEDUP_LIMIT="$_SURVIVED_MEMORY_LINES"
  else
    # No trim → record up to 10 IDs (legacy upper bound).
    _DEDUP_LIMIT=10
  fi
  if [ "$_DEDUP_LIMIT" -gt 0 ]; then
    _INJECTED_IDS=$(echo "$RESULT_IDS" | tr ',' '\n' | grep -E '^[0-9]+$' | head -"$_DEDUP_LIMIT")
    if [ -n "$_INJECTED_IDS" ]; then
      _NEW_LEDGER=$( {
        echo "$_INJECTED_IDS"
        [ -f "$B12_DEDUP_LEDGER" ] && cat "$B12_DEDUP_LEDGER"
      } | awk 'NF && !seen[$0]++' | head -500 )
      echo "$_NEW_LEDGER" > "${B12_DEDUP_LEDGER}.tmp" 2>/dev/null && \
        mv "${B12_DEDUP_LEDGER}.tmp" "$B12_DEDUP_LEDGER" 2>/dev/null
    fi
  fi
fi

# ── T3 dedup ledger (re-surfaced IDs from Q2 long-session block) ──
# Deferred from the earlier resurface block so T1 trim + T2 cumulative
# cap have already vetoed (or accepted) the inject. We only get here
# when the candidate context passed both gates, so the re-surface IDs
# really were rendered to the assistant.
if [ -n "$_RESURFACE_IDS" ]; then
  _NEW_LEDGER=$( {
    echo "$_RESURFACE_IDS"
    [ -f "$B12_DEDUP_LEDGER" ] && cat "$B12_DEDUP_LEDGER"
  } | awk 'NF && !seen[$0]++' | head -500 )
  echo "$_NEW_LEDGER" > "${B12_DEDUP_LEDGER}.tmp" 2>/dev/null && \
    mv "${B12_DEDUP_LEDGER}.tmp" "$B12_DEDUP_LEDGER" 2>/dev/null
fi

# Flag stdout as "primary output" so a late-firing sync-cap watchdog
# (b12_sync_watchdog at script top) doesn't append a `{}` and corrupt
# the JSON we're about to emit. See _b12_common.sh:_b12_sync_cap_handler.
b12_mark_output_emitted 2>/dev/null || _B12_OUTPUT_EMITTED=1

printf '%s' "$_CANDIDATE_CONTEXT" | \
  jq -Rs '{hookSpecificOutput:{hookEventName:"UserPromptSubmit",additionalContext:.}}'

exit 0
