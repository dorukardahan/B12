#!/bin/bash
# B12 Memory System - SessionStart Hook (v5 — Memory Pre-fetch)
# Loads: setup context + compressed instructions + user profile (lazy)
#        + session summary + cross-project hints + memory pre-fetch
# Fires on: startup, resume, compact
#
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
B12_SCRIPTS="${B12_DATA_DIR:-$HOME/.claude}/hooks/scripts"
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
( sleep 15 && kill -TERM $$ 2>/dev/null ) &
_WATCHDOG=$!
trap "kill $_WATCHDOG 2>/dev/null; wait $_WATCHDOG 2>/dev/null" EXIT

INPUT=$(cat)
SOURCE=$(echo "$INPUT" | jq -r '.source // "startup"')
CWD=$(echo "$INPUT" | jq -r '.cwd // ""')

# Central data directory — override with B12_DATA_DIR env var for custom setups
B12_BASE="${B12_DATA_DIR:-$HOME/.claude}"

# Portable stat: macOS uses -f %m, Linux uses -c %Y
file_mtime() { stat -f %m "$1" 2>/dev/null || stat -c %Y "$1" 2>/dev/null || echo "0"; }

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
if [ -n "$_WORK_PAT" ] && { [[ "$B12_BASE" == *"$_WORK_PAT"* ]] || [[ "$CWD" == *"/$_WORK_PAT"* ]] || [[ "$CWD" == *"/${_WORK_PAT,,}"* ]]; }; then
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
# LAST SESSION SUMMARY — project-specific first, global as fallback
# ═══════════════════════════════════════════════════════════════
LAST_SESSION=""
LAST_SESSION_SOURCE=""
if [ -f "$SUMMARY_DIR/${PROJECT_NAME}-latest.md" ]; then
  LAST_SESSION=$(cat "$SUMMARY_DIR/${PROJECT_NAME}-latest.md" 2>/dev/null | head -30)
  LAST_SESSION_SOURCE="project"
elif [ -f "$SUMMARY_DIR/global-latest.md" ]; then
  LAST_SESSION=$(cat "$SUMMARY_DIR/global-latest.md" 2>/dev/null | head -30)
  LAST_SESSION_SOURCE="global"
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
# MEMORY PRE-FETCH — FTS5 + tag-based relevant memory loading
# ═══════════════════════════════════════════════════════════════
MEMORY_PREFETCH=""
if [ "$(uname)" = "Darwin" ]; then
  DB_PATH="$HOME/Library/Application Support/mcp-memory/sqlite_vec.db"
elif [ -d "$HOME/AppData" ]; then
  DB_PATH="$HOME/AppData/Local/mcp-memory/sqlite_vec.db"
else
  DB_PATH="$HOME/.local/share/mcp-memory/sqlite_vec.db"
fi

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
        AND m.valid_until IS NULL
        AND m.memory_type != 'session_summary'
        AND m.tags NOT LIKE '%session-summary%'
        AND (
          ${TAG_COND}
          OR m.id IN (
            SELECT rowid FROM memory_fts
            WHERE ${FTS_COND}
          )
        )
      ORDER BY COALESCE(json_extract(m.metadata, '$.importance_score'), 1.0) * COALESCE(m.strength, 1.0) DESC
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
      AND valid_until IS NULL
    ORDER BY COALESCE(json_extract(metadata, '$.importance_score'), 1.0) * COALESCE(strength, 1.0) DESC
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

