#!/bin/bash
# B12 Memory System - SessionStart Hook (v2)
# Loads: user profile + last session summary + memory instructions
#
# Fires on: startup, resume, compact
# Output: JSON with additionalContext for Claude
#
# Install: Copy to ~/.claude/hooks/ and chmod +x

INPUT=$(cat)
SOURCE=$(echo "$INPUT" | jq -r '.source // "startup"')
CWD=$(echo "$INPUT" | jq -r '.cwd // ""')

PROJECT_NAME=$(basename "$CWD" 2>/dev/null || echo "unknown")
SUMMARY_DIR="$HOME/.claude/memory-summaries"

# Derive project memory directory (same path format as Claude Code uses)
# /Users/foo/myproject -> -Users-foo-myproject
PROJECT_HASH=$(echo "$CWD" | sed 's|/|-|g')
MEMORY_DIR="$HOME/.claude/projects/${PROJECT_HASH}/memory"

# Load user profile (if it exists in the project memory dir)
USER_PROFILE=""
if [ -f "$MEMORY_DIR/user-profile.md" ]; then
  USER_PROFILE=$(cat "$MEMORY_DIR/user-profile.md" 2>/dev/null | head -60)
fi

# Load last session summary for this project
LAST_SESSION=""
if [ -f "$SUMMARY_DIR/${PROJECT_NAME}-latest.md" ]; then
  LAST_SESSION=$(cat "$SUMMARY_DIR/${PROJECT_NAME}-latest.md" 2>/dev/null | head -50)
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

  # Add behavioral instructions
  CONTEXT="${CONTEXT}\n\nAUTOMATIC BEHAVIOR (do silently, never ask user):\n1. SESSION START: Search memory for context relevant to current project/task. Use the memory search tool with project name and topic keywords.\n2. DURING WORK: When you learn something important (bug fix, architecture decision, pattern, user preference), store it immediately using memory tools. Tag with project name.\n3. BEFORE STOPPING: If you discovered key insights this turn, store them before your response ends.\n4. CROSS-PROJECT: When a learning from another project is relevant, recall and apply it.\n5. USER PROFILE: When you learn new preferences or patterns about the user, update the user profile file at ${MEMORY_DIR}/user-profile.md\n\nRULES:\n- NEVER ask the user about memory. Handle it 100% silently.\n- NEVER mention 'I saved this to memory' unless specifically asked about memory.\n- Keep stored memories concise - key insight + context, not full conversations.\n- Tag every memory with: project name, topic, date.\n- Prefer storing actionable knowledge over raw facts.\n- PROACTIVELY save after: decisions made, errors resolved, preferences expressed, tasks completed."

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
  STAGING_DIR="$HOME/.claude/memory-staging"
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
