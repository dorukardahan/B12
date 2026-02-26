# B12 — Persistent Memory System for Claude Code

A local-first, fully automated memory system that makes Claude Code remember everything across sessions. No cloud, no API keys, no manual effort — just persistent context that gets smarter over time.

Built for developers who use Claude Code daily and want it to remember their decisions, patterns, preferences, and lessons learned — without ever being asked.

## How It Works

```
Claude Code Session
    │
    ├── SessionStart ──────────> Inject: user profile + last session summary
    │                             + scope instructions + memory pre-fetch
    │                             + cross-project hints + feedback alerts
    │
    ├── UserPromptSubmit ──────> Ebbinghaus decay-aware memory retrieval
    │                             (FTS5 hybrid: 0.3×decay + 0.3×importance + 0.4×BM25)
    │
    ├── PreToolUse ────────────> Auto-inject scope tags on memory_store
    │                             (proj:<name>, user:<setup>)
    │
    ├── [Claude works, uses B12 MCP tools silently]
    │
    ├── PostToolUse ───────────> Track memory usage patterns (feedback)
    │                             + Track active/modified files (Working Memory)
    │
    ├── PreCompact ────────────> Stage comprehensive transcript summary
    │                             (priority-weighted, token-budgeted)
    │
    ├── SessionStart(compact) ─> Recover staged context + Working Memory
    │
    └── SessionEnd ────────────> Extract session summary (latest + rolling)
                                  + micro-memory extraction via write-time merge
                                  + background embedding generation
                                  ▼
B12 MCP Server (b12_mcp_server.py)
    │
    ├── 4 tools: memory_store / memory_search / memory_update / memory_quality
    ├── SQLite + sqlite-vec (local database, no cloud)
    ├── Embed daemon (sentence-transformers, Unix socket IPC)
    ├── FTS5 hybrid search (BM25 keyword + vector cosine)
    ├── Ebbinghaus strength decay (spaced repetition)
    ├── Write-time semantic merge (cosine > 0.85 = merge, not duplicate)
    └── Auto-backup (daily, 7-day rotation)
```

## Prerequisites

- **Python 3.11+** — required for the MCP server and embedding model
- **Claude Code** or **Codex CLI** — the AI coding assistant B12 extends
- **jq** — used by hooks for JSON processing (`brew install jq` on macOS)
- **sqlite3** — used for memory database operations (pre-installed on macOS)

## Setup via AI Assistant

If you're feeding this repo to Claude Code, Cursor, or another AI coding assistant to set it up for you, here's what the AI needs to do:

```bash
# 1. Clone and run full installer (one command does everything)
git clone https://github.com/dorukardahan/B12.git
cd B12 && chmod +x install.sh && ./install.sh --full

# 2. Restart Claude Code and verify
# Run /mcp in Claude Code — should show: B12 · connected (4 tools)
```

That's it. The `--full` flag creates the Python venv, installs all dependencies, deploys hooks, and configures the MCP server in `~/.claude.json` with correct absolute paths. The database and tables are created automatically on first use.

**Verification checklist for AI assistants:**
- `~/.local/b12-venv/bin/python3 -c "import mcp; print('OK')"` → should print OK
- `ls ~/.B12/hooks/memory-session-start.sh` → should exist
- `python3 -c "import json; c=json.load(open('$HOME/.claude.json')); print('B12' in c.get('mcpServers',{}))"` → should print True

## Features

