---
name: b12-memory
description: >
  B12 cross-session memory. Use when a task needs a fact from an earlier
  session that is absent from the visible conversation and project files, or
  when the user explicitly asks to remember, correct, or forget something. Not
  for current-chat recall, routine work, or session start/end.
---

# B12 Memory for Codex

Codex hooks already inject startup context, track turns, and capture the
session summary at SessionEnd. Use this skill only as a targeted bridge to
missing history or for explicit memory management. It is not a session ritual.

## Decision Boundary

Use B12 when:

- The task needs a fact from a previous session and that fact is absent from
  the visible conversation and current project files. Example: "What did we
  decide in the previous session about authentication?"
- The user explicitly asks for persistence. Example: "Remember this for future
  sessions."
- The user explicitly asks to correct persistent memory. Example: "Correct the
  saved preference."
- The user explicitly asks to remove persistent memory. Example: "Forget that
  saved memory."

Do not use B12 when:

- The answer is already visible in the current conversation. Example: "What
  did I say two messages ago?"
- The task can be completed from the current project files or is a routine
  task with no missing historical dependency.
- A recall word such as "earlier", "before", "az önce", or "daha önce" refers
  to content that is still visible.
- It is merely session start or session end. Hooks own that lifecycle.

## Tool Selection

| Need | Tool |
|------|------|
| Missing cross-session fact | `memory_search` with a focused query |
| Broad prior-project context, only when requested or needed | `memory_session_context` |
| Explicitly persist something | `memory_store` with scoped metadata and tags |
| Change metadata, tags, type, or strength | `memory_update` with `content_hash` |
| Remove one memory | `memory_delete`, soft and reversible by default |
| Privatize or forget a whole session | `memory_forget` |

For a content correction, find the old memory, soft-delete it, then store the
corrected version. Use hard deletion only when the user explicitly requires an
irreversible delete or a captured secret must be removed.

## Search and Storage Rules

- Start with a focused hybrid search and the current project tag when known.
- For approximate dates, allow a one-day buffer for day references and a
  two-day buffer for week references; widen only if the first search is sparse.
- Store only the content the user explicitly wants persisted. Never store
  credentials or secrets.
- Keep tool calls quiet unless the user asks what was searched or changed.
