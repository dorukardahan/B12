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

### Multi-platform flags

```bash
./install.sh --continue        # Continue.dev (VS Code / JetBrains extension)
./install.sh --cline           # Cline + hooks under ~/Documents/Cline/Hooks/
./install.sh --smoke-cron      # Opt-in 24h smoke harness via user crontab
./install.sh --smoke-cron-uninstall  # Remove the smoke cron entry
```

`--smoke-cron` edits **only your user crontab** (no `launchctl` admin write) and is fully reversible. The harness logs to `~/.B12/memory-logs/smoke-YYYYMMDD.log`. If every detected `~/.claude*` setup reports a missing hook, the cron exits 1 to surface damaged installs.

### `[recall.ann]` config (`~/.B12/config.toml`)

The installer seeds `~/.B12/config.toml` from `config/b12-config-template.toml` on first run (never overwritten on subsequent installs):

```toml
[recall.ann]
enabled = true            # exact-KNN (sqlite-vec MATCH) recall — default-on since 2026-06-19
threshold_count = 500     # ANN activates once memory_embeddings reaches this many rows
```

**Default-on since 2026-06-19** (A/B harness: `benchmarks/ann_ab_test.py`). sqlite-vec's `MATCH` is exact brute-force KNN over normalized vectors, so it reproduces the full-table cosine ranking exactly (overlap@5 = 1.00) while removing the `ORDER BY m.id DESC LIMIT 500` blind spot; `threshold_count = 500` matches that cap boundary (at/below it the full-scan already sees everything). Set `enabled = false` to force the legacy full-scan path. When `B12_DATA_DIR` is set, the template is seeded under `$B12_DATA_DIR/config.toml` so a custom-data-dir setup gets a template at the path `scripts/b12_config.py` actually reads. ANN errors at any stage fall through to the full-scan path.

### Environment variables (multi-setup)

| Variable | Controls | Default |
|----------|----------|---------|
| `B12_DATA_DIR` | Data/state: summaries, staging, logs | `~/.B12` |
| `B12_HOOK_DIR` | Hook code: script imports, embed daemon | `~/.B12/hooks` |
| `B12_WORK_PATTERN` | Work setup detection pattern | (none) |
| `B12_IDLE_TIMEOUT_SECONDS` | SessionEnd idle-timeout skip threshold (s); `0` disables | `1800` |

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
        "MCP_EMBEDDING_MODEL": "BAAI/bge-m3",
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