- **Cross-session memory** — automatically captures decisions, errors, learnings, preferences at session end
- **Semantic + full-text search** — hybrid FTS5/vector retrieval finds memories by meaning or keywords
- **Ebbinghaus decay** — frequently accessed memories strengthen, unused ones fade (but never disappear)
- **Write-time merge** — deduplicates at storage time (cosine > 0.85 triggers merge, not insert)
- **Contradiction detection** — ONNX NLI model flags conflicting memories
- **Memory graph** — related/follows/contradicts edges between memories
- **Scope system** — 4 scopes (project, universal, preference, setup) with automatic tagging
- **Working Memory** — tracks active files and search patterns, restored after context compaction
- **B12 pill notifications** — visible inline indicators when memories are stored or retrieved
- **Multi-setup support** — works across `.claude`, `.claude-work`, etc. with shared database
- **Codex CLI support** — same MCP server works with OpenAI Codex CLI (shared memory database)
- **Zero config after install** — hooks handle everything silently in the background
- **Fully local** — no cloud, no API calls, all data stays on your machine

## Quick Start

### 1. One-command setup (recommended)

```bash
git clone https://github.com/dorukardahan/B12.git
cd B12
chmod +x install.sh
./install.sh --full       # Creates venv, installs deps, deploys hooks, configures MCP
# or: ./install.sh --full --all   # Same, but for all ~/.claude* setups
```

This single command:
- Creates `~/.local/b12-venv` with all Python dependencies
- Deploys hooks and scripts to `~/.B12/hooks/`
- Adds the B12 MCP server to `~/.claude.json` (with correct absolute paths)
- Verifies the installation

### 2. Restart Claude Code

Start a new session. Run `/mcp` — you should see `B12 · connected` with 4 tools:
- `memory_store` — store a memory with metadata and tags
- `memory_search` — hybrid semantic + full-text search
- `memory_update` — update metadata, tags, or strength
- `memory_quality` — rate, get, or analyze memory quality

**First run note:** The embedding model (~90MB) downloads automatically on the first session. This is a one-time download — subsequent sessions start instantly.

The database and all tables are created automatically on first use. After your first session ends, check `~/.B12/memory-summaries/` for the generated summary.

### 3. Manual setup (alternative)

If you prefer step-by-step control, see [docs/setup.md](docs/setup.md) for the full installation guide with individual steps.

**Important for manual MCP config:** Claude Code does NOT expand `~` in `~/.claude.json`. Use absolute paths:

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

Replace `/Users/yourname` with your actual home directory (`echo $HOME`).

### Codex CLI Support

B12 also works with OpenAI's Codex CLI. The same MCP server and SQLite database are shared — memories stored in Claude Code sessions are searchable in Codex, and vice versa.

```bash
# Install B12 for Codex CLI (requires existing venv)
./install.sh --codex

# Or full setup from scratch (creates venv + Claude Code + Codex)
./install.sh --full --codex
```

This adds the B12 MCP server to `~/.codex/config.toml`, appends memory instructions to `~/.codex/AGENTS.md`, configures the notify hook for session-end processing, and installs the B12 skill. Restart Codex CLI and type `/mcp` to verify.

**How it works:** The `notify` hook fires after each agent turn. A 2-minute debounce detects session end, then processes the rollout JSONL to extract session summaries, decisions, errors, and learnings. The B12 Codex Skill also instructs the model to proactively search/store memory.

### 4. Optional — Automated tasks

Copy the launchd plists to enable daily backup, consolidation, and weekly audits:

```bash
cp config/launchd-*.plist config/com.b12.graph-enrich.plist ~/Library/LaunchAgents/
# Edit each plist to replace /path/to/home with your actual home directory
sed -i '' "s|/path/to/home|$HOME|g" ~/Library/LaunchAgents/launchd-*.plist ~/Library/LaunchAgents/com.b12.graph-enrich.plist
launchctl load ~/Library/LaunchAgents/launchd-*.plist ~/Library/LaunchAgents/com.b12.graph-enrich.plist
```

See `docs/setup.md` for the full installation guide.

## Project Structure

