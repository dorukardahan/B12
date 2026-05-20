# B12 — Persistent Memory System for AI Coding Assistants

A local-first, fully automated memory system that makes your AI coding assistant remember everything across sessions. No cloud, no API keys, no manual effort — just persistent context that gets smarter over time.

Works with Claude Code, Codex CLI, Gemini CLI, VS Code/Copilot, Cursor, Kimi Code, Windsurf, Cline, and OpenCode — all sharing the same memory database.

## How It Works

The hook-based automation below runs in Claude Code. Other platforms use the MCP server directly with static instruction files — see [Supported Platforms](#supported-platforms).

```
Claude Code Session (full hook automation)
    │
    ├── SessionStart ──────────> Inject: sprint handoff / last session summary
    │                             + scope instructions + memory pre-fetch
    │                             + cross-project hints + feedback alerts
    │                             + content guardrails + version compat
    │                             + setup routing warnings
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
    │                             + direct SQLite store for high-value items
    │
    ├── SessionStart(compact) ─> Recover staged context + Working Memory
    │
    └── SessionEnd ────────────> Extract session summary (latest + rolling)
                                  + micro-memory extraction via write-time merge
                                  + sprint handoff generation
                                  + identity correction cascade
                                  + infra/content pattern extraction
                                  + host version tracking
                                  + background embedding generation
                                  ▼
B12 MCP Server (b12_mcp_server.py)
    │
    ├── 5 tools: memory_store / memory_search / memory_update / memory_quality / memory_session_context
    ├── 4 resources: b12://context/project/{name} / b12://stats / b12://profile / b12://health
    ├── SQLite + sqlite-vec (local database, no cloud)
    ├── Embed daemon (sentence-transformers, Unix socket IPC)
    ├── FTS5 hybrid search (BM25 keyword + vector cosine + porter stemming)
    ├── Ebbinghaus strength decay (spaced repetition)
    ├── Write-time semantic merge (cosine > 0.85 = merge, not duplicate)
    └── Auto-backup (daily, 7-day rotation)
```

## Prerequisites

- **Python 3.11+** — required for the MCP server and embedding model
- **Claude Code** or any supported AI coding assistant (see table below)
- **jq** — used by hooks for JSON processing (`brew install jq` on macOS)
- **sqlite3** — used for memory database operations (pre-installed on macOS)

## Supported Platforms

B12's MCP server works with any tool that supports MCP stdio. The installer handles config for all of these:

| Platform | Flag | MCP Config Location | Instructions File |
|----------|------|--------------------|--------------------|
| Claude Code | (default) | ~/.claude.json | Built-in |
| Codex CLI | `--codex` | ~/.codex/config.toml | ~/.codex/AGENTS.md |
| Gemini CLI | `--gemini` | ~/.gemini/settings.json | ~/.gemini/GEMINI.md |
| VS Code / Copilot | `--vscode` | ~/Library/.../Code/User/mcp.json | .github/copilot-instructions.md * |
| Cursor | `--cursor` | ~/.cursor/mcp.json | ~/.cursor/rules/b12-memory.mdc |
| Kimi Code | `--kimi` | ~/.kimi/mcp.json | ~/.kimi/AGENTS.md |
| Windsurf | `--windsurf` | ~/.codeium/windsurf/mcp_config.json | ~/.codeium/.../global_rules.md |
| Cline | `--cline` | VS Code globalStorage/.../cline_mcp_settings.json | ~/Documents/Cline/Rules/b12-memory.md + ~/Documents/Cline/Hooks/ (TaskStart, UserPromptSubmit, PreCompact with active staging, TaskComplete) |
| Continue.dev | `--continue` | ~/.continue/mcpServers/b12.yaml | ~/.continue/rules/b12-memory.md |
| OpenCode | `--opencode` | ~/.config/opencode/opencode.json | ~/.config/opencode/AGENTS.md + TypeScript plugin (auto-deployed) |

\* VS Code/Copilot instructions are per-project (`.github/copilot-instructions.md`). The installer creates a template in the B12 repo — copy it to each project where you want B12 active.

\** OpenCode also receives a TypeScript plugin (deployed to `~/.config/opencode/plugins/b12/`) for full lifecycle hooks (SessionStart, PreCompact, SessionEnd, tool tracking, memory retrieval). Requires Bun runtime.

All platforms share the same SQLite database — memories stored in one session are searchable in all others.

## Setup via AI Assistant

If you're feeding this repo to Claude Code, Cursor, or another AI coding assistant to set it up for you, here's what the AI needs to do:

```bash
# 1. Clone and run full installer (one command does everything)
git clone https://github.com/dorukardahan/B12.git
cd B12 && chmod +x install.sh && ./install.sh --full

# 2. Restart Claude Code and verify
# Run /mcp in Claude Code — should show: B12 · connected (5 tools)
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
- **Proactive surfacing** — automatically injects relevant past memories when you open files or hit errors
- **Long-session re-surface** — on every Nth UserPromptSubmit turn (default 20), re-injects a small batch of THIS session's early-captured high-importance memories so they don't fade out of the model's effective working window
- **Token budget guardrails** — per-turn char-based cap (~800 tokens) + cumulative session cap (~80K tokens) with skip-event logging to `~/.B12/memory-logs/token-budget-skips.jsonl`
- **Smart consolidation** — deduplication, merge groups, and NLI contradiction detection across memories
- **Export/import** — portable `.b12` format for backup, migration, or sharing memory snapshots
- **Web dashboard** — Flask + Cytoscape.js visual browser for memory graph and statistics
- **Health report** — comprehensive weekly report with health score, trends, and recommendations
- **Porter stemming search** — `memory_fts_stemmed` table matches word variants ("running" → "run")
- **MCP resources** — `b12://` URIs for protocol-standard context access (stats, profile, health)
- **Gemini CLI hooks** — adapter scripts that give Gemini CLI full B12 hook integration
- **LoCoMo benchmark** — retrieval quality evaluation with MRR, NDCG, and regression detection
- **Multi-setup support** — works across `.claude`, `.claude-work`, etc. with shared database
- **Multi-platform support** — Claude Code, Codex, Gemini, VS Code, Cursor, Kimi, Windsurf, Cline, OpenCode (with TypeScript plugin for full lifecycle automation)
- **Zero config after install** — hooks handle everything silently in the background
- **Fully local** — no cloud, no API calls, all data stays on your machine

## Quick Start

### Option A: Claude Code Plugin (recommended for Claude Code users)

```bash
# 1. Add B12 marketplace
/plugin marketplace add dorukardahan/B12

# 2. Install the plugin
/plugin install b12-memory@b12-memory
```

This installs B12 as a Claude Code plugin with hooks, MCP server, skills, and slash commands — all auto-configured. You still need the Python venv for the MCP server:

```bash
git clone https://github.com/dorukardahan/B12.git
cd B12 && ./install.sh --full  # Creates venv + deps only (hooks/MCP managed by plugin)
```

After installation, you get:
- `/b12-search` — search your memory
- `/b12-store` — store a memory
- `/b12-status` — health check

For local plugin development: `claude --plugin-dir /path/to/B12`

### Option B: Script install (recommended for multi-platform)

```bash
git clone https://github.com/dorukardahan/B12.git
cd B12
chmod +x install.sh
./install.sh --full       # Creates venv, installs deps, deploys hooks, configures MCP
# or: ./install.sh --full --all           # Same, but for all ~/.claude* setups
# or: ./install.sh --full --gemini --cursor  # Setup + Gemini CLI + Cursor
```

This single command:
- Creates `~/.local/b12-venv` with all Python dependencies
- Deploys hooks and scripts to `~/.B12/hooks/`
- Adds the B12 MCP server to `~/.claude.json` (with correct absolute paths)
- Verifies the installation

### 2. Restart your AI assistant

Start a new Claude Code session. Run `/mcp` — you should see `B12 · connected` with 5 tools:
- `memory_store` — store a memory with metadata and tags
- `memory_search` — hybrid semantic + full-text search
- `memory_update` — update metadata, tags, or strength
- `memory_quality` — rate, get, or analyze memory quality
- `memory_session_context` — get session start context (project memories, last summary, instructions)

**First run note:** The default embedding model (BGE-M3, ~2.2GB FP32 weights) downloads automatically on the first session. This is a one-time download — subsequent sessions start instantly. Set `B12_EMBED_BACKEND=gguf` + `B12_EMBED_GGUF_PATH=...` to use a Q8_0 / Q4_K_M GGUF (~250-500MB) via `llama-cpp-python` instead.

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
        "MCP_EMBEDDING_MODEL": "BAAI/bge-m3",
        "MCP_MAX_RESPONSE_CHARS": "40000"
      }
    }
  }
}
```

Replace `/Users/yourname` with your actual home directory (`echo $HOME`).

### Multi-Platform Support

B12 works with any MCP-compatible coding assistant. The same MCP server and SQLite database are shared — memories stored in one platform are searchable in all others.

```bash
# Install B12 for additional platforms (requires existing venv)
./install.sh --codex         # OpenAI Codex CLI
./install.sh --gemini        # Google Gemini CLI
./install.sh --vscode        # VS Code / GitHub Copilot
./install.sh --cursor        # Cursor
./install.sh --kimi          # Kimi Code
./install.sh --windsurf      # Windsurf (Codeium)
./install.sh --cline         # Cline (VS Code extension + TaskStart/UserPromptSubmit hooks)
./install.sh --continue      # Continue.dev (VS Code/JetBrains extension)
./install.sh --opencode      # OpenCode
./install.sh --smoke-cron    # Opt-in 24h smoke harness via crontab (memory-session-start + memory-retrieval)
./install.sh --gc-cron       # Opt-in weekly soft-delete GC + VACUUM (--gc-cron-uninstall to remove)

