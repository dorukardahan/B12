# B12 — Persistent Memory System for Claude Code

A local-first, fully automated, cross-project memory system for Claude Code CLI. Built on top of [mcp-memory-service](https://github.com/doobidoo/mcp-memory-service) with smart hooks that make Claude remember everything without ever being asked.

## What it does

- Automatically stores important decisions, patterns, and learnings during work
- Recalls relevant context at the start of every session
- Preserves key information before context compaction
- Carries session summaries forward so the next session knows what happened
- Maintains a user profile that Claude updates as it learns your preferences
- Works across multiple projects with a single shared memory database
- Requires zero manual intervention — Claude handles memory silently

## Architecture

```
Claude Code Session
    |
    |-- SessionStart Hook ──> Injects: user profile
    |                          + last session summary
    |                          + memory instructions
    |
    |-- [Claude works, uses memory MCP tools silently]
    |
    |-- PreCompact Hook ────> Stages comprehensive transcript summary
    |                          (15 user msgs + 10 assistant outputs + files)
    |
    |-- SessionStart(compact) > Recovers staged context, stores to memory
    |
    |-- SessionEnd Hook ────> Extracts session summary (latest + rolling history)
    |                          + logs metadata + cleanup
    v
mcp-memory-service (MCP Server)
    |
    |-- SQLite-vec (local database)
    |-- MiniLM-L6-v2 ONNX (local embeddings, no API calls)
    |-- Quality scoring (ONNX ranker)
    |-- Graph associations
    |-- Auto-backup
```

## Components

| Component | Description |
|-----------|-------------|
| `hooks/memory-session-start.sh` | Injects user profile + last session summary + memory instructions |
| `hooks/memory-precompact.sh` | Parses full transcript, stages comprehensive context before compaction |
| `hooks/memory-session-end.sh` | Extracts session summary, maintains rolling history, logs metadata |
| `templates/user-profile.md` | Template for user profile (Claude updates it as it learns about you) |
| `config/settings-template.json` | Hook configuration template for Claude Code settings |
| `config/mcp-server-template.json` | MCP server configuration template |
| `docs/architecture.md` | Detailed architecture and design decisions |
| `docs/setup.md` | Step-by-step installation guide |

## Quick start

### 1. Install mcp-memory-service

```bash
pipx install mcp-memory-service
# Verify: memory --version
```

### 2. Add MCP server to your Claude Code config

Add to `~/.claude.json` under `mcpServers`:

```json
{
  "memory": {
    "command": "/path/to/memory",
    "args": ["server"],
    "env": {}
  }
}
```

### 3. Copy hooks and create directories

```bash
mkdir -p ~/.claude/hooks ~/.claude/memory-staging ~/.claude/memory-logs ~/.claude/memory-summaries
cp hooks/*.sh ~/.claude/hooks/
chmod +x ~/.claude/hooks/memory-*.sh
```

### 4. Add hooks to settings

Merge the contents of `config/settings-template.json` into your `~/.claude/settings.json`.

### 5. Create user profile (optional)

```bash
# project-hash = CWD with / replaced by -
mkdir -p ~/.claude/projects/-Users-$(whoami)/memory
cp templates/user-profile.md ~/.claude/projects/-Users-$(whoami)/memory/user-profile.md
# Edit with your preferences — Claude will also update it over time
```

### 6. Restart Claude Code

The memory system activates on the next session start. After your first session ends, check `~/.claude/memory-summaries/` for the generated summary.

## 5 layers of memory

| Layer | What | Where | Best for |
|-------|------|-------|----------|
| **MEMORY.md** | Built-in auto-memory | `~/.claude/projects/*/memory/` | Stable project knowledge |
| **mcp-memory-service** | Semantic search DB | `~/Library/Application Support/mcp-memory/` | Detailed learnings, decisions |
| **Smart hooks** | Lifecycle automation | `~/.claude/hooks/` | Glue between all layers |
| **Session summaries** | Per-project latest + history | `~/.claude/memory-summaries/` | Short-term continuity |
| **User profile** | Persistent identity | `~/.claude/projects/*/memory/user-profile.md` | Personalization |

## Multi-setup support

If you run multiple Claude Code setups (e.g., personal + work), the system works across all of them:

- **MCP server** is global (configured in `~/.claude.json`)
- **Hooks** use absolute paths, so they work from any setup
- **Database** is shared — memories from any project are available everywhere
- **Session summaries** are per-project, so they don't overwrite each other
- **Hook config** needs to be added to each setup's `settings.json`

## Changelog

### v2 (2026-02-07)

- **SessionEnd**: Now extracts comprehensive session summaries from transcript (was: just logging)
- **PreCompact**: Full transcript parsing with 15 user msgs + 10 assistant outputs (was: tail -100, 5 msgs)
- **SessionStart**: Loads user profile + last session summary (was: basic instructions only)
- **New**: User profile template (`templates/user-profile.md`)
- **New**: Session summaries directory (`memory-summaries/`) with latest + rolling history
- **Fix**: Heredoc syntax bug — `python3 -` instead of bare `python3` when using heredoc with args
- **Fix**: `datetime.utcnow()` replaced with `datetime.now(timezone.utc)` (Python deprecation)
- **Change**: SessionEnd timeout increased from 5s to 15s (transcript parsing needs more time)
- **Change**: PreCompact cleanup interval increased from 1h to 2h

### v1 (2026-02-07)

- Initial release with basic SessionStart, PreCompact, SessionEnd hooks
- mcp-memory-service integration
- Architecture documentation and setup guide

## Roadmap

- [ ] PostToolUse hook for tracking search failures (feedback loop)
- [ ] Usage-based relevance scoring (promote frequently accessed memories)
- [ ] Auto-archiving of stale memories
- [ ] Multilingual embedding model upgrade
- [ ] Memory consolidation (merge similar entries)
- [ ] 0G Storage + Compute/TEE integration for decentralized private memory
- [ ] Web dashboard for memory visualization

## License

MIT
