# B12 Architecture

## Design principles

1. **Local-first**: All data stays on the user's machine. No cloud dependencies.
2. **Zero manual effort**: The user never needs to say "save this" or "search memory."
3. **Low overhead**: Hooks are fast shell scripts. Memory search adds minimal latency.
4. **Cross-project**: A single database serves all projects. Tags enable filtering.
5. **Recoverable**: PreCompact hook preserves context before it's lost to compaction.
6. **Session-aware**: Each session's summary carries forward to the next one.
7. **Self-improving**: Unused memories decay, frequently accessed ones strengthen.
8. **Secure**: All user inputs are sanitized before touching SQLite.

## System layers

### Layer 1: Native Auto Memory (built-in)

Claude Code's built-in memory system:
- `MEMORY.md` — first 200 lines loaded into every session's system prompt
- Topic files — referenced from MEMORY.md, loaded on demand
- Path: `~/.claude/projects/<project-hash>/memory/`
- Best for: Stable, high-level project knowledge (current state)

### Layer 2: B12 MCP Server (`b12_mcp_server.py`)

Custom FastMCP server providing 4 memory tools (`memory_store`, `memory_search`, `memory_update`, `memory_quality`). Replaces the old `mcp-memory-service` (pipx) with a lightweight ~400-line server that delegates ML operations to a background embed daemon via Unix socket.

- **Database**: SQLite + sqlite-vec (local file)
- **Embeddings**: multilingual-MiniLM-L12-v2 via `embed_daemon.py` (runs locally, no API)
- **Search**: FTS5 hybrid — BM25 keyword + vector cosine
- **Scoring**: Ebbinghaus decay-aware — combines retention, importance, and relevance
- **Dedup**: Write-time semantic merge (cosine > 0.85 = merge, not INSERT)
- **Graph**: Association-based memory connections
- **Backup**: Daily WAL-safe backups with 7-day rotation
- **Location**: Database at `~/Library/Application Support/mcp-memory/sqlite_vec.db` (macOS)
- Best for: Detailed learnings, decisions, patterns that need semantic search

### Layer 3: Smart hooks (automation glue)

Shell scripts that fire at key lifecycle points:

```
Session lifecycle:

[SessionStart] ─── startup/resume ──> Inject: user profile
       |                                + last session summary
       |                                + scope instructions
       |                                + memory pre-fetch (FTS5 + tags)
       |                                + cross-project hints
       |                                + feedback alerts
       v
[UserPromptSubmit] ─── every prompt ──> Extract keywords
       |                                 FTS5 hybrid retrieval
       |                                 Ebbinghaus combined scoring
       |                                 Strength boost top results
       v
[PreToolUse] ─── memory_store ────────> Auto-inject scope tags
       |                                 (proj:<name>, user:<setup>)
       v
[Claude works] ─── uses memory MCP ──> Stores/retrieves silently
       |            tools as needed      Updates user-profile.md
       v
[PostToolUse] ─── memory tools ───────> Log usage patterns (feedback)
       |       ─── file tools ────────> Track active/modified files
       v                                 (Working Memory)
[PreCompact] ──── auto/manual ────────> Priority-weighted extraction
       |                                 Token-budgeted (~8000 chars)
       |                                 Stages to memory-staging/
       v
[SessionStart] ─── compact ───────────> Recover staged summary
       |                                 + Working Memory (files, patterns)
       v
[SessionEnd] ──── any reason ─────────> Structured extraction:
                                          decisions, errors/fixes,
                                          learnings, preferences
                                        + micro-memory via write-time merge
                                        + background embedding
                                        + session metadata logging
```

### Layer 4: Session summaries (continuity bridge)

Per-project markdown files that bridge between sessions:
- `~/.claude/memory-summaries/{project}-latest.md` — last session's summary
- `~/.claude/memory-summaries/{project}-history.md` — rolling last 5 sessions
- Separated by `<!-- SESSION_BREAK -->` markers
- Loaded by SessionStart into the next session's context
- Best for: Short-term continuity ("what did we do last time?")

### Layer 5: User profile (persistent identity)

A markdown file in the project memory directory:
- `~/.claude/projects/<project-hash>/memory/user-profile.md`
- Contains: communication style, preferences, work context, learned patterns
- Claude updates it proactively when learning new preferences
- Loaded by SessionStart if updated within last 7 days (lazy loading)
- Best for: Personal context that makes Claude feel like a consistent collaborator

