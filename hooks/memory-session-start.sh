#!/bin/bash
# B12 Memory System - SessionStart Hook (v6 — Compact Context Rebuild)
# Loads: setup context + compressed instructions + user profile (lazy)
#        + session summary + cross-project hints + memory pre-fetch
# Fires on: startup, resume, compact
#
# v6 changes (2026-02-26):
# - Behavioral instructions + memory pre-fetch now injected on BOTH startup AND compact
# - Extracted BEHAVIORAL_INSTR as shared variable (DRY between paths)
# - Compact path rebuilt to match startup path structure with progressive trimming
# - Fixes: after context compression, LLM retains full B12 knowledge
# v5 changes (2026-02-08):
# - FTS5 + tag-based memory pre-fetch (project-relevant + universal)
# - Direct SQLite queries (no embedding model needed)
# v4 changes:
# - Setup detection (personal vs work)
# - Scope-aware tagging/search instructions
# - Compressed behavioral instructions (~120 tokens vs ~512 in v3)
# - jq for cross-project hints (no python3 startup penalty)
# - Lazy-load user profile (only if updated within 7 days)
# - Dual-layer deconfliction rule (MEMORY.md vs MCP memory)

set -o pipefail 2>/dev/null || true

# ── Orphan hook cleanup ──────────────────────────────────────
# Kill memory hook processes orphaned from previous sessions (PPID=1)
for _pid in $(pgrep -f "memory-.*\.sh" 2>/dev/null); do
  [ "$_pid" = "$$" ] && continue
  [ "$(ps -p "$_pid" -o ppid= 2>/dev/null | tr -d ' ')" = "1" ] && kill "$_pid" 2>/dev/null
done

# ── Start embedding daemon (Phase 1 — latency reduction) ─────
# Persistent SentenceTransformer process. Hooks communicate via Unix socket.
# Model loads async (~12s). First few retrieval calls use cold path.
# Singleton: daemon uses fcntl.flock() — only one instance can run.
_UID=$(id -u 2>/dev/null || echo $$)
# Hardcode /tmp/ — macOS TMPDIR varies per session, causing mismatch with daemon
EMBED_SOCK="/tmp/b12-embed-${_UID}.sock"
EMBED_LOCK="/tmp/b12-embed-${_UID}.lock"
VENV_PYTHON="$HOME/.local/b12-venv/bin/python3"
B12_SCRIPTS="${B12_HOOK_DIR:-$HOME/.B12/hooks}/scripts"
DAEMON_SCRIPT="$B12_SCRIPTS/embed_daemon.py"

_DAEMON_NEEDED=true

# Check 1: Socket exists AND responsive? → daemon ready, skip
if [ -S "$EMBED_SOCK" ]; then
  if echo '{"op":"health"}' | nc -w1 -U "$EMBED_SOCK" 2>/dev/null | grep -q '"ok"'; then
    _DAEMON_NEEDED=false
  fi
fi

# Check 2: Lock file PID alive? → daemon loading model, skip
if $_DAEMON_NEEDED && [ -f "$EMBED_LOCK" ]; then
  _lock_pid=$(cat "$EMBED_LOCK" 2>/dev/null | tr -d '[:space:]')
  if [ -n "$_lock_pid" ] && kill -0 "$_lock_pid" 2>/dev/null; then
    _DAEMON_NEEDED=false
  fi
fi

# Start daemon only if no existing instance detected
if $_DAEMON_NEEDED && [ -x "$VENV_PYTHON" ] && [ -f "$DAEMON_SCRIPT" ]; then
  "$VENV_PYTHON" "$DAEMON_SCRIPT" > /dev/null 2>&1 &
  disown 2>/dev/null
fi

# ── Self-timeout watchdog ─────────────────────────────────────
# Kills this script if it exceeds max runtime. Prevents orphan processes.
# Redirect the watchdog's stdio to /dev/null: otherwise its `sleep` child
# inherits the hook's stdout pipe, and when the watchdog is killed at exit the
# orphaned `sleep` keeps that write-end open — a pipe-capturing consumer (the
# test harness, and any host that reads to EOF) then blocks ~15s after the hook
# has actually finished. With the redirect, the hook's output reaches the
# consumer the moment the main process exits.
( sleep 15 && kill -TERM $$ 2>/dev/null ) >/dev/null 2>&1 &
_WATCHDOG=$!
# Wall-clock deadline (bash $SECONDS, no subprocess) the pagerank phases budget
# against, so the pre-count + rank together can't overrun the 15s watchdog and
# trip its TERM trap (exit 143) on an otherwise-valid repo.
_HOOK_DEADLINE=$(( SECONDS + 15 ))
# Cleanup on ANY exit, including when the watchdog SIGTERMs us mid-pagerank:
# tear down the watchdog AND group-kill any in-flight pagerank child (tracked
# in _PR_GUARD_CHILD by _pagerank_run, which runs in THIS shell so the PID is
# visible here). Without the TERM trap, a watchdog kill would orphan that child
# — it would still self-terminate via its own SIGALRM, but we additionally reap
# it here so the hook never leaves one behind.
_PR_GUARD_CHILD=""
_b12_cleanup() {
  if [ -n "${_PR_GUARD_CHILD:-}" ]; then
    kill -KILL -"$_PR_GUARD_CHILD" 2>/dev/null || kill -KILL "$_PR_GUARD_CHILD" 2>/dev/null
  fi
  kill "$_WATCHDOG" 2>/dev/null
  wait "$_WATCHDOG" 2>/dev/null
}
trap _b12_cleanup EXIT
trap '_b12_cleanup; exit 143' TERM

INPUT=$(cat)
SOURCE=$(echo "$INPUT" | jq -r '.source // "startup"')
CWD=$(echo "$INPUT" | jq -r '.cwd // ""')

# Central data directory — override with B12_DATA_DIR env var for custom setups
B12_BASE="${B12_DATA_DIR:-$HOME/.B12}"

# Portable stat mtime, GNU-first. On Linux `stat -f` is --file-system and writes
# an FS report to stdout (not a clean failure), so a BSD-first probe poisons the
# mtime; the all-digits guard is belt-and-suspenders. (audit #8; matches _b12_common.sh)
file_mtime() { local m; m=$(stat -c %Y "$1" 2>/dev/null || stat -f %m "$1" 2>/dev/null); case "$m" in ''|*[!0-9]*) echo "0" ;; *) echo "$m" ;; esac; }

