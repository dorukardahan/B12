# B12 Setup Guide

## Prerequisites

- Claude Code CLI (latest version)
- Python 3.10+ with pipx
- jq (for hook scripts)

## Step 1: Install mcp-memory-service

```bash
# Install via pipx (recommended — isolated environment)
pipx install mcp-memory-service

# Verify installation
memory --version
which memory
# Note the path — you'll need it for the MCP config
```

The database will be created automatically at:
- **macOS**: `~/Library/Application Support/mcp-memory/sqlite_vec.db`
- **Linux**: `~/.local/share/mcp-memory/sqlite_vec.db`

## Step 2: Configure MCP server

Add the memory server to your `~/.claude.json`:

```bash
# Find the mcpServers key and add:
{
  "mcpServers": {
    "memory": {
      "command": "/path/to/memory",
      "args": ["server"],
      "env": {}
    }
  }
}
```

Replace `/path/to/memory` with the output of `which memory`.

## Step 3: Install hooks

```bash
# Create required directories
mkdir -p ~/.claude/hooks
mkdir -p ~/.claude/memory-staging
mkdir -p ~/.claude/memory-logs
mkdir -p ~/.claude/memory-summaries

# Copy hook scripts
cp hooks/memory-session-start.sh ~/.claude/hooks/
cp hooks/memory-precompact.sh ~/.claude/hooks/
cp hooks/memory-session-end.sh ~/.claude/hooks/

# Make executable
chmod +x ~/.claude/hooks/memory-*.sh
```

## Step 4: Configure hooks in settings

Add the hooks configuration to your Claude Code settings. You can do this through the `/hooks` menu in Claude Code, or manually edit the settings file.

### Manual method

Edit `~/.claude/settings.json` (or `~/.claude-<setup>/settings.json` for other setups):

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "startup|resume|compact",
        "hooks": [
          {
            "type": "command",
            "command": "~/.claude/hooks/memory-session-start.sh",
            "timeout": 10
          }
        ]
      }
    ],
    "PreCompact": [
      {
        "matcher": "auto|manual",
        "hooks": [
          {
            "type": "command",
            "command": "~/.claude/hooks/memory-precompact.sh",
            "timeout": 15
          }
        ]
      }
    ],
    "SessionEnd": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "~/.claude/hooks/memory-session-end.sh",
            "timeout": 15
          }
        ]
      }
    ]
  }
}
```

**Important**: SessionEnd timeout is 15 seconds (not 5) because the v2 hook parses the full transcript with Python. If you have very long sessions (10K+ lines), consider increasing to 20.

**Multi-setup**: Add the hooks config to each setup's settings.json.

## Step 5: Create user profile (optional but recommended)

Create your user profile so Claude knows your preferences from the first session:

```bash
# Find your project memory directory
# Claude Code uses: ~/.claude/projects/<project-hash>/memory/
# where project-hash = CWD with / replaced by -
# Example: /Users/you -> -Users-you

mkdir -p ~/.claude/projects/-Users-$(whoami)/memory
cp templates/user-profile.md ~/.claude/projects/-Users-$(whoami)/memory/user-profile.md
```

Edit the profile with your actual preferences. Claude will also update it automatically as it learns about you.

## Step 6: Verify

1. Start a new Claude Code session
2. You should see the memory system context being loaded (visible with `Ctrl+Shift+L` for verbose mode)
3. Ask Claude: "What's in my memory about [topic]?"
4. Work normally — Claude will silently store important learnings
5. Close and reopen Claude Code — check if it remembers the last session

### Verify session summaries

After your first session ends:

```bash
# Check if session summary was created
ls ~/.claude/memory-summaries/

# View the summary
cat ~/.claude/memory-summaries/<your-project>-latest.md
```

### Test hooks manually

```bash
# Test SessionStart
echo '{"source":"startup","cwd":"/tmp","session_id":"test"}' | ~/.claude/hooks/memory-session-start.sh

# Test SessionEnd (needs a real transcript)
echo '{"session_id":"test","reason":"test","cwd":"/tmp","transcript_path":"/path/to/transcript.jsonl"}' | ~/.claude/hooks/memory-session-end.sh

# Test PreCompact (needs a real transcript)
echo '{"session_id":"test","cwd":"/tmp","transcript_path":"/path/to/transcript.jsonl"}' | ~/.claude/hooks/memory-precompact.sh
```

## Step 7: Optional — Enable native auto-memory

Add `ENABLE_TOOL_SEARCH=true` to your environment for deferred MCP tool loading (~95% token savings per session):

```json
// In settings.json
{
  "env": {
    "ENABLE_TOOL_SEARCH": "true"
  }
}
```

This also works alongside Claude Code's native auto-memory (MEMORY.md), giving you two layers of persistence:
- **MEMORY.md** for stable, high-level project knowledge
- **mcp-memory-service** for detailed, searchable memories
- **Session summaries** for short-term continuity

## Troubleshooting

### Memory server not starting

```bash
# Check if memory command is available
which memory

# Test server manually
memory server --debug
# Press Ctrl+C to stop

# Check for port conflicts or errors in output
```

### Hooks not firing

```bash
# Check if hooks are configured
claude /hooks
# Look for [User] tagged hooks

# Test hook manually
echo '{"source":"startup","cwd":"/tmp","session_id":"test"}' | ~/.claude/hooks/memory-session-start.sh

# Enable verbose mode in Claude Code (Ctrl+Shift+L) to see hook execution
```

### SessionEnd not creating summaries

The most common cause: the heredoc syntax bug. Ensure your hooks use:
```bash
# CORRECT:
python3 - "$ARG" << 'PYEOF'

# NOT:
python3 << 'PYEOF' "$ARG"
```

If `transcript_path` is empty in the hook input, Claude Code may not be providing it. Check with:
```bash
echo '{"session_id":"test","cwd":"/tmp","transcript_path":""}' | ~/.claude/hooks/memory-session-end.sh 2>&1
```

### No memories being stored

- Verify the MCP server is running: look for `memory` in Claude Code's MCP server list
- Check that Claude has access to memory tools: ask "What memory tools do you have?"
- The database is created on first write, so it may not exist until the first memory is stored

### Database location

```bash
# Check where the database is
ls -la ~/Library/Application\ Support/mcp-memory/

# View database stats
memory status
```
