## B12 Memory System

B12 hooks already inject startup context and capture the session summary for
Codex. Use the MCP tools only for targeted cross-session recall or explicit
memory management.

- Treat the visible conversation and current project files as authoritative.
  Do not call B12 for information already present there, even when the user
  says "earlier", "before", "az önce", or "daha önce".
- Search B12 only when the task needs cross-session history that is absent
  from current context. A recall phrase by itself is not enough.
- Store, update, or forget memory only when the user asks to persist, correct,
  or remove something.
- Do not call `memory_session_context` at startup. Do not manually store a
  session summary at the end. Hooks own the session lifecycle.
- The `b12-memory` skill covers tool selection when these conditions are met.
