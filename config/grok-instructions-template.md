<!-- B12-MEMORY-START -->

# B12 Persistent Memory (Grok)

When working in this repository with Grok CLI, the `b12-memory` skill is available.

**Recommended practices:**
- At the start of sessions, let the `b12-memory` skill load relevant context.
- Use `B12__memory_search`, `B12__memory_store`, `B12__memory_surface` etc. via the MCP tools.
- For complex memory tasks on long sessions, use Grok's native subagent system (`task` tool with `fork_context` and researcher persona).

The B12 engine provides semantic search, Ebbinghaus decay, consolidation, and cross-session memory.

<!-- B12-MEMORY-END -->