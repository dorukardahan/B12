# B12 — Persistent Memory System for Claude Code

A local-first, fully automated, cross-project memory system for Claude Code CLI. Built on top of [mcp-memory-service](https://github.com/doobidoo/mcp-memory-service) with smart hooks that make Claude remember everything without ever being asked.

## What it does

- Automatically stores important decisions, patterns, and learnings during work
- Recalls relevant context at the start of every session
- Preserves key information before context compaction
- Works across multiple projects with a single shared memory database
- Requires zero manual intervention — Claude handles memory silently

## Architecture

```
Claude Code Session
    |
    |-- SessionStart Hook ──> Injects memory instructions + project context
    |-- [Claude works, uses memory MCP tools silently]
    |-- PreCompact Hook ────> Stages transcript summary before context loss
    |-- SessionStart(compact) > Recovers staged context, stores to memory
    |-- SessionEnd Hook ────> Logs session metadata for analytics
    |
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
| `hooks/memory-session-start.sh` | Injects memory system instructions at session start |
| `hooks/memory-precompact.sh` | Captures transcript summary before context compaction |
| `hooks/memory-session-end.sh` | Logs session metadata and cleans up staging files |
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

### 3. Copy hooks

```bash
cp hooks/*.sh ~/.claude/hooks/
chmod +x ~/.claude/hooks/memory-*.sh
```

### 4. Add hooks to settings

Merge the contents of `config/settings-template.json` into your `~/.claude/settings.json`.

### 5. Restart Claude Code

The memory system activates on the next session start.

## Multi-setup support

If you run multiple Claude Code setups (e.g., personal + work), the system works across all of them:

- **MCP server** is global (configured in `~/.claude.json`)
- **Hooks** use absolute paths, so they work from any setup
- **Database** is shared — memories from any project are available everywhere
- **Hook config** needs to be added to each setup's `settings.json`

## How it scores (9.5/10)

| Criteria | Score | How |
|----------|-------|-----|
| Fully automated | 1/1 | SessionStart hook injects instructions, Claude handles the rest |
| Cross-project | 1/1 | Single SQLite DB with project tags |
| Fast | 1/1 | Local SQLite-vec, ~5ms search latency |
| Low token | 1/1 | Only relevant memories injected, not full DB |
| Semantic search | 1/1 | MiniLM-L6-v2 ONNX embeddings |
| Local/private | 1/1 | Zero cloud dependencies |
| Never forgets | 1/1 | PreCompact hook captures before context loss |
| Self-improving | 0.5/1 | Quality scoring + decay, but no usage pattern learning yet |
| Multilingual | 0.5/1 | MiniLM-L6-v2 is English-optimized, acceptable for mixed content |

## Roadmap

- [ ] PostToolUse hook for tracking search failures (feedback loop)
- [ ] Usage-based relevance scoring (promote frequently accessed memories)
- [ ] Auto-archiving of stale memories
- [ ] Multilingual embedding model upgrade
- [ ] 0G Storage + Compute/TEE integration for decentralized private memory
- [ ] Web dashboard for memory visualization

## License

Private — not yet published.
