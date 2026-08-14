# B12 Architecture

## Design principles

1. **Local-first**: All data stays on the user's machine. No cloud dependencies.
2. **Zero manual effort**: The user never needs to say "save this" or "search memory."
3. **Low overhead**: Hooks are fast shell scripts. Memory search adds minimal latency.
4. **Cross-project**: A single database serves all projects. Tags enable filtering.
5. **Recoverable**: PreCompact hook preserves context before it's lost to compaction.
6. **Session-aware**: Each session's summary carries forward to the next one.
7. **Self-improving**: Unused memories decay, frequently accessed ones strengthen.
8. **Secure**: User inputs in hook SQL queries are sanitized (keyword stripping, hex-only hashes, alphanumeric project names). The MCP server uses parameterized queries.

## System layers

### Layer 1: Native Auto Memory (built-in)

Claude Code's built-in memory system:
- `MEMORY.md` — first 200 lines loaded into every session's system prompt
- Topic files — referenced from MEMORY.md, loaded on demand
- Path: `~/.claude/projects/<project-hash>/memory/`
- Best for: Stable, high-level project knowledge (current state)

### Layer 2: B12 MCP Server (`b12_mcp_server.py`)

Custom FastMCP server providing 13 memory tools and 4 MCP resources. Replaces the old `mcp-memory-service` (pipx) with a ~2300-line server that delegates ML operations to a background embed daemon via Unix socket.

- **Tools** (13): `memory_store`, `memory_search`, `memory_update`, `memory_delete`, `memory_forget`, `memory_quality`, `memory_session_context`, `memory_consolidate`, `memory_refine`, `memory_surface`, `memory_export`, `memory_import`, `memory_dashboard`
- **Resources**: `b12://context/project/{name}`, `b12://stats`, `b12://profile`, `b12://health`
- **Database**: SQLite + sqlite-vec (local file)
- **Embeddings**: BGE-M3 (BAAI/bge-m3, 1024-dim, multilingual, cls pooling) via `embed_daemon.py` (runs locally, no API). Override via `MCP_EMBEDDING_MODEL`. Opt-in Q8_0 / Q4_K_M GGUF: set `B12_EMBED_BACKEND=gguf` + `B12_EMBED_GGUF_PATH=...`.
- **Runtime self-heal**: both long-lived daemons periodically verify that `sys.executable` still exists. This catches Homebrew patch upgrades that delete the old versioned Cellar interpreter while processes continue running from memory. MCP closes its listener, drains in-flight JSON-RPC requests, and exits for launchd `KeepAlive` respawn; embed exits after its current request and is restarted asynchronously by the next retrieval need. The health CLI also compares each running daemon's interpreter path/version with the B12 venv.
- **Search**: FTS5 hybrid — BM25 keyword + vector cosine + optional porter stemming. `memory_search` bounds only its wait for the serialized embed-daemon queue (`B12_MEMORY_SEARCH_DAEMON_QUEUE_TIMEOUT`, default 2s); on queue timeout hybrid mode immediately keeps its FTS results, while semantic-only mode fails soft. Store/embed operations retain the full queue wait and in-flight socket budget.
- **FTS5 tables**: `memory_fts` (unicode61, exact match), `memory_fts_stemmed` (porter unicode61, morphological), `memory_content_fts` (trigram, legacy)
- **Scoring**: Effective-stability decay-aware — combines retention, importance, relevance, and strength; importance and reinforcement flatten the aging curve
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
       |                                 Effective-stability combined scoring
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
- `~/.B12/memory-summaries/{project}-latest.md` — last session's summary
- `~/.B12/memory-summaries/{project}-history.md` — rolling last 5 sessions
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
- `~/.B12/memory-staging/working-memory.json`
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
4. Run hybrid scoring: `0.25×decay + 0.25×importance + 0.40×relevance + 0.10×strength_score`
   where `decay = max(1/(1 + age_days/(9·eff_stability)), 0.01)`, `eff_stability = strength × (1 + 4×importance)`, and `strength_score = min(strength/5, 1)`
5. Boost strength of top 3 results (+0.2, max 5.0) via CTE-aligned UPDATE
6. **Q2 long-session re-surface** (every Nth turn, default 20): asks
   `scripts/b12_long_session.py` for a small batch of THIS session's
   early-captured high-importance memories so they don't fade out of
   the model's effective working window
7. **T1 per-turn token cap** (~800 token chars-proxy) trims context
   tail before injection; survived memory rows are counted via either
   the Q4 4-field `|src:` anchor or, when Q4 reformat fell back to
   legacy `[type] preview` rows, an awk pattern that excludes the
   known header tokens (`directive`, `Note`, `long-session`, `trimmed`)