# Or full setup from scratch with multiple platforms
./install.sh --full --codex --gemini --cursor
```

Each flag configures the platform's MCP config and injects B12 memory instructions into the platform's instruction file. Restart the platform and check its MCP status to verify.

**Codex CLI specifics:** The `notify` hook fires after each agent turn. A 2-minute debounce detects session end, then processes the rollout JSONL to extract session summaries, decisions, errors, and learnings. The B12 Codex Skill also instructs the model to proactively search/store memory.

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
├── .claude-plugin/                 # Claude Code plugin metadata
│   ├── plugin.json                 #   Plugin manifest (name, version, keywords)
│   └── marketplace.json            #   Marketplace catalog for discovery
├── .mcp.json                       # Plugin MCP server config (auto-loaded)
├── commands/                       # Slash commands (auto-discovered by plugin)
│   ├── b12-search.md               #   /b12-search — search memories
│   ├── b12-store.md                #   /b12-store — store a memory
│   └── b12-status.md               #   /b12-status — health check
├── hooks/                          # Lifecycle hook scripts
│   ├── hooks.json                  #   Plugin hook definitions (auto-loaded)
│   ├── scripts -> ../scripts       #   Symlink to support scripts (for plugin cache)
│   ├── memory-session-start.sh     #   SessionStart — inject context
│   ├── memory-retrieval.sh         #   UserPromptSubmit — per-message retrieval
│   ├── memory-tag-enforce.sh       #   PreToolUse — auto-inject scope tags
│   ├── memory-feedback.sh          #   PostToolUse — track memory usage
│   ├── memory-working-context.sh   #   PostToolUse — track active files
│   ├── memory-precompact.sh        #   PreCompact — stage transcript summary
│   ├── memory-session-end.sh       #   SessionEnd — extract & persist memories
│   ├── memory-proactive-surface.sh #   PostToolUse — proactive memory surfacing
│   ├── memory-checkpoint.sh        #   PostToolUse — mid-session memory capture (rate-limited)
│   ├── memory-instructions-loaded.sh # InstructionsLoaded — CLAUDE.md / rules load telemetry
│   ├── memory-file-changed.sh      #   FileChanged — capture user edits to CLAUDE.md/MEMORY.md
│   ├── memory-tool-failure.sh      #   PostToolUseFailure — capture tool errors as memories
│   ├── memory-turn-end.sh          #   Stop — end-of-turn response scan
│   ├── memory-prompt-expansion.sh  #   UserPromptExpansion — /goal lifecycle + slash log
│   ├── memory-subagent-stop.sh     #   SubagentStop — capture subagent findings
│   ├── memory-backup.sh            #   Scheduled — daily WAL-safe backup
│   ├── memory-consolidate.py       #   Scheduled — dedup, stale detection
│   ├── memory-quality-audit.sh     #   Scheduled — weekly health score
│   ├── memory-feedback-digest.sh   #   Scheduled — weekly usage digest
│   ├── memory-browse.sh            #   Manual — CLI memory browser
│   ├── b12-codex-notify.sh         #   Codex — notify hook (session-end debounce)
│   └── gemini/                     #   Gemini CLI hook adapters
│       ├── b12-gemini-session-start.sh  # SessionStart adapter
│       ├── b12-gemini-session-end.sh    # SessionEnd adapter (transcript conversion)
│       └── b12-gemini-tool-call.sh      # AfterTool adapter (memory retrieval)
├── scripts/                        # Support modules
│   ├── b12_mcp_server.py           #   Custom FastMCP server (5 tools + 4 resources)
│   ├── start-mcp.sh                #   MCP bootstrap (venv detection, used by plugin)
│   ├── embed_daemon.py             #   Background embedding daemon (Unix socket)
│   ├── write_time_merge.py         #   Semantic dedup at write time
│   ├── ebbinghaus.py               #   Decay scoring utilities
│   ├── contradiction_resolver.py   #   ONNX NLI contradiction detection
│   ├── graph_enrich.py             #   Memory graph enrichment
│   ├── consolidation_engine.py     #   Smart consolidation (dedup, merge, contradictions)
│   ├── surfacing_engine.py         #   Proactive memory surfacing engine
│   ├── b12_long_session.py         #   Q2 long-session re-surface (turn-counter + early-batch picker)
│   ├── b12_token_budget.py         #   T1/T2/T3 token budget guardrails (per-turn cap + cumulative + dedup ledger)
│   ├── b12_embed_quant_eval.py     #   S5 BGE-M3 quantization mini-bench (FP32 / Q8_0 / Q4_K_M)
│   ├── migrate_embed_to_bge_m3.py  #   384→1024 dim migration with .bak snapshot + WAL-safe rollback
│   ├── dashboard_server.py         #   Flask web dashboard backend
│   ├── export_import.py            #   Memory export/import (.b12 format)
│   ├── b12_health_report.py        #   Comprehensive health report generator
│   ├── b12_health.py               #   CLI health check diagnostics (v12)
│   ├── compat.json                 #   Host version compatibility database (v12)
│   ├── shared_patterns.py          #   Shared regex patterns + content hash (EN + TR)
│   ├── transcript_adapter.py       #   Unified transcript parser (Claude + Codex)
│   ├── codex_session_end.py        #   Codex session-end memory extraction
│   ├── hook_adapter.py             #   Codex CLI hook adapter (translates Codex events to B12)
│   ├── embedding_backfill.py       #   Backfills embeddings for memories without vectors
│   ├── query_aliases.json          #   Search query alias mappings
│   ├── migrate_ebbinghaus.py       #   Migration: add strength fields
│   ├── migrate_stemmed_fts.py      #   Migration: backfill porter-stemmed FTS5 table
│   └── migrate_v10_13.py           #   Migration: create native FTS5 table
├── skills/                         # Agent skills
│   ├── b12-memory/SKILL.md         #   B12 behavioral skill (plugin, comprehensive)
│   └── b12/SKILL.md               #   B12 Codex Skill (memory workflow)
├── config/                         # Template configuration files
│   ├── mcp-b12-template.json       #   MCP server config for ~/.claude.json
│   ├── settings-template.json      #   Hook config for settings.json
│   ├── codex-config-template.toml  #   MCP server config for Codex config.toml
│   ├── codex-agents-template.md    #   B12 instructions for Codex AGENTS.md
│   ├── gemini-*-template.*         #   Gemini CLI config + instructions
│   ├── vscode-*-template.*         #   VS Code / Copilot config + instructions
│   ├── cursor-*-template.*         #   Cursor config + rules
│   ├── kimi-*-template.*           #   Kimi Code config + instructions
│   ├── windsurf-*-template.*       #   Windsurf config + rules
│   ├── cline-*-template.*          #   Cline config + rules
│   ├── opencode-*-template.*       #   OpenCode config + instructions
│   ├── launchd-*.plist             #   macOS scheduled task agents
│   └── com.b12.graph-enrich.plist  #   launchd plist for graph enrichment
├── templates/
│   └── user-profile.md             #   User profile template
├── dashboard/
│   └── dashboard.html              #   Web dashboard frontend (Cytoscape.js graph)
├── benchmarks/
│   └── locomo/                     #   LoCoMo retrieval evaluation (MRR, NDCG)
├── docs/
│   ├── architecture.md             #   Detailed architecture documentation
│   └── setup.md                    #   Step-by-step installation guide
├── AGENTS.md                       #   Codex agent instructions (auto-deployed)
├── install.sh                      #   One-command installer
├── check.sh                        #   Pre-commit validation script
├── CHANGELOG.md                    #   Version history
├── package.json                    #   semantic-release config (CI versioning)
└── package-lock.json               #   npm lockfile
```