# Configured per-phase pagerank budget (seconds): env, sanitized to a
# non-negative int, clamped <15s watchdog. 0 = documented opt-out (no
# pagerank-level wall-clock kill; still bounded by the hook watchdog trap).
_pr_cfg_budget() {
  local b="${B12_PAGERANK_TIMEOUT_S:-8}"
  case "$b" in (*[!0-9]*|'') b=8 ;; esac
  [ "$b" -gt 12 ] && b=12
  printf '%s' "$b"
}

# Whole seconds left before the 15s self-watchdog fires, minus a 2s safety
# margin. The pre-count and rank phases budget against this so their COMBINED
# wall time can't trip the watchdog's TERM trap (exit 143) on a valid repo.
_pr_remaining() {
  local rem=$(( _HOOK_DEADLINE - SECONDS - 2 ))
  [ "$rem" -lt 0 ] && rem=0
  printf '%s' "$rem"
}

# Run file_pagerank to OUTFILE ($2) under a wall-clock budget ($3 seconds; 0 =
# no pagerank-level kill), killing the whole process GROUP on expiry so an
# orphaned numpy child can never survive the hook — the bug that panicked the
# machine. file_pagerank ALSO self-limits (SIGALRM + os.setsid + best-effort
# RLIMIT_AS); this is the defense-in-depth outer layer.
#
# IMPORTANT: call as a SIMPLE COMMAND in the parent shell (never inside `$( … )`
# or a pipeline). It backgrounds the child and records its PID in the parent's
# _PR_GUARD_CHILD so the EXIT/TERM cleanup trap can group-kill it when the
# watchdog fires mid-run. A command-substitution would run this in a subshell,
# where _PR_GUARD_CHILD would be invisible to the parent trap.
_pagerank_run() {
  local cwd="$1" out="$2" budget="$3"
  case "$budget" in (''|*[!0-9]*) budget=8 ;; esac   # default to a kill, not 0
  "$VENV_PYTHON" "$B12_SCRIPTS/file_pagerank.py" "$cwd" 5 >"$out" 2>/dev/null &
  _PR_GUARD_CHILD=$!   # set in the PARENT → the cleanup trap can group-kill it
  local waited=0
  while kill -0 "$_PR_GUARD_CHILD" 2>/dev/null; do
    if [ "$budget" -gt 0 ] && [ "$waited" -ge "$budget" ]; then
      # file_pagerank called os.setsid → PGID==PID; SIGKILL the whole group.
      kill -KILL -"$_PR_GUARD_CHILD" 2>/dev/null || kill -KILL "$_PR_GUARD_CHILD" 2>/dev/null
      pkill -KILL -P "$_PR_GUARD_CHILD" 2>/dev/null
      break
    fi
    sleep 1
    waited=$((waited + 1))
  done
  wait "$_PR_GUARD_CHILD" 2>/dev/null
  _PR_GUARD_CHILD=""
}

# Set PAGERANK_HINT for project $1 under budget $2. MUST be a simple command in
# the parent shell (not in `$( … )`) so _pagerank_run's child stays trackable.
_set_pagerank_hint() {
  local _o
  _o=$(mktemp 2>/dev/null) || return 0
  _pagerank_run "$1" "$_o" "$2"
  PAGERANK_HINT=$(head -5 "$_o" | sed 's/^/- /')
  rm -f "$_o" 2>/dev/null
}

# Bounded candidate-file pre-count for $1 (cap $2) under budget $3, written to
# the global _PR_COUNT. MUST be a simple command in the parent shell (not in
# `$( … )`): it runs `find` as a TRACKED background child so the cleanup trap
# can kill it and a huge/slow UNPRUNED tree (vendor assets, a mount) can't hang
# the hook. It STOPS find as soon as MAX+1 matches are observed (so an oversized
# repo doesn't traverse/write the whole tree) OR the budget expires. Sets
# _PR_COUNT to the match count, or -1 on a traversal timeout (caller treats -1
# as "too big → skip").
_pagerank_precount() {
  local cwd="$1" max="$2" budget="$3"
  case "$budget" in (''|*[!0-9]*) budget=8 ;; esac
  [ "$budget" -lt 1 ] && budget=1     # the gate must terminate to decide
  local raw; raw=$(mktemp 2>/dev/null) || { _PR_COUNT=0; return; }
  # Single `find` (no pipeline) backgrounded → $! is find's own PID, killable
  # directly (find spawns no children). Prune the SAME dirs file_pagerank._walk
  # does: dot-dirs + the noisy set. -mindepth 1 spares a dot-named root.
  find "$cwd" -mindepth 1 \
      \( -type d \( -name '.*' -o -name node_modules -o -name venv \
         -o -name __pycache__ -o -name dist -o -name build -o -name target \) \) -prune -o \
      -type f \( -name '*.py' -o -name '*.ts' -o -name '*.tsx' -o -name '*.js' \
      -o -name '*.jsx' \) -print > "$raw" 2>/dev/null &
  _PR_GUARD_CHILD=$!
  local waited=0 timed_out=0 cur
  while kill -0 "$_PR_GUARD_CHILD" 2>/dev/null; do
    # Early stop: cap already exceeded → kill find now, don't traverse the rest.
    cur=$(wc -l < "$raw" 2>/dev/null | tr -d ' ')
    if [ "${cur:-0}" -gt "$max" ]; then
      kill -KILL "$_PR_GUARD_CHILD" 2>/dev/null
      pkill -KILL -P "$_PR_GUARD_CHILD" 2>/dev/null
      break
    fi
    if [ "$waited" -ge "$budget" ]; then
      kill -KILL "$_PR_GUARD_CHILD" 2>/dev/null
      pkill -KILL -P "$_PR_GUARD_CHILD" 2>/dev/null
      timed_out=1
      break
    fi
    sleep 1
    waited=$((waited + 1))
  done
  wait "$_PR_GUARD_CHILD" 2>/dev/null
  _PR_GUARD_CHILD=""
  if [ "$timed_out" -eq 1 ]; then
    _PR_COUNT=-1   # traversal too slow/large → caller skips (treat as oversized)
  else
    _PR_COUNT=$(head -n "$((max + 1))" "$raw" | wc -l | tr -d ' ')
  fi
  rm -f "$raw" 2>/dev/null
}

PROJECT_NAME=$(basename "$CWD" 2>/dev/null || echo "unknown")
SUMMARY_DIR="$B12_BASE/memory-summaries"

