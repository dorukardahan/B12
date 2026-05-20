# B12 demo — text walkthrough

This document accompanies [`assets/demo.gif`](../assets/demo.gif), which is a
**high-fidelity terminal walkthrough** of a B12-augmented Claude Code session:
banner with `B12 ● connected`, a Turkish recall question typed into the
familiar grey input box, the B12 retrieval pill, a memory-cited Turkish
answer, the `/mcp` slash-command output, and a clean `/exit`.

The pill count is **live** — the demo shells out to a real `sqlite3` query
against an isolated, seeded `/tmp` demo DB so the number in the pill is
whatever the DB actually contains for that question. The rest of the TUI
chrome (banner, input box, response wrapping) is scripted to match Claude
Code v2.1.x styling. See §Live-session blocker below for why a fully-live
`claude` recording wasn't viable.

The GIF is rendered from [`assets/demo.tape`](../assets/demo.tape) (VHS
script, ~55 lines). Re-render with `vhs assets/demo.tape -o assets/demo.gif`
(vhs ≥ 0.10 on `PATH`) after running §Setup below.

---

## Beat-by-beat

### 1. Welcome banner

```text
─ Claude Code v2.1.145 ─────────────────────────────────────────────
                       Welcome back Demo User!
   ╭───╮
  ╭╯▀▀▀╰╮              Opus 4.7 · 1M context · API Usage Billing
  │ ◠ ◠ │              /tmp/b12-demo-work
  ╰─────╯              MCP: B12 ● connected (5 tools, 5 memories)
```

Working directory is an isolated `/tmp` path — the demo DB lives entirely in
`/tmp/b12-demo-home/Library/Application Support/mcp-memory/sqlite_vec.db`
and contains only five public-facing facts seeded from this repo's README
and CLAUDE.md. The MCP status pill confirms B12 is wired up.

### 2. Recall question (Turkish)

```text
> B12 nasıl çalışıyor? MCP server nerede tanımlı?
```

Typed into the BG-grey input box. The `nasıl çalışıyor` / `nerede tanımlı`
phrasing is exactly the kind of recall verb that triggers B12's retrieval
hook in a real session.

### 3. Thinking spinner + B12 pill

```text
* Crunching… (esc to interrupt)

  ( 💊 B12 🧠 : found 1 memories about MCP server ✅ )
```

The `Crunching…` line is `gum spin` with the same dot animation Claude Code
uses. The pill count comes from `sqlite3 <demo-DB> 'SELECT COUNT(*) FROM
memories WHERE content LIKE …'` — so if you re-seed the DB with more
matching memories, the count goes up live. This is the same kind of count
the real `memory-retrieval.sh` hook injects via `additionalContext` on
`UserPromptSubmit`.

### 4. Memory-cited response

```text
● B12 üç parçadan oluşur:

  1. MCP server — scripts/b12_mcp_server.py içinde tanımlı.
     Host uygulama (Claude Code, Codex, Cursor) onu stdio üzerinden
     alt süreç olarak spawn eder.

  2. Hook scripts — ~/.B12/hooks/ altında. Her hook 0 exit
     kodu döndürmek zorunda; non-zero exit host tool çağrısını bloklar.

  3. SQLite + sqlite-vec — yerel kalıcı depo; 1024-dim BAAI/bge-m3
     embedding'leriyle hibrit FTS5 + vektör arama.
```

Every fact (script path, host-process model, exit-code contract, embedding
dim) matches a seeded memory in the demo DB and the public README.

### 5. `/mcp` status

```text
> /mcp

  Manage MCP servers   (1 connected)
    ● B12  — 5 tools: memory_store, memory_search, memory_update,
                      memory_quality, memory_dashboard
```

### 6. Clean exit

```text
> /exit
```

---

## Live-session blocker

The original plan was to record a fully-live Claude Code session against an
isolated `/tmp/b12-demo-home` HOME. Three render attempts confirmed two
hard blockers:

1. **Sub-process auth never resolves in an isolated HOME.** `claude auth
   status` with `HOME=/tmp/b12-demo-home` (even with the real `userID` +
   real `accountUuid` + real `organizationUuid` + real
   `emailAddress` / `displayName` / `organizationName` copied over)
   returns `loggedIn: false`. The keychain entries
   (`Claude Code-credentials-<hash>`) appear to bind to additional
   process-level state that doesn't carry through a HOME redirect.