## Configuration

### MCP Server (`~/.claude.json`)

The B12 MCP server is a custom FastMCP server (`b12_mcp_server.py`) that replaces the old `mcp-memory-service`. It runs in a dedicated Python venv at `~/.local/b12-venv/`.

Environment variables:
- `MCP_EMBEDDING_MODEL` — sentence-transformer model name (default: `BAAI/bge-m3`; 1024-dim multilingual cls-pooled)
- `B12_EMBED_BACKEND` — `sentence-transformers` (default) or `gguf` (requires `llama-cpp-python`)
- `B12_EMBED_GGUF_PATH` — absolute path to a BGE-M3 GGUF file when `B12_EMBED_BACKEND=gguf`
- `B12_MAX_INJECT_TOKENS` — per-turn injection cap (default `800`, char proxy)
- `B12_MAX_SESSION_TOKENS` — cumulative per-session injection cap (default `80000` = ~8% of 1M)
- `MCP_MAX_RESPONSE_CHARS` — max chars in search results (default: `40000`)

### Hooks (Claude Code `settings.json`)

All 9 lifecycle hook events plus 2 telemetry/observability hooks (InstructionsLoaded, FileChanged) are configured via `config/settings-template.json`. The installer merges this into your `settings.json` automatically. See `docs/setup.md` for manual configuration.