### Layer 6: Working Memory (conversation momentum)

Tracks the files and patterns you're actively working with:
- `~/.claude/memory-staging/working-memory.json`
- Populated by PostToolUse hook on Read/Edit/Write/Glob/Grep
- Loaded by SessionStart after context compaction
- Contains: active files (read), modified files (edited/written), search patterns
- Resets on session change, expires after 2 hours
- Best for: Maintaining context about "what was I just doing?" after compaction

## Hook design details

### SessionStart hook (v5)

**Purpose**: Prime Claude with full context — user identity, last session, memory instructions, and pre-fetched relevant memories.

On `startup` or `resume`:
1. Detects setup context (personal vs work) from path patterns
2. Loads `user-profile.md` if recently updated (within 7 days)
3. Loads `{project}-latest.md` session summary (or global fallback)
4. Loads cross-project topic hints from consolidation index
5. Loads feedback digest alerts (if recent)
6. Pre-fetches project-relevant + universal memories via FTS5 + tag queries
7. Combines everything with scope-aware behavioral instructions
8. Outputs as `additionalContext` JSON

On `compact`:
1. Checks `memory-staging/` for pre-compaction summaries
2. Loads Working Memory (active/modified files, search patterns)
3. If staged context found: injects summary + Working Memory
4. If not: falls back to user profile + last session + scope reminder

**Performance**: Uses `jq` for JSON (no Python startup), `printf '%b'` for POSIX portability, direct SQLite queries for pre-fetch (no embedding model needed).

### UserPromptSubmit hook (v2)

**Purpose**: Proactively retrieve relevant memories before Claude processes each user message.

Process:
1. Skip slash commands (pattern: `/word`)
2. Extract keywords from user prompt (stop-word removal, 12-word limit)
3. Build FTS5 query with OR operators
4. Run hybrid scoring: `0.3×decay + 0.3×importance + 0.4×FTS5_relevance`
5. Boost strength of top 3 results (+0.3, max 5.0) via CTE-aligned UPDATE
6. Return results as `additionalContext`

**SQL injection protection**: All keywords stripped of `'"();{}` characters before SQL interpolation.

### PreToolUse hook (v1)

**Purpose**: Ensure every `memory_store` call has proper scope tags.

Process:
1. Intercepts `mcp__B12__memory_store` calls
2. Derives project name from CWD
3. Detects setup context (personal/work)
4. Injects `proj:<name>` and `user:<setup>` tags if missing
5. Returns modified tool input

### PreCompact hook (v2)

**Purpose**: Capture comprehensive context before it's lost to compaction.

Process:
1. Parses the ENTIRE transcript JSONL file
2. Categorizes content by priority (errors > decisions > preferences > general)
3. Extracts within token budget (~8000 chars)
4. Writes structured summary to `memory-staging/precompact-{session_id}.txt`
5. Cleans up staging files older than 2 hours
6. Logs errors to `memory-logs/memory-errors.log`

**Why priority-weighted**: v1 used simple tail/head. v2 scores each item by category and takes the highest-value content within the budget.

### SessionEnd hook (v5)

**Purpose**: Extract a comprehensive session summary and persist micro-memories.

Process:
1. Parses the transcript JSONL file
2. Extracts structured categories: decisions, errors/fixes, learnings, preferences
3. Scores items by category-specific heuristics
4. Writes to `{project}-latest.md` (overwritten each session)
5. Appends to `{project}-history.md` (rolling, separated by `<!-- SESSION_BREAK -->`)
6. Logs session metadata to `sessions.jsonl`
7. Generates embeddings for micro-memories in background
8. Uses write-time semantic merge (if available) to deduplicate

**Background embedding**: A Python subprocess generates embeddings after the main hook completes. Uses WAL mode + busy_timeout for safe concurrent DB access.

**Write-time merge**: Imports `merge_or_insert` from `scripts/write_time_merge.py`. Falls back to direct INSERT if the script is unavailable (graceful degradation).

### PostToolUse hooks

**Feedback hook** (v3): Logs every memory store/search/update/quality call to `feedback.jsonl`. Tracks: action, query text, result count, session sequence number, scope compliance. Used by the weekly feedback digest.

