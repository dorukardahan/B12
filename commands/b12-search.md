---
name: b12-search
description: Search B12 persistent memory for past decisions, errors, patterns, and learnings
---

Search the B12 memory system for relevant memories.

**Usage**: `/b12-search <query>`

If the user provided a query after the command, use it directly. Otherwise, ask what they want to search for.

Steps:
1. Call `mcp__B12__memory_search` with the query
   - Use `mode: "hybrid"` for best results (combines semantic + keyword)
   - Add `tags: ["proj:{current_project}"]` to scope to current project
   - If searching across projects, omit tags
2. Present results in a concise format:
   - Show the most relevant 3-5 memories
   - Include the date stored and relevance score
   - Group by type (decision, error, pattern, etc.) if multiple types
3. If no results found, try:
   - Broader keywords
   - Removing tag filters
   - Wider time range