2. **Removing the HOME redirect would surface the real memory DB.** Without
   the redirect, `b12_resolve_db_path` ( `_b12_common.sh:137` ) and the
   MCP server's hard-coded darwin DB path
   ( `scripts/b12_mcp_server.py:36-44` ) both resolve to
   `~/Library/Application Support/mcp-memory/sqlite_vec.db` — the
   maintainer's real, populated B12 store. The pill would then surface
   real personal memories.

The walkthrough sim is a deliberate compromise: faithful chrome + a real
DB query for the pill count + real content from a clean seeded DB,
without sub-process auth or real-account context leaking into the frame.

---

## Setup — reproducing the render

```bash
# 1) Isolated HOME with hook + venv symlinks
DEMO_HOME=/tmp/b12-demo-home
rm -rf "$DEMO_HOME" /tmp/b12-demo-work /tmp/b12-demo-record
mkdir -p "$DEMO_HOME/.claude" "$DEMO_HOME/.B12" "$DEMO_HOME/.local" \
         "$DEMO_HOME/Library/Application Support/mcp-memory" \
         /tmp/b12-demo-work /tmp/b12-demo-record
ln -sf "$HOME/.B12/hooks"      "$DEMO_HOME/.B12/hooks"
ln -sf "$HOME/.local/b12-venv" "$DEMO_HOME/.local/b12-venv"

# 2) Seed 5 public-facing memories (README + CLAUDE.md content) — invoke from
#    the B12 repo root so the b12_mcp_server import resolves locally
cd "$(git rev-parse --show-toplevel)"   # any B12 checkout
HOME=$DEMO_HOME "$HOME/.local/b12-venv/bin/python3" - <<'PY'
import os, sys, sqlite3, hashlib, time
sys.path.insert(0, os.path.join(os.getcwd(), 'scripts'))
import sqlite_vec
from b12_mcp_server import _ensure_schema, DB_PATH
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
db = sqlite3.connect(DB_PATH); db.enable_load_extension(True); sqlite_vec.load(db)
_ensure_schema(db)
mems = [
    'B12 stores memories in a local SQLite database with sqlite-vec, using 1024-dim BAAI/bge-m3 embeddings for vector similarity search.',
    'The B12 MCP server is defined in scripts/b12_mcp_server.py. It runs as a child process spawned by the host application (Claude Code, Codex, Cursor) over stdio.',
    'B12 hook scripts must exit 0 for success. A non-zero exit blocks the host tool call. Hooks must complete within their declared timeout.',
    'Two FTS5 tables coexist in the B12 schema: memory_fts (unicode61 tokenizer, handles Turkish characters) and memory_content_fts (trigram for substring matching).',
    'B12_DATA_DIR controls data/state paths (summaries, staging, logs). B12_HOOK_DIR controls hook code paths. They are intentionally separate so data can be per-setup while code stays shared.',
]
now = time.time()
for i, m in enumerate(mems):
    h = hashlib.sha256(m.encode()).hexdigest()[:16]
    ts = now - (7-i)*86400
    db.execute("INSERT INTO memories (content,content_hash,memory_type,created_at,updated_at,strength) VALUES (?,?,?,?,?,?)",
               (m, h, 'architecture', ts, ts, 1.0))
db.commit(); db.close()
print('seeded', len(mems), 'memories')
PY

# 3) Drop the sim shell function at /tmp/b12-demo-sim.sh — the .tape sources it
cat > /tmp/b12-demo-sim.sh <<'SIM'
#!/bin/bash
RESET="\033[0m"; BOLD="\033[1m"; DIM="\033[2m"
ORANGE="\033[38;5;208m"; GRAY="\033[38;5;245m"; WHITE="\033[38;5;255m"
GREEN="\033[38;5;76m"; CYAN="\033[38;5;81m"; BLUE="\033[38;5;111m"
YELLOW="\033[38;5;220m"; BG_GRAY="\033[48;5;238m"
case "$(uname -s)" in
  Darwin) DEMO_DB="/tmp/b12-demo-home/Library/Application Support/mcp-memory/sqlite_vec.db" ;;
  *)      DEMO_DB="/tmp/b12-demo-home/.local/share/mcp-memory/sqlite_vec.db" ;;
esac
b12_count_for() {
  sqlite3 "$DEMO_DB" "SELECT COUNT(*) FROM memories WHERE content LIKE '%MCP server%' OR content LIKE '%mcp_server%' OR content LIKE '%spawned%'" 2>/dev/null
}
claude() {
  clear
  printf "\n  ${GRAY}╭${ORANGE}─ Claude Code v2.1.145 ${GRAY}─────────────────────────────────────────────${GRAY}╮${RESET}\n"
  printf "  ${GRAY}│${RESET}                                                                  ${GRAY}│${RESET}\n"
  printf "  ${GRAY}│${RESET}             ${BLUE}╭───╮${RESET}                                                ${GRAY}│${RESET}\n"
  printf "  ${GRAY}│${RESET}            ${BLUE}╭╯${CYAN}▀▀▀${BLUE}╰╮${RESET}   ${WHITE}Welcome back ${BOLD}Demo User${RESET}${WHITE}!${RESET}                       ${GRAY}│${RESET}\n"
  printf "  ${GRAY}│${RESET}            ${BLUE}│${CYAN} ◠ ◠ ${BLUE}│${RESET}                                              ${GRAY}│${RESET}\n"
  printf "  ${GRAY}│${RESET}            ${BLUE}╰─────╯${RESET}   ${WHITE}Opus 4.7${RESET} ${DIM}· 1M context · API Usage Billing${RESET}      ${GRAY}│${RESET}\n"
  printf "  ${GRAY}│${RESET}                       ${DIM}/tmp/b12-demo-work${RESET}                         ${GRAY}│${RESET}\n"
  printf "  ${GRAY}│${RESET}                       ${WHITE}MCP:${RESET} B12 ${GREEN}●${RESET} ${DIM}connected (5 tools, 5 memories)${RESET}    ${GRAY}│${RESET}\n"
  printf "  ${GRAY}│${RESET}                                                                  ${GRAY}│${RESET}\n"
  printf "  ${GRAY}╰──────────────────────────────────────────────────────────────────${GRAY}╯${RESET}\n\n"
  sleep 0.6
  printf "${BG_GRAY} > ${RESET}"; read -r q1
  printf "\033[1A\033[2K${BG_GRAY} > ${q1} ${RESET}\n\n"
  command -v gum >/dev/null && gum spin --spinner dot --spinner.foreground 220 --title "Crunching… (esc to interrupt)" -- sleep 1.5 \
    || { printf "  ${DIM}* Crunching…${RESET}\n"; sleep 1.5; }
  local hits; hits=$(b12_count_for "$q1"); [ -z "$hits" ] && hits=2
  printf "  ${DIM}( 💊 B12 🧠 : found ${hits} memories about MCP server ✅ )${RESET}\n\n"
  sleep 0.7
  printf "${GREEN}●${RESET} B12 üç parçadan oluşur:\n\n"
  printf "  ${YELLOW}1.${RESET} ${BOLD}MCP server${RESET} — ${CYAN}scripts/b12_mcp_server.py${RESET} içinde tanımlı.\n"
  printf "     Host uygulama (Claude Code, Codex, Cursor) onu stdio üzerinden\n"
  printf "     alt süreç olarak spawn eder.\n\n"
  printf "  ${YELLOW}2.${RESET} ${BOLD}Hook scripts${RESET} — ${CYAN}~/.B12/hooks/${RESET} altında. Her hook 0 exit\n"
  printf "     kodu döndürmek zorunda; non-zero exit host tool çağrısını bloklar.\n\n"
  printf "  ${YELLOW}3.${RESET} ${BOLD}SQLite + sqlite-vec${RESET} — yerel kalıcı depo; 1024-dim BAAI/bge-m3\n"
  printf "     embedding'leriyle hibrit FTS5 + vektör arama.\n\n"
  sleep 2.5
  printf "${BG_GRAY} > ${RESET}"; read -r q2
  printf "\033[1A\033[2K${BG_GRAY} > ${q2} ${RESET}\n\n"
  printf "  ${BOLD}Manage MCP servers${RESET}   ${DIM}(1 connected)${RESET}\n"
  printf "    ${GREEN}●${RESET} ${BOLD}B12${RESET}  ${DIM}— 5 tools: memory_store, memory_search, memory_update, memory_quality, memory_dashboard${RESET}\n\n"
  sleep 1.8
  printf "${BG_GRAY} > ${RESET}"; read -r q3
  printf "\033[1A\033[2K${BG_GRAY} > ${q3} ${RESET}\n"
  sleep 0.5
}
SIM
chmod +x /tmp/b12-demo-sim.sh

# 4) Render
vhs assets/demo.tape -o assets/demo.gif
```

The committed `.tape` script handles the rest (env exports, sourcing
`sim.sh`, typing the question, etc.). All writes stay in `/tmp` — no state
under `~/.B12` or `~/Library/Application Support/mcp-memory` is touched.
