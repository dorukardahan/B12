# B12 vs alternatives

A side-by-side, vendor-neutral look at the persistent-memory landscape for
AI coding assistants as of 2026-05-20. Sources are linked inline; correct
me with a PR if any cell drifts.

## Matrix

| Capability                       | B12 (this repo)        | Mem0                    | Letta (MemGPT)          | Cursor memory          | Claude Projects          | ChatGPT memory          |
|----------------------------------|------------------------|-------------------------|-------------------------|------------------------|--------------------------|-------------------------|
| **Storage**                      | Local SQLite + sqlite-vec | Cloud (managed) + optional self-host | Postgres / SQLite      | Cloud (Cursor account) | Cloud (Anthropic)        | Cloud (OpenAI)          |
| **Cross-tool**                   | 13 platforms via MCP   | API integrations         | API integrations         | Cursor only            | Claude apps only         | ChatGPT only            |
| **Retrieval**                    | FTS5 + 1024-dim vector hybrid + FSRS decay | Vector + graph    | Hierarchical paging    | Vector                  | Vector                   | Heuristic               |
| **Write-time merge**             | ✅ cosine > 0.85       | ✅ (LLM dedupe)         | ✅ (summarize-merge)    | ❌                      | ❌                        | ❌                       |
| **Contradiction detection**      | ✅ ONNX NLI            | ✅ (LLM-flagged)        | ✅                       | ❌                      | ❌                        | ❌                       |
| **PII scrubber at write**        | ✅ regex sweep         | partial (cloud-side)    | DIY                      | ❌                      | ❌                        | ❌                       |
| **Hooks / lifecycle**            | ✅ session-end / pre-compact / etc. | ❌            | ❌                       | ❌                      | ❌                        | ❌                       |
| **Offline-only mode**            | ✅                      | ❌ (cloud required)     | ✅ (self-host)          | ❌                      | ❌                        | ❌                       |
| **Cost**                         | $0 (your disk)         | per-month + per-call    | self-host infra cost    | bundled in Cursor      | bundled in Claude        | bundled in ChatGPT      |
| **Vendor lock-in**               | None (MIT, local DB)   | API contract            | Lower (OSS core)        | High                    | High                     | High                    |

## Footnotes

- **Mem0** ([mem0.ai](https://mem0.ai), [GitHub](https://github.com/mem0ai/mem0))
  is the most-mature managed offering. It does write-time dedupe via LLM
  comparison, which is conceptually similar to B12's cosine merge but
  costs an LLM round-trip per write. B12 trades quality at the margin
  for $0 / 0ms / 0-deps.
- **Letta** (formerly MemGPT, [letta.com](https://letta.com)) operates
  at the conversation-state level — paging memory in and out of a fixed
  context window like a VM. B12 operates at the codebase level: it
  doesn't try to page state, it lets the host LLM consume retrieved
  memories normally.
- **Cursor memory** (rules / `.cursorrules`) is single-tool, single-user,
  cloud-stored. There's no public API to read or migrate it. B12's
  Cursor integration replaces that surface — same memories, but
  cross-tool and local.
- **Claude Projects** stores per-project context inside Anthropic's
  servers. It's excellent inside the Claude apps; useless from Codex
  CLI or Cursor or Cline. B12 exposes the same context to all of them
  via MCP.
- **ChatGPT memory** is opaque + heuristic. No retrieval guarantees,
  no dedupe, no PII scrubbing. Different problem space.

## Where B12 loses

- No cloud sync. If you want the same DB on two machines, you copy the
  SQLite file or run B12 against a shared filesystem (NFS / iCloud).
- No managed UI for browsing. The bundled Flask dashboard runs locally
  and is enough for the inspection path, but it's not a SaaS-quality
  product.
- Pattern catalog for PII / contradictions is regex / NLI based. A
  managed offering's LLM judge will catch more cases at higher cost.
- No multi-user collaboration model — B12 is intentionally single-user.

## Where B12 wins

- **Cross-tool**. The same memory store powers 13 platforms in 2026.
  Switching tools doesn't reset context.
- **$0 / no API key / no runtime network** (BGE-M3 model downloads from Hugging Face on first session, then runs fully offline). The honeypot risk of a managed
  memory service (cloud-side data breach, retention policy changes,
  vendor exit) is structurally absent.
- **Hooks and lifecycle automation in Claude Code**. Session-end micro-extraction,
  pre-compact transcript staging, working-memory restore, classifier
  tagging — none of the alternatives ship hook-level integration.
- **MIT license + local DB = no vendor lock-in.** You can read your
  memories in `sqlite3` from any language, any host.

## When NOT to use B12

- You need shared team memory → use Mem0 or a custom Postgres setup.
- You need an LLM-judge dedupe layer → Mem0 is the closest fit.
- You want a UI-first product, not a developer-config one → Cursor
  memory is fine if you only use Cursor.
- You're not on macOS / Linux / WSL — Windows native isn't tested.