```
B12/
├── hooks/                          # Lifecycle hook scripts (deployed to ~/.B12/hooks/)
│   ├── memory-session-start.sh     #   SessionStart — inject context
│   ├── memory-retrieval.sh         #   UserPromptSubmit — per-message retrieval
│   ├── memory-tag-enforce.sh       #   PreToolUse — auto-inject scope tags
│   ├── memory-feedback.sh          #   PostToolUse — track memory usage
│   ├── memory-working-context.sh   #   PostToolUse — track active files
│   ├── memory-precompact.sh        #   PreCompact — stage transcript summary
│   ├── memory-session-end.sh       #   SessionEnd — extract & persist memories
│   ├── memory-backup.sh            #   Scheduled — daily WAL-safe backup
│   ├── memory-consolidate.py       #   Scheduled — dedup, stale detection
│   ├── memory-quality-audit.sh     #   Scheduled — weekly health score
│   ├── memory-feedback-digest.sh   #   Scheduled — weekly usage digest
│   ├── memory-browse.sh            #   Manual — CLI memory browser
│   └── b12-codex-notify.sh         #   Codex — notify hook (session-end debounce)
├── scripts/                        # Support modules
│   ├── b12_mcp_server.py           #   Custom FastMCP server (replaces mcp-memory-service)
│   ├── embed_daemon.py             #   Background embedding daemon (Unix socket)
│   ├── write_time_merge.py         #   Semantic dedup at write time
│   ├── ebbinghaus.py               #   Decay scoring utilities
│   ├── contradiction_resolver.py   #   ONNX NLI contradiction detection
│   ├── graph_enrich.py             #   Memory graph enrichment
│   ├── shared_patterns.py          #   Shared regex patterns (EN + TR)
│   ├── transcript_adapter.py       #   Unified transcript parser (Claude + Codex)
│   ├── codex_session_end.py        #   Codex session-end memory extraction
│   ├── hook_adapter.py             #   Codex CLI hook adapter (translates Codex events to B12)
│   ├── embedding_backfill.py       #   Backfills embeddings for memories without vectors
│   ├── migrate_ebbinghaus.py       #   Migration: add strength fields
│   └── migrate_v10_13.py           #   Migration: create native FTS5 table
├── skills/                         # Agent skills
│   └── b12/SKILL.md               #   B12 Codex Skill (memory workflow)
├── config/                         # Template configuration files
│   ├── mcp-b12-template.json       #   MCP server config for ~/.claude.json
│   ├── settings-template.json      #   Hook config for settings.json
│   ├── codex-config-template.toml  #   MCP server config for Codex config.toml
│   ├── codex-agents-template.md    #   B12 instructions for Codex AGENTS.md
│   ├── launchd-*.plist             #   macOS scheduled task agents
│   └── com.b12.graph-enrich.plist  #   launchd plist for graph enrichment
├── templates/
│   └── user-profile.md             #   User profile template
├── benchmarks/
│   └── locomo/                     #   LoCoMo retrieval evaluation
├── docs/
│   ├── architecture.md             #   Detailed architecture documentation
│   └── setup.md                    #   Step-by-step installation guide
├── install.sh                      #   One-command installer
└── CHANGELOG.md                    #   Version history
```

## Configuration

### MCP Server (`~/.claude.json`)

The B12 MCP server is a custom FastMCP server (`b12_mcp_server.py`) that replaces the old `mcp-memory-service`. It runs in a dedicated Python venv at `~/.local/b12-venv/`.

Environment variables:
- `MCP_EMBEDDING_MODEL` — sentence-transformer model name (default: `paraphrase-multilingual-MiniLM-L12-v2`)
- `MCP_MAX_RESPONSE_CHARS` — max chars in search results (default: `40000`)

### Hooks (Claude Code `settings.json`)

All 7 hook events are configured via `config/settings-template.json`. The installer merges this into your `settings.json` automatically. See `docs/setup.md` for manual configuration.

### Multi-setup

If you run multiple Claude Code setups (e.g., personal + work):
- **MCP server** is global (configured in `~/.claude.json`)
- **Hooks** are deployed to `~/.B12/hooks/` (shared location)
- **Database** is shared — memories from any project are available everywhere
- **Session summaries** are per-project, so they don't overwrite each other
- **Hook config** needs to be in each setup's `settings.json`
- **Install**: `./install.sh --all` handles all setups