### Multi-setup

If you run multiple Claude Code setups (e.g., personal + work):
- **MCP server** is global (configured in `~/.claude.json`)
- **Hooks** are deployed to `~/.B12/hooks/` (shared location)
- **Database** is shared — memories from any project are available everywhere
- **Session summaries** are per-project, so they don't overwrite each other
- **Hook config** needs to be in each setup's `settings.json`
- **Install**: `./install.sh --all` handles all setups
- **Drift auto-fix**: `./install.sh --all --fix-drift` auto-registers B12 on any detected non-Claude platform (Codex/Gemini/Kimi/Cursor/Windsurf/OpenCode/Grok) whose MCP config is missing a B12 entry. Default behavior remains warn-only — `--fix-drift` is opt-in.

### Environment variables

| Variable | Controls | Default | Example |
|----------|----------|---------|---------|
| `B12_DATA_DIR` | Data/state: summaries, staging, logs | `~/.B12` | `~/.B12-work` |
| `B12_HOOK_DIR` | Hook code: script imports, embed daemon | `~/.B12/hooks` | (rarely needed) |
| `B12_WORK_PATTERN` | Work setup detection pattern | (none) | `mycompany` |
| `B12_IDLE_TIMEOUT_SECONDS` | SessionEnd idle-timeout skip threshold (seconds). When the SessionEnd `.reason` is not `clear` / `logout` / `prompt_input_exit` AND the transcript mtime is older than this, the hook short-circuits before heavy extraction — `idle_skip:true` is logged to `sessions.jsonl`. Set to `0` to disable. | `1800` (30min) | `3600` / `0` |
| `CONTINUE_GLOBAL_DIR` | Continue.dev settings directory override (Continue CLI honors this for `~/.continue/settings.json`). `install.sh --continue` writes hooks to `$CONTINUE_GLOBAL_DIR/settings.json` when set. | `~/.continue` | `~/.continue-work` |

