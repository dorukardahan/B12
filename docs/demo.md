# B12 demo — text walkthrough

This is the text version of [`assets/demo.gif`](../assets/demo.gif). Same
five beats, same fake output, no autoplay needed. The GIF is rendered
from [`assets/demo.tape`](../assets/demo.tape) (VHS script, ~50 LoC).
Re-render any time with:

```bash
vhs assets/demo.tape -o assets/demo.gif
```

---

## 1. Install (one command)

```bash
# 1. Install B12 (venv + MCP + hooks, one command)
git clone https://github.com/dorukardahan/B12.git && cd B12 && ./install.sh --full
```

`./install.sh --full` creates `~/.local/b12-venv`, installs the MCP server
into `~/.claude.json`, and deploys hook scripts to `~/.B12/hooks/`. No
sudo, no Docker, no cloud.

## 2. Restart Claude Code → `/mcp`

```text
# 2. Restart Claude Code, then type /mcp
/mcp
  ▸ B12 · connected ✓   (tools: memory_store / search / update / quality …)
```

The `B12 · connected` line is your green light. If the MCP server failed
to spawn, this line says `B12 · failed` instead — usually a missing
Python or a venv path issue. The installer prints both on first run.

## 3. Store a memory

```python
# 3. Store a memory — hash-deduped at the MCP tool, embedded async
memory_store(
  content='The api/auth.py module uses HS256 JWT with 15-min expiry.',
  metadata={'tags':['proj:webshop','area:auth'], 'type':'architecture'}
)
  ▸ Stored memory (hash: 7c3b9e2a4f81d20a, id: 4218)
```

The MCP tool signature is `memory_store(content, metadata=None)` —
tags, type, scope-like fields all live inside `metadata`. Dedup at this
layer is exact: a SHA-based `content_hash` lookup either reactivates the
existing row or inserts a new one. The cosine-based write-time merge
that collapses near-duplicates (cosine ≥ 0.85) runs at the hook layer
during session-end micro-extraction — see
[`scripts/write_time_merge.py`](../scripts/write_time_merge.py)
(`merge_or_insert`). Embedding generation hands off to the daemon and
indexes the new row asynchronously.

## 4. Search across sessions and tools

```python
# 4. Search — same DB powers Claude Code, Codex, Cursor, Cline, Zed, …
memory_search(query='auth jwt expiry', mode='hybrid', limit=3)
  1. api/auth.py uses HS256 JWT with 15-min expiry …   (0.94)
  2. refresh-token rotation lives in api/refresh.py …  (0.81)
  3. middleware/jwt.py decodes both HS256 and RS256 …  (0.77)
```

Unified scoring blends four dimensions with default weights `decay=0.25`,
`importance=0.25`, `relevance=0.40`, `strength=0.10`, plus a small
overlap bonus when the same memory hits both FTS and semantic search.
Frequently used memories rise; stale ones fade. The exact weights live
in [`scripts/b12_mcp_server.py`](../scripts/b12_mcp_server.py) (`_unified_score`)
and are overridable via `B12_WEIGHT_*` env vars.

## 5. Cross-tool note

```text
# One SQLite DB · one MCP server · 13 platforms · local-first.
# Memory you stored once shows up everywhere you code.
```

Same `~/.B12/memory.db` is read by Claude Code, Codex CLI, Cursor, Cline,
Zed, Continue, Gemini, Kimi, Windsurf, OpenCode, VS Code/Copilot, Amp,
and JetBrains AI. See [Supported Platforms](../README.md#supported-platforms)
for the per-tool config flag.

---

For the architecture diagram see [`docs/architecture.md`](architecture.md).
For benchmarks see the §Benchmarks section in [README.md](../README.md).
