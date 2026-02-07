#!/bin/bash
# B12 Memory System - SessionStart Hook (v4 — Scope-Aware, Token-Efficient)
# Loads: setup context + compressed instructions + user profile (lazy) + session summary + cross-project hints
# Fires on: startup, resume, compact
#
# v4 changes:
# - Setup detection (personal vs work)
# - Scope-aware tagging/search instructions
# - Compressed behavioral instructions (~120 tokens vs ~512 in v3)
# - jq for cross-project hints (no python3 startup penalty)
# - Lazy-load user profile (only if updated within 7 days)
# - Dual-layer deconfliction rule (MEMORY.md vs MCP memory)

INPUT=$(cat)
SOURCE=$(echo "$INPUT" | jq -r '.source // "startup"')
CWD=$(echo "$INPUT" | jq -r '.cwd // ""')

# Central data directory — override with B12_DATA_DIR env var for custom setups
B12_BASE="${B12_DATA_DIR:-$HOME/.claude}"

PROJECT_NAME=$(basename "$CWD" 2>/dev/null || echo "unknown")
SUMMARY_DIR="$B12_BASE/memory-summaries"

# Derive project memory directory (same path format as Claude Code uses)
PROJECT_HASH=$(echo "$CWD" | sed 's|/|-|g')
MEMORY_DIR="$B12_BASE/projects/${PROJECT_HASH}/memory"

# ═══════════════════════════════════════════════════════════════
# SETUP DETECTION — personal vs work
# ═══════════════════════════════════════════════════════════════
if [[ "$B12_BASE" == *".claude-x"* ]] || [[ "$CWD" == *"/0G"* ]] || [[ "$CWD" == *"/0g"* ]]; then
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
  PROFILE_AGE=$(( $(date +%s) - $(stat -f %m "$PROFILE_FILE" 2>/dev/null || echo "0") ))
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
  DIGEST_AGE=$(( $(date +%s) - $(stat -f %m "$DIGEST_FILE" 2>/dev/null || echo "0") ))
  if [ "$DIGEST_AGE" -lt 1209600 ]; then
    # Extract only the Alerts section (most actionable)
    FEEDBACK_HINT=$(sed -n '/^## Alerts/,/^## /p' "$DIGEST_FILE" 2>/dev/null | head -5)
  fi
fi

# ═══════════════════════════════════════════════════════════════
# JSON ESCAPE — single python3 call for all strings
# ═══════════════════════════════════════════════════════════════
escape_json() {
  python3 -c "import sys,json; print(json.dumps(sys.stdin.read()))" 2>/dev/null | sed 's/^"//;s/"$//'
}

if [ "$SOURCE" = "startup" ] || [ "$SOURCE" = "resume" ]; then
  # ═══════════════════════════════════════════════════════════
  # BUILD CONTEXT — scope-aware, token-efficient
  # ═══════════════════════════════════════════════════════════
  CONTEXT="MEMORY SYSTEM ACTIVE (mcp-memory-service v10.7.0). Setup: ${SETUP_CONTEXT}. Project: ${PROJECT_NAME} (${CWD})."

  # --- Compact behavioral instructions (v4 — ~120 tokens vs ~512 in v3) ---
  CONTEXT="${CONTEXT}\n\nMEMORY TOOLS: memory_search (mode=hybrid, max_response_chars=40000), memory_store (always include metadata), memory_update, memory_graph, memory_quality, memory_cleanup."

  # --- Scope classification rules ---
  CONTEXT="${CONTEXT}\n\nSCOPE SYSTEM:\nSetup: ${SETUP_CONTEXT} | Project: ${PROJECT_NAME}\nWhen STORING: Always include tags [proj:${PROJECT_NAME}, user:${SETUP_CONTEXT}] and metadata {project:\"${PROJECT_NAME}\", setup:\"${SETUP_CONTEXT}\", scope:\"<type>\"}.\nScope types:\n- project: codebase-specific (architecture, decisions, bugs) -> tag: proj:${PROJECT_NAME}\n- universal: applies everywhere (patterns, CLI tricks, lessons) -> tag: user:universal\n- preference: user preferences (always global) -> tag: user:pref\n- setup: team/workflow specific to ${SETUP_CONTEXT} -> tag: user:${SETUP_CONTEXT}\nWhen SEARCHING:\n- Default: tags=[\"proj:${PROJECT_NAME}\"] to get project context. Add user:universal for general knowledge.\n- Cross-project: no tag filter. Mentally deprioritize results from unrelated proj: tags.\n- Few results (<3): widen scope, remove tag filter."

  # --- Dual-layer deconfliction ---
  CONTEXT="${CONTEXT}\n\nDUAL MEMORY LAYERS:\n- MEMORY.md = active project state (current architecture, decisions, conventions). Updated each session.\n- MCP memory = historical knowledge (past errors, cross-project patterns, resolved issues, preferences). Searched on demand.\nDo NOT duplicate between them."

  # --- Importance scoring hint ---
  CONTEXT="${CONTEXT}\n\nIMPORTANCE: Set importance_score in metadata (2.0=critical, 1.5=important, 1.0=normal, 0.7=temporary). Use tags: critical, important, reference, temporary."

  # --- Automatic behavior (compressed) ---
  CONTEXT="${CONTEXT}\n\nAUTO BEHAVIOR: 1) Search memory on startup with project + task keywords. 2) Store silently when learning something important — categorize by type (architecture/decision/pattern/gotcha/progress/preference). 3) Update user-profile.md at ${MEMORY_DIR}/user-profile.md when learning new preferences. 4) NEVER mention memory operations to user unless asked."

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

  ESCAPED_CONTEXT=$(echo -e "$CONTEXT" | escape_json)

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

  if [ -n "$STAGED_CONTEXT" ]; then
    ESCAPED=$(echo "$STAGED_CONTEXT" | escape_json)
    cat <<CONTEXT_EOF
{
  "hookSpecificOutput": {
    "hookEventName": "SessionStart",
    "additionalContext": "MEMORY SYSTEM: Context compacted. ${SCOPE_REMINDER}\nPre-compaction summary:\n${ESCAPED}\n\nStore key learnings to permanent memory. Update user-profile.md if needed. Continue working."
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

    cat <<CONTEXT_EOF
{
  "hookSpecificOutput": {
    "hookEventName": "SessionStart",
    "additionalContext": "MEMORY SYSTEM: Context compacted. ${SCOPE_REMINDER}\nSearch memory for context about current task.\n\n${FALLBACK}\n\nContinue where you left off."
  }
}
CONTEXT_EOF
  fi
fi

exit 0
