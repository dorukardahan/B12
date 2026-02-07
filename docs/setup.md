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
# Create hooks directory
mkdir -p ~/.claude/hooks

# Copy hook scripts
cp hooks/memory-session-start.sh ~/.claude/hooks/
cp hooks/memory-precompact.sh ~/.claude/hooks/
cp hooks/memory-session-end.sh ~/.claude/hooks/

# Make executable
chmod +x ~/.claude/hooks/memory-*.sh

# Create required directories
mkdir -p ~/.claude/memory-staging
mkdir -p ~/.claude/memory-logs
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
            "timeout": 5
          }
        ]
      }
    ]
  }
}
```

**Important**: If you have multiple setups, add the hooks config to each setup's settings.json.

## Step 5: Verify

1. Start a new Claude Code session
2. You should see the memory system context being loaded (visible in verbose mode with Ctrl+O)
3. Ask Claude: "What's in my memory about [topic]?"
4. Work normally — Claude will silently store important learnings

## Step 6: Optional — Set up auto-memory

Add `CLAUDE_CODE_DISABLE_AUTO_MEMORY=0` to your environment to enable Claude Code's native auto-memory alongside B12:

```json
// In settings.json
{
  "env": {
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "0"
  }
}
```

This gives you two layers of persistence:
- **MEMORY.md** for stable, high-level project knowledge
- **mcp-memory-service** for detailed, searchable memories

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

# Enable verbose mode in Claude Code (Ctrl+O) to see hook execution
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
