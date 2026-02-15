# B12 — Persistent Memory System for Claude Code

A local-first, fully automated, cross-project memory system for Claude Code CLI. Built on top of [mcp-memory-service](https://github.com/doobidoo/mcp-memory-service) with smart hooks that make Claude remember everything without ever being asked.

## What it does

- Automatically stores important decisions, patterns, and learnings during work
- Recalls relevant context at the start of every session using Ebbinghaus decay scoring
- Preserves key information before context compaction
- Carries session summaries forward so the next session knows what happened
- Maintains a user profile that Claude updates as it learns your preferences
- Works across multiple projects with a single shared memory database
- Requires zero manual intervention — Claude handles memory silently
- Protects against SQL injection in all user-facing inputs
- Tracks conversation momentum (active/modified files) across compactions
- Self-improves: unused memories decay, frequently accessed ones strengthen

## Architecture

```
Claude Code Session
    |
    |── SessionStart ────────> Inject: user profile + last session summary
    |                           + scope instructions + memory pre-fetch
    |                           + cross-project hints + feedback alerts
    |
    |── UserPromptSubmit ────> Ebbinghaus decay-aware memory retrieval
    |                           (FTS5 hybrid: 0.3×decay + 0.3×importance + 0.4×BM25)
    |
    |── PreToolUse ──────────> Auto-inject scope tags on memory_store
    |                           (proj:<name>, user:<setup>)
    |
    |── [Claude works, uses memory MCP tools silently]
    |
    |── PostToolUse ─────────> Track memory usage patterns (feedback)
    |                         + Track active/modified files (Working Memory)
    |
    |── PreCompact ──────────> Stage comprehensive transcript summary
    |                           (priority-weighted, token-budgeted)
    |
    |── SessionStart(compact) > Recover staged context + Working Memory
    |
    |── SessionEnd ──────────> Extract session summary (latest + rolling)
    |                           + micro-memory extraction via write-time merge
    |                           + background embedding generation
    v
mcp-memory-service (MCP Server)
    |
    |── SQLite-vec (local database)
    |── FTS5 hybrid search (BM25 keyword + vector cosine, 70/30 weight)
    |── MiniLM-L12-v2 ONNX (local embeddings, no API calls)
    |── Ebbinghaus strength decay (spaced repetition)
    |── Write-time semantic merge (cosine > 0.85 = merge, not duplicate)
    |── Graph associations
    |── Auto-backup (daily, 7-day rotation)
```

## Components

### Hooks (lifecycle automation)

| Hook | Event | Description |
|------|-------|-------------|
| `memory-session-start.sh` | SessionStart | Injects user profile, session summary, scope instructions, memory pre-fetch, cross-project hints, feedback alerts. Handles startup, resume, and compact modes |
| `memory-retrieval.sh` | UserPromptSubmit | Ebbinghaus decay-aware retrieval — extracts keywords, FTS5 hybrid scoring, strength boost for top results |
| `memory-tag-enforce.sh` | PreToolUse | Auto-injects `proj:<name>` and `user:<setup>` scope tags on `memory_store` calls |
| `memory-precompact.sh` | PreCompact | Priority-weighted transcript extraction with token budget (~8000 chars). Stages context for post-compaction recovery |
| `memory-session-end.sh` | SessionEnd | Structured extraction of decisions, errors/fixes, learnings, preferences. Background embedding. Write-time semantic merge for micro-memories |
| `memory-feedback.sh` | PostToolUse | Tracks memory store/search usage patterns, empty result detection, scope compliance |
| `memory-working-context.sh` | PostToolUse | Tracks active files, modified files, search patterns during conversation. Persists to working-memory.json for post-compaction recovery |
| `memory-consolidate.py` | Scheduled | Dedup (Jaccard similarity), stale detection, cross-project index generation |
| `memory-quality-audit.sh` | Scheduled | Weekly health score — type distribution, scope compliance, strength distribution, orphan detection |
| `memory-feedback-digest.sh` | Scheduled | Weekly digest — search patterns, refinement detection, self-improving retrieval (strength decay for unused memories) |
| `memory-backup.sh` | Scheduled | Daily WAL-safe backup with 7-day rotation and integrity check |
| `memory-upgrade.sh` | Manual | pipx upgrade + FTS5 patch check + bytecache clear |
| `memory-browse.sh` | Manual | CLI browser — search, stats, CRUD, tag filter. SQL-sanitized inputs |

### Scripts (support modules)

| Script | Description |
|--------|-------------|
| `scripts/write_time_merge.py` | Semantic dedup at write time — cosine similarity > 0.85 triggers content merge instead of INSERT. Handles graph hash rewriting and vec0 table upsert |
| `scripts/ebbinghaus.py` | Ebbinghaus decay scoring utilities — half-life calculation, strength-based retention |
| `scripts/migrate-ebbinghaus.py` | Migration script — adds `strength` and `last_accessed_at` fields to existing DB |

### Config templates

| File | Description |
|------|-------------|
| `config/settings-template.json` | Full hook configuration for `~/.claude/settings.json` — all 7 hook events |
| `config/mcp-server-template.json` | MCP server configuration for `~/.claude.json` |
| `config/launchd-backup.plist` | Daily backup agent (1:00 AM) |
| `config/launchd-consolidate.plist` | Daily consolidation agent (2:00 AM) |
| `config/launchd-feedback-digest.plist` | Weekly digest agent (Monday 3:00 AM) |
| `config/launchd-quality-audit.plist` | Weekly quality audit agent (Wednesday 3:00 AM) |

### Other

| File | Description |
|------|-------------|
| `templates/user-profile.md` | Template for user profile — Claude updates it as it learns about you |
| `docs/architecture.md` | Detailed architecture and design decisions |
| `docs/setup.md` | Step-by-step installation guide |

## Quick start

### 1. Install mcp-memory-service

```bash
pipx install mcp-memory-service
# Verify: memory --version
```

### 2. Run the installer

```bash
git clone https://github.com/youruser/B12.git
cd B12
chmod +x install.sh
./install.sh              # Install to ~/.claude (default)
# or: ./install.sh --all  # Install to all ~/.claude* setups
```

### 3. Add MCP server to your Claude Code config

Add to `~/.claude.json` under `mcpServers`. Use `config/mcp-server-template.json` for the full configuration with recommended environment variables:

```json
{
  "memory": {
    "command": "/path/to/memory",
    "args": ["server"],
    "env": {
      "MCP_EMBEDDING_MODEL": "paraphrase-multilingual-MiniLM-L12-v2",
      "MCP_DECAY_ENABLED": "false",
      "MCP_FORGETTING_ENABLED": "false",
      "MCP_ASSOCIATIONS_ENABLED": "true"
    }
  }
}
```

Replace `/path/to/memory` with the output of `which memory`. See `config/mcp-server-template.json` for all available options.

### 4. Restart Claude Code

The memory system activates on the next session start. After your first session ends, check `~/.claude/memory-summaries/` for the generated summary.

### 5. Optional — Set up automated tasks

Copy the launchd plists to enable daily backup, consolidation, and weekly quality audits:

```bash
# Replace /path/to/home with your actual home directory in each plist
cp config/launchd-*.plist ~/Library/LaunchAgents/
# Edit each plist to replace /path/to/home
launchctl load ~/Library/LaunchAgents/com.b12.memory-*.plist
```

See `docs/setup.md` for the full installation guide.

## Memory layers

| Layer | What | Where | Best for |
|-------|------|-------|----------|
| **MEMORY.md** | Built-in auto-memory | `~/.claude/projects/*/memory/` | Stable project knowledge (current state) |
| **mcp-memory-service** | Semantic search DB | `~/Library/Application Support/mcp-memory/` | Detailed learnings, decisions (historical) |
| **Smart hooks** | Lifecycle automation | `~/.claude/hooks/` | Glue between all layers |
| **Session summaries** | Per-project latest + history | `~/.claude/memory-summaries/` | Short-term continuity |
| **User profile** | Persistent identity | `~/.claude/projects/*/memory/user-profile.md` | Personalization |
| **Working Memory** | Conversation momentum | `~/.claude/memory-staging/working-memory.json` | Post-compaction recovery |

## Key features

### Ebbinghaus decay scoring
Every memory has a `strength` field (0.3–5.0). Frequently retrieved memories get stronger (+0.3 per retrieval, max 5.0). Unused memories decay weekly (-0.05, min 0.3). Retrieval scoring combines: `0.3 × decay + 0.3 × importance + 0.4 × FTS5 relevance`.

### FTS5 hybrid search
Combines BM25 keyword search with vector cosine similarity (70/30 weight). Auto-synced via SQLite triggers — no manual reindexing needed.

### Write-time semantic merge
When storing a new memory, if an existing memory has cosine similarity > 0.85, the content is merged instead of creating a duplicate. Handles graph hash rewriting for association integrity.

### Scope system
4 scopes for organizing memories:
- **project**: Codebase-specific (architecture, decisions, bugs) — tag: `proj:<name>`
- **universal**: Applies everywhere (patterns, CLI tricks, lessons) — tag: `user:universal`
- **preference**: User preferences (always global) — tag: `user:pref`
- **setup**: Team/workflow specific — tag: `user:<setup-name>`

### Working Memory
Tracks which files you're reading, editing, and searching during a conversation. After context compaction, this momentum is restored so Claude doesn't lose track of what you were working on.

### SQL injection protection
All user-facing inputs (search queries, hash prefixes, project names) are sanitized before touching SQLite. Character stripping and type-specific validation at every entry point.

## Multi-setup support

If you run multiple Claude Code setups (e.g., personal + work), the system works across all of them:

- **MCP server** is global (configured in `~/.claude.json`)
- **Hooks** use absolute paths, so they work from any setup
- **Database** is shared — memories from any project are available everywhere
- **Session summaries** are per-project, so they don't overwrite each other
- **Hook config** needs to be added to each setup's `settings.json`
- **Install**: `./install.sh --all` handles multiple setups

## Changelog

### v8.2 (2026-02-15)

- **PreCompact IndentationError fix**: Python heredoc had 16-space indent instead of 12, causing SyntaxError since creation — PreCompact hook never successfully extracted transcript content
- **write_time_merge.py rename**: `scripts/write-time-merge.py` → `scripts/write_time_merge.py`. Python cannot import modules with hyphens; `from write_time_merge import merge_or_insert` was silently failing via ImportError catch
- **Turkish keyword extraction**: Replaced ASCII-only `grep -oE '[a-zA-Z0-9_.-]{3,}'` with Python `re.findall(r'[\w]{3,}', text, re.UNICODE)` + 60+ Turkish/English stop words. Queries like "hafıza sistemi kararları" now extract all keywords instead of returning empty
- **Semantic vector fallback**: When FTS5 returns 0 results, falls back to pure vector similarity search (SentenceTransformer embedding, cosine similarity > 0.3 threshold, 4s timeout, top 5). Only triggers on zero-result queries — no overhead on normal retrievals
- **Turkish SessionEnd patterns + scoring**: Added Turkish alternatives to all 4 regex patterns (DECISION_RE, ERROR_RE, LEARNING_RE, PREFERENCE_RE) and Turkish keywords to `score_extraction()`. Turkish decisions, errors, and learnings are now captured
- **Filename reference cleanup**: Updated all references from `write-time-merge.py` to `write_time_merge.py` across README, docs, and internal comments

### v8.1 (2026-02-09)

- **Query-adaptive search mode**: Retrieval hook (v4) classifies queries before deciding on vector re-rank. Negation/adversarial → always re-rank (hybrid +18pp). Attribute/preference → skip re-rank (keyword +4.7pp). Default → re-rank. Few results (< 2) → fallback re-rank regardless. Saves ~200ms on ~20% of queries
- **LoCoMo benchmark**: Eval script with keyword/hybrid/adaptive/compare modes. 10 conversations, 1986 QA pairs. Results: keyword 25.8%, hybrid 23.9%, adaptive 24.1% (Recall@3 Answerable). Hybrid wins overall (36.5%) due to adversarial filtering

### v8 (2026-02-09)

- **Vector re-rank in retrieval hook**: FTS5 top-10 candidates → Python cosine re-rank → top-5 results. Uses mcp-memory-service venv's sentence-transformers with 3-second timeout; falls back to FTS5-only silently
- **Phrase-aware FTS5 queries**: Bigram detection in both hook and MCP service. Compound terms like "docker compose" become `NEAR(docker compose, 2)` instead of `docker OR compose`
- **Adaptive hybrid weights**: `_get_hybrid_weights()` in sqlite_vec.py — technical queries (error codes, file paths) get 50/50 vector/FTS5; conceptual queries get 70/30 (default)
- **Softened Ebbinghaus decay**: `exp(-t/(S*3))` instead of `exp(-t/S)`. At strength=1.0: 2-day memory 0.13→0.51, 7-day 0.001→0.10
- **Project hierarchy detection**: Walks up directory tree to find `.git` root. Running from `/B12/benchmarks/locomo` now finds `proj:B12` memories
- **Importance-based pre-fetch**: `ORDER BY importance_score * strength DESC` instead of `created_at DESC`
- **Post-compact pre-fetch re-enabled**: Memory pre-fetch now runs after context compaction (was skipped)
- **Hook retrieval feedback logging**: Every retrieval logged to `feedback.jsonl` with query, keyword count, result count, rerank status
- **Bug fixes**: `recall()` missing `deleted_at IS NULL` (2 locations), SessionEnd scanning only first 400→2000 chars with context extraction, results increased from 3→5

### v7 (2026-02-08)

- **SQL injection protection**: All user inputs sanitized in retrieval, browse, and tag-enforce hooks
- **Write-time semantic merge**: New `scripts/write_time_merge.py` — cosine > 0.85 triggers merge. Integrated into SessionEnd micro-memory extraction with graceful degradation
- **Self-improving retrieval**: Weekly strength decay in feedback-digest (-0.05 for memories not accessed in 7 days, min 0.3)
- **Working Memory**: New PostToolUse hook tracks active/modified files and search patterns. Loaded by SessionStart after compaction
- **Bug fixes**: CTE alignment for strength boost, printf '%b' POSIX fix, valid_until IS NULL filter, deleted_at IS NULL in quality audit, error logging in PreCompact, narrowed slash command regex

### v6 (2026-02-08)

- **SessionStart v5**: Memory pre-fetch via FTS5 + tag-based queries (project-relevant + universal). No embedding model needed at startup
- **Ebbinghaus decay integration**: Combined scoring in retrieval (0.3×decay + 0.3×importance + 0.4×FTS5)
- **Strength boost**: Top 3 retrieved memories get +0.3 strength per access (max 5.0)

### v5 (2026-02-08)

- **FTS5 hybrid search**: `memory_fts` table with 4 auto-sync triggers. BM25 keyword + vector cosine (70/30 weight) in retrieve/recall
- **New**: `scripts/ebbinghaus.py` — decay scoring utilities
- **New**: `scripts/migrate-ebbinghaus.py` — adds strength/last_accessed_at fields

### v4 (2026-02-08)

- **Scope system**: 4 scopes (project, universal, preference, setup) with tag namespaces
- **SessionStart v4**: Setup detection (personal vs work), scope-aware instructions, compressed behavioral instructions (~120 tokens vs ~512 in v3)
- **New**: PreToolUse tag enforcement hook (`memory-tag-enforce.sh`)
- **New**: UserPromptSubmit retrieval hook (`memory-retrieval.sh`)
- **New**: Quality audit hook (`memory-quality-audit.sh`)
- **New**: Backup hook (`memory-backup.sh`)
- **New**: Browse CLI (`memory-browse.sh`)
- **New**: Upgrade script (`memory-upgrade.sh`)
- **Change**: Dual-layer deconfliction (MEMORY.md = active state, MCP = historical)

### v3 (2026-02-07)

- **SessionEnd v3**: Structured extraction — regex-based detection of decisions, errors/fixes, learnings, user preferences
- **SessionStart v3**: Cross-project topic hints loaded from index, enhanced behavioral instructions with typed memories
- **New**: PostToolUse feedback hook (`memory-feedback.sh`) — tracks store/search patterns, empty result detection
- **New**: Consolidation script (`memory-consolidate.py`) — Jaccard dedup, stale detection, cross-project index

### v2 (2026-02-07)

- **SessionEnd**: Comprehensive session summary extraction from transcript
- **PreCompact**: Full transcript parsing with 15 user msgs + 10 assistant outputs
- **SessionStart**: Loads user profile + last session summary
- **New**: User profile template, session summaries directory

### v1 (2026-02-07)

- Initial release with basic SessionStart, PreCompact, SessionEnd hooks
- mcp-memory-service integration

## Roadmap

- [x] ~~PostToolUse hook for tracking search failures~~ — Done in v3
- [x] ~~Memory consolidation (merge similar entries)~~ — Done in v3
- [x] ~~Usage-based relevance scoring~~ — Done in v6 (Ebbinghaus)
- [x] ~~Auto-archiving of stale memories~~ — Done in v7 (strength decay)
- [ ] Contradiction detection (NLI model)
- [ ] Graph-based memory traversal and clustering
- [ ] Multilingual embedding model upgrade
- [ ] Web dashboard for memory visualization
- [ ] 0G Storage + Compute/TEE integration for decentralized private memory

## License

MIT