#### LLM extraction (opt-in, default off)

The LLM extraction subagent runs at SessionEnd in a detached background process and writes through the same `merge_or_insert` path as regex extraction. Default-off; set `B12_LLM_PROVIDER` to enable.

| Variable | Controls | Default | Example |
|----------|----------|---------|---------|
| `B12_LLM_PROVIDER` | Provider selector. `none` disables LLM extraction entirely. | `none` | `anthropic` / `ollama` / `none` |
| `B12_LLM_MODEL` | Override provider's default model id. | provider default | `claude-haiku-4-5-20251001` / `qwen2.5:1.5b` |
| `ANTHROPIC_API_KEY` | Required when provider is `anthropic`. Never logged. | (unset) | `sk-ant-…` |
| `OLLAMA_HOST` | Ollama HTTP endpoint. Used when provider is `ollama`. | `http://127.0.0.1:11434` | `http://localhost:11434` |
| `B12_LLM_TIMEOUT_S` | Background per-call timeout. Hook is already done; this only protects the detached worker. | `60` | `90` |
| `B12_LLM_MAX_MEMORIES` | Hard cap on memories returned per call. | `10` | `5` |
| `B12_LLM_TRANSCRIPT_CAP_CHARS` | Transcript chunk sent to the LLM. Ollama auto-caps at 25K when this is unset. | `50000` (Anthropic) / `25000` (Ollama) | `30000` |

