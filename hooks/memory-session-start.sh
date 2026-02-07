#!/bin/bash
# B12 Memory System - SessionStart Hook
# Injects memory system instructions into Claude's context
#
# Fires on: startup, resume, compact
# Output: JSON with additionalContext for Claude
#
# Install: Copy to ~/.claude/hooks/ and chmod +x

INPUT=$(cat)
SOURCE=$(echo "$INPUT" | jq -r '.source // "startup"')
CWD=$(echo "$INPUT" | jq -r '.cwd // ""')

# Extract project name from cwd for context tagging
PROJECT_NAME=$(basename "$CWD" 2>/dev/null || echo "unknown")

if [ "$SOURCE" = "startup" ] || [ "$SOURCE" = "resume" ]; then
  # New or resumed session - inject full memory instructions
  cat <<CONTEXT_EOF
{
  "hookSpecificOutput": {
    "hookEventName": "SessionStart",
    "additionalContext": "MEMORY SYSTEM ACTIVE: You have a persistent semantic memory via the 'memory' MCP server (mcp-memory-service). Current project: ${PROJECT_NAME} (${CWD}).\n\nAUTOMATIC BEHAVIOR (do silently, never ask user):\n1. SESSION START: Search memory for context relevant to current project/task. Use the memory search tool with project name and topic keywords.\n2. DURING WORK: When you learn something important (bug fix, architecture decision, pattern, user preference), store it immediately using memory tools. Tag with project name.\n3. BEFORE STOPPING: If you discovered key insights this turn, store them before your response ends.\n4. CROSS-PROJECT: When a learning from another project is relevant, recall and apply it.\n\nRULES:\n- NEVER ask the user about memory. Handle it 100% silently.\n- NEVER mention 'I saved this to memory' unless specifically asked about memory.\n- Keep stored memories concise - key insight + context, not full conversations.\n- Tag every memory with: project name, topic, date.\n- Prefer storing actionable knowledge over raw facts."
  }
}
CONTEXT_EOF

elif [ "$SOURCE" = "compact" ]; then
  # After compaction - recover staged context if available
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
    cat <<CONTEXT_EOF
{
  "hookSpecificOutput": {
    "hookEventName": "SessionStart",
    "additionalContext": "MEMORY SYSTEM: Context was just compacted. Pre-compaction summary saved:\n${STAGED_CONTEXT}\n\nIMPORTANT: Store any key learnings from this summary to permanent memory using memory tools, then continue working."
  }
}
CONTEXT_EOF
  else
    cat <<CONTEXT_EOF
{
  "hookSpecificOutput": {
    "hookEventName": "SessionStart",
    "additionalContext": "MEMORY SYSTEM: Context was just compacted. Search memory for relevant context about the current task in project: ${PROJECT_NAME}. Continue where you left off."
  }
}
CONTEXT_EOF
  fi
fi

exit 0
