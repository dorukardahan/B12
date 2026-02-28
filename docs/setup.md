# B12 Setup Guide

## Prerequisites

- **Claude Code CLI** (latest version)
- **Python 3.11+** (for the MCP server and embedding daemon)
- **jq** (for hook scripts — pre-installed on most systems)
- **sqlite3 CLI** (for pre-fetch and browse — pre-installed on macOS/Linux)

## Quick Install

```bash
git clone https://github.com/dorukardahan/B12.git
cd B12
chmod +x install.sh
./install.sh --full          # Full setup: venv + deps + hooks + MCP config
```

This single command creates the venv, installs dependencies, deploys hooks, and configures the MCP server with correct absolute paths.

For multiple Claude Code setups: `./install.sh --full --all`

The installer:
1. Creates required directories (`hooks/`, `memory-staging/`, `memory-logs/`, `memory-summaries/`)
2. Copies all hook scripts to `~/.B12/hooks/`
3. Copies support scripts to `~/.B12/hooks/scripts/` (includes `b12_mcp_server.py`)
4. Merges hook configuration into `~/.claude/settings.json`
5. Runs database migration (if existing database found)

For multiple setups: `./install.sh --all` installs to all `~/.claude*` directories.

### Environment variables (multi-setup)

| Variable | Controls | Default |
|----------|----------|---------|
| `B12_DATA_DIR` | Data/state: summaries, staging, logs | `~/.B12` |
| `B12_HOOK_DIR` | Hook code: script imports, embed daemon | `~/.B12/hooks` |
| `B12_WORK_PATTERN` | Work setup detection pattern | (none) |

Set per-setup in each `settings.json` `env` block. `B12_DATA_DIR` and `B12_HOOK_DIR` are separate — data can be per-setup while hook code stays shared.

## Step-by-Step Install

### Step 1: Clone the repository

```bash
git clone https://github.com/dorukardahan/B12.git
cd B12
```

### Step 2: Create the B12 Python environment

B12 uses a dedicated venv to avoid conflicts with your system Python:

```bash
python3 -m venv ~/.local/b12-venv
~/.local/b12-venv/bin/pip install mcp sentence-transformers sqlite-vec
```

Verify the installation:

```bash
~/.local/b12-venv/bin/python3 -c "import mcp; print('mcp OK')"
~/.local/b12-venv/bin/python3 -c "import sentence_transformers; print('sentence-transformers OK')"
~/.local/b12-venv/bin/python3 -c "import sqlite_vec; print('sqlite-vec OK')"
```

The key packages:
- **`mcp`** — Model Context Protocol SDK (FastMCP server framework)
- **`sentence-transformers`** — local embedding model for semantic search
- **`sqlite-vec`** — vector similarity extension for SQLite

### Step 3: Run the installer

```bash
chmod +x install.sh
./install.sh
```

This copies all hooks and scripts to `~/.B12/hooks/` and merges the hook configuration into your `settings.json`.

### Step 4: Configure the MCP server

**Option A — Automatic (recommended):** If you used `./install.sh --full`, this is already done. Skip to Step 5.

**Option B — Manual:** Add the B12 MCP server to your `~/.claude.json`:

```json
{
  "mcpServers": {
    "B12": {
      "command": "/Users/yourname/.local/b12-venv/bin/python3",
      "args": ["/Users/yourname/.B12/hooks/scripts/b12_mcp_server.py"],
      "env": {
        "MCP_EMBEDDING_MODEL": "paraphrase-multilingual-MiniLM-L12-v2",
        "MCP_MAX_RESPONSE_CHARS": "40000"
      }
    }
  }
}
```

Replace `/Users/yourname` with your actual home directory (run `echo $HOME` to find it).

A full template is available at `config/mcp-b12-template.json`.

**Important — tilde paths don't work:** Claude Code does NOT expand `~` in MCP server configs. You must use absolute paths (`/Users/you/...` or `/home/you/...`), not `~/.local/...`. The `--full` installer handles this automatically.

### Step 5: Verify the installation

Restart Claude Code, then run `/mcp`. You should see:

```
B12 · connected
  Tools: memory_store, memory_search, memory_update, memory_quality
```

**First run note:** The embedding model (~90MB) downloads automatically on the first session start. This is a one-time download and may take 30-60 seconds. Subsequent sessions start instantly. The database and all tables are created automatically by the MCP server on first use.

If the server shows as disconnected, check:
- Python path exists: `ls ~/.local/b12-venv/bin/python3`
- Script path exists: `ls ~/.B12/hooks/scripts/b12_mcp_server.py`
- MCP package is installed: `~/.local/b12-venv/bin/python3 -c "import mcp"`

### Step 6: Configure hooks (usually automatic)

The installer merges hook configuration from `config/settings-template.json` into your `settings.json`. To verify hooks are active, start a Claude Code session and run `/hooks` — you should see `[User]` tagged hooks.

#### Manual hook configuration

