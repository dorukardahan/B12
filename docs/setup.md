# B12 Setup Guide

## Prerequisites

- Claude Code CLI (latest version)
- Python 3.10+ with pipx
- jq (for hook scripts)
- sqlite3 CLI (for pre-fetch and browse — usually pre-installed on macOS/Linux)

## Quick install

```bash
git clone https://github.com/youruser/B12.git
cd B12
chmod +x install.sh
./install.sh          # Install to ~/.claude (default)
```

The installer:
1. Creates required directories (`hooks/`, `memory-staging/`, `memory-logs/`, `memory-summaries/`)
2. Copies all hook scripts to `~/.claude/hooks/`
3. Copies support scripts to `~/.claude/hooks/scripts/`
4. Merges hook configuration into `~/.claude/settings.json`

For multiple setups: `./install.sh --all` installs to all `~/.claude*` directories.

## Step-by-step install

### Step 1: Install mcp-memory-service

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

### Step 2: Configure MCP server

Add the memory server to your `~/.claude.json`:

```json
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

### Step 3: Install hooks and scripts

```bash
# Create required directories
mkdir -p ~/.claude/hooks
mkdir -p ~/.claude/hooks/scripts
mkdir -p ~/.claude/memory-staging
mkdir -p ~/.claude/memory-logs
mkdir -p ~/.claude/memory-summaries

# Copy all hook scripts
cp hooks/memory-*.sh hooks/memory-*.py ~/.claude/hooks/

