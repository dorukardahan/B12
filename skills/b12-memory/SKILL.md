---
name: b12-memory
description: >
  B12 persistent memory system — behavioral instructions for memory tools.
  Use when working with memory_search, memory_store, memory_update,
  memory_quality, or any B12 MCP tool. Triggers on: memory, remember,
  previous session, last time, store this, save this, recall, context.
---

# B12 Memory System — Behavioral Guide

You have persistent cross-session memory via B12 MCP tools. Use them proactively.

## Core Tools

| Tool | Purpose | Key Parameters |
|------|---------|----------------|
| `memory_search` | Semantic + keyword hybrid search | `mode=hybrid`, `after/before=ISO date`, `max_response_chars=40000` |
| `memory_store` | Store a memory with metadata | Always include `metadata` and `tags` |
| `memory_update` | Update existing memory content | Requires memory ID |
| `memory_quality` | Analyze memory quality and health | `analyze` or `report` mode |
| `memory_refine` | Batch refinement of candidates | For improving multiple memories |
| `memory_consolidate` | Merge duplicate/related memories | Reduces redundancy |
| `memory_surface` | Proactive memory surfacing | Context-aware retrieval |
| `memory_session_context` | Full session context on first call | Rich startup context |
| `memory_export` / `memory_import` | Backup and restore | Portable B12 snapshots |
| `memory_dashboard` | Visual health overview | Metrics and stats |

## Time Search

When user says approximate time ("2 days ago", "last week", "this morning"), use wide buffer:
- ±1 day for days, ±2 days for weeks
- Example: "2 days ago" → `after=3_days_ago`, `before=1_day_ago`
- If few results, widen range

## Scope System

When **STORING**, always include:
- Tags: `[proj:{project_name}, user:{setup}]`
- Metadata: `{project: "{project}", setup: "{setup}", scope: "<type>"}`

Scope types:
- `project` — codebase-specific (architecture, decisions, bugs) → tag: `proj:{project}`
- `universal` — applies everywhere (patterns, CLI tricks, lessons) → tag: `user:universal`
- `preference` — user preferences (always global) → tag: `user:pref`
- `setup` — team/workflow specific → tag: `user:{setup}`

When **SEARCHING**:
- Default: `tags=["proj:{project}"]` to get project context. Add `user:universal` for general knowledge.
- Cross-project: no tag filter. Mentally deprioritize results from unrelated `proj:` tags.
- Few results (<3): widen scope, remove tag filter.

## Dual Memory Layers

- **MEMORY.md** = active project state (current architecture, decisions, conventions). Updated each session.
- **MCP memory** = historical knowledge (past errors, cross-project patterns, resolved issues, preferences). Searched on demand.
- Do NOT duplicate between them.

## Importance Scoring

Set `importance_score` in metadata:
- `2.0` = critical
- `1.5` = important
- `1.0` = normal
- `0.7` = temporary

Use tags: `critical`, `important`, `reference`, `temporary`.

## Auto Behavior

1. Search memory on startup with project + task keywords
2. Store silently when learning something important — categorize by type (architecture/decision/pattern/gotcha/progress/preference)
3. Update user-profile.md when learning new preferences
4. At session start, print ONE short line with the B12 pill format
5. When retrieval hook returns relevant memories or when storing, use these EXACT formats:
   - Retrieval: `( 💊 B12 🧠 : found N memories about [topic], stored [date] ✅ )`
   - Store: `( 💊 B12 🧠 : saved to memory ✅ )`
   - Not found (only when user explicitly asks): `( 💊 B12 🧠 : searched but nothing found ❌ )` — then try wider time range or different keywords before giving up
   - Keep under 15 words. Only ✅ or ❌ at the end, no other emojis after the colon.

## Store Key Learnings

When you discover decisions, errors, preferences, or architectural patterns during this session, store them as memories. For batch refinement of multiple candidates, use `memory_refine` tool.