If you prefer manual setup, edit `~/.claude/settings.json` (or `~/.claude-<setup>/settings.json`):

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "startup|resume|compact",
        "hooks": [
          {
            "type": "command",
            "command": "~/.B12/hooks/memory-session-start.sh",
            "timeout": 20
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
            "command": "~/.B12/hooks/memory-precompact.sh",
            "timeout": 30
          }
        ]
      }
    ],
    "SessionEnd": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "~/.B12/hooks/memory-session-end.sh",
            "timeout": 35
          }
        ]
      }
    ],
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "~/.B12/hooks/memory-retrieval.sh",
            "timeout": 15
          }
        ]
      }
    ],
    "PreToolUse": [
      {
        "matcher": "mcp__B12__memory_store",
        "hooks": [
          {
            "type": "command",
            "command": "~/.B12/hooks/memory-tag-enforce.sh",
            "timeout": 8
          }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "mcp__B12__memory_store|mcp__B12__memory_search|mcp__B12__memory_quality|mcp__B12__memory_update",
        "hooks": [
          {
            "type": "command",
            "command": "~/.B12/hooks/memory-feedback.sh",
            "timeout": 8
          }
        ]
      },
      {
        "matcher": "Read|Edit|Write|Glob|Grep",
        "hooks": [
          {
            "type": "command",
            "command": "~/.B12/hooks/memory-working-context.sh",
            "timeout": 8
          }
        ]
      }
    ]
  }
}
```

**Hook timeout note**: Timeouts must be >= watchdog timer + 5s. The `settings-template.json` has correct values. SessionEnd is 35s because it parses the full transcript with Python.

### Step 7: Create user profile (optional but recommended)

```bash
# Find your project memory directory
# Claude Code uses: ~/.claude/projects/<project-hash>/memory/
# where project-hash = CWD with / replaced by -
# Example: /Users/you -> -Users-you

mkdir -p ~/.claude/projects/-Users-$(whoami)/memory
cp templates/user-profile.md ~/.claude/projects/-Users-$(whoami)/memory/user-profile.md
```

Edit the profile with your actual preferences. Claude will also update it automatically as it learns about you.

### Step 8: Set up automated tasks (optional)

#### macOS (launchd)

```bash
# Copy plist templates
cp config/launchd-*.plist config/com.b12.graph-enrich.plist ~/Library/LaunchAgents/

# Edit each plist to replace /path/to/home with your actual home directory
sed -i '' "s|/path/to/home|$HOME|g" ~/Library/LaunchAgents/launchd-*.plist ~/Library/LaunchAgents/com.b12.graph-enrich.plist

# Load the agents
launchctl load ~/Library/LaunchAgents/launchd-backup.plist
launchctl load ~/Library/LaunchAgents/launchd-consolidate.plist
launchctl load ~/Library/LaunchAgents/launchd-feedback-digest.plist
launchctl load ~/Library/LaunchAgents/launchd-quality-audit.plist
```

| Agent | Schedule | What it does |
|-------|----------|-------------|
| `com.b12.memory-backup` | Daily 1:00 AM | WAL-safe DB backup, 7-day rotation, integrity check |
| `com.b12.memory-consolidate` | Daily 2:00 AM | Dedup, stale cleanup, cross-project index |
| `com.b12.memory-feedback-digest` | Monday 3:00 AM | Search pattern analysis, usage stats, alerts |
| `com.b12.memory-quality-audit` | Wednesday 3:00 AM | Health score, scope compliance, strength distribution |

**Graph enrichment** (optional, requires ONNX NLI model):

```bash
cp config/com.b12.graph-enrich.plist ~/Library/LaunchAgents/
sed -i '' "s|/path/to/home|$HOME|g" ~/Library/LaunchAgents/com.b12.graph-enrich.plist
launchctl load ~/Library/LaunchAgents/com.b12.graph-enrich.plist
```

| Agent | Schedule | What it does |
|-------|----------|-------------|
| `com.b12.graph-enrich` | Daily 4:00 AM | Discovers related/contradicts/supports edges between memories |

#### Linux (cron)

```bash
# Add to crontab -e
0 1 * * * /bin/bash ~/.B12/hooks/memory-backup.sh --quiet
0 2 * * * /usr/bin/python3 ~/.B12/hooks/memory-consolidate.py --auto
0 3 * * 1 /bin/bash ~/.B12/hooks/memory-feedback-digest.sh --quiet
0 3 * * 3 /bin/bash ~/.B12/hooks/memory-quality-audit.sh --quiet
```

## Verify Installation

### Check MCP server

```bash
# In a Claude Code session
/mcp
# Should show: B12 · connected (5 tools)
```

### Check hooks

```bash
# In a Claude Code session
/hooks
# Should show [User] tagged hooks for all 7 events
```

### Check session summaries

After your first session ends:

```bash
ls ~/.B12/memory-summaries/
cat ~/.B12/memory-summaries/<your-project>-latest.md
```

### Test hooks manually

```bash
# Test SessionStart
echo '{"source":"startup","cwd":"/tmp","session_id":"test"}' | ~/.B12/hooks/memory-session-start.sh

