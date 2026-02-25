---
name: b12-memory
description: >
  Persistent cross-session memory system. Use at session start to load context
  from previous sessions, when user asks about past work, and before session end
  to store important findings. Triggers on: memory, remember, previous session,
  last time, what did we, store this, save this finding.
---

# B12 Memory System

You have persistent memory via 4 MCP tools. Use them proactively.

## Session Start Ritual

When starting a new session, ALWAYS do this first:

```
mcp__B12__memory_search(query="recent work", tags="proj:{project_name}")
```

Replace `{project_name}` with the current directory name. This loads context from previous sessions (both Claude Code and Codex).

## When to Search

- User asks about past work, decisions, or errors
- User references "last time", "before", "previously", "remember when"
- You need context about a project's conventions or patterns
- Before making architectural decisions (check for existing decisions)

## When to Store

Store memories BEFORE the session ends. There is no automatic session summary on Codex.

### What to Store

| Type | When | Importance |
|------|------|------------|
| `decision` | Architecture choices, trade-offs | 0.8-0.9 |
| `gotcha` | Bugs found and fixed | 0.8-0.9 |
| `pattern` | Recurring code conventions | 0.7 |
| `learning` | New insights about tools/APIs | 0.7 |
| `preference` | User workflow preferences | 0.7 |
| `progress` | Session summary, what was accomplished | 0.6 |

### Tagging

Every memory MUST have:
- `proj:{directory_name}` — project scope
- `user:codex` — identifies Codex-originated memories
- `type:{type}` — from the table above
- `platform:codex` — cross-platform tracking

### Store Example

```
mcp__B12__memory_store(
  content="[Decision] Chose WebSocket over SSE for real-time updates because bidirectional communication needed for collaborative editing.",
  metadata="type:decision, importance:0.8",
  tags="proj:myapp, user:codex, type:decision, platform:codex"
)
```

## Session End Ritual

Before the user ends the session, proactively store:

1. **What was accomplished** (type: progress)
2. **Key decisions made** (type: decision)
3. **Bugs found and fixed** (type: gotcha)
4. **New things learned** (type: learning)

If the session was significant (>5 user messages or files modified), always store at least a progress summary.

## Cross-Platform Note

Memories are shared with Claude Code sessions. When you search, you'll find memories from both platforms. The `platform:codex` or `user:personal` tags distinguish the source.