# ── Project hierarchy detection ──────────────────────────────────
# Walk up to find project root (directory with .git, package.json, etc.)
# This ensures /B12/benchmarks/locomo still finds proj:B12 memories
PARENT_PROJECT=""
_dir="$CWD"
while [ "$_dir" != "/" ] && [ "$_dir" != "$HOME" ]; do
  _parent=$(dirname "$_dir")
  _pname=$(basename "$_parent" 2>/dev/null)
  if [ -d "$_dir/.git" ] || [ -f "$_dir/package.json" ] || [ -f "$_dir/Cargo.toml" ] || [ -f "$_dir/go.mod" ] || [ -f "$_dir/pyproject.toml" ]; then
    _root_name=$(basename "$_dir" 2>/dev/null)
    if [ "$_root_name" != "$PROJECT_NAME" ]; then
      PARENT_PROJECT="$_root_name"
    fi
    break
  fi
  _dir="$_parent"
done

# Derive project memory directory (same path format as Claude Code uses)
PROJECT_HASH=$(echo "$CWD" | sed 's|/|-|g')
MEMORY_DIR="$B12_BASE/projects/${PROJECT_HASH}/memory"

# ═══════════════════════════════════════════════════════════════
# SETUP DETECTION — personal vs work
# Set B12_WORK_PATTERN to match your work directories/setup name.
# Example: B12_WORK_PATTERN="mycompany" matches .claude-mycompany or /mycompany/ dirs
# ═══════════════════════════════════════════════════════════════
_WORK_PAT="${B12_WORK_PATTERN:-}"
_WORK_PAT_LOWER=$(echo "$_WORK_PAT" | tr '[:upper:]' '[:lower:]')
if [ -n "$_WORK_PAT" ] && { [[ "$B12_BASE" == *"$_WORK_PAT"* ]] || [[ "$CWD" == *"/$_WORK_PAT"* ]] || [[ "$CWD" == *"/${_WORK_PAT_LOWER}"* ]]; }; then
  SETUP_CONTEXT="work"
else
  SETUP_CONTEXT="personal"
fi

# ═══════════════════════════════════════════════════════════════
# USER PROFILE — lazy load (only if updated within 7 days)
# ═══════════════════════════════════════════════════════════════
USER_PROFILE=""
PROFILE_FILE="$MEMORY_DIR/user-profile.md"
if [ -f "$PROFILE_FILE" ]; then
  PROFILE_AGE=$(( $(date +%s) - $(file_mtime "$PROFILE_FILE") ))
  if [ "$PROFILE_AGE" -lt 604800 ]; then
    USER_PROFILE=$(cat "$PROFILE_FILE" 2>/dev/null | head -40)
  fi
fi

# ═══════════════════════════════════════════════════════════════
# SPRINT HANDOFF / LAST SESSION — handoff preferred over full summary (v12)
# Handoff is compact (~300-500 chars), replaces full summary for continuity.
# Falls back to full summary if no recent handoff exists.
# ═══════════════════════════════════════════════════════════════
LAST_SESSION=""
LAST_SESSION_SOURCE=""

# Try handoff first (24h gate)
HANDOFF_FILE="$SUMMARY_DIR/${PROJECT_NAME}-handoff.md"
if [ -f "$HANDOFF_FILE" ]; then
  HANDOFF_AGE=$(( $(date +%s) - $(file_mtime "$HANDOFF_FILE") ))
  if [ "$HANDOFF_AGE" -lt 86400 ]; then
    LAST_SESSION=$(head -20 "$HANDOFF_FILE")
    LAST_SESSION_SOURCE="handoff"
  fi
fi

# Fallback: full session summary
if [ -z "$LAST_SESSION" ]; then
  if [ -f "$SUMMARY_DIR/${PROJECT_NAME}-latest.md" ]; then
    LAST_SESSION=$(cat "$SUMMARY_DIR/${PROJECT_NAME}-latest.md" 2>/dev/null | head -30)
    LAST_SESSION_SOURCE="project"
  elif [ -f "$SUMMARY_DIR/global-latest.md" ]; then
    LAST_SESSION=$(cat "$SUMMARY_DIR/global-latest.md" 2>/dev/null | head -30)
    LAST_SESSION_SOURCE="global"
  fi
fi

