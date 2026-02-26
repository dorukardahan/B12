## B12 Memory System

You have access to a persistent memory system (B12) via MCP tools. Use it proactively.

### Available Tools

| Tool | Purpose |
|------|---------|
| `mcp__B12__memory_store` | Store important findings, decisions, patterns, errors |
| `mcp__B12__memory_search` | Search past memories by keywords, tags, or semantic similarity |
| `mcp__B12__memory_update` | Update metadata, tags, or strength of existing memories |
| `mcp__B12__memory_quality` | Rate memory quality or check system health |

### When to Search Memory

- **Session start**: Search for `proj:{project_name}` to load context from previous sessions
- **Before answering questions about past work**: Search with relevant keywords
- **When the user references something from before**: Search to find the exact details

### When to Store Memory

- **Decisions**: Architecture choices, design trade-offs, "we chose X because Y"
- **Errors & Fixes**: Bugs encountered and how they were resolved
- **Patterns**: Recurring code patterns, naming conventions, project-specific rules
- **Preferences**: User's workflow preferences, tool choices, communication style
- **Learnings**: New insights about the codebase, APIs, or tools

### Tagging Rules

Every memory MUST have scope tags:
- `proj:{project_name}` — project scope (use the directory name)
- `user:{username}` — user scope (use the system username)

Memory types: `architecture`, `decision`, `pattern`, `gotcha`, `preference`, `progress`

### Example Usage

```
# At session start
mcp__B12__memory_search(query="recent work", tags="proj:myproject")

# After solving a tricky bug
mcp__B12__memory_store(
  content="PostgreSQL connection pool exhaustion was caused by unclosed cursors in the batch job. Fix: added context manager wrapper.",
  metadata="type:gotcha, importance:0.9",
  tags="proj:myproject, user:yourname"
)

# Before ending session
mcp__B12__memory_store(
  content="Session summary: Implemented OAuth2 flow, fixed pool exhaustion bug, updated API docs.",
  metadata="type:progress, importance:0.7",
  tags="proj:myproject, user:yourname"
)
```

### Important

- Memories are shared across Claude Code, Codex, Kimi Code, and other MCP-compatible sessions
- Search returns memories from ALL past sessions (all platforms)
- Store important findings BEFORE the session ends — there is no automatic session summary
- When uncertain if something was discussed before, SEARCH first
