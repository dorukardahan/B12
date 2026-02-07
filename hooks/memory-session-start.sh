#!/bin/bash
# B12 Memory System - SessionStart Hook (v3)
# Loads: user profile + session summary + cross-project hints + behavioral instructions
# Fires on: startup, resume, compact
#
# Install: Copy to ~/.claude/hooks/ and chmod +x
# Override data dir: export B12_DATA_DIR=/path/to/data

INPUT=$(cat)
SOURCE=$(echo "$INPUT" | jq -r '.source // "startup"')
CWD=$(echo "$INPUT" | jq -r '.cwd // ""')

# Central data directory — override with B12_DATA_DIR env var for custom setups
B12_BASE="${B12_DATA_DIR:-$HOME/.claude}"

PROJECT_NAME=$(basename "$CWD" 2>/dev/null || echo "unknown")
SUMMARY_DIR="$B12_BASE/memory-summaries"

# Derive project memory directory (same path format as Claude Code uses)
# /Users/foo/myproject -> -Users-foo-myproject
PROJECT_HASH=$(echo "$CWD" | sed 's|/|-|g')
MEMORY_DIR="$B12_BASE/projects/${PROJECT_HASH}/memory"

# Load user profile
USER_PROFILE=""
if [ -f "$MEMORY_DIR/user-profile.md" ]; then
  USER_PROFILE=$(cat "$MEMORY_DIR/user-profile.md" 2>/dev/null | head -60)
fi

# Load last session summary for this project
LAST_SESSION=""
if [ -f "$SUMMARY_DIR/${PROJECT_NAME}-latest.md" ]; then
  LAST_SESSION=$(cat "$SUMMARY_DIR/${PROJECT_NAME}-latest.md" 2>/dev/null | head -50)
fi

# Load cross-project index (compact — just topic list for current project)
CROSS_PROJECT_HINT=""
INDEX_FILE="$SUMMARY_DIR/cross-project-index.json"
if [ -f "$INDEX_FILE" ]; then
  # Extract topics that mention OTHER projects (not current one)
  CROSS_PROJECT_HINT=$(python3 -c "
import json, sys
try:
    with open('$INDEX_FILE') as f:
        idx = json.load(f)
    hints = []
    for topic, projects in idx.get('topics', {}).items():
        other = [p for p in projects if p != '$PROJECT_NAME']
        if other:
            hints.append(f'{topic} (in: {\", \".join(other[:3])})')
    if hints:
        print('Cross-project topics: ' + '; '.join(hints[:10]))
except: pass
" 2>/dev/null)
fi

# Escape for JSON
escape_json() {
  python3 -c "import sys,json; print(json.dumps(sys.stdin.read()))" 2>/dev/null | sed 's/^"//;s/"$//'
}

if [ "$SOURCE" = "startup" ] || [ "$SOURCE" = "resume" ]; then
  # Build context with all available information
  CONTEXT="MEMORY SYSTEM ACTIVE: You have a persistent semantic memory via the 'memory' MCP server (mcp-memory-service). Current project: ${PROJECT_NAME} (${CWD})."

  # Add user profile
  if [ -n "$USER_PROFILE" ]; then
    CONTEXT="${CONTEXT}\n\n--- USER PROFILE ---\n${USER_PROFILE}\n--- END PROFILE ---"
  fi

  # Add last session summary
  if [ -n "$LAST_SESSION" ]; then
    CONTEXT="${CONTEXT}\n\n--- LAST SESSION ---\n${LAST_SESSION}\n--- END LAST SESSION ---"
  fi

  # Add cross-project hints (compact — just topic list)
  if [ -n "$CROSS_PROJECT_HINT" ]; then
    CONTEXT="${CONTEXT}\n\n--- CROSS-PROJECT HINTS ---\n${CROSS_PROJECT_HINT}\nUse memory_search without project filter to find details on these topics from other projects.\n--- END HINTS ---"
  fi

  # Add behavioral instructions (v3 — typed memories, hybrid search, full tool usage)
  CONTEXT="${CONTEXT}\n\nMEMORY TOOLS REFERENCE:\n- memory_search: Use mode='hybrid' for best results. Always set max_response_chars=40000 to prevent context overflow.\n- memory_store: Always include metadata: {project, topic, type, date}.\n- memory_quality: Rate important memories after storing (improves ranking over time).\n- memory_graph: Explore connections between related memories.\n- memory_update: Update existing memories instead of creating near-duplicates.\n- memory_cleanup: Run occasionally to remove low-quality entries.\n\nMEMORY TYPES (use as 'type' in metadata):\n- architecture: System design, component relationships, tech stack choices\n- decision: Why X was chosen over Y, tradeoffs considered\n- pattern: Reusable solutions, conventions, best practices discovered\n- gotcha: Bugs, pitfalls, things that broke and their fixes\n- progress: Task status, what was done, what remains\n- preference: User preferences, workflow habits, communication style\n\nAUTOMATIC BEHAVIOR (do silently, never ask user):\n1. SESSION START: Search memory with project name + current task keywords. Use hybrid mode.\n2. DURING WORK: Store immediately when you learn something important. Categorize by type.\n3. CROSS-PROJECT: Search without project filter to find insights from other projects when relevant.\n4. BEFORE STOPPING: Store key insights from this turn. Update user profile if new preferences learned.\n5. USER PROFILE: Update ${MEMORY_DIR}/user-profile.md when you learn new preferences about the user.\n\nRULES:\n- NEVER ask the user about memory. Handle it 100% silently.\n- NEVER mention 'I saved this to memory' unless specifically asked.\n- Keep stored memories concise — key insight + context, not full conversations.\n- Prefer storing actionable knowledge over raw facts.\n- UPDATE existing memories instead of creating near-duplicates.\n- PROACTIVELY save after: decisions made, errors resolved, preferences expressed, tasks completed."

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
  # After compaction - load staged context
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

  if [ -n "$STAGED_CONTEXT" ]; then
    ESCAPED=$(echo "$STAGED_CONTEXT" | escape_json)
    cat <<CONTEXT_EOF
{
  "hookSpecificOutput": {
    "hookEventName": "SessionStart",
    "additionalContext": "MEMORY SYSTEM: Context was just compacted. Pre-compaction summary:\n${ESCAPED}\n\nIMPORTANT: Store any key learnings from this summary to permanent memory using memory tools. Also update user-profile.md if you learned new preferences. Then continue working."
  }
}
CONTEXT_EOF
  else
    # No staged context - load last session + user profile as fallback
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
    "additionalContext": "MEMORY SYSTEM: Context was just compacted. Search memory for relevant context about the current task in project: ${PROJECT_NAME}.\n\n${FALLBACK}\n\nContinue where you left off."
  }
}
CONTEXT_EOF
  fi
fi

exit 0