# ═══════════════════════════════════════════════════════════════
# CROSS-PROJECT HINTS — jq instead of python3 for speed
# ═══════════════════════════════════════════════════════════════
CROSS_PROJECT_HINT=""
INDEX_FILE="$SUMMARY_DIR/cross-project-index.json"
if [ -f "$INDEX_FILE" ] && command -v jq &>/dev/null; then
  CROSS_PROJECT_HINT=$(jq -r --arg proj "$PROJECT_NAME" '
    [.topics | to_entries[] |
     select(.value | to_entries | map(select(.key != $proj)) | length > 0) |
     "\(.key) (in: \(.value | to_entries | map(select(.key != $proj)) | map(.key) | .[0:3] | join(", ")))"]
    | .[0:8] | join("; ")
  ' "$INDEX_FILE" 2>/dev/null)
fi

# ═══════════════════════════════════════════════════════════════
# FEEDBACK DIGEST — load if recent
# ═══════════════════════════════════════════════════════════════
FEEDBACK_HINT=""
DIGEST_FILE="$B12_BASE/memory-logs/feedback-digest.md"
if [ -f "$DIGEST_FILE" ]; then
  DIGEST_AGE=$(( $(date +%s) - $(file_mtime "$DIGEST_FILE") ))
  if [ "$DIGEST_AGE" -lt 1209600 ]; then
    # Extract only the Alerts section (most actionable)
    FEEDBACK_HINT=$(sed -n '/^## Alerts/,/^## /p' "$DIGEST_FILE" 2>/dev/null | head -5)
  fi
fi

# ═══════════════════════════════════════════════════════════════
# DATABASE PATH — needed by guardrails, pre-fetch, and compat checks
# ═══════════════════════════════════════════════════════════════
# shellcheck source=./_b12_common.sh disable=SC1091
. "${B12_HOOK_DIR:-$HOME/.B12/hooks}/_b12_common.sh" 2>/dev/null || true
if command -v b12_resolve_db_path >/dev/null 2>&1; then
  DB_PATH="$(b12_resolve_db_path)"
else
  # Fallback when _b12_common.sh is missing (pre-v11.54 install).
  if [ "$(uname)" = "Darwin" ]; then
    DB_PATH="$HOME/Library/Application Support/mcp-memory/sqlite_vec.db"
  elif [ -d "$HOME/AppData" ]; then
    DB_PATH="$HOME/AppData/Local/mcp-memory/sqlite_vec.db"
  else
    DB_PATH="$HOME/.local/share/mcp-memory/sqlite_vec.db"
  fi
fi

# ═══════════════════════════════════════════════════════════════
# HOST VERSION COMPAT CHECK (v12 — F8)
# ═══════════════════════════════════════════════════════════════
COMPAT_WARN=""
VERSION_FILE="$B12_BASE/memory-state/host-version.txt"
COMPAT_FILE="${B12_HOOK_DIR:-$HOME/.B12/hooks}/scripts/compat.json"
if [ -f "$VERSION_FILE" ] && [ -f "$COMPAT_FILE" ] && command -v jq &>/dev/null; then
  HOST_VER=$(cat "$VERSION_FILE" 2>/dev/null)
  COMPAT_WARN=$(jq -r --arg v "$HOST_VER" '.known_issues[$v] // ""' "$COMPAT_FILE" 2>/dev/null)
fi

# ═══════════════════════════════════════════════════════════════
# CONTENT GUARDRAILS — always surface for content sessions (v12 — F9)
# ═══════════════════════════════════════════════════════════════
CONTENT_GUARDRAILS=""
IS_CONTENT=false
echo "$CWD" | grep -qiE '(blog|content|marketing|seo|article|typefully)' && IS_CONTENT=true
[ -f "$SUMMARY_DIR/${PROJECT_NAME}-handoff.md" ] && grep -qiE '(blog|content|article|editorial)' "$SUMMARY_DIR/${PROJECT_NAME}-handoff.md" 2>/dev/null && IS_CONTENT=true

if $IS_CONTENT && [ -f "$DB_PATH" ]; then
  CONTENT_GUARDRAILS=$(sqlite3 "$DB_PATH" "
    SELECT substr(content, 1, 200) FROM memories
    WHERE tags LIKE '%content-guardrail%' AND deleted_at IS NULL
    ORDER BY max(min(CASE WHEN json_valid(metadata) AND json_type(metadata, '\$.importance_score') IN ('integer','real') THEN (CASE WHEN json_extract(metadata, '\$.importance_score') >= 1.0 THEN json_extract(metadata, '\$.importance_score') / 2.0 ELSE json_extract(metadata, '\$.importance_score') END) ELSE 0.50 END, 1.0), 0.0) DESC LIMIT 3
  " 2>/dev/null)
fi

# ═══════════════════════════════════════════════════════════════
# SETUP-AWARE SESSION ROUTING (v12 — F10)
# ═══════════════════════════════════════════════════════════════
ROUTING_WARN=""
if [ -n "$_WORK_PAT" ]; then
  CWD_HAS_WORK=$(echo "$CWD" | grep -ci "$_WORK_PAT" || true)
  if [ "$CWD_HAS_WORK" -gt 0 ] && [ "$SETUP_CONTEXT" = "personal" ]; then
    ROUTING_WARN="[ROUTING] CWD matches work pattern but using personal setup. Consider .claude-${_WORK_PAT}."
  fi
fi

# ═══════════════════════════════════════════════════════════════
# MEMORY PRE-FETCH — FTS5 + tag-based relevant memory loading
# ═══════════════════════════════════════════════════════════════
MEMORY_PREFETCH=""

if [ -f "$DB_PATH" ]; then
  # Sanitize project name for SQL (alphanumeric + dash/underscore only)
  SAFE_PROJECT=$(echo "$PROJECT_NAME" | sed 's/[^a-zA-Z0-9_-]//g')

  # Parent project tag (for subdirectory awareness)
  SAFE_PARENT=""
  if [ -n "$PARENT_PROJECT" ]; then
    SAFE_PARENT=$(echo "$PARENT_PROJECT" | sed 's/[^a-zA-Z0-9_-]//g')
  fi

  # Project-relevant memories (tag match OR FTS5 keyword match, exclude session summaries + superseded)
  # Searches both current dir name AND parent project name
  if [ -n "$SAFE_PROJECT" ] && [ ${#SAFE_PROJECT} -gt 1 ]; then
    # Build tag/FTS conditions — include parent project if different
    TAG_COND="m.tags LIKE '%proj:${SAFE_PROJECT}%'"
    FTS_COND="memory_fts MATCH '\"${SAFE_PROJECT}\"'"
    if [ -n "$SAFE_PARENT" ] && [ ${#SAFE_PARENT} -gt 1 ]; then
      TAG_COND="${TAG_COND} OR m.tags LIKE '%proj:${SAFE_PARENT}%'"
      FTS_COND="memory_fts MATCH '\"${SAFE_PROJECT}\" OR \"${SAFE_PARENT}\"'"
    fi

    PROJ_MEMS=$(sqlite3 "$DB_PATH" "
      SELECT '[' || m.memory_type || '] ' || substr(m.content, 1, 200)
      FROM memories m
      WHERE m.deleted_at IS NULL
        AND (m.valid_until IS NULL OR m.valid_until > datetime('now'))
        AND m.memory_type != 'session_summary'
        AND m.tags NOT LIKE '%session-summary%'
        AND (
          ${TAG_COND}
          OR m.id IN (
            SELECT rowid FROM memory_fts
            WHERE ${FTS_COND}
          )
        )
      ORDER BY max(min(CASE WHEN json_valid(m.metadata) AND json_type(m.metadata, '$.importance_score') IN ('integer','real') THEN (CASE WHEN json_extract(m.metadata, '$.importance_score') >= 1.0 THEN json_extract(m.metadata, '$.importance_score') / 2.0 ELSE json_extract(m.metadata, '$.importance_score') END) ELSE 0.50 END, 1.0), 0.0) * COALESCE(m.strength, 1.0) DESC
      LIMIT 3
    " 2>/dev/null)

    if [ -n "$PROJ_MEMS" ]; then
      PREFETCH_LABEL="${SAFE_PROJECT}"
      [ -n "$SAFE_PARENT" ] && PREFETCH_LABEL="${SAFE_PARENT}/${SAFE_PROJECT}"
      MEMORY_PREFETCH="Project memories (${PREFETCH_LABEL}):\n${PROJ_MEMS}"
    fi
  fi

  # Universal knowledge (cross-project patterns, CLI tricks, lessons)
  UNIVERSAL_MEMS=$(sqlite3 "$DB_PATH" "
    SELECT '[' || memory_type || '] ' || substr(content, 1, 200)
    FROM memories
    WHERE tags LIKE '%user:universal%'
      AND deleted_at IS NULL
      AND (valid_until IS NULL OR valid_until > datetime('now'))
    ORDER BY max(min(CASE WHEN json_valid(metadata) AND json_type(metadata, '$.importance_score') IN ('integer','real') THEN (CASE WHEN json_extract(metadata, '$.importance_score') >= 1.0 THEN json_extract(metadata, '$.importance_score') / 2.0 ELSE json_extract(metadata, '$.importance_score') END) ELSE 0.50 END, 1.0), 0.0) * COALESCE(strength, 1.0) DESC
    LIMIT 2
  " 2>/dev/null)

  if [ -n "$UNIVERSAL_MEMS" ]; then
    if [ -n "$MEMORY_PREFETCH" ]; then
      MEMORY_PREFETCH="${MEMORY_PREFETCH}\nUniversal knowledge:\n${UNIVERSAL_MEMS}"
    else
      MEMORY_PREFETCH="Universal knowledge:\n${UNIVERSAL_MEMS}"
    fi
  fi
fi

# ═══════════════════════════════════════════════════════════════
# JSON ESCAPE — single python3 call for all strings
# ═══════════════════════════════════════════════════════════════
escape_json() {
  jq -Rs '.' 2>/dev/null | sed 's/^"//;s/"$//'
}


# ═══════════════════════════════════════════════════════════════
# SHARED VARIABLES — used by both startup and compact paths
# ═══════════════════════════════════════════════════════════════
PARENT_INFO=""
[ -n "$PARENT_PROJECT" ] && PARENT_INFO=" (parent: ${PARENT_PROJECT})"

STORE_TAG="proj:${PROJECT_NAME}"
SEARCH_HINT="Default: tags=[\"proj:${PROJECT_NAME}\"] to get project context."
if [ -n "$PARENT_PROJECT" ]; then
  STORE_TAG="proj:${PARENT_PROJECT}"
  SEARCH_HINT="Default: tags=[\"proj:${PARENT_PROJECT}\"] (parent project). Also try proj:${PROJECT_NAME} for subdir-specific."
fi

# Behavioral instructions — v8 (2026-05-17): directive primer
#
# Forensic audit 2026-03-16..2026-05-17 flagged 30 of 36 in-window
# Claude Code sessions (83%) reading this block and calling zero memory
# tools. v7 was descriptive ("memory_search can ...") which left the
# trigger up to the model. v8 leads with the trigger condition, keeps
# the store-tag policy because it stays session-dynamic, and condenses
# the emoji-pill detail to one line.
# v7 lineage: ~600 chars (down from v6 ~2000). Static instructions live
# in skills/b12-memory/SKILL.md.
BEHAVIORAL_INSTR="CALL memory_search BEFORE answering when ANY of these holds:"
BEHAVIORAL_INSTR="${BEHAVIORAL_INSTR}\n- user uses a recall verb (EN: remember, recall, last time, before, previously, prior, earlier, said, told, mentioned, stored / TR: hat\u0131rla, hat\u0131rl\u0131yor, ge\u00e7en sefer, daha \u00f6nce, \u00f6nceki, demi\u015ftik, s\u00f6ylemi\u015ftim, kaydetmi\u015ftik)"
BEHAVIORAL_INSTR="${BEHAVIORAL_INSTR}\n- user references work that is not visible in the current conversation"
BEHAVIORAL_INSTR="${BEHAVIORAL_INSTR}\n- starting a non-trivial task in this project"

BEHAVIORAL_INSTR="${BEHAVIORAL_INSTR}\n\nCALL memory_store WHEN: a decision, fact, preference, or workflow pattern that should outlive this conversation. Store silently. Always include tags [${STORE_TAG}, user:${SETUP_CONTEXT}] and metadata {project:\"${PARENT_PROJECT:-${PROJECT_NAME}}\", setup:\"${SETUP_CONTEXT}\", scope:\"<type>\"}."

BEHAVIORAL_INSTR="${BEHAVIORAL_INSTR}\n\nSEARCH HINT: ${SEARCH_HINT} Few results (<3): widen scope, remove tag filter."

BEHAVIORAL_INSTR="${BEHAVIORAL_INSTR}\n\nTOOLS: memory_search (mode=hybrid, ISO-date after/before), memory_store, memory_update, memory_quality. Pill: ( \ud83d\udc8a B12 \ud83e\udde0 : found N memories about [topic] \u2705 ) on retrieval, ( \ud83d\udc8a B12 \ud83e\udde0 : saved to memory \u2705 ) on store, \u274c only when user explicitly asked and nothing was found."

BEHAVIORAL_INSTR="${BEHAVIORAL_INSTR}\n\nFor importance scoring, time search, scope types, and dual memory layers: invoke /b12-memory skill."

if [ "$SOURCE" = "startup" ] || [ "$SOURCE" = "resume" ]; then
  # ═══════════════════════════════════════════════════════════
  # BUILD CONTEXT — scope-aware, token-efficient
  # ═══════════════════════════════════════════════════════════
  CONTEXT="MEMORY SYSTEM ACTIVE (b12-memory v1.0). Setup: ${SETUP_CONTEXT}. Project: ${PROJECT_NAME}${PARENT_INFO} (${CWD})."

  # --- Behavioral instructions (shared variable, injected on startup + compact) ---
  CONTEXT="${CONTEXT}\n\n${BEHAVIORAL_INSTR}"

  # Add user profile (if recent)
  if [ -n "$USER_PROFILE" ]; then
    CONTEXT="${CONTEXT}\n\n--- USER PROFILE ---\n${USER_PROFILE}\n--- END PROFILE ---"
  fi

  # Add last session / sprint handoff (Tier 0 — never trimmed)
  if [ -n "$LAST_SESSION" ]; then
    if [ "$LAST_SESSION_SOURCE" = "handoff" ]; then
      CONTEXT="${CONTEXT}\n\n--- LAST SESSION ---\n${LAST_SESSION}\n--- END LAST SESSION ---"
    elif [ "$LAST_SESSION_SOURCE" = "global" ]; then
      CONTEXT="${CONTEXT}\n\n--- LAST SESSION (from different project) ---\n${LAST_SESSION}\n--- END LAST SESSION ---"
    else
      CONTEXT="${CONTEXT}\n\n--- LAST SESSION ---\n${LAST_SESSION}\n--- END LAST SESSION ---"
    fi
  fi

  # Add content guardrails (Tier 0 — never trimmed, v12 F9)
  if [ -n "$CONTENT_GUARDRAILS" ]; then
    CONTEXT="${CONTEXT}\n\n--- CONTENT GUARDRAILS ---\n${CONTENT_GUARDRAILS}\n--- END GUARDRAILS ---"
  fi

  # Add compat warning (v12 F8)
  if [ -n "$COMPAT_WARN" ]; then
    CONTEXT="${CONTEXT}\n[B12 COMPAT] Claude Code ${HOST_VER}: ${COMPAT_WARN}"
  fi

  # Add routing warning (v12 F10)
  if [ -n "$ROUTING_WARN" ]; then
    CONTEXT="${CONTEXT}\n${ROUTING_WARN}"
  fi

  # Add cross-project hints
  if [ -n "$CROSS_PROJECT_HINT" ]; then
    CONTEXT="${CONTEXT}\n\n--- CROSS-PROJECT ---\n${CROSS_PROJECT_HINT}\nSearch without tag filter to explore these.\n--- END CROSS-PROJECT ---"
  fi

  # Add feedback alerts (if any)
  if [ -n "$FEEDBACK_HINT" ]; then
    CONTEXT="${CONTEXT}\n\n--- MEMORY USAGE FEEDBACK ---\n${FEEDBACK_HINT}\n--- END FEEDBACK ---"
  fi

  # Cursor MDC globs Auto-Attached + PageRank file-rank (Plan §B3) —
  # surface rules whose globs match active files (extracted from
  # LAST_SESSION) and the top-5 import-graph-central files. Cursor's own
  # runtime is unaffected; this primes B12 sessions with the same signal.
  CURSOR_HINT=""
  if [ -d "$CWD/.cursor/rules" ] && [ -x "$VENV_PYTHON" ]; then
    _ACTIVE_FILES=""
    if [ -n "$LAST_SESSION" ]; then
      _ACTIVE_FILES=$(printf '%s' "$LAST_SESSION" \
        | grep -Eo '[A-Za-z0-9_./-]+\.(py|ts|tsx|js|jsx|sh|md|toml|json)' \
        | sort -u | head -10 | tr '\n' ' ')
    fi
    CURSOR_HINT=$("$VENV_PYTHON" "$B12_SCRIPTS/cursor_mdc.py" --lines "$CWD" $_ACTIVE_FILES 2>/dev/null)
  fi
  if [ -n "$CURSOR_HINT" ]; then
    CONTEXT="${CONTEXT}\n\n--- CURSOR RULES (auto-attached) ---\n${CURSOR_HINT}\n--- END CURSOR RULES ---"
  fi

  PAGERANK_HINT=""
  if [ -d "$CWD" ] && [ -x "$VENV_PYTHON" ] && [ -f "$B12_SCRIPTS/file_pagerank.py" ]; then
    # ── Memory-safety guard (the 2026-06 OOM fix) ──────────────
    # Never let pagerank walk a giant tree. Resolve symlinks so a symlinked
    # $HOME or nested mount can't slip past the equality checks, then require
    # being inside a git work tree and a BOUNDED candidate-file pre-count before
    # invoking python.
    _PR_CWD=$(cd "$CWD" 2>/dev/null && pwd -P)
    _PR_HOME=$(cd "$HOME" 2>/dev/null && pwd -P)
    _PR_MAX="${B12_PAGERANK_MAX_NODES:-20000}"
    # Non-integer env value → fall back to the default (mirror file_pagerank's
    # robust _env_int). After this _PR_MAX is always a non-negative integer.
    case "$_PR_MAX" in (*[!0-9]*|'') _PR_MAX=20000 ;; esac
    # Use git's own work-tree detection rather than testing for `.git` at $CWD:
    # that only matched a repo ROOT, so launching in a subdirectory (e.g.
    # repo/packages/api) or a linked worktree wrongly skipped pagerank. $HOME is
    # still excluded by the equality check above even if it is itself a repo.
    if [ -n "$_PR_CWD" ] && [ "$_PR_CWD" != "$_PR_HOME" ] && [ "$_PR_CWD" != "/" ] \
       && git -C "$_PR_CWD" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
      # The pre-count and rank phases SHARE the remaining watchdog budget so
      # their combined wall time can't trip the 15s TERM trap (exit 143) on a
      # valid repo. _PR_CFG is the per-phase ceiling (0 = wall-clock-kill opt-out).
      _PR_CFG=$(_pr_cfg_budget)
      _PR_REM=$(_pr_remaining)
      if [ "$_PR_MAX" -le 0 ]; then
        # Cap disabled (B12_PAGERANK_MAX_NODES=0) — honor the documented opt-out:
        # skip the pre-count gate, rank directly. Still bounded by the $HOME/git
        # guards + process-group kill + watchdog trap + SIGALRM/sparse guards.
        if [ "$_PR_REM" -ge 2 ]; then
          if [ "$_PR_CFG" -gt 0 ]; then
            _RK=$_PR_CFG; [ "$_RK" -gt "$_PR_REM" ] && _RK=$_PR_REM
          else
            _RK=0   # opt-out: no pagerank-level kill, watchdog-bounded
          fi
          _set_pagerank_hint "$_PR_CWD" "$_RK"
        fi
      elif [ "$_PR_REM" -ge 2 ]; then
        # Bounded pre-count via _pagerank_precount (a TRACKED background `find`).
        # Give it min(cfg, remaining) — but a forced floor so it can decide even
        # at TIMEOUT_S=0. It sets _PR_COUNT (or -1 on traversal timeout).
        if [ "$_PR_CFG" -gt 0 ]; then _PC=$_PR_CFG; else _PC=8; fi
        [ "$_PC" -gt "$_PR_REM" ] && _PC=$_PR_REM
        _pagerank_precount "$_PR_CWD" "$_PR_MAX" "$_PC"
        if [ "${_PR_COUNT:-0}" -gt 0 ] && [ "${_PR_COUNT:-0}" -le "$_PR_MAX" ]; then
          # Re-check remaining AFTER the pre-count: a slow pre-count must not
          # leave the rank pass to overrun the watchdog — skip rank if <2s left.
          _PR_REM=$(_pr_remaining)
          if [ "$_PR_REM" -ge 2 ]; then
            if [ "$_PR_CFG" -gt 0 ]; then
              _RK=$_PR_CFG; [ "$_RK" -gt "$_PR_REM" ] && _RK=$_PR_REM
            else
              _RK=0
            fi
            _set_pagerank_hint "$_PR_CWD" "$_RK"
          fi
        fi
      fi
    fi
  fi
  if [ -n "$PAGERANK_HINT" ]; then
    CONTEXT="${CONTEXT}\n\n--- LIKELY-NEXT FILES (pagerank) ---\n${PAGERANK_HINT}\n--- END LIKELY-NEXT ---"
  fi

  # Add pre-fetched memories (project-relevant + universal)
  if [ -n "$MEMORY_PREFETCH" ]; then
    CONTEXT="${CONTEXT}\n\n--- MEMORY PRE-FETCH ---\n${MEMORY_PREFETCH}\n--- END PRE-FETCH ---"
  fi

  # Teammate context (Plan §B2) — only fires when THIS session is a
  # participant in an agent-team. Resolution order (Codex review PR #44
  # round 3 P2: teammate sessions have different session_ids than the
  # TeamCreate caller, so the session_id path is best-effort only):
  #   1. CLAUDE_CODE_AGENT_ID / CLAUDE_CODE_PARENT_AGENT_ID env vars
  #      → match against .members[].agent_id (runtime), then fall back
  #      to .members[].name / .agent_type (for older team API revs).
  #   2. session_id from stdin → match against .caller_session_id
  #      (lead session re-entering after creating a team — edge case).
  # Without a match, we DO NOT inject team context, even if a recent
  # team-*.json exists, preventing cross-session roster leakage between
  # unrelated projects sharing the same B12_DATA_DIR.
  TEAM_STATE_DIR="$B12_BASE/state"
  SESSION_ID=$(printf '%s' "$INPUT" | jq -r '.session_id // ""' 2>/dev/null)
  AGENT_ID="${CLAUDE_CODE_AGENT_ID:-${CLAUDE_CODE_PARENT_AGENT_ID:-}}"
  if [ -d "$TEAM_STATE_DIR" ] && [ -n "$SESSION_ID$AGENT_ID" ]; then
    MATCHED_TEAM=""
    for team_file in $(ls -t "$TEAM_STATE_DIR"/team-*.json 2>/dev/null); do
      [ -r "$team_file" ] || continue
      age=$(( $(date +%s) - $(file_mtime "$team_file") ))
      [ "$age" -gt 21600 ] && break
      # Primary: match by AGENT_ID against .members[].agent_id (the
      # runtime-assigned ID Claude Code teams API yields in tool_response).
      if [ -n "$AGENT_ID" ] && \
         jq -e --arg aid "$AGENT_ID" \
            '.members // [] | any(.agent_id == $aid or .name == $aid or .agent_type == $aid)' \
            "$team_file" >/dev/null 2>&1; then
        MATCHED_TEAM="$team_file"
        break
      fi
      # Edge case: lead session re-enters after creating a team
      # (caller_session_id of the TeamCreate call).
      caller_sid=$(jq -r '.caller_session_id // .session_id // ""' "$team_file" 2>/dev/null)
      if [ -n "$SESSION_ID" ] && [ "$caller_sid" = "$SESSION_ID" ]; then
        MATCHED_TEAM="$team_file"
        break
      fi
    done
    if [ -n "$MATCHED_TEAM" ]; then
      TEAMMATE_LINES=$(jq -r '
        if (.members | length) > 0 then
          "Team " + (.team_name // .team_id // "") + " members:\n"
          + ([.members[] |
              "- " + (.name // "?")
              + " (" + (.agent_type // "?") + "): "
              + (.task // "")
            ] | join("\n"))
        else "" end
      ' "$MATCHED_TEAM" 2>/dev/null)
      if [ -n "$TEAMMATE_LINES" ]; then
        CONTEXT="${CONTEXT}\n\n--- TEAMMATES ---\n${TEAMMATE_LINES}\n--- END TEAMMATES ---"
      fi
    fi
  fi

  # ── Context hard cap — progressive trimming ──────────────────
  # Fixed instructions (~2120 chars) are always kept.
  # Variable sections trimmed in priority order (least valuable first).
  # Expand literal \n once, then measure/trim in consistent units.
  CONTEXT=$(printf '%b' "$CONTEXT")
  MAX_CONTEXT_CHARS=6000
  _ctx_len=${#CONTEXT}
  if [ "$_ctx_len" -gt "$MAX_CONTEXT_CHARS" ]; then
    # Tier 1: Remove memory pre-fetch
    CONTEXT=$(echo "$CONTEXT" | sed '/--- MEMORY PRE-FETCH ---/,/--- END PRE-FETCH ---/d')
    _ctx_len=${#CONTEXT}
  fi
  if [ "$_ctx_len" -gt "$MAX_CONTEXT_CHARS" ]; then
    # Tier 1b: Remove pagerank likely-next files (B3 — additive, easy
    # to drop because Cursor handles its own context separately)
    CONTEXT=$(echo "$CONTEXT" | sed '/--- LIKELY-NEXT FILES (pagerank) ---/,/--- END LIKELY-NEXT ---/d')
    _ctx_len=${#CONTEXT}
  fi
  if [ "$_ctx_len" -gt "$MAX_CONTEXT_CHARS" ]; then
    # Tier 1c: Remove cursor rules (B3 — duplicated by Cursor's own
    # rules pipeline when running inside Cursor)
    CONTEXT=$(echo "$CONTEXT" | sed '/--- CURSOR RULES (auto-attached) ---/,/--- END CURSOR RULES ---/d')
    _ctx_len=${#CONTEXT}
  fi
  if [ "$_ctx_len" -gt "$MAX_CONTEXT_CHARS" ]; then
    # Tier 2: Remove cross-project hints
    CONTEXT=$(echo "$CONTEXT" | sed '/--- CROSS-PROJECT ---/,/--- END CROSS-PROJECT ---/d')
    _ctx_len=${#CONTEXT}
  fi
  if [ "$_ctx_len" -gt "$MAX_CONTEXT_CHARS" ]; then
    # Tier 3: Remove feedback digest
    CONTEXT=$(echo "$CONTEXT" | sed '/--- MEMORY USAGE FEEDBACK ---/,/--- END FEEDBACK ---/d')
    _ctx_len=${#CONTEXT}
  fi
  if [ "$_ctx_len" -gt "$MAX_CONTEXT_CHARS" ]; then
    # Tier 4: Truncate to hard cap (last resort — keeps fixed instructions intact)
    CONTEXT=$(echo "$CONTEXT" | head -c "$MAX_CONTEXT_CHARS")
  fi

  ESCAPED_CONTEXT=$(echo "$CONTEXT" | escape_json)

  cat <<CONTEXT_EOF
{
  "hookSpecificOutput": {
    "hookEventName": "SessionStart",
    "additionalContext": "${ESCAPED_CONTEXT}"
  }
}
CONTEXT_EOF

elif [ "$SOURCE" = "compact" ]; then
  # ═══════════════════════════════════════════════════════════
  # POST-COMPACTION — full context rebuild (v6)
  # v6: includes behavioral instructions + memory pre-fetch
  # so the LLM retains B12 knowledge after context compression
  # ═══════════════════════════════════════════════════════════
  STAGING_DIR="$B12_BASE/memory-staging"
  STAGED_CONTEXT=""

  if [ -d "$STAGING_DIR" ]; then
    for f in "$STAGING_DIR"/*.txt; do
      if [ -f "$f" ]; then
        STAGED_CONTEXT=$(cat "$f" 2>/dev/null)
        rm -f "$f" 2>/dev/null
        break
      fi
    done
  fi

  # Load working memory (active files, modified files, search patterns)
  WORKING_MEM=""
  WM_FILE="$STAGING_DIR/working-memory.json"
  if [ -f "$WM_FILE" ] && command -v jq &>/dev/null; then
    WM_AGE=$(( $(date +%s) - $(file_mtime "$WM_FILE") ))
    # Only use if updated within last 2 hours (same session)
    if [ "$WM_AGE" -lt 7200 ]; then
      WM_MODIFIED=$(jq -r '.modified_files // [] | .[0:8] | join(", ")' "$WM_FILE" 2>/dev/null)
      WM_ACTIVE=$(jq -r '.active_files // [] | .[0:8] | join(", ")' "$WM_FILE" 2>/dev/null)
      WM_SEARCH=$(jq -r '.search_patterns // [] | .[0:5] | join(", ")' "$WM_FILE" 2>/dev/null)
      if [ -n "$WM_MODIFIED" ] || [ -n "$WM_ACTIVE" ]; then
        WORKING_MEM="WORKING MEMORY (conversation momentum):"
        [ -n "$WM_MODIFIED" ] && WORKING_MEM="${WORKING_MEM}\n  Modified: ${WM_MODIFIED}"
        [ -n "$WM_ACTIVE" ] && WORKING_MEM="${WORKING_MEM}\n  Active: ${WM_ACTIVE}"
        [ -n "$WM_SEARCH" ] && WORKING_MEM="${WORKING_MEM}\n  Searched: ${WM_SEARCH}"
      fi
    fi
  fi

  # Build comprehensive context (same structure as startup, with staged summary replacing last session)
  CONTEXT="MEMORY SYSTEM: Context compacted. Setup: ${SETUP_CONTEXT}. Project: ${PROJECT_NAME}${PARENT_INFO} (${CWD})."

  # Full behavioral instructions — critical for proper B12 tool usage after compaction
  CONTEXT="${CONTEXT}\n\n${BEHAVIORAL_INSTR}"

  # Pre-compaction summary (what the LLM was working on)
  if [ -n "$STAGED_CONTEXT" ]; then
    CONTEXT="${CONTEXT}\n\n--- PRE-COMPACTION SUMMARY ---\n${STAGED_CONTEXT}\n--- END PRE-COMPACTION ---"
  fi

  # Working memory (file context from the session)
  if [ -n "$WORKING_MEM" ]; then
    CONTEXT="${CONTEXT}\n\n${WORKING_MEM}"
  fi

  # Memory pre-fetch (project-relevant + universal knowledge)
  if [ -n "$MEMORY_PREFETCH" ]; then
    CONTEXT="${CONTEXT}\n\n--- MEMORY PRE-FETCH ---\n${MEMORY_PREFETCH}\n--- END PRE-FETCH ---"
  fi

  # Cross-project hints (if available)
  if [ -n "$CROSS_PROJECT_HINT" ]; then
    CONTEXT="${CONTEXT}\n\n--- CROSS-PROJECT ---\n${CROSS_PROJECT_HINT}\nSearch without tag filter to explore these.\n--- END CROSS-PROJECT ---"
  fi

  # ── Context hard cap — progressive trimming (compact path) ──
  # Priority: behavioral instructions > staged summary > working memory > pre-fetch > cross-project
  CONTEXT=$(printf '%b' "$CONTEXT")
  MAX_CONTEXT_CHARS=6000
  _ctx_len=${#CONTEXT}
  if [ "$_ctx_len" -gt "$MAX_CONTEXT_CHARS" ]; then
    # Tier 1: Remove cross-project hints
    CONTEXT=$(echo "$CONTEXT" | sed '/--- CROSS-PROJECT ---/,/--- END CROSS-PROJECT ---/d')
    _ctx_len=${#CONTEXT}
  fi
  if [ "$_ctx_len" -gt "$MAX_CONTEXT_CHARS" ]; then
    # Tier 2: Remove memory pre-fetch
    CONTEXT=$(echo "$CONTEXT" | sed '/--- MEMORY PRE-FETCH ---/,/--- END PRE-FETCH ---/d')
    _ctx_len=${#CONTEXT}
  fi
  if [ "$_ctx_len" -gt "$MAX_CONTEXT_CHARS" ]; then
    # Tier 3: Remove staged summary entirely (behavioral instructions take priority)
    CONTEXT=$(echo "$CONTEXT" | sed '/--- PRE-COMPACTION SUMMARY ---/,/--- END PRE-COMPACTION ---/{
      /--- PRE-COMPACTION SUMMARY ---/b
      /--- END PRE-COMPACTION ---/b
      d
    }')
    _ctx_len=${#CONTEXT}
  fi
  if [ "$_ctx_len" -gt "$MAX_CONTEXT_CHARS" ]; then
    # Tier 4: Hard truncate
    CONTEXT=$(echo "$CONTEXT" | head -c "$MAX_CONTEXT_CHARS")
  fi

  ESCAPED_CONTEXT=$(echo "$CONTEXT" | escape_json)

  cat <<CONTEXT_EOF
{
  "hookSpecificOutput": {
    "hookEventName": "SessionStart",
    "additionalContext": "${ESCAPED_CONTEXT}\n\nStore key learnings to permanent memory. Continue where you left off."
  }
}
CONTEXT_EOF
fi

exit 0
