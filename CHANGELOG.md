# Changelog

# [10.7.0](https://github.com/dorukardahan/B12/compare/v10.6.0...v10.7.0) (2026-02-26)


### Features

* **templates:** Rewrite all platform instruction templates with full B12 API ([bb337c9](https://github.com/dorukardahan/B12/commit/bb337c9ad6e9e71fe722141c647c539b96bd9b64))

# [10.6.0](https://github.com/dorukardahan/B12/compare/v10.5.1...v10.6.0) (2026-02-26)


### Features

* **hooks:** Inject full behavioral instructions after context compression ([3746b7e](https://github.com/dorukardahan/B12/commit/3746b7e042e25f44c5e517d3896f174037f76312))

## [10.5.1](https://github.com/dorukardahan/B12/compare/v10.5.0...v10.5.1) (2026-02-26)


### Bug Fixes

* Address code review issues from multi-platform integration ([73df115](https://github.com/dorukardahan/B12/commit/73df1152f761e4059bd75bba49de5c7bbf729c2c))

All notable changes to B12 are documented in this file.

## v10.4 (2026-02-25) — ~/.B12 Migration

### Breaking Changes
- **All B12 data/hooks moved from `~/.claude/` to `~/.B12/`** — platform-agnostic, no longer tied to Claude Code directory
- `B12_DATA_DIR` default: `~/.claude` → `~/.B12`
- `B12_HOOK_DIR` default: `~/.claude/hooks` → `~/.B12/hooks`
- Hooks, scripts, summaries, staging, logs, backups all live under `~/.B12/`

### Added
- **Auto-migration** in `install.sh`: copies existing data from `~/.claude/` to `~/.B12/` using `cp -rn` (safe, doesn't delete originals)
- Codex-only users can now install B12 without needing a `~/.claude/` directory

### Changed
- All 9 hook scripts, 5 Python scripts, 5 launchd plists, and 4 documentation files updated
- `settings-template.json` hook commands now point to `~/.B12/hooks/`
- `install.sh` deploys to `~/.B12/hooks/` and creates data dirs under `~/.B12/`

## v10.3 (2026-02-25) — Codex CLI Full Support

### Added (Layer 2)
- **Notify hook** (`hooks/b12-codex-notify.sh`): Triggered by Codex's `agent-turn-complete` event. Uses 2-minute debounce to detect session end, then processes rollout JSONL to extract session summaries and micro-memories.
- **Transcript adapter** (`scripts/transcript_adapter.py`): Unified parser for both Claude Code and Codex CLI transcript formats. Normalizes to common `Message` and `SessionInfo` dataclasses.
- **Session end processor** (`scripts/codex_session_end.py`): Extracts decisions, errors, learnings, preferences from Codex rollouts using `shared_patterns.py`. Stores session summaries and micro-memories to shared SQLite with correct schema.
- **B12 Codex Skill** (`skills/b12/SKILL.md`): Instructs Codex to proactively search memory at session start and store findings before session end.
- Installer now configures `notify` in `config.toml` and installs B12 skill to `~/.codex/skills/b12/`

### Added (Layer 1)
- **Codex CLI support**: B12 MCP server now works with OpenAI's Codex CLI. Same SQLite database is shared between Claude Code and Codex — memories are cross-platform.
- **`--codex` installer flag**: `./install.sh --codex` injects B12 MCP server into `~/.codex/config.toml` and appends memory instructions to `~/.codex/AGENTS.md`.
- **`config/codex-config-template.toml`**: TOML config template for Codex MCP server registration.
- **`config/codex-agents-template.md`**: Memory behavioral instructions for Codex's AGENTS.md.

### Changed
- Installer banner bumped to v10.3
- README updated with Codex CLI setup section
- Setup docs updated with Codex installation steps

## v10.1 (2026-02-25) — Path Isolation + Context Cap

### Fixed
- **Script/data path conflation**: `B12_DATA_DIR` no longer controls script import paths. New `B12_HOOK_DIR` env var controls hook code location independently. Fixes `ModuleNotFoundError: shared_patterns` when `B12_DATA_DIR` pointed to a per-setup directory.
- **Inconsistent `write_time_merge` import**: Was hardcoded to `~/.claude/hooks/scripts` while others used `B12_DATA_DIR`. Now unified under `B12_HOOK_DIR`.

### Added
- **Context injection hard cap** (6000 chars): SessionStart progressively trims variable sections when context exceeds limit. Trim order: memory pre-fetch → cross-project hints → feedback digest → hard truncation. Prevents long-context 429 errors on extended sessions.
- **Environment variables documentation**: README now has a table of all B12 env vars with defaults and examples.

### Changed
- 4 files updated: `memory-session-start.sh`, `memory-precompact.sh`, `memory-session-end.sh` (2 locations)
- `CLAUDE.md` updated with path separation rule and context cap documentation

## v10.0 (2026-02-20) — Custom MCP Server

### Breaking Changes
- Replaced `mcp-memory-service` (pipx) with `b12_mcp_server.py` — custom FastMCP server (~400 lines vs 804MB package)
- MCP server renamed from `"memory"` to `"B12"` in all configs
- Tool names: `mcp__memory__*` → `mcp__B12__*`
- Python environment: `pipx install mcp-memory-service` → `b12-venv` with `pip install mcp sentence-transformers sqlite-vec`

### Added
- `b12_mcp_server.py` — minimal FastMCP server with 4 tools (memory_store, memory_search, memory_update, memory_quality)
- `embed_daemon.py` — background embedding daemon with Unix socket IPC and `fcntl.flock` singleton
- `contradiction_resolver.py` — ONNX NLI contradiction detection (83MB model vs 8GB Ollama)
- `graph_enrich.py` — memory graph enrichment (related/follows/contradicts edges)
- `shared_patterns.py` — shared regex patterns for English and Turkish
- B12 pill notifications (`💊 B12 🧠`) for visible memory operations
- Fuzzy time-range search (`after`/`before` with ±1 day buffer)
- `_require_db()` null guard on all MCP tool functions
- WAL checkpoint before backups
- FTS5 operator sanitization (AND/OR/NOT/NEAR)
- `install.sh` excludes deprecated scripts

### Fixed
- Content hash unified across all 3 code paths (`strip().lower()`)
- `memory_quality analyze` NULL crash on fresh DB
- BSD sed word boundary compatibility (macOS)
- Stale pipx/venv paths in 5+ files
- Embed daemon singleton prevents multiple instances

### Changed
- Documentation overhaul: README, setup guide, architecture docs fully rewritten
- Created CHANGELOG.md (extracted from README)
- MCP server config template: `mcp-server-template.json` → `mcp-b12-template.json`

### Removed
- Dead code: `combined_score()`, `preserve_timestamps`, `IntegrityError` handler
- Ghost tools from context (`memory_graph`, `memory_cleanup`)
- Deprecated `mcp-server-template.json`
- `patch_validate_input.py` no longer needed (B12 server doesn't have the SDK bug)

## v9.1 (2026-02-16) — MCP SDK Validation Fix

- **Fix intermittent `memory_store` validation error**: Root-caused `"Input validation error: 'content' is a required property"` to MCP SDK's `jsonschema.validate` in `server_impl.py`. The `call_tool()` decorator defaults to `validate_input=True`, but the handler does its own validation — matching FastMCP's approach of `validate_input=False`
- **New `scripts/patch_validate_input.py`**: Idempotent patch that disables SDK-level input validation in `server_impl.py`. Supports `--check`, `--revert`. Auto-applied by `install.sh` and re-applied by `memory-upgrade.sh` after `pipx upgrade`
- **Upgrade script updated**: `memory-upgrade.sh` now has 4 steps: upgrade → migrate → patch → bytecache clear

## v9.0 (2026-02-16) — mcp-memory-service v10.13.0 Migration

- **mcp-memory-service v10.13.0 migration**: Upstream upgrade wiped all 5 B12 patches from `sqlite_vec.py`. Instead of re-patching, B12 hooks are now fully independent of server-side code
- **Retired `apply-patches.py`**: No longer needed — B12 hooks do their own hybrid search (bash sqlite3 + Python re-rank) directly on the database, independent of server patches
- **New `scripts/migrate_v10_13.py`**: One-time migration script that creates the native `memory_content_fts` FTS5 table (trigram tokenizer) on existing databases. v10.13.0 skips this table creation on existing DBs, breaking native hybrid search
- **SessionEnd tool tracking update**: Tool name counters now match both old (`memory_store`) and new (`store_memory`) MCP tool names for accurate metrics across the transition
- **Installer migration step**: `install.sh` now runs DB migration automatically to ensure `memory_content_fts` exists

## v8.2 (2026-02-15) — Turkish Support & Bug Fixes

- **PreCompact IndentationError fix**: Python heredoc had 16-space indent instead of 12, causing SyntaxError since creation — PreCompact hook never successfully extracted transcript content
- **write_time_merge.py rename**: `scripts/write-time-merge.py` → `scripts/write_time_merge.py`. Python cannot import modules with hyphens; `from write_time_merge import merge_or_insert` was silently failing via ImportError catch
- **Turkish keyword extraction**: Replaced ASCII-only `grep -oE '[a-zA-Z0-9_.-]{3,}'` with Python `re.findall(r'[\w]{3,}', text, re.UNICODE)` + 60+ Turkish/English stop words. Queries like "hafıza sistemi kararları" now extract all keywords instead of returning empty
- **Semantic vector fallback**: When FTS5 returns 0 results, falls back to pure vector similarity search (SentenceTransformer embedding, cosine similarity > 0.3 threshold, 4s timeout, top 5). Only triggers on zero-result queries — no overhead on normal retrievals
- **Turkish SessionEnd patterns + scoring**: Added Turkish alternatives to all 4 regex patterns (DECISION_RE, ERROR_RE, LEARNING_RE, PREFERENCE_RE) and Turkish keywords to `score_extraction()`. Turkish decisions, errors, and learnings are now captured
- **Filename reference cleanup**: Updated all references from `write-time-merge.py` to `write_time_merge.py` across README, docs, and internal comments
- **sqlite_vec double-load fix**: `_ensure_sqlite_vec_loaded()` in `write_time_merge.py` now checks `vec_version()` before loading the extension, preventing `OperationalError` when `merge_or_insert` is called from the SessionEnd embed script which already has sqlite_vec loaded
- **Semantic fallback + re-rank fix**: Two bugs — (1) semantic fallback opened DB without loading sqlite_vec extension, causing silent `no such module: vec0` error; (2) both semantic fallback and vector re-rank used `timeout` command which doesn't exist on macOS. Replaced with Python `signal.alarm()` for self-timeout. Both features were completely non-functional since creation

## v8.1 (2026-02-09) — Query-Adaptive Search

- **Query-adaptive search mode**: Retrieval hook (v4) classifies queries before deciding on vector re-rank. Negation/adversarial → always re-rank (hybrid +18pp). Attribute/preference → skip re-rank (keyword +4.7pp). Default → re-rank. Few results (< 2) → fallback re-rank regardless. Saves ~200ms on ~20% of queries
- **LoCoMo benchmark**: Eval script with keyword/hybrid/adaptive/compare modes. 10 conversations, 1986 QA pairs. Results: keyword 25.8%, hybrid 23.9%, adaptive 24.1% (Recall@3 Answerable). Hybrid wins overall (36.5%) due to adversarial filtering

## v8 (2026-02-09) — Hybrid Retrieval

- **Vector re-rank in retrieval hook**: FTS5 top-10 candidates → Python cosine re-rank → top-5 results. Uses sentence-transformers with 3-second timeout; falls back to FTS5-only silently
- **Phrase-aware FTS5 queries**: Bigram detection in both hook and MCP service. Compound terms like "docker compose" become `NEAR(docker compose, 2)` instead of `docker OR compose`
- **Adaptive hybrid weights**: Technical queries (error codes, file paths) get 50/50 vector/FTS5; conceptual queries get 70/30 (default)
- **Softened Ebbinghaus decay**: `exp(-t/(S*3))` instead of `exp(-t/S)`. At strength=1.0: 2-day memory 0.13→0.51, 7-day 0.001→0.10
- **Project hierarchy detection**: Walks up directory tree to find `.git` root. Running from `/B12/benchmarks/locomo` now finds `proj:B12` memories
- **Importance-based pre-fetch**: `ORDER BY importance_score * strength DESC` instead of `created_at DESC`
- **Post-compact pre-fetch re-enabled**: Memory pre-fetch now runs after context compaction (was skipped)
- **Hook retrieval feedback logging**: Every retrieval logged to `feedback.jsonl` with query, keyword count, result count, rerank status
- **Bug fixes**: `recall()` missing `deleted_at IS NULL` (2 locations), SessionEnd scanning only first 400→2000 chars with context extraction, results increased from 3→5

## v7 (2026-02-08) — Security & Write-Time Merge

- **SQL injection protection**: All user inputs sanitized in retrieval, browse, and tag-enforce hooks
- **Write-time semantic merge**: New `scripts/write_time_merge.py` — cosine > 0.85 triggers merge. Integrated into SessionEnd micro-memory extraction with graceful degradation
- **Self-improving retrieval**: Weekly strength decay in feedback-digest (-0.05 for memories not accessed in 7 days, min 0.3)
- **Working Memory**: New PostToolUse hook tracks active/modified files and search patterns. Loaded by SessionStart after compaction
- **Bug fixes**: CTE alignment for strength boost, printf '%b' POSIX fix, valid_until IS NULL filter, deleted_at IS NULL in quality audit, error logging in PreCompact, narrowed slash command regex

## v6 (2026-02-08) — Ebbinghaus Decay

- **SessionStart v5**: Memory pre-fetch via FTS5 + tag-based queries (project-relevant + universal). No embedding model needed at startup
- **Ebbinghaus decay integration**: Combined scoring in retrieval (0.3×decay + 0.3×importance + 0.4×FTS5)
- **Strength boost**: Top 3 retrieved memories get +0.3 strength per access (max 5.0)

## v5 (2026-02-08) — FTS5 Hybrid Search

- **FTS5 hybrid search**: `memory_fts` table with 4 auto-sync triggers. BM25 keyword + vector cosine (70/30 weight) in retrieve/recall
- **New**: `scripts/ebbinghaus.py` — decay scoring utilities
- **New**: `scripts/migrate_ebbinghaus.py` — adds strength/last_accessed_at fields

## v4 (2026-02-08) — Scope System

- **Scope system**: 4 scopes (project, universal, preference, setup) with tag namespaces
- **SessionStart v4**: Setup detection (personal vs work), scope-aware instructions, compressed behavioral instructions (~120 tokens vs ~512 in v3)
- **New**: PreToolUse tag enforcement hook (`memory-tag-enforce.sh`)
- **New**: UserPromptSubmit retrieval hook (`memory-retrieval.sh`)
- **New**: Quality audit hook (`memory-quality-audit.sh`)
- **New**: Backup hook (`memory-backup.sh`)
- **New**: Browse CLI (`memory-browse.sh`)
- **New**: Upgrade script (`memory-upgrade.sh`)
- **Change**: Dual-layer deconfliction (MEMORY.md = active state, MCP = historical)

## v3 (2026-02-07) — Structured Extraction

- **SessionEnd v3**: Structured extraction — regex-based detection of decisions, errors/fixes, learnings, user preferences
- **SessionStart v3**: Cross-project topic hints loaded from index, enhanced behavioral instructions with typed memories
- **New**: PostToolUse feedback hook (`memory-feedback.sh`) — tracks store/search patterns, empty result detection
- **New**: Consolidation script (`memory-consolidate.py`) — Jaccard dedup, stale detection, cross-project index

## v2 (2026-02-07) — Session Summaries

- **SessionEnd**: Comprehensive session summary extraction from transcript
- **PreCompact**: Full transcript parsing with 15 user msgs + 10 assistant outputs
- **SessionStart**: Loads user profile + last session summary
- **New**: User profile template, session summaries directory

## v1 (2026-02-07) — Initial Release

- Initial release with basic SessionStart, PreCompact, SessionEnd hooks
- mcp-memory-service integration
