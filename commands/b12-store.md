---
name: b12-store
description: Store a memory in B12 persistent memory system
---

Store a new memory in the B12 system.

**Usage**: `/b12-store <content>` or `/b12-store` (interactive)

If content was provided after the command, store it directly. Otherwise, ask what to store.

Steps:
1. Determine the memory content and appropriate metadata:
   - Classify type: decision, error/gotcha, pattern, learning, preference, progress
   - Set importance_score: 2.0 (critical), 1.5 (important), 1.0 (normal), 0.7 (temporary)
   - Add project scope tag: `proj:{current_project}`
2. Call `mcp__B12__memory_store` with:
   - `content`: The memory text, prefixed with type label (e.g., `[Decision]`, `[Gotcha]`)
   - `metadata`: `{"type": "...", "importance_score": N, "project": "...", "setup": "...", "scope": "project"}`
   - `tags`: `["proj:{project}", "user:{setup}", "type:{type}"]`
3. Confirm storage: `( 💊 B12 🧠 : saved to memory ✅ )`