# Test retrieval
echo '{"prompt":"how does authentication work","cwd":"/tmp","session_id":"test"}' | ~/.B12/hooks/memory-retrieval.sh
```

### Check database

```bash
# Browse memories
~/.B12/hooks/memory-browse.sh stats
~/.B12/hooks/memory-browse.sh search "your query"

# Database location (macOS)
ls -la ~/Library/Application\ Support/mcp-memory/
```

## Troubleshooting

### B12 MCP server not connecting

```bash
# Check if the Python path is correct
ls ~/.local/b12-venv/bin/python3

# Check if the server script exists
ls ~/.B12/hooks/scripts/b12_mcp_server.py

# Test the server manually (should print nothing and wait for stdin)
~/.local/b12-venv/bin/python3 ~/.B12/hooks/scripts/b12_mcp_server.py
# Press Ctrl+C to stop

# Check if MCP package is installed
~/.local/b12-venv/bin/python3 -c "import mcp; print(mcp.__version__)"
```

### Hooks not firing

```bash
# Check if hooks are configured
# In Claude Code: /hooks
# Look for [User] tagged hooks

# Test hook manually
echo '{"source":"startup","cwd":"/tmp","session_id":"test"}' | ~/.B12/hooks/memory-session-start.sh
```

### SessionEnd not creating summaries

The most common cause: the Python heredoc syntax in the hook script. Ensure hooks use:
```bash
# CORRECT:
python3 - "$ARG" << 'PYEOF'

# NOT:
python3 << 'PYEOF' "$ARG"
```

### No memories being stored

1. Verify the MCP server is running: `/mcp` in Claude Code
2. Check that Claude has access to memory tools: ask "What memory tools do you have?"
3. The database is created on first write — it may not exist until the first memory is stored

### Embed daemon not starting

The embed daemon (`embed_daemon.py`) starts automatically when the MCP server needs it. If semantic search isn't working:

```bash
# Check if daemon socket exists
ls /tmp/b12-embed-$(id -u).sock

# Check if sentence-transformers is installed
~/.local/b12-venv/bin/python3 -c "from sentence_transformers import SentenceTransformer; print('OK')"
```

### Upgrading from mcp-memory-service

If you were using the old `mcp-memory-service` (pipx) system:

1. The SQLite database is compatible — B12 reads the same `sqlite_vec.db`
2. Remove the old MCP config from `~/.claude.json` (the `"memory"` key)
3. Add the new B12 config (the `"B12"` key — see Step 4 above)
4. Hook matchers already use `mcp__B12__*` tool names
5. You can uninstall the old package: `pipx uninstall mcp-memory-service`

### Updating B12

```bash
cd /path/to/B12
git pull
./install.sh --all    # Re-deploys hooks and scripts
# Restart Claude Code to pick up changes
```

## Codex CLI Setup

B12 also works with OpenAI's Codex CLI. The same MCP server and SQLite database are shared — memories from Claude Code sessions appear in Codex searches, and vice versa.

### Quick Install (Codex)

```bash
# If you already have the venv from Claude Code setup:
./install.sh --codex

# Fresh install (creates venv + Claude Code + Codex):
./install.sh --full --codex
```

### What `--codex` Does

1. Injects B12 MCP server config into `~/.codex/config.toml`
2. Appends B12 memory instructions to `~/.codex/AGENTS.md` (between `<!-- B12-MEMORY-START -->` and `<!-- B12-MEMORY-END -->` markers)

### Manual Codex Setup

If you prefer manual configuration, add to `~/.codex/config.toml`:

```toml
[mcp_servers.B12]
command = "/Users/yourname/.local/b12-venv/bin/python3"
args = ["/Users/yourname/.B12/hooks/scripts/b12_mcp_server.py"]
enabled = true
startup_timeout_sec = 30

[mcp_servers.B12.env]
MCP_EMBEDDING_MODEL = "paraphrase-multilingual-MiniLM-L12-v2"
MCP_MAX_RESPONSE_CHARS = "40000"
```

Replace `/Users/yourname` with your actual home directory.

### Verify Codex Installation

```bash
# Start Codex CLI, then type:
/mcp
# Should show: B12 · connected
```

### Differences from Claude Code

| Feature | Claude Code | Codex CLI |
|---------|-------------|-----------|
| MCP tools (store/search/update/quality) | Automatic | Automatic |
| Per-prompt memory retrieval | Automatic (hook) | Manual (model follows AGENTS.md instructions) |
| Session summary extraction | Automatic (hook) | Not yet (planned for Layer 2) |
| Tag auto-injection | Automatic (hook) | Manual (model follows instructions) |
| Working context tracking | Automatic (hook) | Not available |

The 4 MCP tools work identically on both platforms. The difference is in automation — Claude Code hooks handle retrieval and storage silently, while Codex relies on AGENTS.md instructions to guide the model.