8. **T2 cumulative cap** (~80K token chars-proxy per session): logs
   skip events to `memory-logs/token-budget-skips.jsonl` and exits
   without inject when crossing the ceiling
9. **T3 dedup ledger** (`session-dedup-<sid>.txt`): only writes IDs
   AFTER T1/T2 accept the inject, so re-surface IDs that the model
   never saw won't suppress later turns
10. Return results as `additionalContext`

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
1. Parses the last ~3000 lines of the transcript JSONL file (optimized for large sessions)
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

**Session-summary identity**: `metadata.session_id` identifies a
`session_summary`. `upsert_session_summary()` takes `BEGIN IMMEDIATE`, updates the
newest active row in place while preserving `created_at` and refreshing
`updated_at`, and inserts only when none exists. An intentionally non-unique JSON
index supports legacy databases that already contain duplicates. Session-salted
content hashes, graph endpoints, external-content FTS tables, and sqlite-vec are
updated in the same transaction; existing duplicates and summaries without a
session ID are untouched.

Historical cleanup is explicit and dry-run-first via `scripts/b12_dedupe_session_summaries.py`.
It reuses the runtime selector, reports exact row IDs and platform totals, and executes in one
`BEGIN IMMEDIATE` only with `--execute`. Cleanup stamps only `deleted_at`; content, metadata,
hashes, graph edges, vectors, and summaries without a usable session ID remain untouched.

**Codex lifecycle split**: `Stop` captures only cheap per-turn goal progress.
`SessionEnd` owns detached summary extraction after rollout flush because upstream
caps teardown hooks at three seconds. The retired legacy `notify` path was a
turn-complete callback with a 120-second debounce, not a session-end signal.
Claude Code keeps the equivalent turn/session split.

