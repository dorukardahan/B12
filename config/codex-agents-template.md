## B12 Memory System

You have access to a persistent memory system (B12) via MCP tools. Use it proactively to remember decisions, patterns, errors, and preferences across sessions.

### Available Tools

| Tool | Purpose |
|------|---------|
| `mcp__B12__memory_store` | Store decisions, patterns, errors, preferences to persistent memory |
| `mcp__B12__memory_search` | Search memories by keywords, tags, dates, or semantic similarity |
| `mcp__B12__memory_update` | Update tags, metadata, or strength of existing memories |
| `mcp__B12__memory_quality` | Rate memory quality, get stats, or analyze system health |
| `mcp__B12__memory_session_context` | Get session start context — project memories, last summary, instructions |

### Session Start (REQUIRED)

Your **FIRST action** in every new session MUST be:
```
mcp__B12__memory_session_context(project_name="<project>")
```
This returns pre-fetched project memories, last session summary, and behavioral instructions in a single call.

### Tool Parameters

**memory_store(content, metadata)**
- `content` (str, required): The memory text
- `metadata` (dict, optional): Contains tags, type, importance, quality score
  - `"tags"`: list or comma-string, e.g. `["proj:myproject", "user:personal"]`
  - `"type"`: memory category — `architecture`, `decision`, `pattern`, `gotcha`, `preference`, `progress`, `learning`, `error_fix`
  - `"importance_score"`: float — 2.0 (critical), 1.5 (important), 1.0 (normal), 0.7 (temporary)
  - `"quality_score"`: 0.0-1.0 (auto-managed; override via memory_quality)

**memory_search(query, mode, tags, limit, after, before, max_response_chars)**
- `query` (str): Search keywords or natural language
- `mode` (str): `"hybrid"` (default — keyword + semantic), `"semantic"`, `"exact"`
- `tags` (list or str, optional): Filter by scope tags, e.g. `["proj:myproject"]`
- `limit` (int): Max results (default: 10)
- `after` / `before` (str, optional): ISO date filters, e.g. `"2025-01-15"`
- `max_response_chars` (int): Response size cap (default: 40000)

**memory_update(content_hash, updates)**
- `content_hash` (str, required): Hash of the memory to update
- `updates` (dict): Fields to change — `"tags"`, `"memory_type"`, `"metadata"` (merged, not replaced), `"strength"`

**memory_quality(action, content_hash, rating, feedback)**
- `action` (str): `"rate"`, `"get"`, or `"analyze"`
- `content_hash` (str): Required for rate/get
- `rating` (str): `"1"` (good), `"0"` (neutral), `"-1"` (bad)
- `feedback` (str, optional): Quality feedback text

### Usage Examples

```
# Search for project context at session start
mcp__B12__memory_search(query="recent architecture decisions", tags=["proj:myproject"])

# Store a decision with proper metadata dict
mcp__B12__memory_store(
    content="Chose PostgreSQL over SQLite for the API — need concurrent writes and full-text search.",
    metadata={
        "tags": ["proj:myproject", "user:personal"],
        "type": "decision",
        "importance_score": 1.5
    }
)

# Store an error fix (cross-project knowledge)
mcp__B12__memory_store(
    content="Connection pool exhaustion caused by unclosed cursors in batch job. Fix: context manager wrapper.",
    metadata={
        "tags": ["proj:myproject", "user:universal"],
        "type": "gotcha",
        "importance_score": 2.0
    }
)

# Search with date range filter
mcp__B12__memory_search(query="deployment issues", after="2025-01-10", before="2025-01-15")

# Rate a memory's quality
mcp__B12__memory_quality(action="rate", content_hash="abc123", rating="1", feedback="Very useful, referenced 3 times")
```

### Scope System

Every memory MUST include scope tags in the metadata dict:

| Tag | When to use |
|-----|-------------|
| `proj:{project_name}` | Project-specific: architecture, decisions, bugs (use directory name) |
| `user:universal` | Cross-project: CLI tricks, general patterns, reusable lessons |
| `user:pref` | User preferences: workflow, tool choices, communication style |
| `user:{setup}` | Setup-specific: e.g. `user:personal`, `user:work` |

**When searching:**
- Default: filter by `tags=["proj:{project_name}"]` for project context
- Add `user:universal` for general knowledge
- Cross-project search: omit tag filter
- Few results (<3): widen scope — remove tag filter, broaden query

### Time Search

When the user references approximate time ("2 days ago", "last week"), use wide date buffers:
- **Days**: +/-1 day — e.g. "2 days ago" -> `after` = 3 days ago, `before` = 1 day ago
- **Weeks**: +/-2 days
- If few results, widen the range further before giving up

### Dual Memory Layers

Two memory systems coexist — do NOT duplicate between them:

| Layer | Purpose | Updated |
|-------|---------|---------|
| **MEMORY.md** | Active project state (current architecture, conventions) | Each session |
| **B12 MCP memory** | Historical knowledge (past errors, cross-project patterns, preferences) | On demand via tools |

Use MEMORY.md for "what's current now." Use B12 for "what happened before."

### Importance Scoring

Set `importance_score` in metadata when storing:

| Score | When to use |
|-------|-------------|
| 2.0 | Critical: breaking changes, security issues, core architecture |
| 1.5 | Important: design patterns, key decisions, recurring problems |
| 1.0 | Normal: session progress, standard findings (default) |
| 0.7 | Temporary: work-in-progress notes, quick references |

Higher importance = higher ranking in search results and pre-fetch.

### Auto Behavior

1. **Session start**: Search memory with project name + task keywords to load context
2. **During work**: Store silently when learning something important — always include scope tags and type
3. **Before answering about past work**: Search first — do not rely on conversation context alone
4. **Session end**: Store key findings, decisions, and progress
5. **Preferences**: When learning a new user preference, store with `user:pref` tag

### Important

- Memories are shared across **all** MCP-connected sessions (Claude Code, Codex, Gemini, VS Code, Cursor, Kimi, Windsurf, Cline, OpenCode) — same SQLite database
- Search returns memories from ALL past sessions across all platforms
- Store important findings DURING the session — do not wait until the end
- When uncertain if something was discussed before, SEARCH first
- Always pass `metadata` as a proper dict (not a string) when calling memory_store