**Working Context hook** (v1): Fires on Read/Edit/Write/Glob/Grep. Extracts the file path or search pattern. Persists to `working-memory.json` with atomic writes (tmp + rename). Tracks session ID to reset on session change.

## Ebbinghaus decay model

Every memory has a `strength` field (0.3–5.0, default 1.0):

- **Retrieval boost**: +0.3 per access (capped at 5.0)
- **Weekly decay**: -0.05 for memories not accessed in 7 days (floor at 0.3)
- **Combined scoring**: `0.3 × exp(-age/strength) + 0.3 × importance/2 + 0.4 × FTS5_rank`

This creates natural selection: memories that are frequently useful survive and get easier to find. Memories that were stored but never retrieved gradually fade but never fully disappear (minimum 0.3).

## FTS5 hybrid search

The database has two FTS5 virtual tables:

- **`memory_fts`** (B12): `unicode61` tokenizer, synced via 4 triggers. Used by B12 hooks for word-boundary keyword matching
- **`memory_content_fts`** (legacy, originally from mcp-memory-service v10.13.0): `trigram` tokenizer, synced via 3 triggers. Kept for backward compatibility

B12 hook search combines:

- **BM25 keyword score** (FTS5 rank via `memory_fts`): Fast exact-match and phrase search
- **Vector cosine similarity** (sqlite-vec): Semantic meaning match
- **Weight**: 70% keyword + 30% vector (keyword-heavy because most searches use specific terms)

The hybrid approach handles both precise technical queries ("FTS5 trigger") and semantic queries ("how to search memories").

B12 hooks are fully independent of the MCP server's search implementation — they query the database directly. This decoupling means upstream upgrades no longer break B12 functionality.

## Write-time semantic merge

When a new memory is stored via SessionEnd:
1. Generate embedding for the new content
2. Query existing memories with `vec_distance_cosine`
3. If similarity > 0.85: merge content into existing memory, update metadata, rewrite graph hashes
4. If similarity < 0.85: INSERT as new memory
5. Handles vec0 table upsert (sqlite-vec requires DELETE+INSERT, not UPDATE)

This prevents the "thousand similar memories" problem that accumulates over months of use.

## Scope system

4 scopes organize memories for multi-project, multi-setup use:

| Scope | Tag | When to use |
|-------|-----|-------------|
| **project** | `proj:<name>` | Architecture decisions, project-specific bugs, conventions |
| **universal** | `user:universal` | Cross-project patterns, CLI tricks, general lessons |
| **preference** | `user:pref` | User preferences (always global) |
| **setup** | `user:<setup>` | Team/workflow specific to a particular setup |

The PreToolUse tag enforcement hook automatically injects `proj:` and `user:` tags on every `memory_store` call. The retrieval hook defaults to filtering by `proj:<current>` and widens scope when results are sparse.

## Security

### SQL injection protection

All hooks that interpolate user input into SQLite queries apply sanitization:
- **Keywords** (retrieval): Strip `'"();{}` characters
- **Hash prefixes** (browse): Allow only hex characters `[a-fA-F0-9]`
- **Project names** (pre-fetch, browse): Allow only `[a-zA-Z0-9_-]`

### Data isolation

- All data stays local (no cloud, no API calls for embeddings)
- Database is SQLite in user's home directory
- No secrets or credentials in hook scripts
- MCP server config uses `env: {}` (no environment secrets needed)

## Limitations and future work

### Current limitations

1. **English-optimized embeddings**: MiniLM-L12-v2 is multilingual but primarily optimized for English. Mixed-language content may have reduced semantic accuracy.

2. **Contradiction detection coverage**: `contradiction_resolver.py` provides ONNX NLI-based contradiction detection, but it runs as a scheduled task (graph enrichment), not inline at write time. Contradicting memories may coexist until the next enrichment cycle.

3. **Linear memory scan**: Write-time merge checks against all memories for similarity. As the database grows (10K+ memories), this may need index optimization.

### Planned improvements
- **Graph-based traversal**: Walk memory associations for context expansion
- **Memory clustering**: Group related memories for batch review
- **Web dashboard**: Visual memory graph browser
- **0G integration**: Decentralized storage + TEE-based embedding for privacy-preserving cloud memory