**Write-side importance scoring** (`scripts/b12_importance.py`): before a row is stored, content is scored into a fractional `[0, 0.95]` importance band with **no manual tagging**. Five bands (trivial 0.30 / baseline 0.50 / fact 0.70 / decision 0.75 / memorable 0.90, max-wins) are floored by a language-agnostic **signal taxonomy** layered on the original remember/decision/fact tokens: explicit save-cues, commitment/obligation verbs (negation-aware), deadlines/dates, `@handle`/email person mentions, numeric-with-context values, and identifiers (PR#/git-SHA/host-path). Detectors cover **11 languages** (en, tr, zh, hi, es, fr, ar, ru, pt, id, de): the
six signal detectors plus native remember/decision/trivial lexicons per language,
matched script-aware — word-boundary for spaced scripts, NFKC-normalized substring
for CJK/Devanagari/Arabic (Arabic also tashkeel-stripped). The language is detected
by script presence (an optional `lang_code` overrides it); trivial cues only floor
when they are the whole memory. A read-only audit (`scripts/audit_importance_gap.py`,
`mode=ro`) measures the "importance gap" — how many high-value memories the heuristic
would only score at baseline — to decide whether a future ML classifier is worth it
(it reports the gap %, band distribution, scrubbed samples, the typed/untyped
split by `memory_type`, and excludes secret-suppressed rows; full design in
[docs/DESIGN-Phase2-Importance.md](DESIGN-Phase2-Importance.md)). A credential-shaped string (detected via the shared `b12_pii_scrubber` patterns) is **held at baseline** by the scorer, so writers that store its output unmodified do not amplify it; other writers bypass the cap and PII-scrubbing coverage varies by writer (the OpenCode plugin now scrubs + caps on its native TypeScript write path — PR #124). The exact per-writer cap/leak status and the recommended centralized fix are covered in the [design doc §4](DESIGN-Phase2-Importance.md) — see it for the authoritative treatment. Regex scans are bounded with linear (non-backtracking) patterns so a large pasted blob cannot stall the synchronous store. This value flows into `metadata.importance_score` and the effective-stability aging below; the read-side normalization is unchanged (see RET-3).

### PostToolUse hooks

**Feedback hook** (v3): Logs every memory store/search/update/quality call to `feedback.jsonl`. Tracks: action, query text, result count, session sequence number, scope compliance. Used by the weekly feedback digest.

**Working Context hook** (v1): Fires on Read/Edit/Write/Glob/Grep. Extracts the file path or search pattern. Persists to `working-memory.json` with atomic writes (tmp + rename). Tracks session ID to reset on session change.

## Decay model (FSRS effective stability)

Every memory has a `strength` field (0.3–5.0, default 1.0):

- **Retrieval boost**: +0.2 per access (capped at 5.0)
- **Weekly decay**: -0.05 for memories not accessed in 7 days (floor at 0.3) _(legacy background process; superseded by the FSRS curve below for retrieval scoring)_
- **Combined scoring**: `0.25 × decay + 0.25 × importance + 0.40 × relevance + 0.10 × strength_score`, where:
  - `decay = max(1 / (1 + age_days / (9 · eff_stability)), 0.01)` — FSRS power-forgetting curve (floor at 0.01)
  - `eff_stability = strength × (1 + 4 × importance)` — both explicit importance and reinforcement (strength) flatten the aging curve; old-but-valuable memories fade slowly, trivial untouched ones decay quickly
  - `strength_score = min(strength / 5, 1)` — normalized access count
  - `importance` normalizes `importance_score` across the **two write-side scales** that coexist in the data — fractional `[0, 0.95]` (`b12_importance.py`) and level multipliers `[0.7, 2.0]` (critical 2.0 / important 1.5 / normal 1.0 / temporary 0.7). A value `≥ 1.0` is a level multiplier and is divided by 2 (2.0→1.0, 1.5→0.75, 1.0→0.5); a fractional value `< 1.0` passes through unchanged; missing / null / non-numeric defaults to the `0.50` baseline; the result is clamped to `[0, 1]` (see RET-3)

This creates natural selection: memories that are frequently useful or explicitly marked important survive and get easier to find. Memories that were stored but never retrieved gradually fade but never fully disappear (decay floor at 0.01).

## FTS5 hybrid search

The database has three FTS5 virtual tables:

- **`memory_fts`** (B12): `unicode61` tokenizer, synced via 4 triggers. Used by B12 hooks for word-boundary keyword matching
- **`memory_fts_stemmed`** (B12, v11.7): `porter unicode61` tokenizer, synced via 4 triggers. Enables morphological matching — "running" matches "run", "configured" matches "config". Activated via `--stemmed` flag in `memory_search`
- **`memory_content_fts`** (legacy, originally from mcp-memory-service v10.13.0): `trigram` tokenizer, synced via 3 triggers. Kept for backward compatibility

B12 hook search combines:

- **BM25 keyword score** (FTS5 rank via `memory_fts`): Fast exact-match and phrase search
- **Vector cosine similarity** (sqlite-vec): Semantic meaning match
- **Combined scoring**: `0.25 * decay + 0.25 * importance + 0.40 * relevance + 0.10 * strength_score`
- **Relevance source**: FTS5 BM25 keyword score, with parallel semantic search (cosine similarity) when the embed daemon is available

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

Hooks that interpolate user input into SQLite queries apply character-level sanitization. The MCP server (`b12_mcp_server.py`) uses parameterized queries exclusively. Coverage details:
- **Keywords** (retrieval): Strip `'"();{}` characters
- **Hash prefixes** (browse): Allow only hex characters `[a-fA-F0-9]`
- **Project names** (pre-fetch, browse): Allow only `[a-zA-Z0-9_-]`

### Data isolation

- All data stays local (no cloud, no API calls for embeddings)
- Database is SQLite in user's home directory
- No secrets or credentials in hook scripts
- MCP server config uses `env: {}` (no environment secrets needed)

## Proactive surfacing engine

`scripts/surfacing_engine.py` — automatically injects relevant past memories when the user interacts with files or encounters errors. Called from `hooks/memory-proactive-surface.sh` (PostToolUse event).

- **Triggers**: `file` (file path → keywords), `error` (error message), `topic` (keywords)
- **Pipeline**: Rate limit check → build query → daemon semantic search → filter (similarity > 0.80, strength > 0.5, age > 24h, not already surfaced) → format as `additionalContext`
- **Rate limiting**: Max once per 5 tool calls, 60s cooldown between surfacings
- **Batch optimization**: Single DB connection for all candidate lookups (N+1 → 1 query)
- **State**: `~/.B12/surfacing-state.json` tracks surfaced IDs, cooldowns, tool call counts

## Consolidation engine

`scripts/consolidation_engine.py` — scheduled analysis that identifies duplicate groups, merge candidates, and contradictions across all memories.

- **Three-pass approach**: (1) Dedup actions (mark losers consumed), (2) Contradiction flags, (3) Merge groups from unconsumed memories
- **Contradiction detection**: ONNX NLI model via embed daemon (`nli_check` op)
- **Output**: Candidates written to `~/.B12/memory-logs/consolidation-*.json` for review

## Export/import

`scripts/export_import.py` — portable `.b12` format for backup, migration, or sharing memory snapshots.

- **Export**: Filters by tags, memory type, date range. Includes content hash for integrity
- **Import**: Merges into existing database, skips duplicates by content hash
- **Path security**: Export paths restricted to `~/.B12/exports/` (traversal protection)

## Web dashboard

`scripts/dashboard_server.py` (Flask) + `dashboard/dashboard.html` (Cytoscape.js) — visual memory browser.

- **API endpoints**: `/api/memories`, `/api/graph`, `/api/stats`, `/api/memory/<id>`, SSE events
- **Graph visualization**: Interactive Cytoscape.js graph of memory associations
- **Real-time stats**: Memory counts by type, graph edge counts, strength distribution

## Health report

`scripts/b12_health_report.py` — comprehensive health assessment combining quality audit, feedback digest, and session logs.

- **8 sections**: Executive summary, DB metrics, growth trends, retrieval performance (p50/p95/p99), retrieval quality, memory lifecycle, top issues, recommendations
- **Health score**: 0–100, formula: `100 - stale%×30 - orphan%×20 - dup%×20 - latency_penalty - empty_search_penalty`
- **Output**: Markdown or JSON to `~/.B12/memory-logs/health-report-YYYY-MM-DD.{md|json}`
- **CLI**: `python3 scripts/b12_health_report.py --db-path DB --format md|json`

## Antigravity and Gemini CLI integrations

`plugins/antigravity/b12/` is the native Antigravity package template. `install.sh --antigravity` stages it to `~/.B12/antigravity-plugin/b12/` with runtime absolute paths, installs that bundle through `agy plugin install`, and registers B12's stdio MCP server in Antigravity's global `~/.gemini/config/mcp_config.json` shape:

- **`plugin.json`**: plugin metadata.
- **`mcp_config.json`**: `mcpServers.B12.command,args,env` for the local B12 stdio server.
- **`hooks.json`**: Antigravity-native `PreInvocation`, `PostToolUse`, and `Stop` commands.
- **`rules/`**: B12 memory-use guidance for Antigravity.

`scripts/antigravity_hook_adapter.py` implements Antigravity's hook contracts directly, not by renaming Gemini adapters:

- **PreInvocation** calls `memory-session-start.sh` and returns `{"injectSteps":[{"ephemeralMessage":"..."}]}`. A conversation guard injects on the documented first invocation (`invocationNum=1`) and avoids repeated full context injection in the same conversation.
- **PostToolUse** consumes the documented payload safely, logs only a compact stderr receipt, and returns `{}` because Antigravity's documented fields do not reliably provide enough tool-result evidence for B12 retrieval/checkpoint semantics.
- **Stop** runs session-end only when `fullyIdle=true`, converts Antigravity's `USER_INPUT`/`PLANNER_RESPONSE` trajectory JSONL into B12's session-end transcript shape (including normalization of documented file-edit tool names and paths) in a private temporary staging file, adapts `conversationId`, `workspacePaths`, `transcriptPath`, and `terminationReason`, and returns a non-continuation decision. The shared session-end hook removes that temporary file after synchronous extraction, or after the optional background LLM extractor has consumed it.

Provider detection outside Antigravity hooks is intentionally not guessed from undocumented environment variables; hook payload metadata is authoritative. Repository validation is limited to local schema/help/plugin checks until a real authenticated Antigravity run proves MCP tools and hooks end-to-end.

`hooks/gemini/` remains the legacy Gemini CLI integration for Standard/Enterprise/Cloud or paid API-key users:

- **`b12-gemini-session-start.sh`**: Converts Gemini `SessionStart` to Claude Code format, calls `memory-session-start.sh`
- **`b12-gemini-session-end.sh`**: Converts Gemini session transcript (JSON → JSONL), calls `memory-session-end.sh` in background
- **`b12-gemini-tool-call.sh`**: Triggers memory retrieval on built-in Gemini tool calls (read_file, search_files, run_shell_command)
- **Installation**: `install.sh --gemini` registers hooks in `~/.gemini/settings.json`; it is not repointed to Antigravity.

## MCP resources

Four read-only resources registered via `@server.resource()` decorator, accessible via `b12://` URIs:

| URI | Returns |
|-----|---------|
| `b12://context/project/{name}` | Pre-fetched project context (top memories, last summary, instructions) |
| `b12://stats` | Memory statistics (counts by type, graph edges, embedding coverage) |
| `b12://profile` | User profile from `user-profile.md` |
| `b12://health` | Quick health check (stale count, embedding %, recent growth) |

Resources complement tools by providing passive, cacheable content that any MCP client can read without triggering side effects.

## SubagentStart per-agent recall (v11.48+)

Three cooperating hooks (Codex review PR #49 noted the split — earlier draft attributed all responsibilities to `memory-subagent-start.sh` alone, which was wrong):

- **`hooks/memory-team-create.sh`** — PostToolUse hook matching the `TeamCreate` tool. Writes `~/.B12/state/team-<team_id>.json` capturing the team roster (per-member `agent_id`, `agent_type`, `name`) plus the caller session_id for re-entry lookups.
- **`hooks/memory-subagent-start.sh`** — fires when a teammate Claude actually starts up. Tails the parent transcript for the last `Agent` / `Task` tool_use to recover the original task description (subagent payloads don't carry `.description` natively), then performs task-scoped recall (daemon socket first, direct SQLite FTS5 fallback on cold-daemon path).
- **`hooks/memory-session-start.sh`** — when launched as a teammate, matches the runtime `CLAUDE_CODE_AGENT_ID` env against `.members[].agent_id` in any recent `team-*.json` file. On hit, injects a `TEAMMATES` block listing co-spawned agents as `additionalContext`. (The team-roster lookup is here, not in subagent-start, because SessionStart fires for every Claude launch including teammates.)

Cross-platform DB-path resolution uses a `case "$(uname -s)"` switch (Darwin / Linux / MINGW / CYGWIN) inline. Future cleanup: extract into a shared `b12_resolve_db_path()` helper in `hooks/_b12_common.sh`.

## Cursor MDC + PageRank surfacing (v11.49+)

SessionStart now surfaces two project-derived context blocks:

- **`CURSOR RULES`** (`scripts/cursor_mdc.py`): parses `.cursor/rules/*.mdc` YAML frontmatter (stdlib-only) and emits the rule body for every Auto-Attached rule whose `globs` pattern matches at least one file in the current Working Memory. Globs use `fnmatch`-style matching; multiple globs OR together per rule.
- **`LIKELY-NEXT FILES`** (`scripts/file_pagerank.py`): pure-numpy PageRank over the import graph of the project's `.py`/`.ts`/`.tsx`/`.js`/`.jsx` files. Cached at `~/.B12/state/pagerank-<repo-hash>.json` with a 24h TTL and git HEAD invalidation; recomputed on first SessionStart after a new commit.

Both blocks are token-budgeted within the SessionStart 6000-char cap and trim before older tier-1b/1c sections.

### PageRank memory safety (the 2026-06 OOM fix)

The original `file_pagerank._pagerank` built a **dense `n × n` float32 matrix with no node cap**, and both SessionStart and `b12_smoke.sh` ran it against `$HOME` (~167k code files). At `n = 167k` that matrix is ~112 GB *per process*; several concurrent runs exhausted 64 GB of RAM, starved WindowServer, and tripped the macOS userspace watchdog → kernel panic + reboot. The fix is defense-in-depth — any **one** layer prevents the crash; together they make it unreachable:

1. **Sparse power iteration.** `_pagerank` now iterates over the adjacency lists as three flat `(src, dst, weight)` arrays with a vectorized `np.add.at` scatter-add — O(nodes + edges) memory, never a dense matrix. A 100k-node graph costs a few MB; verified to reproduce the former dense ranking to float rounding (top-N identical).
2. **Node cap.** `top_n` refuses a root with more than `B12_PAGERANK_MAX_NODES` (default 20000) candidate files — it logs the reason and returns `[]` *before* reading any file, so a giant tree is never walked/read.
3. **SessionStart guard.** The hook skips pagerank entirely when the CWD is `$HOME` / `/`, is not a git repo, or fails a **cheap bounded pre-count** (`find … | head -n MAX+1`, which closes the pipe so `find` stops early instead of walking an unbounded tree). It runs the Python under a hard wall-clock budget and **kills the whole process group** on expiry (`timeout -s KILL` where available; otherwise background + poll + `kill -KILL -<pgid>`) so an orphaned numpy child can never survive the hook — the exact path that leaked the runaway allocator.
4. **Process self-limits.** Run as a script, `file_pagerank` installs a SIGALRM wall-clock self-timeout (`B12_PAGERANK_TIMEOUT_S`, default 8s — orphan-proof), calls `os.setsid()` (so the hook's group-kill is clean), and best-effort sets `RLIMIT_AS` (`B12_PAGERANK_MAX_MEM_MB`, default 2048; a real backstop on Linux, a no-op on macOS).
5. **Smoke harness.** `b12_smoke.sh` drives the hooks against a tiny throwaway git repo, never `$HOME`.

Long-lived daemons (`embed_daemon.py`, `b12_mcp_daemon.py`) additionally carry a `getrusage(ru_maxrss)`-based RSS self-guard (`shared_patterns.rss_exceeds`): above `B12_EMBED_MAX_RSS_MB` / `B12_MCP_MAX_RSS_MB` they log and exit cleanly so the host respawns a fresh process. `getrusage` is a pure read, so the guard works on macOS — unlike `RLIMIT_AS` / `ulimit -v`, which Darwin does not enforce.

## Exact-KNN recall over memory_embeddings (v11.49+; default-on since 2026-06-19)

When `[recall.ann]` is enabled and the embeddings table is at least `threshold_count` rows, `_semantic_search` and `_recall` in `embed_daemon.py` use sqlite-vec's KNN MATCH:

```sql
SELECT rowid FROM memory_embeddings
WHERE content_embedding MATCH ? AND k = ?
```

This bypasses the `ORDER BY m.id DESC LIMIT 500` full-scan cap that silently hides older memories at production scale. The candidate set is oversampled 30× (capped at 150) so the three downstream attrition layers — active-memory filter, skip_ids ledger, similarity threshold — have headroom before triggering a fall-through to the existing full-scan path. ANN errors at any stage fall through transparently; an empty `MATCH` result is logged (likely sqlite-vec failure or empty vec table) and `threshold_count` is clamped to `[100, 1e6]`.

**Default-on (`enabled = true`, `threshold_count = 500`).** `MATCH` is *exact* brute-force KNN over normalized vectors, not an approximate index, so it reproduces the full-table cosine ranking exactly. The 2026-06-19 A/B (`benchmarks/ann_ab_test.py`, 300 real-vector probes) measured `overlap@5(MATCH, exact) = 1.00`, while the legacy LIMIT-500 path matched the true ranking only ~15% of the time (87% of queries had their true nearest neighbour beyond the 500 newest rows). `threshold_count` is set to the cap boundary (500): at/below it the full-scan already sees every row, so MATCH adds nothing; above it MATCH is the only path that doesn't drop older memories. The flag/threshold are read once per process (`b12_config._load` is `@lru_cache`), so changing them requires an embed-daemon restart.

## MCP daemon maintenance: connection reaping + WAL checkpoint (P2/P7)

`scripts/b12_mcp_daemon.py` runs two background asyncio tasks for the lifetime of the shared daemon:

- **Idle-connection reaper — DISABLED by default (`B12_MCP_IDLE_TIMEOUT=0`, since 2026-06-27).** Each accepted connection is tracked with a last-activity timestamp (bumped on every inbound JSON-RPC line), and the reaper *can* cancel connections idle beyond `B12_MCP_IDLE_TIMEOUT`; a `B12_MCP_MAX_CONN` cap (default 256) is retained as an emergency backstop that evicts the most-idle connection under pressure. **The reaper is off because reaping a live connection is NOT client-invisible.** The original design assumed "the host proxy sees the socket close and the host respawns it on next use" — but Claude Code does *not* auto-respawn an exited stdio MCP server; it marks B12 "disconnected" until a manual `/mcp` reconnect. So reaping a live-but-idle session (no B12 tool call for the timeout window — a routine coding stretch) dropped B12 mid-session, and reconnecting merely reset the idle clock so it dropped again. Reaping is also unnecessary for cleanup: a closed tab, a crashed host, and a SIGKILL'd proxy all deliver socket EOF to the daemon (`handle_client` completes normally and the connection is removed), so connections track open editors 1:1 and self-clean. **Caveat (now absorbed by Fix C):** the `MAX_CONN` cap evictor, the RSS self-guard's `os._exit`, a launchd restart, and a daemon redeploy each close the socket the same way and used to surface as a one-time client-visible drop. **Fix C (shipped 2026-06-27) makes the proxy reconnect transparently:** on socket EOF *while stdin is still open*, `_proxy_session` re-dials the daemon with capped backoff (total budget `B12_MCP_RECONNECT_BUDGET`, default 30s ≈ one launchd respawn), replays the cached `initialize` request (swallowing the new response after a protocol/capability-drift check) and `notifications/initialized`, synthesizes a retryable JSON-RPC error (`-32001`) for each in-flight request so the host fails fast instead of hanging, and resumes piping — so the host never sees the break. It exits (legacy "disconnected until manual `/mcp`") only if reconnection can't succeed within the budget or the negotiated session materially drifts. Disable with `B12_MCP_PROXY_RECONNECT=0`. See `b12_mcp_daemon.py:_reap_idle_connections` / `b12_mcp_server.py:_proxy_session` (+ `_observe_client_line`/`_observe_server_line`/`_init_responses_compatible`).
- **WAL checkpoint timer.** Every `B12_MCP_WAL_CHECKPOINT_INTERVAL` (default 300s) the daemon runs `PRAGMA wal_checkpoint(TRUNCATE)` **off the event loop** (`asyncio.to_thread`) on a dedicated short-lived connection with a short `busy_timeout` — *not* synchronously on the loop. A TRUNCATE checkpoint can wait on a contending reader up to `busy_timeout`, so running it synchronously on the single event-loop thread would freeze every MCP client for that window; the worker-thread connection keeps the daemon responsive and a contended cycle simply retries next interval. `PRAGMA wal_autocheckpoint=100` only fires on writes, so without this an idle daemon or long-lived legacy reader could let the WAL grow unbounded.

Every connection runs `PRAGMA synchronous=NORMAL` + `temp_store=MEMORY` (`b12_mcp_server.py:_configure_connection`): NORMAL is the corruption-safe WAL durability mode — committed transactions survive any app/process crash; only an OS crash or power loss can roll back the most-recent commit(s).

## MCP SQLite concurrency: per-connection reads + a single serialized writer (BB1)

The daemon serves every CLI tab on one event loop. Originally each DB-touching handler held a global `async with _db_lock: <sync _db.execute(...)>` on the loop thread, so all tabs serialized on that lock **and** a slow synchronous sqlite call blocked the whole loop — a contended write could stall every tab up to `busy_timeout` (30s). BB1 replaces that with a per-connection model in `b12_mcp_server.py`:

- **Reads** run off the event loop on a `ThreadPoolExecutor` (`B12_MCP_READ_POOL`, auto-sized to `max(4, min(8, cpu))`). Each worker thread owns its own connection (cached in `threading.local()`); WAL allows unlimited concurrent readers, so a slow read on one tab never blocks others. `_run_read` wraps each op in one `BEGIN`/`COMMIT` for a consistent snapshot.
- **All writes + SELECT-then-write transactional ops** go through ONE serialized writer thread (`max_workers=1`) with a single connection pinned to it. FIFO submission replaces the asyncio lock as the serialization mechanism, and `_run_write` wraps each op in `BEGIN IMMEDIATE` — so each logical op (e.g. dedup-then-insert) is atomic on one connection / one thread and two writes never interleave.
- **No connection is ever shared across threads** — this is why a naive `asyncio.to_thread(_db.execute)` was rejected: the shared `_db` is `check_same_thread=True` and the ~16 lock blocks are multi-statement transactions that must not be split across threads. The unit of offload is therefore a whole transactional op, dispatched via `await _read(op)` / `await _write(op)` (a nested `def op(db): ...` per former lock block — no fragile edits across the ~47 `db.execute` call sites).
- `memory_delete(hard=True)` embeds `b12_gc.collect_one`, which opens its **own** connection mid-op; it runs via `_write_raw` (autocommit on the writer thread, no held `BEGIN IMMEDIATE`) so the writer never deadlocks against `collect_one`'s connection.

Preserved across the rewrite: the stdio-proxy wire contract, legacy in-process mode, the per-session tracker ContextVar, the atexit flush, `_ensure_schema` / `_flush_session_tracker` signatures, and all pragmas. `scripts/tests/test_daemon_concurrency.py` proves no thread/`ProgrammingError`, dedup atomicity, concurrent-equals-serial final state, and that a slow read/write never blocks the other pool.

## Codex cloud_exec / cloud_apply ingestion (v11.52+)

`scripts/codex_session_end.py:_extract_cloud_tasks(info)` pairs `cloud_exec` events with `cloud_apply` events by `cloud_task_id` and emits structured task rows: `{cloud_task_id, task, status, files, branch}`. The rows are appended to the session summary's `## Cloud Tasks` block (one bullet per task) and the whole summary is stored as a single `memory_type='session_summary'` row — there is **no** separate `cloud_task` memory_type. Cloud signal lives inside the per-project session summary file and is searchable via FTS5 / semantic search on the summary content. Gated on `B12_CODEX_CLOUD_INGEST=true` (default off) — cloud sessions are rare and the matching is best-effort.

## Continue.dev + Cline platform glue (v11.50+, v11.51+)

`transcript_adapter._parse_continue()` reads `~/.continue/sessions/*.json` (single-file JSON, not JSONL) and yields normalized turn records. `install.sh --continue` writes `~/.continue/mcpServers/b12.yaml` (MCP entry) + `~/.continue/rules/b12-memory.md` (rules) + `~/.continue/settings.json` (lifecycle hooks, v11.58+). Continue CLI's `extensions/cli/src/hooks/hookConfig.ts` reads `~/.continue/settings.json` with the Claude Code hook schema verbatim, so the install writes the same `.hooks` block as `config/settings-template.json` and merges non-hook keys (theme, model overrides) without overwriting them.

`config/cline-hooks/{TaskStart,UserPromptSubmit,PreCompact}` are deployed to `~/Documents/Cline/Hooks/` (authoritative location per `cline/cline:.clinerules/hooks/README.md`). Each shim normalizes Cline's nested JSON payload (`.workspaceRoots[0]` → `.cwd`, `.userPromptSubmit.prompt` → `.prompt`) before delegating to the corresponding B12 hook, and translates the response's `hookSpecificOutput.additionalContext` into Cline's `contextModification` wire key (camelCase, verified against `cline/cline:src/core/hooks/templates.ts`). PreCompact is a passive `{cancel: false}` placeholder pending upstream stabilization of `.transcript_path` + `.preCompact` payload shape.

## Classifier retraining recipe (PR15 — v11.x BGE-M3 1024-dim)

The shipped `models/classifier-head.pkl` is a sklearn `LogisticRegression`
fitted on 1024-dim BGE-M3 embeddings, mapping content to 7 canonical
memory types (`decision`, `error_fix`, `learning`, `preference`,
`observation`, `knowledge`, `session_summary`). The embed daemon loads
it at startup; on classify ops it encodes the input → predicts.

To regenerate the head against a different base model or with fresh
training data:

```bash
# 1. Build silver-label corpus from your live DB
#    (or substitute /tmp/b12-setfit-candidates.json with a hand-labeled file
#    of the same {content_preview, proposed_label, split} shape)
~/.local/b12-venv/bin/python3 scripts/build_classifier_corpus.py

# 2. Train head + save daemon-compatible pickle
~/.local/b12-venv/bin/python3 scripts/train_classifier_head_pkl.py

# 3. (optional) override base model
B12_TRAIN_MODEL=BAAI/bge-large-en-v1.5 \
  ~/.local/b12-venv/bin/python3 scripts/train_classifier_head_pkl.py

# 4. Restart the embed daemon — it lazy-loads from
#    ~/.B12/models/classifier-head.pkl on the next classify op
```

The training writes `~/.B12/models/classifier-head.pkl`. The repo
copy under `models/classifier-head.pkl` is the version shipped to new
installers (copied at install time).

**Silver-label corpus**: `scripts/build_classifier_corpus.py` reads
typed memories from the live DB and maps fine-grained types (`gotcha`,
`bugfix`, `architecture`, etc.) to the canonical 7-label schema. This
is a "trust the prior typing" approach — it works because most live
memories were typed either by Claude using `classify_by_prefix` or by
the user explicitly. For higher-quality training, run a TeamCreate
gold-label pass (Claude + Gemini + Codex cross-validation) and replace
the corpus.

**Baseline accuracy**: silver-label corpus (1797 items, 80/20 split) →
~69% test accuracy. Gold-validation typically lifts this to ~84% on
the same architecture.

**Embedding dim guard**: the daemon refuses to use a pickle whose
`head.n_features_in_` doesn't match its runtime `EXPECTED_DIM` (set
from the loaded model). Set `B12_CLASSIFIER_BACKEND=off` to silence
the per-call error while you regenerate.

## Limitations and future work

### Current limitations

1. **Embedding-quality vs disk**: BGE-M3 (1024-dim, 100+ languages) is the default since v11.34. FP32 weights are ~2.2GB on disk; users on tight disk budgets can switch to a Q8_0 or Q4_K_M GGUF via `B12_EMBED_BACKEND=gguf` after installing `llama-cpp-python`.

2. **Contradiction detection coverage**: `contradiction_resolver.py` provides ONNX NLI-based contradiction detection, but it runs as a scheduled task (graph enrichment), not inline at write time. Contradicting memories may coexist until the next enrichment cycle.

3. **Linear memory scan**: Write-time merge checks against all memories for similarity. As the database grows (10K+ memories), this may need index optimization.

### Planned improvements
- **Graph-based traversal**: Walk memory associations for context expansion during retrieval
- **Memory clustering**: Group related memories for batch review and consolidation
- **LLM-assisted extraction**: Use MCP sampling (when supported) for higher-quality session-end extraction
- **0G integration**: Decentralized storage + TEE-based embedding for privacy-preserving cloud memory