Failure modes are silent: API outage, rate limit, missing key, or malformed output all log to `~/.B12/memory-logs/llm-extraction-errors.log` and the hook still exits 0. LLM-written memories carry `tag:llm-extracted` and `metadata.extraction_method=llm-<provider>` for downstream filtering. See `docs/B12_llm_extraction_design.md` for full architecture, cost estimate, and dedup logic.

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

**Between sessions** — scheduled tasks run daily backup, consolidation (Jaccard dedup), and weekly quality audits. Unused memories decay in strength (-0.05/week), while frequently accessed ones strengthen (+0.2 per retrieval).

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

### v11.47–v11.52 (2026-05-19) — Polyglot Cleanup + Self-Improve

- **C13 — 24h smoke harness** (`scripts/b12_smoke.sh` + `install.sh --smoke-cron`): opt-in cron job drives `memory-session-start.sh` + `memory-retrieval.sh` against every detected `~/.claude*` setup, flagging damaged installs (all-setups-missing → exit 1). Crontab-only, fully reversible via `--smoke-cron-uninstall`.
- **C14 — ANN index over `memory_embeddings`** (`scripts/embed_daemon.py` + `[recall.ann]` in `~/.B12/config.toml`): default-off `sqlite-vec` `MATCH` pre-filter that bypasses the LIMIT-500 cap once `COUNT(*) FROM memory_embeddings >= threshold_count` (default 10000). 30× oversample absorbs soft-delete + skip_ids + threshold attrition before falling through to full-scan.
- **SubagentStart per-agent recall** (`hooks/memory-subagent-start.sh` + `memory-team-create.sh` + `memory-session-start.sh`): `memory-team-create.sh` writes `~/.B12/state/team-<id>.json` on `TeamCreate` PostToolUse; `memory-session-start.sh` matches `CLAUDE_CODE_AGENT_ID` against `.members[]` and injects a TEAMMATES block when a teammate starts; `memory-subagent-start.sh` performs task-scoped recall with cold-daemon FTS5 fallback.
- **Cursor MDC + PageRank in SessionStart** (`scripts/cursor_mdc.py` + `scripts/file_pagerank.py`): SessionStart now surfaces `.cursor/rules/*.mdc` Auto-Attached rules whose `globs` match active files, plus the top-N files by PageRank over the project's `.py`/`.ts`/`.tsx`/`.js`/`.jsx` import graph (24h cache + git HEAD invalidation).
- **Continue.dev integration** (`install.sh --continue` + `config/continue-mcp-template.yaml` + `config/continue-instructions-template.md`): MCP server registration + behavioral rules + `transcript_adapter._parse_continue()` for single-file JSON sessions under `~/.continue/sessions/`.
- **Cline hooks** (`config/cline-hooks/{TaskStart,UserPromptSubmit,PreCompact}` → `~/Documents/Cline/Hooks/`): TaskStart ≡ Claude Code SessionStart(source=startup); UserPromptSubmit delegates to `memory-retrieval.sh`. Output `{cancel: false, contextModification: ...}` (camelCase wire key, verified against `cline/cline:src/core/hooks/templates.ts`). PreCompact is a passive placeholder until Cline upstream finalizes the `.transcript_path` shape.
- **Codex cloud_exec / cloud_apply ingestion** (`scripts/codex_session_end.py`): `_extract_cloud_tasks(info)` pairs `cloud_exec` with `cloud_apply` by `cloud_task_id` and emits `{cloud_task_id, task, status, files, branch}` rows. Gated on `B12_CODEX_CLOUD_INGEST` (default off).

### v11.42+ (2026-05-18) — Claude Code v2.1.139+ hook coverage