if [ "$SOURCE" = "startup" ] || [ "$SOURCE" = "resume" ]; then
  # ═══════════════════════════════════════════════════════════
  # BUILD CONTEXT — scope-aware, token-efficient
  # ═══════════════════════════════════════════════════════════
  PARENT_INFO=""
  [ -n "$PARENT_PROJECT" ] && PARENT_INFO=" (parent: ${PARENT_PROJECT})"
  CONTEXT="MEMORY SYSTEM ACTIVE (b12-memory v1.0). Setup: ${SETUP_CONTEXT}. Project: ${PROJECT_NAME}${PARENT_INFO} (${CWD})."

  # --- Compact behavioral instructions (v4 — ~120 tokens vs ~512 in v3) ---
  CONTEXT="${CONTEXT}\n\nMEMORY TOOLS: memory_search (mode=hybrid, after/before=ISO date, max_response_chars=40000), memory_store (always include metadata), memory_update, memory_quality.\nTIME SEARCH: When user says approximate time (\"2 days ago\", \"last week\", \"this morning\"), use wide buffer: ±1 day for days, ±2 days for weeks. Example: \"2 days ago\" → after=3_days_ago, before=1_day_ago. If few results, widen range."

  # --- Scope classification rules ---
  STORE_TAG="proj:${PROJECT_NAME}"
  SEARCH_HINT="Default: tags=[\"proj:${PROJECT_NAME}\"] to get project context."
  if [ -n "$PARENT_PROJECT" ]; then
    STORE_TAG="proj:${PARENT_PROJECT}"
    SEARCH_HINT="Default: tags=[\"proj:${PARENT_PROJECT}\"] (parent project). Also try proj:${PROJECT_NAME} for subdir-specific."
  fi
  CONTEXT="${CONTEXT}\n\nSCOPE SYSTEM:\nSetup: ${SETUP_CONTEXT} | Project: ${PROJECT_NAME}${PARENT_INFO}\nWhen STORING: Always include tags [${STORE_TAG}, user:${SETUP_CONTEXT}] and metadata {project:\"${PARENT_PROJECT:-${PROJECT_NAME}}\", setup:\"${SETUP_CONTEXT}\", scope:\"<type>\"}.\nScope types:\n- project: codebase-specific (architecture, decisions, bugs) -> tag: ${STORE_TAG}\n- universal: applies everywhere (patterns, CLI tricks, lessons) -> tag: user:universal\n- preference: user preferences (always global) -> tag: user:pref\n- setup: team/workflow specific to ${SETUP_CONTEXT} -> tag: user:${SETUP_CONTEXT}\nWhen SEARCHING:\n- ${SEARCH_HINT} Add user:universal for general knowledge.\n- Cross-project: no tag filter. Mentally deprioritize results from unrelated proj: tags.\n- Few results (<3): widen scope, remove tag filter."

  # --- Dual-layer deconfliction ---
  CONTEXT="${CONTEXT}\n\nDUAL MEMORY LAYERS:\n- MEMORY.md = active project state (current architecture, decisions, conventions). Updated each session.\n- MCP memory = historical knowledge (past errors, cross-project patterns, resolved issues, preferences). Searched on demand.\nDo NOT duplicate between them."

  # --- Importance scoring hint ---
  CONTEXT="${CONTEXT}\n\nIMPORTANCE: Set importance_score in metadata (2.0=critical, 1.5=important, 1.0=normal, 0.7=temporary). Use tags: critical, important, reference, temporary."

  # --- Automatic behavior (compressed) ---
  CONTEXT="${CONTEXT}\n\nAUTO BEHAVIOR: 1) Search memory on startup with project + task keywords. 2) Store silently when learning something important — categorize by type (architecture/decision/pattern/gotcha/progress/preference). 3) Update user-profile.md at ${MEMORY_DIR}/user-profile.md when learning new preferences. 4) At session start, print ONE short line with the B12 pill format. 5) When retrieval hook returns relevant memories or when storing, use these EXACT formats:\nRetrieval: ( \ud83d\udc8a B12 \ud83e\udde0 : found N memories about [topic], stored [date] \u2705 )\nStore: ( \ud83d\udc8a B12 \ud83e\udde0 : saved to memory \u2705 )\nNot found (only when user explicitly asks): ( \ud83d\udc8a B12 \ud83e\udde0 : searched but nothing found \u274c ) — then try wider time range or different keywords before giving up.\nKeep under 15 words. Only \u2705 or \u274c at the end, no other emojis after the colon."

  # Add user profile (if recent)
  if [ -n "$USER_PROFILE" ]; then
    CONTEXT="${CONTEXT}\n\n--- USER PROFILE ---\n${USER_PROFILE}\n--- END PROFILE ---"
  fi

  # Add last session summary
  if [ -n "$LAST_SESSION" ]; then
    if [ "$LAST_SESSION_SOURCE" = "global" ]; then
      CONTEXT="${CONTEXT}\n\n--- LAST SESSION (from different project) ---\n${LAST_SESSION}\n--- END LAST SESSION ---"
    else
      CONTEXT="${CONTEXT}\n\n--- LAST SESSION ---\n${LAST_SESSION}\n--- END LAST SESSION ---"
    fi
  fi

  # Add cross-project hints
  if [ -n "$CROSS_PROJECT_HINT" ]; then
    CONTEXT="${CONTEXT}\n\n--- CROSS-PROJECT ---\n${CROSS_PROJECT_HINT}\nSearch without tag filter to explore these.\n--- END ---"
  fi

  # Add feedback alerts (if any)
  if [ -n "$FEEDBACK_HINT" ]; then
    CONTEXT="${CONTEXT}\n\n--- MEMORY USAGE FEEDBACK ---\n${FEEDBACK_HINT}\n--- END FEEDBACK ---"
  fi

  # Add pre-fetched memories (project-relevant + universal)
  if [ -n "$MEMORY_PREFETCH" ]; then
    CONTEXT="${CONTEXT}\n\n--- MEMORY PRE-FETCH ---\n${MEMORY_PREFETCH}\n--- END PRE-FETCH ---"
  fi

  ESCAPED_CONTEXT=$(printf '%b' "$CONTEXT" | escape_json)

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
  # POST-COMPACTION — load staged context or fallback
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

  # Always include scope context after compaction
  SCOPE_REMINDER="Setup: ${SETUP_CONTEXT} | Project: ${PROJECT_NAME}. Scope tags: proj:${PROJECT_NAME}, user:${SETUP_CONTEXT}."

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

  if [ -n "$STAGED_CONTEXT" ]; then
    ESCAPED=$(echo "$STAGED_CONTEXT" | escape_json)
    WM_ESCAPED=""
    if [ -n "$WORKING_MEM" ]; then
      WM_ESCAPED=$(printf '%b' "$WORKING_MEM" | escape_json)
      WM_ESCAPED="\\n${WM_ESCAPED}"
    fi
    cat <<CONTEXT_EOF
{
  "hookSpecificOutput": {
    "hookEventName": "SessionStart",
    "additionalContext": "MEMORY SYSTEM: Context compacted. ${SCOPE_REMINDER}${WM_ESCAPED}\nPre-compaction summary:\n${ESCAPED}\n\nStore key learnings to permanent memory. Update user-profile.md if needed. Continue working."
  }
}
CONTEXT_EOF
  else
    # No staged context - minimal fallback with scope reminder
    FALLBACK=""
    if [ -n "$USER_PROFILE" ]; then
      FALLBACK="USER PROFILE:\n$(echo "$USER_PROFILE" | escape_json)\n\n"
    fi
    if [ -n "$LAST_SESSION" ]; then
      FALLBACK="${FALLBACK}LAST SESSION:\n$(echo "$LAST_SESSION" | escape_json)"
    fi

    WM_ESCAPED=""
    if [ -n "$WORKING_MEM" ]; then
      WM_ESCAPED=$(printf '%b' "$WORKING_MEM" | escape_json)
      WM_ESCAPED="\\n${WM_ESCAPED}"
    fi
    cat <<CONTEXT_EOF
{
  "hookSpecificOutput": {
    "hookEventName": "SessionStart",
    "additionalContext": "MEMORY SYSTEM: Context compacted. ${SCOPE_REMINDER}${WM_ESCAPED}\nSearch memory for context about current task.\n\n${FALLBACK}\n\nContinue where you left off."
  }
}
CONTEXT_EOF
  fi
fi

exit 0
