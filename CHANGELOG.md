# Changelog

# [11.25.0](https://github.com/dorukardahan/B12/compare/v11.24.0...v11.25.0) (2026-05-17)


### Features

* **skills:** b12-memory skill description auto-fires on EN+TR recall verbs ([#5](https://github.com/dorukardahan/B12/issues/5)) ([aa345a4](https://github.com/dorukardahan/B12/commit/aa345a4c88cef6bce04cd43aada4037b15d27c81)), closes [#4](https://github.com/dorukardahan/B12/issues/4)

# [11.24.0](https://github.com/dorukardahan/B12/compare/v11.23.0...v11.24.0) (2026-05-17)


### Features

* **hooks:** shorter directive SessionStart context primer (v8) ([#4](https://github.com/dorukardahan/B12/issues/4)) ([3b160ea](https://github.com/dorukardahan/B12/commit/3b160ea1ea25df4e0c0ed7b597fdeaed6320fb85))

# [11.23.0](https://github.com/dorukardahan/B12/compare/v11.22.0...v11.23.0) (2026-05-17)


### Features

* configurable retrieval weights + explicit strength dimension ([#2](https://github.com/dorukardahan/B12/issues/2)) ([c84201d](https://github.com/dorukardahan/B12/commit/c84201da0fc322936bdbc21cef8746b9a7b6476b))

# [11.22.0](https://github.com/dorukardahan/B12/compare/v11.21.0...v11.22.0) (2026-05-17)


### Features

* **mcp:** shared MCP daemon architecture — one launchd-managed `b12_mcp_daemon.py` process now serves multiple concurrent Claude Code (and other CLI) sessions over a Unix socket at `/tmp/b12-mcp-<UID>.sock`. `b12_mcp_server.py` auto-detects the daemon and becomes a thin stdio↔socket proxy; if the daemon is unreachable it falls back to in-process FastMCP stdio mode, so every existing Codex / Gemini / Kimi / OpenCode / Grok integration stays functional with zero config changes. ([cdf7243](https://github.com/dorukardahan/B12/commit/cdf724373c31fe8b94551fcb254af39c82037c9d))
* **install:** new `--daemon` and `--daemon-uninstall` flags. `./install.sh --daemon` renders `config/com.b12.mcp.daemon.plist`, writes it to `~/Library/LaunchAgents/`, runs `launchctl load`, and waits up to 10 s for the socket to appear.
* **cleanup:** removed `external-mcp-server` MCP entirely (Doruk's direct request — 0G docs are now accessible from the website). Config entries deleted from `~/.claude.json` and `~/.codex/config.toml`; binaries archived to `/tmp/0g-mcp-archive-<TS>/` for reversibility.

### Performance

* **mcp:** ~59% RSS reduction with 4 parallel Claude Code sessions (~600 MB → ~248 MB). Cold-start handshake latency unchanged at the proxy boundary because the proxy module still imports `b12_mcp_server` transitively; a strictly thinner proxy is queued as a v11.23 follow-up (estimated additional ~0.3 s × N-sessions saving).

### Forensic audit (2026-03-16 → 2026-05-17)

* Audited 2,981 sessions across all 6 supported CLIs. **B12 is stable when called (0 errors, 0 timeouts) but massively underutilized.** Claude Code: 30/36 sessions (83%) consumed the `MEMORY SYSTEM ACTIVE` SessionStart context and never invoked any B12 tool. OpenCode/Grok/Kimi: 0% real invocation rate despite the MCP server being loaded. Gemini: 1/139 sessions invoked B12 once and got `response={}` (integration broken end-to-end). Codex 2026-05-07: 10 parallel rollouts all missed the "[redacted-quote]" recall trigger. Full evidence at `docs/B12_session_audit_2026-03-16_2026-05-17.md` and `/tmp/b12-forensic-findings.md`.
* Closing the underutilization gap is **out of scope** for this release — logged as v11.23 follow-up backlog (skill / SessionStart-context redesign, Gemini path fix, Codex recall-trigger pattern matching).

### PR #2 review (`feat/hybrid-weights-rebalance` by @AytuncYildizli)

* Reviewed end-to-end at `docs/B12_PR2_review.md`. **Recommendation: MERGE with minor follow-ups** (manual recall-DB comparison, normalizer-flag tracker, daemon-migration env-var note). Code is clean, surgical (44/3 LOC, single file), references validate, mergeable against current `main`. PR adds `strength` as 4th score dimension with env-overridable weights — no conflict with the daemon migration. Pending Doruk's manual recall comparison.

### Claude Code CLI 2.1.143 compatibility

* Audited every hook event B12 wires (`SessionStart`, `PreCompact`, `SessionEnd`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`) against the current Claude Code 2.1.143 hook contract. **All payload shapes unchanged**, no regressions. Opportunities surfaced (not adopted in this release): `PostToolUseFailure`, `PostCompact`, `effort.level` field, `CLAUDE_CODE_SESSION_ID` env var.


## [11.20.2](https://github.com/dorukardahan/B12/compare/v11.20.1...v11.20.2) (2026-03-16)


### Bug Fixes

* **cli:** resolve symlink before finding b12_cli.py ([b9d73ed](https://github.com/dorukardahan/B12/commit/b9d73ed5d4a1c15015739d3aba9a224eeed0c16e))

## [11.20.1](https://github.com/dorukardahan/B12/compare/v11.20.0...v11.20.1) (2026-03-16)


### Bug Fixes

* **mcp:** remove CLAUDE_PLUGIN_ROOT dependency from .mcp.json ([a3f5e29](https://github.com/dorukardahan/B12/commit/a3f5e29040ba3618a689dc6e2b00d44eeed2f596))

# [11.20.0](https://github.com/dorukardahan/B12/compare/v11.19.2...v11.20.0) (2026-03-16)


### Features

* **classify:** integrate LogReg head into runtime pipeline ([239348d](https://github.com/dorukardahan/B12/commit/239348d7c4f6f94ba54ab82d7af18dba8c318e76))

## [11.19.2](https://github.com/dorukardahan/B12/compare/v11.19.1...v11.19.2) (2026-03-15)


### Bug Fixes

* **classify:** remove regex classification + fix prefix map + ship LogReg head ([f3a03a6](https://github.com/dorukardahan/B12/commit/f3a03a625ea6c8f5d14c3816d94062c5b2ab23b6))

## [11.19.1](https://github.com/dorukardahan/B12/compare/v11.19.0...v11.19.1) (2026-03-15)


### Bug Fixes

* **checkpoint:** prefix classify now reaches buffer flush + remove dead import ([d91e6f5](https://github.com/dorukardahan/B12/commit/d91e6f5b40dada4b7f94660b5a73b2cedf05d909))

# [11.19.0](https://github.com/dorukardahan/B12/compare/v11.18.2...v11.19.0) (2026-03-15)


### Features

* **classify:** 3-layer extraction pipeline — summary filter + prefix classifier + regex ([05a9825](https://github.com/dorukardahan/B12/commit/05a98257330b6002d56ff2293e842c26209d4ca5))

## [11.18.2](https://github.com/dorukardahan/B12/compare/v11.18.1...v11.18.2) (2026-03-15)


### Bug Fixes

* **review:** address code review findings — schema, cleanup, atexit, versions ([6859a20](https://github.com/dorukardahan/B12/commit/6859a20b1aa0eb9b1747f4ece68ff1165626a97c))

## [11.18.1](https://github.com/dorukardahan/B12/compare/v11.18.0...v11.18.1) (2026-03-15)


### Bug Fixes

* **mcp:** session tracker for MCP-only platforms — auto-capture on shutdown ([9d5d91f](https://github.com/dorukardahan/B12/commit/9d5d91fd14174f69ee01f7b897e4fa124aa2972b))

# [11.18.0](https://github.com/dorukardahan/B12/compare/v11.17.0...v11.18.0) (2026-03-15)


### Features

* **dist:** pyproject.toml for pip install b12-memory distribution ([6030bee](https://github.com/dorukardahan/B12/commit/6030beec27abcbed540904edadc6b4a2bcc323c5))

# [11.17.0](https://github.com/dorukardahan/B12/compare/v11.16.0...v11.17.0) (2026-03-15)


### Features

* **gemini:** add checkpoint hook to AfterTool adapter — mid-session capture for Gemini CLI ([185a33b](https://github.com/dorukardahan/B12/commit/185a33ba8899dd63bd9481db392b0a7962c60fa7))

# [11.16.0](https://github.com/dorukardahan/B12/compare/v11.15.0...v11.16.0) (2026-03-15)


### Features

* **cli:** b12 command — direct terminal access to memory system ([adc6066](https://github.com/dorukardahan/B12/commit/adc6066f74b81cd22f42c3d1a986889e72004aac))

# [11.15.0](https://github.com/dorukardahan/B12/compare/v11.14.0...v11.15.0) (2026-03-15)


### Features

* **codex:** upgrade extraction with all shared_patterns — implicit decisions, blockers, corrections ([ab10f90](https://github.com/dorukardahan/B12/commit/ab10f9032be2ed501ae06b22e4b9e4e59246c1b3))

# [11.14.0](https://github.com/dorukardahan/B12/compare/v11.13.0...v11.14.0) (2026-03-15)


### Features

* **scheduler:** FSRS-6 hybrid spaced repetition — replaces primitive Ebbinghaus ([b72dec8](https://github.com/dorukardahan/B12/commit/b72dec862089c78e63be910431d72e8177f90528))

# [11.13.0](https://github.com/dorukardahan/B12/compare/v11.12.0...v11.13.0) (2026-03-15)


### Features

* **patterns:** implicit decisions, reasoning, and blockers — 3 new regex patterns ([f513de5](https://github.com/dorukardahan/B12/commit/f513de5a1db2f0e321236c273ab2f6022dfb8d74))

# [11.12.0](https://github.com/dorukardahan/B12/compare/v11.11.0...v11.12.0) (2026-03-15)


### Features

* **token:** behavioral instructions → skill — 58% context reduction ([ece7184](https://github.com/dorukardahan/B12/commit/ece7184e8116aae15ac588fe343b7ccb9fa063ea))

# [11.11.0](https://github.com/dorukardahan/B12/compare/v11.10.0...v11.11.0) (2026-03-15)


### Features

* **hooks:** mid-session checkpoint hook — captures decisions/errors/learnings between compactions ([b990ffb](https://github.com/dorukardahan/B12/commit/b990ffb38d658b3c971110d433abc0d19889737e))

# [11.10.0](https://github.com/dorukardahan/B12/compare/v11.9.0...v11.10.0) (2026-03-15)


### Features

* **plugin:** Claude Code plugin format — marketplace discovery + slash commands ([d4bc053](https://github.com/dorukardahan/B12/commit/d4bc0535321de396bf13c0772f17df8abbf2e345))

# [11.9.0](https://github.com/dorukardahan/B12/compare/v11.8.4...v11.9.0) (2026-03-14)


### Features

* **integrity:** 3-layer metadata validation — prevent invalid JSON at write time ([e99f607](https://github.com/dorukardahan/B12/commit/e99f60788afd1f5533efe85a931f9b375d0b537d))

## [11.8.4](https://github.com/dorukardahan/B12/compare/v11.8.3...v11.8.4) (2026-03-14)


### Bug Fixes

* **mcp:** guard all json_extract(metadata) against malformed JSON ([8477ffd](https://github.com/dorukardahan/B12/commit/8477ffdeffe9d135c5d063cdf40629a21c7e92ae))

## [11.8.3](https://github.com/dorukardahan/B12/compare/v11.8.2...v11.8.3) (2026-03-14)


### Bug Fixes

* **mcp:** memory_quality analyze crashes on legacy metadata (malformed JSON) ([b5bf3c0](https://github.com/dorukardahan/B12/commit/b5bf3c0be54f40a96d1dba6d95cd19c8e7900360))

## [11.8.2](https://github.com/dorukardahan/B12/compare/v11.8.1...v11.8.2) (2026-03-14)


### Bug Fixes

* **mcp:** return full content_hash in search results instead of truncated ([ecb328e](https://github.com/dorukardahan/B12/commit/ecb328ec4eb4e41e72937c47d3d0283514b95f7e))

## [11.8.1](https://github.com/dorukardahan/B12/compare/v11.8.0...v11.8.1) (2026-03-14)


### Bug Fixes

* **docs:** correct CHANGELOG version from 12.0.0 to 11.8.0 ([18fd448](https://github.com/dorukardahan/B12/commit/18fd448795cbbbb2a8a032d9b9615c7ff4bcb844))

# [11.8.0](https://github.com/dorukardahan/B12/compare/v11.7.3...v11.8.0) (2026-03-14)

### Features

* **session-end:** Sprint Handoff Memory — compact state file (`{project}-handoff.md`) for seamless session continuity (F1)
* **precompact:** Aggressive PreCompact Extraction — high-value items (decision, error_fix, learning, preference) stored directly to SQLite during compaction (F2)
* **codex:** Cross-Platform Session Bridge — fix metadata to valid JSON, add memory_type, consistent tags (F3)
* **session-end:** Identity Correction Cascade — detect "not X, actually Y" patterns and cascade-update existing memories (F4)
* **patterns:** Infrastructure Entity Auto-Extraction — IP, SSH, port, version patterns with scoring (F5)
* **patterns:** Content Decision Auto-Capture — blog publish, editorial decision, guardrail patterns (F6)
* **health:** B12 Health Check CLI (`b12_health.py`) — 8 diagnostic checks with colored output, `--json` and `--fix` flags (F7)
* **session-end/start:** Host Version Tracking — extract Claude Code version from transcript, store in state file, check against `compat.json` (F8)
* **session-start:** Content Guardrails Always-Surface — auto-inject content-guardrail tagged memories for content sessions (F9)
* **session-start:** Setup-Aware Session Routing — warn when CWD matches work pattern but personal setup is active (F10)
* **install:** add `--health` flag to run health check diagnostics

### Bug Fixes

* **codex:** metadata was invalid f-string format, now proper `json.dumps()` output
* **codex:** tags had inconsistent spacing (spaces after commas), now matches Claude Code hook format
* **codex:** `store_memory()` was missing `memory_type` column in INSERT

## [11.7.3](https://github.com/dorukardahan/B12/compare/v11.7.2...v11.7.3) (2026-03-14)


### Bug Fixes

* **install:** remove unused label variable in update_launchd_plists ([f624f2e](https://github.com/dorukardahan/B12/commit/f624f2e81cd22dd33db11cd5320d435c69f136f0))

## [11.7.2](https://github.com/dorukardahan/B12/compare/v11.7.1...v11.7.2) (2026-03-14)


### Bug Fixes

* **install:** migrate launchd plists from ~/.claude/ to ~/.B12/ paths ([39fab9c](https://github.com/dorukardahan/B12/commit/39fab9cb3c535c0ddcb29d22c77f74e6b0e8907b))

## Unreleased

### Bug Fixes

* **install:** add `update_launchd_plists()` to migrate `~/.claude/hooks/` → `~/.B12/hooks/` and `~/.claude/memory-logs/` → `~/.B12/memory-logs/` in launchd plist files, then reload affected jobs — previously `install.sh --all` copied hooks to the new location but left 5 launchd jobs pointing at the old path

## [11.7.1](https://github.com/dorukardahan/B12/compare/v11.7.0...v11.7.1) (2026-03-03)


### Bug Fixes

* **release:** add @semantic-release/npm plugin for package.json version bumps ([0283d89](https://github.com/dorukardahan/B12/commit/0283d894cfd6c2341b086aa43441e63dacad8882))

# [11.7.0](https://github.com/dorukardahan/B12/compare/v11.6.1...v11.7.0) (2026-03-03)


### Features

* implement B12 v11 Tier 3 — stemming, health report, Gemini hooks, MCP resources ([6bfc937](https://github.com/dorukardahan/B12/commit/6bfc937324b075335adc5e6bacd5d851b4b9333f))

## [11.6.1](https://github.com/dorukardahan/B12/compare/v11.6.0...v11.6.1) (2026-03-03)


### Bug Fixes

* resolve 17 issues from v12 code review (crash, dashboard, correctness) ([4711a0c](https://github.com/dorukardahan/B12/commit/4711a0c8471280d62c6b633bd3204961655856fd))

# [11.6.0](https://github.com/dorukardahan/B12/compare/v11.5.0...v11.6.0) (2026-03-03)


### Features

* **benchmark:** LoCoMo operationalization with MRR, NDCG, regression detection (v12.0.0) ([2fc72fc](https://github.com/dorukardahan/B12/commit/2fc72fc0aff77efa6a0d50d3fd82b3ff616daf68))

# [11.5.0](https://github.com/dorukardahan/B12/compare/v11.4.0...v11.5.0) (2026-03-03)


### Features

* **dashboard:** Web Dashboard with Flask backend + Cytoscape.js frontend (v11.5.0) ([8873029](https://github.com/dorukardahan/B12/commit/8873029f4cba25ade0932176ad37996861aed6cc)), closes [#11](https://github.com/dorukardahan/B12/issues/11)

# [11.4.0](https://github.com/dorukardahan/B12/compare/v11.3.0...v11.4.0) (2026-03-03)


### Features

* **export:** Memory export/import with portable .b12 format (v11.4.0) ([f6d3f6f](https://github.com/dorukardahan/B12/commit/f6d3f6f587f32b5ad6fec7c640aaf88353378eda))

# [11.3.0](https://github.com/dorukardahan/B12/compare/v11.2.0...v11.3.0) (2026-03-03)


### Features

* **surfacing:** Proactive memory surfacing with rate limiting (v11.3.0) ([e2f5811](https://github.com/dorukardahan/B12/commit/e2f5811d92003ec28b42f4abcc4d2643997a8087))

# [11.2.0](https://github.com/dorukardahan/B12/compare/v11.1.0...v11.2.0) (2026-03-03)


### Features

* **extraction:** Enhanced session-end extraction with 4 new patterns + memory_refine tool (v11.2.0) ([88292f7](https://github.com/dorukardahan/B12/commit/88292f73749cbac6a0245aba24dab019681a9b67))

# [11.1.0](https://github.com/dorukardahan/B12/compare/v11.0.0...v11.1.0) (2026-03-03)


### Features

* **consolidation:** Smart Consolidation engine with HDBSCAN clustering (v11.1.0) ([6259f5f](https://github.com/dorukardahan/B12/commit/6259f5f04b44ac60bd571f0e632ac17405b31cf3))

# [11.0.0](https://github.com/dorukardahan/B12/compare/v10.8.5...v11.0.0) (2026-02-28)

Major quality milestone: 3 independent AI auditors (Claude, Gemini, Codex) + 4-tier cross-platform testing.

### Breaking Changes

* **BM25 scoring corrected** — MCP search results now rank correctly (best keyword matches rank highest). Previously inverted: best matches got lowest scores due to `1.0 - abs(rank)/20` formula.

### Features

* **Spaced repetition in MCP search** — `memory_search` now boosts `strength +0.2` and increments `access_count` for returned memories. Previously only hook-based retrieval did this, so non-Claude platforms (Gemini, Codex, Cursor, etc.) never reinforced memories.
* **`valid_until` support in `memory_store`** — TTL/dormancy can now be set at store time via `metadata.valid_until`.
* **`valid_until` and `deleted_at` in `memory_update`** — soft-delete and TTL management via MCP tool.
* **Ghost memory fix** — re-storing a previously soft-deleted memory now undeletes it instead of silently failing via `INSERT OR IGNORE`.

### Bug Fixes

* **BM25 inversion** (CRITICAL) — `1.0 - min(abs(rank)/20, 0.9)` → `min(abs(rank)/20, 1.0)` in MCP server FTS scoring
* **tag-enforce hook** — `updatedInput` now preserves all original tool_input fields (was dropping `content` and `metadata`)
* **embed_daemon WAL mode** — added `journal_mode=WAL` + `busy_timeout=5000` to prevent blocking MCP writes
* **MCP server busy_timeout** — increased from 10s to 30s for concurrent multi-CLI access, added `wal_autocheckpoint=100`
* **FTS trigger detection** — changed `sql LIKE '%memory_fts%'` to `name LIKE 'memory_fts_%'` to prevent false matches
* **`memory_quality analyze`** — explicit None→float conversion, early return for empty databases
* 27 cross-audit findings fixed (SQL safety, lifecycle, concurrency, docs)

### Verified

* **i18n**: Turkish, Japanese, Chinese, Korean, Russian — store + search all pass
* **Security**: SQL injection payloads safely stored and retrieved, tables intact
* **Cross-platform**: Claude → Gemini → Codex store/search chain verified
* **Spaced repetition**: Strength boost confirmed across all platforms (1.0 → 2.0+ after multiple searches)
* **Stress**: 2KB+ metadata, mixed-script content, concurrent 3-CLI writes

## [10.8.6](https://github.com/dorukardahan/B12/compare/v10.8.5...v10.8.6) (2026-02-28)


### Bug Fixes

* Increase SQLite busy_timeout to 30s for concurrent multi-CLI access ([45fa696](https://github.com/dorukardahan/B12/commit/45fa696436de62204b0f0c81c8a20600673bdd32))

## [10.8.5](https://github.com/dorukardahan/B12/compare/v10.8.4...v10.8.5) (2026-02-28)


### Bug Fixes

* Preserve full tool_input in tag-enforce hook updatedInput ([4eefa15](https://github.com/dorukardahan/B12/commit/4eefa15ab7e544e70e152470dd74f382b50a8c60))

## [10.8.4](https://github.com/dorukardahan/B12/compare/v10.8.3...v10.8.4) (2026-02-28)


### Bug Fixes

* Resolve 7 functional test findings — BM25 inversion, ghost memories, spaced repetition ([d01a0cc](https://github.com/dorukardahan/B12/commit/d01a0cc18cad2feb4ea150904dffbf11e2095958))

## [10.8.3](https://github.com/dorukardahan/B12/compare/v10.8.2...v10.8.3) (2026-02-28)


### Bug Fixes

* Address 27 cross-audit findings — SQL safety, lifecycle, concurrency, docs ([eefe096](https://github.com/dorukardahan/B12/commit/eefe09620aff50f3063f26009aaa4f592f86ee99))

## [10.8.2](https://github.com/dorukardahan/B12/compare/v10.8.1...v10.8.2) (2026-02-27)


### Bug Fixes

* Update stale +0.3 references to +0.2 after strength boost alignment ([5cd5ad6](https://github.com/dorukardahan/B12/commit/5cd5ad60c9583a038208ab6f156c0cb86340651a))

## [10.8.1](https://github.com/dorukardahan/B12/compare/v10.8.0...v10.8.1) (2026-02-27)


### Bug Fixes

* Address cross-audit findings — FTS5 injection, falsy eval, type consistency ([3ba9740](https://github.com/dorukardahan/B12/commit/3ba9740c8f4385c0d999c53c63b89601d3858096))

# [10.8.0](https://github.com/dorukardahan/B12/compare/v10.7.2...v10.8.0) (2026-02-26)


### Features

* B12 v11 — retrieval, lifecycle, and observability improvements ([e6a94d3](https://github.com/dorukardahan/B12/commit/e6a94d3f66dca149ebee93dacb5cfcdabea9a3dd))

## [10.7.2](https://github.com/dorukardahan/B12/compare/v10.7.1...v10.7.2) (2026-02-26)


### Bug Fixes

* **docs:** Update branding from Claude Code-only to multi-platform ([4e794a6](https://github.com/dorukardahan/B12/commit/4e794a6dad296bb9683a8d52b41a49a6d5b30a28))

## [10.7.1](https://github.com/dorukardahan/B12/compare/v10.7.0...v10.7.1) (2026-02-26)


### Bug Fixes

* Address agent team review findings — set-e safety and comment accuracy ([9d4b703](https://github.com/dorukardahan/B12/commit/9d4b703d8e359d9ae76f3cd85c4f7e28c2025d9f))

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
- `b12_mcp_server.py` — minimal FastMCP server with 5 tools (memory_store, memory_search, memory_update, memory_quality, memory_session_context)
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
