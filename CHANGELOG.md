# Changelog

## Unreleased

### Performance

* **mcp-daemon:** idle-connection reaping + connection cap (P2). The shared MCP
  daemon now tracks per-connection activity and cancels connections idle beyond
  `B12_MCP_IDLE_TIMEOUT` (default 1800s), plus a `B12_MCP_MAX_CONN` cap (default
  64) that evicts the most-idle connection under pressure. Bounds the FD /
  coroutine growth that accumulated 1:1 with open CLI tabs (observed 13 live
  proxies + 14 never-reaped daemon socket FDs). On reap the stdio proxy now exits
  promptly on socket-EOF (`_run_as_proxy` waits FIRST_COMPLETED, so it no longer
  blocks on stdin and lose the host's next request) and the host respawns it on
  the next call, reconnecting to a fresh daemon session.
  `scripts/b12_mcp_daemon.py`, `scripts/b12_mcp_server.py`.
* **mcp-daemon:** periodic WAL checkpoint (P7). A 5-min `PRAGMA
  wal_checkpoint(TRUNCATE)` timer (`B12_MCP_WAL_CHECKPOINT_INTERVAL`) keeps an
  idle daemon or long-lived reader from letting the WAL grow unbounded
  (`wal_autocheckpoint=100` only fires on writes). Runs **off the event loop** on
  a dedicated short-lived connection with a short `busy_timeout` — a TRUNCATE
  checkpoint can wait on a contending reader, so running it on the loop under the
  server lock would freeze every client for that window. `scripts/b12_mcp_daemon.py`.
* **mcp-server:** `PRAGMA synchronous=NORMAL` + `temp_store=MEMORY` (P10). NORMAL
  is the recommended WAL durability mode — corruption-safe; committed
  transactions survive any app/process crash (daemon restart, kill, terminal
  close). Only an OS crash / power loss can roll back the most-recent commit(s);
  the DB is never corrupted. Lower write latency on the shared memory DB.
  `scripts/b12_mcp_server.py`.
* **hooks:** cache the resolved DB path (P3). High-frequency hooks
  (`memory-retrieval.sh` per prompt, `memory-proactive-surface.sh` per
  Read/Edit/Write/Bash) read the DB path from a 60s-TTL cache
  (`$B12_DATA_DIR/state/db-path.cache`) via a new `b12_get_db_path` in
  `_b12_common.sh` instead of spawning `python3` on every fire.
* **hooks (codex):** background the Codex PostToolUse telemetry write (P11) so
  the hook returns immediately (`hooks/memory-codex-post-tool.sh`).
* **launcher:** bounded (~2s) daemon-up probe in `start-mcp.sh` (P8) so tabs
  opened during the login window don't race into the slow legacy in-process
  path; falls through to legacy fast when the daemon is genuinely down.

### Changed

* **recall:** ANN exact-KNN recall is now **enabled by default** with
  `threshold_count = 500` (P5). sqlite-vec's `vec0 MATCH` is exact brute-force
  KNN over normalized vectors, so it reproduces the full-table cosine ranking
  exactly (A/B `benchmarks/ann_ab_test.py`: overlap@5 = 1.00 over 300 real-vector
  probes) while removing the `ORDER BY m.id DESC LIMIT 500` blind spot — which on
  a ~3.6k-vector DB matched the true ranking only ~15% of the time (87% of
  queries had their true nearest neighbour beyond the 500 newest rows). The
  install-template default flips to `enabled = true`. Hardening: threshold clamp
  to `[100, 1e6]` + empty-`topk` health logging in `embed_daemon.py`, the A/B
  harness, and `scripts/tests/test_ann_recall_path.py`.

### Security

* **pii-scrub:** close the write-path gap — the secret scrubber now runs on
  **every** write path, not just write-time merge + Codex. Added scrub calls to
  the MCP `memory_store` tool (`b12_mcp_server.py`), the SessionEnd summary store
  (`memory-session-end.sh`), the PreCompact priority store (`memory-precompact.sh`),
  and the checkpoint flush (`memory-checkpoint.sh`). Previously a secret pasted
  into chat could land raw in SQLite via any of these paths despite the docs'
  "scrub on every write" claim. Honors `B12_DISABLE_PII_SCRUB=1` everywhere.
* **pii-scrub:** expand the pattern catalog — added Google API keys (`AIza…`),
  Stripe keys (`sk_live_`/`sk_test_`/`rk_…`), PEM private-key blocks, and
  credential-bearing DB connection URIs; extended the generic credential pattern
  with Turkish keywords (`parola`, `şifre`/`sifre`, `gizli anahtar`). Added 7 unit
  tests (`scripts/tests/test_b12_pii_scrubber.py`).

### Bug Fixes

* **retrieval:** normalize importance across both write-side scales in ranking
  (RET-3). `importance_score` is written on two coexisting scales — fractional
  `[0, 0.95]` (`b12_importance.py`) and level multipliers `[0.7, 2.0]` (critical
  2.0 / important 1.5 / normal 1.0 / temporary 0.7; `memory-session-end.sh` caps at
  2.0). The read path applied a blanket `/2.0`, which correctly normalized the
  level scale but silently **halved the fractional band** (a `0.95` memory
  contributed only `0.475`). The read paths now normalize per scale: a value
  `≥ 1.0` (a level multiplier) is divided by 2 (2.0→1.0, 1.5→0.75, 1.0→0.5) while a
  fractional value `< 1.0` passes through; missing / null / non-numeric / boolean
  default to the `0.50` baseline; the result is clamped to `[0, 1]`. Applied
  identically in MCP `_unified_score` (`b12_mcp_server.py`), the retrieval-hook SQL
  (`memory-retrieval.sh`), and the OpenCode plugin (`plugins/opencode/src/lib/db.ts`
  + `dist`, which also stops coercing a stored `0`/`null` via `|| 1.0`). Added
  regression tests across all three paths (`scripts/tests/test_retrieval_correctness.py`
  + `plugins/opencode/tests/scoring.test.ts`). Scale references:
  `scripts/b12_importance.py`, `plugins/opencode/src/lib/scoring.ts`. Known limit:
  the overlap zone `[0.7, 0.95]` is ambiguous (level `temporary` 0.7 vs a fractional
  0.7), so `temporary` passes through at 0.7 and can out-rank a `normal` (→0.5) on
  the importance axis; the complete fix is write-side scale unification + migration
  (deferred).
* **install:** self-heal `MCP_EMBEDDING_MODEL` drift on every run. `install.sh`
  now reads the live DB's vec0 `FLOAT[N]` dimension, derives the canonical
  model (1024 → `BAAI/bge-m3`), and reaffirms it across all deployed configs
  (3 Claude `.claude.json` + Codex `config.toml`) via
  `scripts/heal_embedding_model.py` — idempotent, repairs partial migrations
  (the bge-m3 v11.34 migration only rewrote one Claude config, so other setups
  silently kept 384-dim MiniLM and degraded to FTS-only recall).
* **install:** `inject_codex_mcp_config` no longer strips the
  `[mcp_servers.B12.tools.memory_store] approval_mode = "auto"` block on
  `--codex` re-runs; the block is now part of the regenerated config and is
  reaffirmed by the drift self-heal.
* **grok:** repair the Grok lifecycle hooks, which were dead-on-arrival. They
  resolved the shared core via fragile `__file__` path depth (broke once
  deployed to `~/.grok/plugins/b12/`), imported `extract_*` helpers that never
  existed, and called `merge_or_insert` with the wrong signature; `hooks.json`
  also hardcoded the Claude-only `${CLAUDE_PLUGIN_ROOT}`. Now a shared
  `_b12_grok_core.py` resolves the core via `$B12_HOOK_DIR`, real `extract_*`
  helpers live in `shared_patterns.py`, the canonical write path is used
  (daemon-encode → `merge_or_insert`, with an FTS-only fallback when the daemon
  is down), and `install.sh` substitutes a `__B12_PLUGIN_ROOT__` marker. Adds an
  end-to-end Grok hook test (`scripts/tests/test_grok_hooks.py`).

### Performance

* **embed_daemon:** load the embedding model with `local_files_only=True`
  (download fallback on first run). Skips a network `model_info()` round-trip
  `transformers` makes for repo-id loads, cutting cold model-load ~9.4s → ~4.7s
  (model_load 5.5s → 1.3s on Apple Silicon) and removing the Hugging Face
  network dependency from session startup.

## [11.74.1](https://github.com/dorukardahan/B12/compare/v11.74.0...v11.74.1) (2026-05-21)


### Bug Fixes

* resolve B12 Clawpatch audit findings ([0a39ce1](https://github.com/dorukardahan/B12/commit/0a39ce118f5337a4452d5d737a38e9f7474fae34))

# [11.74.0](https://github.com/dorukardahan/B12/compare/v11.73.0...v11.74.0) (2026-05-20)


### Features

* **demo:** adapt OrangeClaudeTerminal banner to Ink ([#77](https://github.com/dorukardahan/B12/issues/77)) ([7f0b408](https://github.com/dorukardahan/B12/commit/7f0b408f5f7785e85174adf3018c1a13b0aa7fb4))

# [11.73.0](https://github.com/dorukardahan/B12/compare/v11.72.0...v11.73.0) (2026-05-20)


### Features

* **demo:** English content + faithful Claude Code v2.1.x banner ([#76](https://github.com/dorukardahan/B12/issues/76)) ([e22fcc8](https://github.com/dorukardahan/B12/commit/e22fcc812220ba84d2476e5487a41a2fb7d9e7cd))

# [11.72.0](https://github.com/dorukardahan/B12/compare/v11.71.0...v11.72.0) (2026-05-20)


### Features

* **demo:** Ink-rendered Claude Code TUI sim — pixel-close (v11.72.0) ([#75](https://github.com/dorukardahan/B12/issues/75)) ([753ff41](https://github.com/dorukardahan/B12/commit/753ff41cf694f6a887c9978fd28447e8eb58f12b))

# [11.71.0](https://github.com/dorukardahan/B12/compare/v11.70.0...v11.71.0) (2026-05-20)


### Features

* **demo:** high-fidelity B12 walkthrough — live retrieval pill (v11.71.0) ([#74](https://github.com/dorukardahan/B12/issues/74)) ([163c909](https://github.com/dorukardahan/B12/commit/163c9097300178b50cf68d54abdead18ccff62a3)), closes [hi#fidelity](https://github.com/hi/issues/fidelity) [hi#fidelity](https://github.com/hi/issues/fidelity)

# [11.70.0](https://github.com/dorukardahan/B12/compare/v11.69.0...v11.70.0) (2026-05-20)


### Features

* **demo:** real Claude Code session GIF — replaces synthetic MCP-tool demo ([#73](https://github.com/dorukardahan/B12/issues/73)) ([e6447a8](https://github.com/dorukardahan/B12/commit/e6447a853e3fd0e9f8922609d14c84f20587ef6b))

# [11.69.0](https://github.com/dorukardahan/B12/compare/v11.68.0...v11.69.0) (2026-05-20)


### Features

* **bench:** LoCoMo 9-cell matrix + benchmark workflow ([#72](https://github.com/dorukardahan/B12/issues/72)) ([d56179c](https://github.com/dorukardahan/B12/commit/d56179c6104404c523b8aaf80b9518f8d4b7346a)), closes [#9](https://github.com/dorukardahan/B12/issues/9)

# [11.68.0](https://github.com/dorukardahan/B12/compare/v11.67.0...v11.68.0) (2026-05-20)


### Features

* **demo:** VHS-rendered demo.gif + walkthrough doc ([#71](https://github.com/dorukardahan/B12/issues/71)) ([471645d](https://github.com/dorukardahan/B12/commit/471645d949a8217dbc3c255ac1839329a3b53048))

# [11.67.0](https://github.com/dorukardahan/B12/compare/v11.66.0...v11.67.0) (2026-05-20)


### Features

* **ml:** classifier retrain on bge-m3 1024-dim + reproducible recipe ([#70](https://github.com/dorukardahan/B12/issues/70)) ([a13d8ac](https://github.com/dorukardahan/B12/commit/a13d8ac89bbd8fe3b6d892ac37bb0452231b1301)), closes [#56](https://github.com/dorukardahan/B12/issues/56) [#56](https://github.com/dorukardahan/B12/issues/56)

# [11.66.0](https://github.com/dorukardahan/B12/compare/v11.65.0...v11.66.0) (2026-05-20)


### Features

* **quality:** write-time fragment gate (re-opened from [#59](https://github.com/dorukardahan/B12/issues/59) after PR [#57](https://github.com/dorukardahan/B12/issues/57) squash) ([#69](https://github.com/dorukardahan/B12/issues/69)) ([52c3869](https://github.com/dorukardahan/B12/commit/52c386982aaf136f2c1bcfd1477160c4e2dbc0a8))

# [11.65.0](https://github.com/dorukardahan/B12/compare/v11.64.0...v11.65.0) (2026-05-20)


### Features

* **amp:** MCP template + --amp install flag (Cody → Amp pivot) ([#62](https://github.com/dorukardahan/B12/issues/62)) ([4151157](https://github.com/dorukardahan/B12/commit/415115714a1b1694e14ff6dd5d1e16b0d7043c2a))

# [11.64.0](https://github.com/dorukardahan/B12/compare/v11.63.0...v11.64.0) (2026-05-20)


### Features

* **jetbrains:** paste template + README docs (no install.sh wiring) ([#63](https://github.com/dorukardahan/B12/issues/63)) ([ef1c740](https://github.com/dorukardahan/B12/commit/ef1c7407087bb8d304bc01c8e51c8b9389182098))

# [11.63.0](https://github.com/dorukardahan/B12/compare/v11.62.0...v11.63.0) (2026-05-20)


### Features

* **install:** no-flag default → safe-defaults for first-run ([#64](https://github.com/dorukardahan/B12/issues/64)) ([456603a](https://github.com/dorukardahan/B12/commit/456603a4a5bdf4f5f918ed6f75e5537eb65d5a8d))

# [11.62.0](https://github.com/dorukardahan/B12/compare/v11.61.0...v11.62.0) (2026-05-20)


### Features

* **security:** PII / secret scrubber + 11 unit tests + write-time wiring ([#67](https://github.com/dorukardahan/B12/issues/67)) ([a05aebe](https://github.com/dorukardahan/B12/commit/a05aebed78dec396216b0f91b33192f2c08c0eb2))

# [11.61.0](https://github.com/dorukardahan/B12/compare/v11.60.0...v11.61.0) (2026-05-20)


### Features

* **mcp:** memory_delete tool + b12_gc.collect_one helper ([#58](https://github.com/dorukardahan/B12/issues/58)) ([50fbb3d](https://github.com/dorukardahan/B12/commit/50fbb3dae02a33da9aa1976a64986d07ba2d7a16))

# [11.60.0](https://github.com/dorukardahan/B12/compare/v11.59.0...v11.60.0) (2026-05-20)


### Features

* **maint:** GC cron default-on, 90-day TTL, --no-gc-cron opt-out ([#60](https://github.com/dorukardahan/B12/issues/60)) ([bf84b90](https://github.com/dorukardahan/B12/commit/bf84b90b52da41ebceaa44e1bea2f14574f7bf21))
* **quality:** NLI surface threshold + fragment pre-filter + migration ([#57](https://github.com/dorukardahan/B12/issues/57)) ([a3d52bb](https://github.com/dorukardahan/B12/commit/a3d52bb8154bfdf00de8a8561f437d4cf0934259))

# [11.59.0](https://github.com/dorukardahan/B12/compare/v11.58.2...v11.59.0) (2026-05-20)


### Features

* **cline:** TaskComplete + PreCompact delegation + _parse_cline ([#61](https://github.com/dorukardahan/B12/issues/61)) ([e5f219c](https://github.com/dorukardahan/B12/commit/e5f219c96c55f53ca70f9a6cdfa94cfd6c8a515f)), closes [cline#7510](https://github.com/cline/issues/7510) [cline#7513](https://github.com/cline/issues/7513)

## [11.58.2](https://github.com/dorukardahan/B12/compare/v11.58.1...v11.58.2) (2026-05-19)


### Bug Fixes

* **classifier:** dim-guard warning + B12_CLASSIFIER_BACKEND=off escape hatch ([#56](https://github.com/dorukardahan/B12/issues/56)) ([814690f](https://github.com/dorukardahan/B12/commit/814690f3e053450851270f2c6326dbad7dda992b)), closes [#23](https://github.com/dorukardahan/B12/issues/23)

## [11.58.1](https://github.com/dorukardahan/B12/compare/v11.58.0...v11.58.1) (2026-05-19)


### Bug Fixes

* **grok:** templatize .mcp.json + SKILL.md paths ([ae49620](https://github.com/dorukardahan/B12/commit/ae4962090c4fca9c1d30bb9acb2e3d98535503ab))

# [11.58.0](https://github.com/dorukardahan/B12/compare/v11.57.0...v11.58.0) (2026-05-19)


### Features

* **continue:** auto-install B12 lifecycle hooks to ~/.continue/settings.json ([#55](https://github.com/dorukardahan/B12/issues/55)) ([c30704d](https://github.com/dorukardahan/B12/commit/c30704db6e3895844544f917e703521261894736))

# [11.57.0](https://github.com/dorukardahan/B12/compare/v11.56.0...v11.57.0) (2026-05-19)


### Features

* **cc:** SessionEnd idle-timeout skip (B12_IDLE_TIMEOUT_SECONDS) ([#54](https://github.com/dorukardahan/B12/issues/54)) ([5c23731](https://github.com/dorukardahan/B12/commit/5c23731010dafbde82164856e5a605359270150e))

# [11.56.0](https://github.com/dorukardahan/B12/compare/v11.55.0...v11.56.0) (2026-05-19)


### Features

* **zed:** MCP context_server template + --zed install flag ([#53](https://github.com/dorukardahan/B12/issues/53)) ([7668a69](https://github.com/dorukardahan/B12/commit/7668a69bf1d2d1c17c8c3cd4158143b5a80d21dd)), closes [#1](https://github.com/dorukardahan/B12/issues/1) [#2](https://github.com/dorukardahan/B12/issues/2) [#1](https://github.com/dorukardahan/B12/issues/1) [#2](https://github.com/dorukardahan/B12/issues/2)

# [11.55.0](https://github.com/dorukardahan/B12/compare/v11.54.0...v11.55.0) (2026-05-19)


### Features

* **maint:** soft-delete GC + VACUUM ([#51](https://github.com/dorukardahan/B12/issues/51)) ([f900fa8](https://github.com/dorukardahan/B12/commit/f900fa85ce465c927cd826bbc9a41c7b8aec73c7)), closes [#1](https://github.com/dorukardahan/B12/issues/1) [#2](https://github.com/dorukardahan/B12/issues/2)

# [11.54.0](https://github.com/dorukardahan/B12/compare/v11.53.0...v11.54.0) (2026-05-19)


### Features

* **opencode:** [M#] macro verbs ingestion ([#50](https://github.com/dorukardahan/B12/issues/50)) ([614aedc](https://github.com/dorukardahan/B12/commit/614aedcc49c4bceee8c26f8f3e124ca496cf817d)), closes [hi#value](https://github.com/hi/issues/value) [M#decision](https://github.com/M/issues/decision) [M#decision](https://github.com/M/issues/decision) [M#learning](https://github.com/M/issues/learning) [M#preferrence](https://github.com/M/issues/preferrence) [M#todo](https://github.com/M/issues/todo) [#1](https://github.com/dorukardahan/B12/issues/1) [M#decision](https://github.com/M/issues/decision) [#2](https://github.com/dorukardahan/B12/issues/2)

# [11.53.0](https://github.com/dorukardahan/B12/compare/v11.52.0...v11.53.0) (2026-05-19)


### Features

* **common:** cross-platform DB-path resolver ([#52](https://github.com/dorukardahan/B12/issues/52)) ([0556758](https://github.com/dorukardahan/B12/commit/05567588ab5cd34be6e08af7efd3806f3b1a1df4))

# [11.52.0](https://github.com/dorukardahan/B12/compare/v11.51.0...v11.52.0) (2026-05-19)


### Features

* **codex:** _all_tools ingestion for cloud_exec/cloud_apply (Phase E self-improve E1) ([#48](https://github.com/dorukardahan/B12/issues/48)) ([c41bb58](https://github.com/dorukardahan/B12/commit/c41bb5871b307b6dab33862934da730941dfe256))

# [11.51.0](https://github.com/dorukardahan/B12/compare/v11.50.0...v11.51.0) (2026-05-19)


### Features

* **cline:** wire B12 hooks into ~/.cline/hooks/ (TaskStart + UserPromptSubmit + PreCompact) ([#47](https://github.com/dorukardahan/B12/issues/47)) ([18226ab](https://github.com/dorukardahan/B12/commit/18226ab5357ca78adb7b5ab90bf901234c21768b)), closes [post-#46-merge](https://github.com/post-/issues/46-merge) [post-#46](https://github.com/post-/issues/46)

# [11.50.0](https://github.com/dorukardahan/B12/compare/v11.49.0...v11.50.0) (2026-05-19)


### Features

* **continue:** MCP template + transcript adapter + --continue install flag ([#46](https://github.com/dorukardahan/B12/issues/46)) ([99981a3](https://github.com/dorukardahan/B12/commit/99981a32e966aabf521444386365a1721ea11b97))

# [11.49.0](https://github.com/dorukardahan/B12/compare/v11.48.0...v11.49.0) (2026-05-19)


### Features

* **cc:** C13 24h smoke harness + C14 ANN index over memory_embeddings ([#43](https://github.com/dorukardahan/B12/issues/43)) ([64b952f](https://github.com/dorukardahan/B12/commit/64b952fb7a4dab2e7cb8e9ee60660f8a5a69ecc6)), closes [#1](https://github.com/dorukardahan/B12/issues/1) [#2](https://github.com/dorukardahan/B12/issues/2)

# [11.48.0](https://github.com/dorukardahan/B12/compare/v11.47.0...v11.48.0) (2026-05-19)


### Features

* **cc:** SubagentStart per-agent recall + agent-teams teammate-aware SessionStart ([#44](https://github.com/dorukardahan/B12/issues/44)) ([84824ce](https://github.com/dorukardahan/B12/commit/84824ce1cc063698315e7fe0a95bd6c131ece552)), closes [anthropics/claude-code#52628](https://github.com/anthropics/claude-code/issues/52628) [#1](https://github.com/dorukardahan/B12/issues/1) [#2](https://github.com/dorukardahan/B12/issues/2) [#2](https://github.com/dorukardahan/B12/issues/2)

# [11.47.0](https://github.com/dorukardahan/B12/compare/v11.46.0...v11.47.0) (2026-05-19)


### Features

* **cc:** Cursor MDC globs Auto-Attached + PageRank file-rank in SessionStart ([#45](https://github.com/dorukardahan/B12/issues/45)) ([993e6ea](https://github.com/dorukardahan/B12/commit/993e6ead90c3e47146ab45668f6bc3f8e23fe97a))

# [11.46.0](https://github.com/dorukardahan/B12/compare/v11.45.0...v11.46.0) (2026-05-18)


### Features

* **hooks:** Codex PreToolUse + PostToolUse + PreCompact + cloud bridge (CX2) ([#42](https://github.com/dorukardahan/B12/issues/42)) ([1705b4b](https://github.com/dorukardahan/B12/commit/1705b4b1b632fa577509d0fb869077ee85c42ee9))

# [11.45.0](https://github.com/dorukardahan/B12/compare/v11.44.0...v11.45.0) (2026-05-18)


### Features

* **hooks:** Codex Round 0 + SessionStart/Stop/UserPromptSubmit (CX0+CX1) ([#41](https://github.com/dorukardahan/B12/issues/41)) ([d67b9a5](https://github.com/dorukardahan/B12/commit/d67b9a51ce74e145a327ef4369e3f4df5d1deb78)), closes [#22861](https://github.com/dorukardahan/B12/issues/22861) [#22008](https://github.com/dorukardahan/B12/issues/22008) [#21160](https://github.com/dorukardahan/B12/issues/21160) [#22861](https://github.com/dorukardahan/B12/issues/22861) [#7](https://github.com/dorukardahan/B12/issues/7) [#22008](https://github.com/dorukardahan/B12/issues/22008) [#8](https://github.com/dorukardahan/B12/issues/8) [#21160](https://github.com/dorukardahan/B12/issues/21160) [#9](https://github.com/dorukardahan/B12/issues/9) [#22861](https://github.com/dorukardahan/B12/issues/22861) [#39](https://github.com/dorukardahan/B12/issues/39) [#8](https://github.com/dorukardahan/B12/issues/8) [#1](https://github.com/dorukardahan/B12/issues/1) [hi#risk](https://github.com/hi/issues/risk) [#22861](https://github.com/dorukardahan/B12/issues/22861) [#22861](https://github.com/dorukardahan/B12/issues/22861)

# [11.44.0](https://github.com/dorukardahan/B12/compare/v11.43.0...v11.44.0) (2026-05-18)


### Features

* **hooks:** InstructionsLoaded telemetry + FileChanged on rule files ([#40](https://github.com/dorukardahan/B12/issues/40)) ([73cea73](https://github.com/dorukardahan/B12/commit/73cea731f2467b33d4a49bf31389e53796f22ee6)), closes [#52176](https://github.com/dorukardahan/B12/issues/52176) [#44925](https://github.com/dorukardahan/B12/issues/44925) [#38](https://github.com/dorukardahan/B12/issues/38) [#38](https://github.com/dorukardahan/B12/issues/38) [#38](https://github.com/dorukardahan/B12/issues/38) [#52176](https://github.com/dorukardahan/B12/issues/52176)

# [11.43.0](https://github.com/dorukardahan/B12/compare/v11.42.0...v11.43.0) (2026-05-18)


### Features

* **hooks:** native /goal awareness + SubagentStop capture ([#39](https://github.com/dorukardahan/B12/issues/39)) ([e70fc62](https://github.com/dorukardahan/B12/commit/e70fc6269371549f78aeb7145dcafd3c79b44a1f)), closes [#38](https://github.com/dorukardahan/B12/issues/38)

# [11.42.0](https://github.com/dorukardahan/B12/compare/v11.41.0...v11.42.0) (2026-05-18)


### Features

* **hooks:** Stop + PostToolUseFailure capture ([#38](https://github.com/dorukardahan/B12/issues/38)) ([216b306](https://github.com/dorukardahan/B12/commit/216b306fa5bcb288129c2f268ca6ae01e387b4df)), closes [hi#importance](https://github.com/hi/issues/importance)

# [11.41.0](https://github.com/dorukardahan/B12/compare/v11.40.1...v11.41.0) (2026-05-18)


### Features

* **recall:** cross-session high-importance candidates ([#36](https://github.com/dorukardahan/B12/issues/36)) ([c7d5a17](https://github.com/dorukardahan/B12/commit/c7d5a1705f60dc876fbd31a819c17972e608f47c)), closes [hi#importance](https://github.com/hi/issues/importance) [#34](https://github.com/dorukardahan/B12/issues/34) [hi#importance](https://github.com/hi/issues/importance)

## [11.40.1](https://github.com/dorukardahan/B12/compare/v11.40.0...v11.40.1) (2026-05-18)


### Bug Fixes

* **speed:** C14 sync-path microopt — vectorise daemon recall cosine ([#37](https://github.com/dorukardahan/B12/issues/37)) ([05f3588](https://github.com/dorukardahan/B12/commit/05f3588cc771c3dd36782213b4fea7a40eeaf172))

# [11.40.0](https://github.com/dorukardahan/B12/compare/v11.39.0...v11.40.0) (2026-05-18)


### Features

* **eval:** S5 GGUF Q4 vs Q8 benchmark + eval doc completion ([#35](https://github.com/dorukardahan/B12/issues/35)) ([9cb2b56](https://github.com/dorukardahan/B12/commit/9cb2b5633d0d232403fa91b4e0b92729a54d4e8d))

# [11.39.0](https://github.com/dorukardahan/B12/compare/v11.38.2...v11.39.0) (2026-05-18)


### Features

* **longsession:** Q2 topic-shift cosine-drift trigger (P-BURNIN-C) ([#34](https://github.com/dorukardahan/B12/issues/34)) ([5010d48](https://github.com/dorukardahan/B12/commit/5010d48e1fc77cb1bb12b397a57be250c31a847e)), closes [hi#importance](https://github.com/hi/issues/importance) [hi#importance](https://github.com/hi/issues/importance)

## [11.38.2](https://github.com/dorukardahan/B12/compare/v11.38.1...v11.38.2) (2026-05-18)


### Bug Fixes

* **hooks:** hard sync timeout via foreground-child kill (P-BURNIN-B) ([#33](https://github.com/dorukardahan/B12/issues/33)) ([aabb908](https://github.com/dorukardahan/B12/commit/aabb908920b1a6bc682733e833b34891aebf875e)), closes [#24](https://github.com/dorukardahan/B12/issues/24)

## [11.38.1](https://github.com/dorukardahan/B12/compare/v11.38.0...v11.38.1) (2026-05-18)


### Bug Fixes

* **hooks:** pipefail sweep + surfacing-state flock ([#32](https://github.com/dorukardahan/B12/issues/32)) ([8d4c516](https://github.com/dorukardahan/B12/commit/8d4c5163fdc12dd63c77acee96f571796a747111))

# [11.38.0](https://github.com/dorukardahan/B12/compare/v11.37.0...v11.38.0) (2026-05-18)


### Features

* **eval+final:** S5 quantization mini-bench + R11 sprint summary (P-EVAL+P-FINAL) ([#31](https://github.com/dorukardahan/B12/issues/31)) ([c22080a](https://github.com/dorukardahan/B12/commit/c22080a4a779e3cb62b17821a30d95ceb4f9c52d)), closes [#27](https://github.com/dorukardahan/B12/issues/27) [#27](https://github.com/dorukardahan/B12/issues/27) [#27](https://github.com/dorukardahan/B12/issues/27) [#27](https://github.com/dorukardahan/B12/issues/27) [#27](https://github.com/dorukardahan/B12/issues/27) [#27](https://github.com/dorukardahan/B12/issues/27)

# [11.37.0](https://github.com/dorukardahan/B12/compare/v11.36.0...v11.37.0) (2026-05-18)


### Features

* **longsession:** periodic re-surface of early-session high-importance memories (P-LONGSESSION / Q2) ([#30](https://github.com/dorukardahan/B12/issues/30)) ([6e9da5a](https://github.com/dorukardahan/B12/commit/6e9da5aed6f08f05ddee438bb67f783c76482b43)), closes [hi#importance](https://github.com/hi/issues/importance) [hi#importance](https://github.com/hi/issues/importance) [hi#importance](https://github.com/hi/issues/importance) [#26](https://github.com/dorukardahan/B12/issues/26) [#26](https://github.com/dorukardahan/B12/issues/26)

# [11.36.0](https://github.com/dorukardahan/B12/compare/v11.35.0...v11.36.0) (2026-05-18)


### Features

* **recall:** Q3 semantic trigger + Q4 4-field surface format + Q5 checkpoint telemetry (P-RECALL) ([#29](https://github.com/dorukardahan/B12/issues/29)) ([d3a382a](https://github.com/dorukardahan/B12/commit/d3a382a736d6d166690ccaf2aa70546f3b23841f)), closes [#25](https://github.com/dorukardahan/B12/issues/25) [#25](https://github.com/dorukardahan/B12/issues/25) [#25](https://github.com/dorukardahan/B12/issues/25)

# [11.35.0](https://github.com/dorukardahan/B12/compare/v11.34.0...v11.35.0) (2026-05-18)


### Features

* **speed:** async hooks + sync cap + trivial skip + Codex round-3 fixes (P-SPEED) ([#28](https://github.com/dorukardahan/B12/issues/28)) ([cfe12b9](https://github.com/dorukardahan/B12/commit/cfe12b932782ac86711c558d965b1ace6611d723)), closes [#23](https://github.com/dorukardahan/B12/issues/23) [#23](https://github.com/dorukardahan/B12/issues/23) [#24](https://github.com/dorukardahan/B12/issues/24) [#24](https://github.com/dorukardahan/B12/issues/24)

# [11.34.0](https://github.com/dorukardahan/B12/compare/v11.33.0...v11.34.0) (2026-05-18)


### Features

* **foundation:** BGE-M3 multilingual embed + token budget + daemon recall (P-FOUNDATION) ([#23](https://github.com/dorukardahan/B12/issues/23)) ([f668a03](https://github.com/dorukardahan/B12/commit/f668a03af6d2c1b67b8f68eb26b2ae64b76fcbb4)), closes [#26](https://github.com/dorukardahan/B12/issues/26) [#26](https://github.com/dorukardahan/B12/issues/26)

# [11.33.0](https://github.com/dorukardahan/B12/compare/v11.32.0...v11.33.0) (2026-05-18)


### Features

* **extraction:** LLM extraction subagent (Anthropic + Ollama, default-off, opt-in) ([#17](https://github.com/dorukardahan/B12/issues/17)) ([952d4c5](https://github.com/dorukardahan/B12/commit/952d4c5b917d1c857512de82a27fddf0f3145beb))

# [11.32.0](https://github.com/dorukardahan/B12/compare/v11.31.1...v11.32.0) (2026-05-18)


### Features

* **health:** host-side plugin-load probe for MCP B12 entries ([#20](https://github.com/dorukardahan/B12/issues/20)) ([8c5331d](https://github.com/dorukardahan/B12/commit/8c5331d279cb3943ea89202b8783e72f9d0181d5))
* **install:** --fix-drift opt-in mode to auto-register detected platforms ([#19](https://github.com/dorukardahan/B12/issues/19)) ([93ce9fd](https://github.com/dorukardahan/B12/commit/93ce9fda583d0b7e4d92c38b82ae8461a839ef41))
* **scripts:** durable JSONL ingest queue with ACK pointer (mahobrain port) ([#21](https://github.com/dorukardahan/B12/issues/21)) ([5bd2f05](https://github.com/dorukardahan/B12/commit/5bd2f058ebf7303163887c14fda2c9b60be411bd))

## [11.31.1](https://github.com/dorukardahan/B12/compare/v11.31.0...v11.31.1) (2026-05-18)


### Bug Fixes

* **skills:** resolve b12-memory skill name collision ([#18](https://github.com/dorukardahan/B12/issues/18)) ([8e4f23e](https://github.com/dorukardahan/B12/commit/8e4f23e6190bd2e5ac90f5889e20a4386373a5ef))

# [11.31.0](https://github.com/dorukardahan/B12/compare/v11.30.2...v11.31.0) (2026-05-17)


### Bug Fixes

* **kimi:** platform-mcp-drift warning + investigation doc ([#13](https://github.com/dorukardahan/B12/issues/13)) ([8962314](https://github.com/dorukardahan/B12/commit/896231452f94603493cef1922e439076d6bbf4ba))


### Features

* **grok:** native Grok CLI integration + investigation doc ([#14](https://github.com/dorukardahan/B12/issues/14)) ([dbdf50c](https://github.com/dorukardahan/B12/commit/dbdf50c7239b9bd69aaefcbe239be93cf6c16080)), closes [hi#value](https://github.com/hi/issues/value)

## [11.30.2](https://github.com/dorukardahan/B12/compare/v11.30.1...v11.30.2) (2026-05-17)


### Bug Fixes

* **opencode:** pre-compact signature + dist async fixes + investigation doc ([#12](https://github.com/dorukardahan/B12/issues/12)) ([f27b6fa](https://github.com/dorukardahan/B12/commit/f27b6fa7661577b65f1f72633cda641572d55340))

## [11.30.1](https://github.com/dorukardahan/B12/compare/v11.30.0...v11.30.1) (2026-05-17)


### Bug Fixes

* **gemini:** project_name normalization for bench / temp-dir hosts ([#11](https://github.com/dorukardahan/B12/issues/11)) ([bde5e9d](https://github.com/dorukardahan/B12/commit/bde5e9d1eeaa50346cb41c1c34ff58da3eb58400))

# [11.30.0](https://github.com/dorukardahan/B12/compare/v11.29.0...v11.30.0) (2026-05-17)


### Features

* **scripts:** Codex CLI research doc + cross-CLI canonicalization helper ([#10](https://github.com/dorukardahan/B12/issues/10)) ([8b6ba3f](https://github.com/dorukardahan/B12/commit/8b6ba3f674108fee0d26e35d8c116a6e01bf8f60))

# [11.29.0](https://github.com/dorukardahan/B12/compare/v11.28.0...v11.29.0) (2026-05-17)


### Features

* **mcp:** markdown session_context renderer with per-row content cap ([#9](https://github.com/dorukardahan/B12/issues/9)) ([64f2ee4](https://github.com/dorukardahan/B12/commit/64f2ee4e95410e207aa47142b1c301a09d694917))

# [11.28.0](https://github.com/dorukardahan/B12/compare/v11.27.0...v11.28.0) (2026-05-17)


### Features

* **mcp:** memory_forget privacy + cleanup MCP tool ([#8](https://github.com/dorukardahan/B12/issues/8)) ([b48d53a](https://github.com/dorukardahan/B12/commit/b48d53ad23ab729cffa9948ad51c2370e29d75a4))

# [11.27.0](https://github.com/dorukardahan/B12/compare/v11.26.0...v11.27.0) (2026-05-17)


### Features

* **scoring:** ingest-time importance heuristics with EN+TR tokens ([#7](https://github.com/dorukardahan/B12/issues/7)) ([1cbcf25](https://github.com/dorukardahan/B12/commit/1cbcf25541ea0f3bb1b39561b7252d8d33132fdf)), closes [#2](https://github.com/dorukardahan/B12/issues/2)

# [11.26.0](https://github.com/dorukardahan/B12/compare/v11.25.0...v11.26.0) (2026-05-17)


### Features

* **hooks:** UserPromptSubmit auto-recall boost on EN+TR recall verbs ([#6](https://github.com/dorukardahan/B12/issues/6)) ([13f4d30](https://github.com/dorukardahan/B12/commit/13f4d303d242d4b14d21b128a6789a08609eb40e)), closes [#4](https://github.com/dorukardahan/B12/issues/4) [#5](https://github.com/dorukardahan/B12/issues/5)

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
* **cleanup:** removed an unused third-party MCP server entirely. Config entries deleted from `~/.claude.json` and `~/.codex/config.toml`; binaries archived to `/tmp/mcp-archive-<TS>/` for reversibility.

### Performance

* **mcp:** ~59% RSS reduction with 4 parallel Claude Code sessions (~600 MB → ~248 MB). Cold-start handshake latency unchanged at the proxy boundary because the proxy module still imports `b12_mcp_server` transitively; a strictly thinner proxy is queued as a v11.23 follow-up (estimated additional ~0.3 s × N-sessions saving).

### Forensic audit (2026-03-16 → 2026-05-17)

* Audited 2,981 sessions across all 6 supported CLIs. **B12 is stable when called (0 errors, 0 timeouts) but massively underutilized.** Claude Code: 30/36 sessions (83%) consumed the `MEMORY SYSTEM ACTIVE` SessionStart context and never invoked any B12 tool. OpenCode/Grok/Kimi: 0% real invocation rate despite the MCP server being loaded. Gemini: 1/139 sessions invoked B12 once and got `response={}` (integration broken end-to-end). Codex 2026-05-07: 10 parallel rollouts all missed an expected recall trigger.
* Closing the underutilization gap is **out of scope** for this release — logged as v11.23 follow-up backlog (skill / SessionStart-context redesign, Gemini path fix, Codex recall-trigger pattern matching).

### PR #2 review (`feat/hybrid-weights-rebalance` by @AytuncYildizli)

* Reviewed end-to-end. **Recommendation: MERGE with minor follow-ups** (manual recall-DB comparison, normalizer-flag tracker, daemon-migration env-var note). Code is clean, surgical (44/3 LOC, single file), references validate, mergeable against current `main`. PR adds `strength` as 4th score dimension with env-overridable weights — no conflict with the daemon migration.

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