- **`UserPromptExpansion` hook** (`memory-prompt-expansion.sh`) —
  detects native `/goal <condition>` and `/goal clear|stop|off`
  expansions, persists active-goal text to
  `~/.B12/state/active-goal-<sid>.txt`, and writes goal-start /
  goal-end memories into the checkpoint buffer with score=9 (decision
  category). `/plan`, `/memory`, `/clear`, `/resume`, `/branch`
  expansions are logged to `~/.B12/memory-logs/slash-commands.jsonl`
  for future analysis.
- **`SubagentStop` hook** (`memory-subagent-stop.sh`) — captures
  subagent responses (Agent tool, `/batch`, Explore/Plan/general-purpose)
  as candidate memories before they vanish from parent context. Tags
  with `[subagent:<type>]` prefix so future retrieval can distinguish
  who said what. Caps at 4 candidates per subagent return.
- **`Stop` hook** (`memory-turn-end.sh`) — scans the assistant's
  end-of-turn response text for decision / learning / error / preference
  / correction patterns and queues matches into the existing
  checkpoint buffer. Captures commentary that `PostToolUse` never sees.
- **`PostToolUseFailure` hook** (`memory-tool-failure.sh`) — records
  failed tool calls (Bash, Edit, Write, WebFetch, MCP) as high-importance
  error memories. Skips noise sources (Read / Glob / Grep failures).

### v11.7 (2026-03-03) — Tier 3: Stemming, Health Report, Gemini Hooks, MCP Resources

- **Porter stemming FTS5** — `memory_fts_stemmed` table with `tokenize='porter unicode61'` for morphological matching
- **B12 Health Report** — `scripts/b12_health_report.py` with 8 sections, health score 0-100, and recommendations
- **Gemini CLI hooks** — adapter scripts in `hooks/gemini/` give Gemini CLI full B12 hook integration
- **MCP Resources** — 4 `b12://` resources for protocol-standard context access (stats, profile, health, project context)
- **Migration script** — `scripts/migrate_stemmed_fts.py` backfills stemmed FTS from existing memories

### v11.6 (2026-03-03) — LoCoMo Benchmark & Code Review

- **LoCoMo operationalization** — retrieval evaluation with MRR, NDCG, regression detection
- **17 code review fixes** — crash/data-loss, dashboard, correctness, and performance improvements
- **Content hash centralized** — `shared_patterns.content_hash()` eliminates hash mismatch across modules
- **N+1 query fix** — batch DB fetch in surfacing engine
- **Path traversal protection** — export paths restricted to `~/.B12/exports/`

### v11.5 (2026-03-03) — Web Dashboard

- **Web Dashboard** — Flask backend + Cytoscape.js frontend for visual memory graph browsing
- **Memory statistics** — real-time counts, type distribution, graph edges

### v11.1–v11.4 (2026-03-03) — Consolidation, Extraction, Surfacing, Export

- **Smart consolidation engine** — dedup groups, merge candidates, NLI contradiction detection
- **Enhanced session-end extraction** — 4 new memory patterns + `memory_refine` MCP tool
- **Proactive memory surfacing** — context-aware injection on file opens and errors
- **Memory export/import** — portable `.b12` format for backup and migration

### v11.0 (2026-02-28) — Audit, i18n & Cross-Platform Verification

- **BM25 scoring corrected** — MCP search results now rank correctly (was inverted)
- **Spaced repetition in MCP search** — strength boost works on all platforms, not just Claude Code hooks
- **`valid_until` + `deleted_at` support** in `memory_store` and `memory_update`
- **Ghost memory fix** — re-storing soft-deleted memories now works
- **i18n verified** — Turkish, Japanese, Chinese, Korean, Russian store + search
- **Cross-platform verified** — Claude → Gemini → Codex chain tested
- **40+ audit findings** fixed across 3 independent auditor rounds
- **Concurrent multi-CLI** — busy_timeout increased to 30s for parallel access

### v10.5 (2026-02-26) — Multi-Platform Support

- **7 new platform integrations**: Gemini CLI, VS Code/Copilot, Cursor, Kimi Code, Windsurf, Cline, OpenCode
- Each platform gets its own `--flag` for install.sh (`--gemini`, `--vscode`, `--cursor`, etc.)
- Shared `inject_b12_section()` helper eliminates duplicated marker-injection code
- All platforms share the same SQLite database — cross-platform memory
- 14 config templates in `config/` for MCP configs + instruction files
- Fixed Cursor tool naming bug (single → double underscore)

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
