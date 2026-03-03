#!/bin/bash
# B12 Gemini CLI — AfterTool Hook Adapter (Memory Retrieval)
#
# Triggers B12 memory retrieval after model tool calls that indicate
# the user is asking about past work, decisions, or context.
# Uses the AfterTool event to inject relevant memories as additional context.
#
# Gemini CLI AfterTool input (stdin):
#   { "session_id": "...", "cwd": "...", "hook_event_name": "AfterTool",
#     "tool_name": "...", "tool_input": {...}, "tool_response": {...} }
#
# Gemini CLI AfterTool output (stdout):
#   { "hookSpecificOutput": { "additionalContext": "..." } }
#
# Config in ~/.gemini/settings.json:
#   "hooks": { "AfterTool": [{ "matcher": ".*",
#     "hooks": [{ "type": "command",
#       "command": "~/.B12/hooks/gemini/b12-gemini-tool-call.sh" }] }] }

set -euo pipefail

exec 3>&2

B12_HOOK_DIR="${B12_HOOK_DIR:-$HOME/.B12/hooks}"
B12_HOOK="$B12_HOOK_DIR/memory-retrieval.sh"

# Read Gemini CLI input
INPUT=$(cat)

# Check B12 hook exists
if [ ! -f "$B12_HOOK" ]; then
  echo '{}'
  exit 0
fi

# ── Filter: only trigger on B12 MCP tool calls ──
# We only want to add context for B12 memory tool calls, not all tools.
# This avoids unnecessary overhead on every file read, shell command, etc.
TOOL_NAME=$(echo "$INPUT" | jq -r '.tool_name // ""')

case "$TOOL_NAME" in
  mcp__B12__memory_search|mcp__B12__memory_store|mcp__B12__memory_session_context)
    # B12 MCP tools — let them through, they already use the MCP server
    echo '{}'
    exit 0
    ;;
  read_file|list_directory|run_shell_command|write_file|search_files)
    # Built-in Gemini tools — could trigger retrieval for context
    ;;
  *)
    # Unknown tools — skip
    echo '{}'
    exit 0
    ;;
esac

# ── Extract query context from tool input ──
# For file reads, use the file path as retrieval context
# For shell commands, use the command itself
QUERY=""
CWD=$(echo "$INPUT" | jq -r '.cwd // ""')

case "$TOOL_NAME" in
  read_file)
    QUERY=$(echo "$INPUT" | jq -r '.tool_input.path // .tool_input.file_path // ""')
    # Use just the filename for retrieval
    QUERY=$(basename "$QUERY" 2>/dev/null || echo "$QUERY")
    ;;
  search_files)
    QUERY=$(echo "$INPUT" | jq -r '.tool_input.query // .tool_input.pattern // ""')
    ;;
  run_shell_command)
    QUERY=$(echo "$INPUT" | jq -r '.tool_input.command // ""')
    # Extract meaningful keywords from shell commands
    QUERY=$(echo "$QUERY" | sed 's/[|;&><]/ /g' | head -c 200)
    ;;
  *)
    echo '{}'
    exit 0
    ;;
esac

# Skip if no meaningful query
if [ -z "$QUERY" ] || [ ${#QUERY} -lt 3 ]; then
  echo '{}'
  exit 0
fi

# ── Build Claude Code retrieval format ──
# memory-retrieval.sh expects:
#   { "tool_input": { "query": "...", "tags": [...] }, "cwd": "..." }
PROJECT_NAME=$(basename "$CWD" 2>/dev/null || echo "unknown")

CLAUDE_INPUT=$(jq -n \
  --arg query "$QUERY" \
  --arg cwd "$CWD" \
  --arg proj "proj:$PROJECT_NAME" \
  '{ "tool_input": { "query": $query, "tags": [$proj] }, "cwd": $cwd }')

# ── Call B12 retrieval hook ──
RESULT=$(echo "$CLAUDE_INPUT" | bash "$B12_HOOK" 2>&3) || true

# ── Extract context from retrieval result ──
if [ -n "$RESULT" ] && echo "$RESULT" | jq empty 2>/dev/null; then
  CONTEXT=$(echo "$RESULT" | jq -r '.hookSpecificOutput.additionalContext // empty')
  if [ -n "$CONTEXT" ]; then
    jq -n --arg ctx "$CONTEXT" \
      '{ "hookSpecificOutput": { "additionalContext": $ctx } }'
    exit 0
  fi
fi

echo '{}'
exit 0