# Copy support scripts
cp scripts/*.py ~/.claude/hooks/scripts/

# Make executable
chmod +x ~/.claude/hooks/memory-*.sh
```

### Step 4: Configure hooks in settings

Add the hooks configuration to your Claude Code settings.

#### Automated method

```bash
# Using the installer (recommended)
./install.sh
```

#### Manual method

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
    ],
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "~/.claude/hooks/memory-retrieval.sh",
            "timeout": 3
          }
        ]
      }
    ],
    "PreToolUse": [
      {
        "matcher": "mcp__memory__memory_store",
        "hooks": [
          {
            "type": "command",
            "command": "~/.claude/hooks/memory-tag-enforce.sh",
            "timeout": 3
          }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "mcp__memory__memory_store|mcp__memory__memory_search|mcp__memory__memory_quality|mcp__memory__memory_update",
        "hooks": [
          {
            "type": "command",
            "command": "~/.claude/hooks/memory-feedback.sh",
            "timeout": 3
          }
        ]
      },
      {
        "matcher": "Read|Edit|Write|Glob|Grep",
        "hooks": [
          {
            "type": "command",
            "command": "~/.claude/hooks/memory-working-context.sh",
            "timeout": 2
          }
        ]
      }
    ]
  }
}
```

**Important**: SessionEnd timeout is 15 seconds because the hook parses the full transcript with Python. If you have very long sessions (10K+ lines), consider increasing to 20.

**Multi-setup**: Add the hooks config to each setup's settings.json.

### Step 5: Create user profile (optional but recommended)

```bash
# Find your project memory directory
# Claude Code uses: ~/.claude/projects/<project-hash>/memory/
# where project-hash = CWD with / replaced by -
# Example: /Users/you -> -Users-you

mkdir -p ~/.claude/projects/-Users-$(whoami)/memory
cp templates/user-profile.md ~/.claude/projects/-Users-$(whoami)/memory/user-profile.md
```

Edit the profile with your actual preferences. Claude will also update it automatically as it learns about you.

### Step 6: Set up FTS5 hybrid search (recommended)

The FTS5 hybrid search requires a patch to mcp-memory-service's Python code. This adds a `memory_fts` FTS5 virtual table and modifies the `retrieve()` and `recall()` methods to use hybrid scoring.

```bash
# Find the installed package location
SITE=$(python3 -c "import mcp_memory_service; print(mcp_memory_service.__path__[0])" 2>/dev/null || pipx runpip mcp-memory-service show mcp-memory-service | grep Location | awk '{print $2}')

# The file to patch:
# $SITE/mcp_memory_service/storage/sqlite_vec.py

# Apply the FTS5 patch (see scripts/migrate-ebbinghaus.py for schema changes)
# The patch adds:
#   - memory_fts FTS5 virtual table
#   - 4 triggers (INSERT, UPDATE, DELETE, content sync)
#   - Hybrid scoring in retrieve() and recall()
```

**Important**: The database schema persists across upgrades, but the Python code patch needs to be re-applied after `pipx upgrade mcp-memory-service`. The `memory-upgrade.sh` script detects and warns if the patch is missing.

### Step 7: Set up automated tasks (optional)

#### macOS (launchd)

```bash
# Copy plist templates
cp config/launchd-*.plist ~/Library/LaunchAgents/

# Edit each plist to replace /path/to/home with your actual home directory
# For example: sed -i '' "s|/path/to/home|$HOME|g" ~/Library/LaunchAgents/com.b12.memory-*.plist

# Load the agents
launchctl load ~/Library/LaunchAgents/com.b12.memory-backup.plist
launchctl load ~/Library/LaunchAgents/com.b12.memory-consolidate.plist
launchctl load ~/Library/LaunchAgents/com.b12.memory-feedback-digest.plist
launchctl load ~/Library/LaunchAgents/com.b12.memory-quality-audit.plist
```

| Agent | Schedule | What it does |
|-------|----------|-------------|
| `com.b12.memory-backup` | Daily 1:00 AM | WAL-safe DB backup, 7-day rotation, integrity check |
| `com.b12.memory-consolidate` | Daily 2:00 AM | Dedup, stale cleanup, cross-project index |
| `com.b12.memory-feedback-digest` | Monday 3:00 AM | Search pattern analysis, usage stats, alerts |
| `com.b12.memory-quality-audit` | Wednesday 3:00 AM | Health score, scope compliance, strength distribution |

#### Linux (cron)

```bash
# Add to crontab -e
0 1 * * * /bin/bash ~/.claude/hooks/memory-backup.sh --quiet
0 2 * * * /usr/bin/python3 ~/.claude/hooks/memory-consolidate.py --auto
0 3 * * 1 /bin/bash ~/.claude/hooks/memory-feedback-digest.sh --quiet
0 3 * * 3 /bin/bash ~/.claude/hooks/memory-quality-audit.sh --quiet
```

### Step 8: Restart Claude Code

The memory system activates on the next session start. Verify:

1. Start a new Claude Code session
2. You should see the memory system context being loaded
3. Work normally — Claude will silently store important learnings
4. Close and reopen Claude Code — check if it remembers the last session

## Verify installation

### Check session summaries

After your first session ends:

```bash
ls ~/.claude/memory-summaries/
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

# Test retrieval
echo '{"prompt":"how does authentication work","cwd":"/tmp","session_id":"test"}' | ~/.claude/hooks/memory-retrieval.sh
```

### Check database

```bash
# Browse memories
~/.claude/hooks/memory-browse.sh stats
~/.claude/hooks/memory-browse.sh search "your query"

# Run quality audit
~/.claude/hooks/memory-quality-audit.sh

# Check backup
ls ~/.claude/memory-backups/
```

## Troubleshooting

### Memory server not starting

```bash
# Check if memory command is available
which memory

# Test server manually
memory server --debug
# Press Ctrl+C to stop
```

### Hooks not firing

```bash
# Check if hooks are configured
claude /hooks
# Look for [User] tagged hooks

# Test hook manually
echo '{"source":"startup","cwd":"/tmp","session_id":"test"}' | ~/.claude/hooks/memory-session-start.sh

# Enable verbose mode in Claude Code to see hook execution
```

### SessionEnd not creating summaries

The most common cause: the heredoc syntax bug. Ensure your hooks use:
```bash
# CORRECT:
python3 - "$ARG" << 'PYEOF'

# NOT:
python3 << 'PYEOF' "$ARG"
```

### No memories being stored

- Verify the MCP server is running: look for `memory` in Claude Code's MCP server list
- Check that Claude has access to memory tools: ask "What memory tools do you have?"
- The database is created on first write, so it may not exist until the first memory is stored

### Database location

```bash
# macOS
ls -la ~/Library/Application\ Support/mcp-memory/

# Linux
ls -la ~/.local/share/mcp-memory/

# View database stats via browse CLI
~/.claude/hooks/memory-browse.sh stats
```

### Upgrading

```bash
# Upgrade mcp-memory-service
~/.claude/hooks/memory-upgrade.sh

# Re-run installer to update hooks
cd /path/to/B12
git pull
./install.sh --all

# Note: After pipx upgrade, re-apply the FTS5 patch if you use hybrid search
```