**First run note:** The default BGE-M3 embedding model (~2.2GB FP32 weights) downloads automatically on the first session start. This is a one-time download; subsequent sessions start instantly. For a smaller footprint, first install the optional backend from the repository with `~/.local/b12-venv/bin/pip install -e '.[gguf]'`, then set `B12_EMBED_BACKEND=gguf` with `B12_EMBED_GGUF_PATH=...` to use a Q4_K_M (~438MB) or Q8_0 (~635MB) GGUF. The database and all tables are created automatically by the MCP server on first use.

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
./install.sh --all    # Re-deploys hooks + scripts, and reloads the MCP daemon if running
# Restart Claude Code to pick up changes
```

> **macOS MCP daemon:** if the shared MCP daemon (`com.b12.mcp.daemon`) is
> running, the installer now restarts it automatically on any script-copying run
> (`--all`/`--full`/`--codex`/bare/…) so freshly-pulled daemon code
> (`b12_mcp_daemon.py` / `b12_mcp_server.py`) takes effect — the long-lived
> launchd process otherwise keeps serving the old in-memory code. (`--daemon`
> handles its own reload; `--daemon-uninstall` removes it.)
> Active sessions see a brief socket drop during the reload (the stdio proxy
> reconnects automatically; an in-flight memory call may fail — just retry).
> To reload it manually: `launchctl unload ~/Library/LaunchAgents/com.b12.mcp.daemon.plist && launchctl load ~/Library/LaunchAgents/com.b12.mcp.daemon.plist`.

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
MCP_EMBEDDING_MODEL = "BAAI/bge-m3"
MCP_MAX_RESPONSE_CHARS = "40000"

# Skip the per-call approval prompt for memory_store (B12's silent
# batch-write tool). install.sh --codex writes this automatically.
[mcp_servers.B12.tools.memory_store]
approval_mode = "auto"
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

### Codex hook migration: `notify` → `Stop`

Codex CLI **0.130.0** (hooks GA on 2026-05-14) replaces the legacy
root-level `notify = [...]` config knob with the formal `Stop` hook event.
B12 ships both paths so existing installs keep working:

| Codex version | Path | What B12 ships |
|---------------|------|----------------|
| < 0.130.0     | root-level `notify = ["<...>/b12-codex-notify.sh"]` in `config.toml` | `hooks/b12-codex-notify.sh` — debounced rollout post-scrape (legacy) |
| ≥ 0.130.0     | `[hooks.events.Stop]` block in `hooks.json` plus `[hooks.state]` SHA-256 pinning | `hooks/memory-codex-*.sh` family (Stop, SessionStart, UserPromptSubmit, PreToolUse, PostToolUse, PreCompact) |

Both paths are safe to leave configured simultaneously — Codex 0.130.0
honors `notify` for backwards compatibility while preferring the formal
hook events when present. Upgrade by running `./install.sh --codex`
again; the new hooks will be deployed and pre-pinned into
`[hooks.state]` (Round 0 fix #1 — SHA-256 trusted_hash entries skip
Codex's StartupHooksReview prompt-trust flow).

**Live-session caveat (issue #21160).** Editing `~/.codex/hooks.json` or
`~/.codex/config.toml` while a Codex session is live silent-disables ALL
hooks for the rest of that session, even with valid file content.
`install.sh --codex` and `--all` now run `pgrep -f codex` and warn you
if a session is open — restart any open Codex windows after re-running
the installer so the new hook config actually loads.

## Cross-Platform Setup (Other Platforms)

B12 ships an `install.sh --<platform>` flag for each supported host. All
platforms share the same MCP server, SQLite database, and embedding daemon
— so memories stored from one client surface in searches from any other.
Pick the flag matching your editor / CLI, run it once, restart the host.

| Platform | Install flag | Config template | Verify |
|----------|--------------|-----------------|--------|
| Gemini CLI | `./install.sh --gemini` | `config/gemini-config-template.json` + `config/gemini-instructions-template.md` | `gemini /mcp` |
| VS Code (GitHub Copilot Chat) | `./install.sh --vscode` | `config/mcp-b12-template.json` + `config/vscode-instructions-template.md` | Copilot Chat → "Show MCP servers" |
| Cursor | `./install.sh --cursor` | `config/cursor-mcp-template.json` + `config/cursor-rules-template.md` | Cursor Settings → MCP → B12 row |
| Kimi Code | `./install.sh --kimi` | `config/kimi-mcp-template.json` + `config/kimi-agents-template.md` | `kimi /mcp` |
| Windsurf | `./install.sh --windsurf` | `config/mcp-b12-template.json` (Cascade format) | Windsurf MCP panel |
| Cline (VS Code ext) | `./install.sh --cline` | `config/cline-mcp-template.json` + `config/cline-rules-template.md` + `config/cline-hooks/` | Cline panel → MCP Servers |
| OpenCode | `./install.sh --opencode` | `config/opencode-config-template.json` + `config/opencode-instructions-template.md` | `opencode /mcp` |
| Zed | `./install.sh --zed` | `config/mcp-b12-template.json` | Zed Settings → Context Servers |
| Amp | `./install.sh --amp` | `config/amp-settings-template.json` | Amp Settings → MCP |
| Grok CLI (xAI) | `./install.sh --grok` | `config/grok-config-template.toml` + `config/grok-instructions-template.md` | `grok /mcp` |
| Continue.dev (VS Code / JetBrains) | `./install.sh --continue` | `config/continue-mcp-template.yaml` + `config/continue-instructions-template.md` | Continue panel → MCP |
| JetBrains AI Assistant | (manual) | `config/jetbrains-ai-mcp-template.json` | JetBrains → AI Assistant → MCP |

### What each flag does

Each platform-specific flag performs the same minimal contract:

1. **Inject the B12 MCP server entry** into that platform's config file
   (`~/.gemini/settings.json`, `~/.codex/config.toml`, `~/.cursor/mcp.json`,
   `~/.vscode/settings.json`, `~/.windsurf/...`, etc.) using absolute paths
   (no `~` — Claude Code is not the only host that refuses tilde expansion).

2. **Append memory instructions** to that platform's system-prompt file
   (`AGENTS.md`, `instructions.md`, `.cursorrules`, etc.) wrapped between
   `<!-- B12-MEMORY-START -->` and `<!-- B12-MEMORY-END -->` markers so
   re-runs are idempotent and uninstall is a clean sed delete.

3. **Wire up host-specific hooks** if the platform exposes a hook surface
   (Cline `hooks/`, Codex `[hooks.events.*]` blocks). Hooks are
   intentionally minimal on non-Claude-Code platforms — most retrieval
   happens via MCP tool calls rather than implicit hooks.

### What gets automated vs. manual per platform

| Surface | Claude Code | Codex (≥0.130) | Cline | Other platforms |
|---------|-------------|----------------|-------|-----------------|
| Memory store (MCP tool) | Automatic | Automatic | Automatic | Automatic |
| Memory search (MCP tool) | Automatic | Automatic | Automatic | Automatic |
| Pre-prompt retrieval (hook) | ✓ silent | ✓ via `UserPromptSubmit` hook | ✓ via `userPromptSubmit` hook | Model follows instructions |
| Session-end summary extraction | ✓ silent | ✓ via `Stop` hook | ✗ | ✗ |
| Tag auto-injection on store | ✓ silent | ✓ via `PreToolUse` hook | ✓ | Model follows instructions |
| Working-context tracking on tool use | ✓ silent | ✓ via `PostToolUse` hook | ✗ | ✗ |
| `/mcp` verification | ✓ | ✓ | Cline panel | Host-specific UI |

Claude Code remains the most automated surface because it ships the
richest hook event taxonomy (13 events). Codex caught up in 0.130 with
formal hooks. Other platforms rely on the model reading the
B12-MEMORY-START instructions block to invoke MCP tools at the right
moments.

### Shared database, shared state

All platforms point at the same SQLite database at the path resolved by
`B12_DATA_DIR` (default `~/.B12/sqlite_vec.db`). A memory stored from
Cursor surfaces in a Gemini search 30 seconds later; a session summary
written by Claude Code's SessionEnd hook shows up in Codex's SessionStart
context. The MCP server is single-instance — the first host that
launches it owns the process; subsequent hosts connect via the same
Unix socket (`B12_MCP_DAEMON_SOCK`, default `/tmp/b12-mcp-$UID.sock`).

### Manual installation (if `install.sh` doesn't cover your host)

If your host isn't in the flag list above but supports MCP over stdio,
the minimum viable wiring is:

1. Copy `config/mcp-b12-template.json` and replace `__VENV_PYTHON__` with
   the output of `which python3` from your B12 venv, and replace
   `__B12_SCRIPT__` with the absolute path to
   `~/.B12/hooks/scripts/b12_mcp_server.py`.

2. Inject the resulting JSON object into your host's MCP server config
   (path varies — check your host's docs).

3. Restart the host, run its `/mcp` equivalent, confirm B12 shows up
   with 13 tools.

The MCP protocol is stable across hosts — the same server process works
identically whether the caller is Claude Code, Codex, Cursor, or a
custom client.

### Troubleshooting non-Claude-Code platforms

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| MCP server shows disconnected on a non-Claude-Code host | Host expanded `~` in path | Replace `~` with absolute path in that host's config |
| Memories store but never retrieve | Host doesn't auto-call `memory_search` | Add the memory-instructions template to your host's system prompt |
| Embed daemon not starting on Linux | `/tmp/b12-embed-$UID.sock` permission | Check `umask`; daemon writes with mode `0o600` |
| Multiple hosts can't connect simultaneously | First host crashed leaving stale socket | `rm /tmp/b12-mcp-$UID.sock && rm /tmp/b12-embed-$UID.sock` and let next host re-spawn |
