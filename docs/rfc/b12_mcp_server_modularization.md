# RFC: `b12_mcp_server.py` modularization

**Status:** Proposed
**Date:** 2026-05-24
**Driver:** Independent audit (2026-05-24) flagged the file as a contributor-onboarding bottleneck and merge-conflict hotspot.

## Problem

`scripts/b12_mcp_server.py` is currently a single 91 KB / ~2 200-line module that holds:

- 13 MCP tool handlers (`memory_store`, `memory_search`, `memory_update`, `memory_delete`, `memory_forget`, `memory_quality`, `memory_session_context`, `memory_consolidate`, `memory_refine`, `memory_surface`, `memory_export`, `memory_import`, `memory_dashboard`)
- 4 MCP resources (`b12://context/project/{name}`, `b12://stats`, `b12://profile`, `b12://health`)
- Schema initialization (`_ensure_schema()` — FTS5 tables, triggers, embeddings table, audit table)
- Hybrid scoring engine (BM25 + cosine + strength)
- Spaced-repetition `_boost_strength_on_search()` mechanics
- Working-context tracker (per-session ring buffer)
- Schema migration entry points
- Logger + signal handlers + entry point

This colocation has three concrete costs:

1. **Onboarding** — first-time contributors must read the entire file before they can confidently change any single handler, because helper functions and shared state live cross-cut.
2. **Merge conflicts** — every PR that adds a tool / scoring tweak / migration touches the same file. Two parallel PRs almost always conflict on the imports block, the `_logger` decl, or the `_ensure_schema()` body.
3. **Test surface** — `scripts/tests/` currently has no unit coverage for individual tool handlers because importing the module pulls in the entire MCP server lifecycle (socket binding, signal handlers, etc.).

## Proposed split

The current file becomes a thin shell (`scripts/b12_mcp_server.py`, ~150 lines) whose only job is to:

- Construct the FastMCP server
- Register every tool / resource by importing from sub-modules
- Wire signal handlers
- Run

Sub-modules live under `scripts/mcp/` (new package, `__init__.py` re-exports public symbols for backwards compatibility):

```
scripts/mcp/
├── __init__.py             #   public surface (memory_store, memory_search, …)
├── server.py               #   FastMCP server build + signal handlers
├── schema.py               #   _ensure_schema(), table DDL, triggers
├── scoring.py              #   BM25 / cosine / strength fusion, time decay
├── working_context.py      #   per-session ring buffer
├── audit.py                #   audit row helpers (memory_audit table)
├── tools/
│   ├── __init__.py
│   ├── store.py            #   memory_store
│   ├── search.py           #   memory_search
│   ├── update.py           #   memory_update
│   ├── delete.py           #   memory_delete + memory_forget (shared core)
│   ├── quality.py          #   memory_quality
│   ├── session_context.py  #   memory_session_context
│   ├── consolidate.py      #   memory_consolidate
│   ├── refine.py           #   memory_refine
│   ├── surface.py          #   memory_surface
│   ├── transfer.py         #   memory_export + memory_import
│   └── dashboard.py        #   memory_dashboard
└── resources/
    ├── __init__.py
    ├── project_context.py
    ├── stats.py
    ├── profile.py
    └── health.py
```

Each tool file exports a single `register(server)` function that the shell's `server.py` calls during boot. Tool files own their own helper functions and only import shared utilities (scoring, schema, audit) from sibling modules.

## Migration strategy

This refactor MUST NOT change runtime behavior. The migration sequence:

1. **PR 1 — extract pure helpers** (low risk). Move `scoring.py`, `working_context.py`, `audit.py`, `schema.py` out of the monolith. Keep public symbols re-exported from `b12_mcp_server.py` via `from scripts.mcp.scoring import *`. CI passes unchanged; no functional drift.

2. **PR 2 — extract resources** (low risk). Resources are stateless and have minimal cross-cutting. Move all 4 to `scripts/mcp/resources/` and register them from the shell.

3. **PR 3-N — extract tools one at a time** (medium risk). Each PR extracts exactly one tool, moves its helpers, adds dedicated unit tests under `scripts/tests/mcp/tools/`, and verifies via LoCoMo bench that recall@5 / token_f1 don't shift. Order: simplest first (`memory_quality`, `memory_dashboard`, `memory_surface`) before complex (`memory_search`, `memory_store`, `memory_session_context`).

4. **PR N+1 — collapse re-exports** (cleanup). Once all callers (hooks, install.sh paths, other scripts) reference symbols via the new package paths, remove the wildcard re-exports from the shell. The shell shrinks to ~150 lines.

## Compatibility contract

- **Hook side:** Every hook that shells out to `b12_mcp_server.py` (via `~/.B12/hooks/scripts/b12_mcp_server.py`) continues to work because the entry-point path is unchanged.
- **MCP side:** Tool names and signatures don't change. AI hosts (Claude Code, Codex, Cursor, etc.) see no API drift.
- **DB side:** Schema doesn't change. No migration required.
- **Import path:** External consumers (e.g., `scripts/dashboard_server.py`) that currently do `from b12_mcp_server import memory_search` keep working via re-exports until PR N+1, at which point they switch to `from scripts.mcp.tools.search import memory_search`.

## Non-goals

This RFC explicitly does **not** propose:

- Changing any MCP tool's behavior or signature
- Touching the `validate_input` patch mechanism (still applies to the shell)
- Splitting `b12_mcp_daemon.py` (the thin daemon proxy — already small)
- Migrating to a different MCP framework (FastMCP is fine)
- Adding new tools or resources

Behavioral changes go in separate PRs after the modularization lands.

## Estimated effort

- PR 1 (pure helpers): 1 day, low risk
- PR 2 (resources): 0.5 day, low risk
- PR 3-N (per-tool): 0.5 day each × 13 tools = 6.5 days, medium risk
- PR N+1 (collapse re-exports): 0.5 day, low risk

Total: ~8 days of focused work, spread across 2-3 weeks to allow LoCoMo bench runs and incremental review.

## Open questions

1. **Test runner choice for new tool unit tests** — extend the existing `scripts/tests/` suite or carve a new `scripts/tests/mcp/` tree? Recommend the latter for visual separation.
2. **Re-export strategy** — wildcard `from scripts.mcp.tools.store import *` is convenient but loses static analysis. Explicit re-export lists are stricter but require maintenance during PR 3-N. Recommend explicit.
3. **Schema migration colocation** — the `migrate_*.py` scripts at the top of `scripts/` are version-pinned schema migrations. They stay where they are; only the runtime `_ensure_schema()` moves into `scripts/mcp/schema.py`.

## Decision point

Implementation begins after this RFC is reviewed and a tracking issue is opened. Reviewers should focus on:

- Is the split-by-tool granularity right, or should related tools (e.g., `memory_delete` + `memory_forget`) collapse into one module per audit's "overlap" observation?
- Is the `scripts/mcp/` package name correct, or should it be `scripts/b12_mcp/` to avoid confusion with the MCP SDK itself?
- Does the re-export strategy break any current static-analysis or IDE tooling assumptions?

This RFC is itself the planning artifact called out in the 2026-05-24 audit's Section 3 #2 finding. No code changes accompany it; implementation work waits for explicit go-ahead.