### Environment variables

| Variable | Controls | Default | Example |
|----------|----------|---------|---------|
| `B12_DATA_DIR` | Data/state: summaries, staging, logs | `~/.B12` | `~/.B12-work` |
| `B12_HOOK_DIR` | Hook code: script imports, embed daemon | `~/.B12/hooks` | (rarely needed) |
| `B12_WORK_PATTERN` | Work setup detection pattern | (none) | `mycompany` |

**Important:** `B12_DATA_DIR` and `B12_HOOK_DIR` are separate by design. Data can be per-setup while hook code stays shared. Set them in your setup's `settings.json`:
```json
{
  "env": {
    "B12_DATA_DIR": "~/.B12-work"
  }
}
```

### Context injection limits

SessionStart injects behavioral instructions + variable data (profile, session summary, pre-fetch, etc.). A hard cap of **6000 characters** prevents context bloat. When exceeded, variable sections are trimmed in priority order: pre-fetch first, then cross-project hints, then feedback digest, then hard truncation.

## How Memory Works

**Session start** — the SessionStart hook loads your user profile, last session's summary, cross-project hints, and pre-fetches relevant memories from the database using FTS5 + tag queries. All of this is injected as `additionalContext`.

**During conversation** — every user message triggers the retrieval hook, which extracts keywords, runs hybrid FTS5/vector search with Ebbinghaus decay scoring, and injects the top results. The PreToolUse hook ensures every `memory_store` call has proper scope tags.

**Session end** — the SessionEnd hook parses the full transcript, extracts decisions/errors/learnings/preferences using regex patterns (English + Turkish), generates embeddings in the background, and stores micro-memories with write-time dedup.

**Between sessions** — scheduled tasks run daily backup, consolidation (Jaccard dedup), and weekly quality audits. Unused memories decay in strength (-0.05/week), while frequently accessed ones strengthen (+0.3 per retrieval).

## Memory Layers

| Layer | What | Where | Best for |
|-------|------|-------|----------|
| **MEMORY.md** | Built-in auto-memory | `~/.claude/projects/*/memory/` | Stable project knowledge |
| **B12 MCP Server** | Semantic search DB | `~/Library/Application Support/mcp-memory/` | Detailed learnings, decisions |
| **Smart hooks** | Lifecycle automation | `~/.B12/hooks/` | Glue between all layers |
| **Session summaries** | Per-project latest + history | `~/.B12/memory-summaries/` | Short-term continuity |
| **User profile** | Persistent identity | `~/.claude/projects/*/memory/user-profile.md` | Personalization |
| **Working Memory** | Conversation momentum | `~/.B12/memory-staging/working-memory.json` | Post-compaction recovery |

## Changelog (recent)

### v10.0 (2026-02-20) — Custom MCP Server & Documentation Overhaul

- **Replaced mcp-memory-service** with `b12_mcp_server.py` — custom FastMCP server, 400 lines vs 804MB pipx package
- **MCP server renamed** from `"memory"` to `"B12"` in all configs
- **Tool names**: `mcp__memory__*` → `mcp__B12__*`
- **Embed daemon** (`embed_daemon.py`) — background process handles all ML ops via Unix socket
- **B12 pill notifications** — visible inline indicators for memory operations
- **Documentation overhaul** — README, setup guide, and architecture docs fully updated
- See [CHANGELOG.md](CHANGELOG.md) for full history

### v9.1 (2026-02-16) — MCP SDK Validation Fix

- Patched intermittent `memory_store` validation error from MCP SDK

### v9.0 (2026-02-16) — mcp-memory-service v10.13.0 Migration

- B12 hooks fully independent of server-side code
- Native FTS5 migration for existing databases

See [CHANGELOG.md](CHANGELOG.md) for the complete version history (v1–v10.0).

## License

MIT
