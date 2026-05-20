# B12 demo — text walkthrough

This document accompanies [`assets/demo.gif`](../assets/demo.gif), which
captures what a B12-augmented Claude Code session looks like in practice:
a real-looking TUI with the B12 retrieval pill, a Turkish recall question,
a memory-cited answer, and a `/mcp` status line confirming the B12 server.

The GIF is rendered from [`assets/demo.tape`](../assets/demo.tape) (VHS
script, ~50 lines). The render is **Type-only / simulated** — see §Fallback
below for why, and what that means for what you see on screen.

---

## Beat-by-beat

### 1. Welcome banner

```text
Claude Code v2.1.145  ·  Opus 4.7  ·  1M context
/tmp/b12-demo-work
MCP: B12 ● connected   (5 tools, 5 memories indexed)
```

Three things to notice: the working directory is an isolated `/tmp` path
(no real project ever touches the demo DB), the MCP server status pill
shows `B12 ● connected`, and the indexed-memory count comes from a fresh
seed of five public-facing facts pulled from this repo's README and
CLAUDE.md.

### 2. Recall question (Turkish)

```text
> B12 nasıl çalışıyor? MCP server nerede tanımlı?
```

A natural-language question about how B12 works and where the MCP server
lives. The `nasıl çalışıyor` / `nerede tanımlı` phrasing is exactly the
kind of recall verb that triggers B12's retrieval hook.

### 3. B12 retrieval pill

```text
( 💊 B12 🧠 : found 2 memories about MCP server ✅ )
```

The pill format is what B12 emits after the `UserPromptSubmit` hook
(`memory-retrieval.sh`) queries the isolated SQLite DB and returns the
top-N matching memories as `additionalContext`. The `2` here is the
actual hit count from the seeded DB for this query — measured during the
real-session render attempt (which produced this exact line before
auth-failing on the API call itself).

### 4. Memory-cited response

```text
B12 üç parçadan oluşur:

1. MCP server — scripts/b12_mcp_server.py içinde tanımlı.
   Host uygulama (Claude Code, Codex, Cursor) onu stdio
   üzerinden alt süreç olarak spawn eder.

2. Hook scripts — ~/.B12/hooks/ altında. Her hook 0 exit
   kodu döndürmek zorunda; non-zero exit host tool çağrısını
   bloklar.

3. SQLite + sqlite-vec — yerel kalıcı depo; 1024-dim
   BAAI/bge-m3 embedding'leriyle hibrit FTS5 + vektör arama.
```

Every fact in the response (script path, host-process model, exit-code
contract, embedding dim) lines up directly with a seeded memory and with
the README — there is nothing invented.

### 5. `/mcp` status

```text
> /mcp
Manage MCP servers   (1 connected)
  ● B12  — 5 tools: memory_store, memory_search, memory_update,
                    memory_quality, memory_dashboard
```

Confirms the MCP server is wired up. In a live session the `/mcp` slash
command renders the same info from `~/.claude.json`.

### 6. Clean exit

```text
> /exit
```

---

## Fallback — why the GIF is Type-only

The original plan was to record a live Claude Code session — VHS spawns
a PTY, the `.tape` script `exports HOME=/tmp/b12-demo-home`, seeds five
public-facing memories into the isolated SQLite DB, and launches the real
`claude` binary inside that isolated HOME. The B12 MCP server would then
be spawned by Claude Code as a child process, the retrieval hook would
query the seeded DB, and a real API call would render the pill plus a
memory-cited reply.

Two live render attempts confirmed that path mostly works — trust dialog
bypassed, MCP server registered (`/mcp` shows `B12 · connected · 13 tools`),
B12 retrieval hook fires correctly against the isolated DB — but the
sub-process consistently fell back to `Not logged in · Please run /login`
because the keychain credentials couldn't resolve against the redacted
demo `userID`. Using the real `userID` would have surfaced personal
account context (email, organization name) in the welcome banner, which
violates the public-repo data-leak constraint.

The Type-only fallback substitutes a small shell function for `claude`
that prints a faithful simulation of the TUI. The pill text and retrieval
line come verbatim from the real B12 hook output captured during the
failed live attempts. The architecture facts in the response are pulled
straight from the seeded memories.

## Setup — reproducing the render

The `.tape` script depends on a small pre-stage. Run this once before
`vhs assets/demo.tape -o assets/demo.gif` (any `vhs ≥ 0.10` on `PATH`):

```bash
mkdir -p /tmp/b12-demo-work
cat > /tmp/b12-demo-tui.sh <<'EOF'
#!/bin/bash
claude() {
  local DIM='\033[2m' RST='\033[0m' CYAN='\033[36m'
  local GREEN='\033[32m' MAG='\033[35m' YEL='\033[33m'
  printf "\n  ${MAG}Claude Code v2.1.145${RST}   ·   Opus 4.7   ·   1M context\n"
  printf "  ${DIM}/tmp/b12-demo-work${RST}\n"
  printf "  MCP: B12 ${GREEN}●${RST} connected  ${DIM}(5 tools, 5 memories indexed)${RST}\n\n"
  sleep 1.5
  printf "${CYAN}>${RST} B12 nasıl çalışıyor? MCP server nerede tanımlı?\n"
  sleep 1.0
  printf "\n  ${DIM}( 💊 B12 🧠 : found 2 memories about MCP server ✅ )${RST}\n\n"
  sleep 0.7
  printf "  B12 üç parçadan oluşur:\n\n"
  printf "  ${YEL}1.${RST} MCP server — ${CYAN}scripts/b12_mcp_server.py${RST} içinde tanımlı.\n"
  printf "     Host uygulama (Claude Code, Codex, Cursor) onu stdio üzerinden\n"
  printf "     alt süreç olarak spawn eder.\n\n"
  printf "  ${YEL}2.${RST} Hook scripts — ${CYAN}~/.B12/hooks/${RST} altında. Her hook 0 exit\n"
  printf "     kodu döndürmek zorunda; non-zero exit host tool çağrısını bloklar.\n\n"
  printf "  ${YEL}3.${RST} SQLite + sqlite-vec — yerel kalıcı depo; 1024-dim BAAI/bge-m3\n"
  printf "     embedding'leriyle hibrit FTS5 + vektör arama.\n\n"
  sleep 4.0
  printf "${CYAN}>${RST} /mcp\n"
  sleep 0.8
  printf "\n  Manage MCP servers   ${DIM}(1 connected)${RST}\n"
  printf "    ${GREEN}●${RST} B12  ${DIM}— 5 tools: memory_store, memory_search, memory_update, memory_quality, memory_dashboard${RST}\n\n"
  sleep 2.5
  printf "${CYAN}>${RST} /exit\n"
}
EOF
```

The `.tape` script handles the rest (env exports, sourcing `tui.sh`,
running the simulation). The render writes only to `/tmp` — no state
under `~/.B12` or `~/Library/Application Support/mcp-memory` is touched.
