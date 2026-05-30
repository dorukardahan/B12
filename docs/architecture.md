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
- **Search**: FTS5 hybrid — BM25 keyword + vector cosine + optional porter stemming
- **FTS5 tables**: `memory_fts` (unicode61, exact match), `memory_fts_stemmed` (porter unicode61, morphological), `memory_content_fts` (trigram, legacy)
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
4. Run hybrid scoring: `0.3×decay + 0.3×importance + 0.4×FTS5_relevance`
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

### PostToolUse hooks

**Feedback hook** (v3): Logs every memory store/search/update/quality call to `feedback.jsonl`. Tracks: action, query text, result count, session sequence number, scope compliance. Used by the weekly feedback digest.

**Working Context hook** (v1): Fires on Read/Edit/Write/Glob/Grep. Extracts the file path or search pattern. Persists to `working-memory.json` with atomic writes (tmp + rename). Tracks session ID to reset on session change.

## Ebbinghaus decay model

Every memory has a `strength` field (0.3–5.0, default 1.0):

- **Retrieval boost**: +0.2 per access (capped at 5.0)
- **Weekly decay**: -0.05 for memories not accessed in 7 days (floor at 0.3)
- **Combined scoring**: `0.3 × exp(-age/strength) + 0.3 × importance + 0.4 × FTS5_rank`, where `importance` normalizes `importance_score` across the **two write-side scales** that coexist in the data — fractional `[0, 0.95]` (`b12_importance.py`) and level multipliers `[0.7, 2.0]` (critical 2.0 / important 1.5 / normal 1.0 / temporary 0.7). A value `≥ 1.0` is a level multiplier and is divided by 2 (2.0→1.0, 1.5→0.75, 1.0→0.5); a fractional value `< 1.0` passes through unchanged; missing / null / non-numeric defaults to the `0.50` baseline; the result is clamped to `[0, 1]` (see RET-3)

This creates natural selection: memories that are frequently useful survive and get easier to find. Memories that were stored but never retrieved gradually fade but never fully disappear (minimum 0.3).

## FTS5 hybrid search

The database has three FTS5 virtual tables:

- **`memory_fts`** (B12): `unicode61` tokenizer, synced via 4 triggers. Used by B12 hooks for word-boundary keyword matching
- **`memory_fts_stemmed`** (B12, v11.7): `porter unicode61` tokenizer, synced via 4 triggers. Enables morphological matching — "running" matches "run", "configured" matches "config". Activated via `--stemmed` flag in `memory_search`
- **`memory_content_fts`** (legacy, originally from mcp-memory-service v10.13.0): `trigram` tokenizer, synced via 3 triggers. Kept for backward compatibility

B12 hook search combines:

- **BM25 keyword score** (FTS5 rank via `memory_fts`): Fast exact-match and phrase search
- **Vector cosine similarity** (sqlite-vec): Semantic meaning match
- **Combined scoring**: `0.3 * decay + 0.3 * importance + 0.4 * relevance`
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

## Gemini CLI hook integration

`hooks/gemini/` — adapter scripts that give Gemini CLI full B12 hook coverage:

- **`b12-gemini-session-start.sh`**: Converts Gemini `SessionStart` to Claude Code format, calls `memory-session-start.sh`
- **`b12-gemini-session-end.sh`**: Converts Gemini session transcript (JSON → JSONL), calls `memory-session-end.sh` in background
- **`b12-gemini-tool-call.sh`**: Triggers memory retrieval on built-in Gemini tool calls (read_file, search_files, run_shell_command)
- **Installation**: `install.sh --gemini` registers hooks in `~/.gemini/settings.json`

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

## ANN index over memory_embeddings (v11.49+)

When `~/.B12/config.toml` enables `[recall.ann]` and the embeddings table grows past `threshold_count` (default 10000), `_semantic_search` and `_recall` in `embed_daemon.py` switch to sqlite-vec's KNN MATCH:

```sql
SELECT rowid FROM memory_embeddings
WHERE content_embedding MATCH ? AND k = ?
```

This bypasses the LIMIT-500 full-scan cap that silently hides ~85% of memories at production scale. The candidate set is oversampled 30× (capped at 150) so the three downstream attrition layers — active-memory filter, skip_ids ledger, similarity threshold — have headroom before triggering a fall-through to the existing full-scan path. ANN errors at any stage fall through transparently.

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

1. **Embedding-quality vs disk**: BGE-M3 (1024-dim, 100+ languages) is the default since v11.34. FP32 weights are ~2.2GB on disk; users on tight disk budgets can switch to a Q8_0 or Q4_K_M GGUF via `B12_EMBED_BACKEND=gguf` after installing `llama-cpp-python`. `docs/B12_embed_quant_eval_2026-05.md` (P-EVAL) tracks the quality/speed/disk trade-off.

2. **Contradiction detection coverage**: `contradiction_resolver.py` provides ONNX NLI-based contradiction detection, but it runs as a scheduled task (graph enrichment), not inline at write time. Contradicting memories may coexist until the next enrichment cycle.

3. **Linear memory scan**: Write-time merge checks against all memories for similarity. As the database grows (10K+ memories), this may need index optimization.

### Planned improvements
- **Graph-based traversal**: Walk memory associations for context expansion during retrieval
- **Memory clustering**: Group related memories for batch review and consolidation
- **LLM-assisted extraction**: Use MCP sampling (when supported) for higher-quality session-end extraction
- **0G integration**: Decentralized storage + TEE-based embedding for privacy-preserving cloud memory
