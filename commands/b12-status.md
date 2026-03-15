---
name: b12-status
description: Check B12 memory system health and statistics
---

Run a B12 health check and display system status.

Steps:
1. Call `mcp__B12__memory_dashboard` to get overall statistics
2. Call `mcp__B12__memory_quality` with mode `report` for quality metrics
3. Display a concise status report:

```
💊 B12 Status
├─ Memories: {total} ({active} active, {archived} archived)
├─ Projects: {list of project tags}
├─ DB size: {size}
├─ Quality score: {score}/10
├─ Last stored: {date}
├─ Embeddings: {embedded}/{total} ({percentage}%)
└─ MCP server: {connected/disconnected}
```

4. If any issues found, suggest fixes:
   - Low embedding coverage → "Run memory_quality analyze to check"
   - Stale memories → "Consider running memory_consolidate"
   - Quality issues → List specific recommendations
