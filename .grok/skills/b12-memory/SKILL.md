---
name: b12-memory
description: >
  B12 persistent cross-session memory system for Grok. Use automatically at the start of any work session, when the user mentions previous sessions/decisions/patterns/gotchas ("remember", "last time", "what did we decide", "history", "before"), before making architectural choices, or when asked to recall/store/find context. Always prefer the B12__memory_* tools. Supports spawning specialized subagents via the task tool with fork_context for deep extraction, auditing, or surfacing on long sessions.
short-description: Persistent memory via B12 (MCP + embeddings + Ebbinghaus decay + consolidation)
when-to-use: "memory recall, previous work, project history, decisions, bugs fixed, patterns, user preferences, before major refactors or architecture decisions, session handoff"
allowed-tools: "B12__memory_search, B12__memory_store, B12__memory_update, B12__memory_session_context, B12__memory_surface, B12__memory_consolidate, B12__memory_refine, B12__memory_quality, B12__memory_dashboard, B12__memory_export, B12__memory_import, task"
---

# B12 Memory System — Grok Native

You have a high-quality persistent memory system powered by the B12 MCP server (already connected as `B12`).

## Core Principles

- **Dual Layer**: 
  - `MEMORY.md` files in the repo for active, human-readable project state.
  - B12 MCP (SQLite + FTS5 + vector embeddings + Ebbinghaus/FSRS decay) for historical, searchable, cross-session knowledge.
- **Always tag on store**: `proj:{project}`, `user:{setup}` (e.g. `user:personal` or `user:0g`), plus `type:decision|gotcha|pattern|learning|preference|progress`.
- **Importance scoring**: Use `importance_score` in metadata (2.0 = critical, 1.5 = important, 1.0 = normal).
- **Time-aware search**: When user says "2 days ago", "last week", use wide buffers (`after`/`before` with ±1-2 days).

## Session Start Ritual (Do this early)

When starting work in the B12 directory (or any project using B12):

1. Call `B12__memory_session_context(project_name="B12", cwd="<your project working directory>")` for rich startup context.
2. Follow with targeted `B12__memory_search` (hybrid mode, proper tags, time filters if relevant).
3. Output a short, clean **B12 pill** in this exact format (one line):

   `( 💊 B12 🧠 : found N memories about [topic], stored [date] ✅ )`

Only use ✅ or ❌ at the end. Keep under 15 words.

## When to Store

Proactively store before the user ends the session or after significant progress:

| Type       | When to store                          | Importance |
|------------|----------------------------------------|------------|
| decision   | Architecture choices, trade-offs       | 0.8-0.9    |
| gotcha     | Bugs found + root cause + fix          | 0.8-0.9    |
| pattern    | Recurring conventions or workflows     | 0.7        |
| learning   | New insights about tools, models, APIs | 0.7        |
| preference | User workflow / setup preferences      | 0.7        |
| progress   | Major accomplishments in the session   | 0.6        |

Use `B12__memory_store` with full metadata and tags. For related items, consider `B12__memory_consolidate` or `B12__memory_refine` later.

## Using Subagents for Heavy Memory Work (Grok Strength)

For complex or long sessions, do **not** do everything yourself. Spawn specialized memory workers:

``` 
Use the task tool with:
- subagent_type: "explore" or "general-purpose"
- persona: "researcher" or custom memory-auditor
- fork_context: true (so they see the current session history)
- prompt: Clear instructions to use B12__memory_* tools, analyze chat_history.jsonl if needed, produce structured findings
```

This is one of Grok's biggest advantages over other platforms — use it.

## During Work

- Call `B12__memory_surface` proactively when context about past work would help.
- On significant file edits or decisions, consider a quick `B12__memory_store`.
- Before big refactors or architecture discussions, always search first.

## Tool Usage Notes (Grok Specific)

- Tool names are namespaced: `B12__memory_search`, `B12__memory_store`, etc.
- You can verify loaded tools and config anytime with `grok inspect`.
- Skills are discoverable and manageable via `/skills` or the TUI (`Ctrl+L`).
- The plugin version lives in `.grok/plugins/b12/`.

## Verification Commands

- `grok inspect` — see that B12 MCP + skills + any hooks are loaded.
- `/skills` — confirm b12-memory is active.
- `grok mcp list` — confirm the B12 server is connected and healthy.

This skill unifies and improves the previous b12 / b12-memory skills for Grok's native strengths (declarative auto-invoke, subagents with fork_context, clean TUI integration, and plugin distribution). 

Always keep the shared B12 core (MCP server, engines, embed daemon) as the single source of truth — this skill is just the Grok-native interface layer.