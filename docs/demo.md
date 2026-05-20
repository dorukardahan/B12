# B12 demo — text walkthrough

This document accompanies [`assets/demo.gif`](../assets/demo.gif), a
high-fidelity simulation of a B12-augmented Claude Code session rendered
by **React + Ink** — the same framework Claude Code itself uses. The
banner, spinner, `● Memory(…)` tool call, `⎿` tree output, retrieval
pill, response formatting, and `/mcp` slash-command layout match Claude
Code v2.1.x closely enough to read like a real screencast.

The retrieval pill count is **live**: the Ink app's `liveCount()` shells
out to `sqlite3` against an isolated demo DB at
`/tmp/b12-demo-home/Library/Application Support/mcp-memory/sqlite_vec.db`
(seeded with five public-facing facts from this repo's README and
CLAUDE.md), so the number in the pill reflects actual seeded content.

The GIF is rendered from [`assets/demo.tape`](../assets/demo.tape) (VHS
script, ~50 lines). Re-render with `vhs assets/demo.tape -o assets/demo.gif`
after running §Setup below.

---

## Beat-by-beat

### 1. Banner

```text
✻  ╭───╮    Claude Code v2.1.145
✻ ╭╯███╰╮   Opus 4.7 · 1M context · API Usage Billing
✻ │ ◠ ◠ │   /tmp/b12-demo-work
✻ ╰─────╯   MCP: ● B12 connected · 5 tools · 5 memories indexed
```

The cwd is an isolated `/tmp` path. The `●` is bright green, signaling
the B12 MCP server is up. The `5 memories indexed` count is the row
count of the seeded demo DB.

### 2. Turkish recall question

```text
> B12 nasıl çalışıyor? MCP server nerede tanımlı?
```

Auto-typed character-by-character with a blinking cursor (the Ink app
ships its own typing animation; VHS just records the terminal output).

### 3. Memory tool call

```text
● Memory(memory_search(query="B12 MCP server", mode="hybrid"))
  ⎿ found 2 matches in /tmp/b12-demo-work
```

Same render as a real Claude Code tool call: `●` in blue, args in
grey, output line in green under a `⎿` tree branch.

### 4. B12 retrieval pill

```text
( 💊 B12 🧠 : found 1 memories about MCP server ✅ )
```

The count comes from a live `sqlite3` query against the seeded demo
DB. Re-seed with different memories and the number changes.

### 5. Memory-cited response

```text
B12 üç parçadan oluşur:

1. MCP server — scripts/b12_mcp_server.py içinde tanımlı. Host
   uygulama (Claude Code, Codex, Cursor) onu stdio üzerinden
   alt süreç olarak spawn eder.

2. Hook scripts — ~/.B12/hooks/ altında. Her hook 0 exit kodu
   döndürmek zorunda; non-zero exit host tool çağrısını bloklar.

3. SQLite + sqlite-vec — yerel kalıcı depo; 1024-dim BAAI/bge-m3
   embedding'leriyle hibrit FTS5 + vektör arama.
```

Every fact matches a seeded memory and the public README.

### 6. `/mcp` status

```text
> /mcp

Manage MCP servers   (1 connected)
  ● B12   5 tools: memory_store, memory_search, memory_update,
                   memory_quality, memory_dashboard
```

### 7. Clean exit

```text
> /exit
```

---

## Live-session blocker

The original plan was to record a fully-live Claude Code session against
an isolated `/tmp/b12-demo-home` HOME. Multiple render attempts confirmed
two hard blockers:

1. **Sub-process auth never resolves in an isolated HOME.** `claude auth
   status` with `HOME=/tmp/b12-demo-home` returns
   `{"loggedIn": false, "authMethod": "none"}` even after copying the
   full real `oauthAccount` block (real `userID` + `accountUuid` +
   `organizationUuid` + display fields) into the demo `.claude.json`.
   The keychain entries (`Claude Code-credentials-<hash>`) appear to
   bind to process-level state that doesn't carry through a HOME
   redirect.
2. **Without the HOME redirect, the maintainer's real B12 DB is read.**
   `b12_resolve_db_path` and `b12_mcp_server.py:36-44` both derive the
   DB path via `os.path.expanduser("~")` → real
   `~/Library/Application Support/mcp-memory/sqlite_vec.db`. Real
   personal memories would surface in the pill.

The Ink simulation is the cleanest middle path: real B12 retrieval
pipeline against clean seeded data, real Claude Code chrome (banner,
spinner, tool-call rendering — all via the same Ink framework Claude
Code uses), zero personal account context, zero real-data leakage.

---

## Setup — reproducing the render

```bash
# 1) Isolated demo HOME with seeded DB (path matches b12_mcp_server.py:36-44
#    branching, so the Ink app's liveCount() reads from the same file)
DEMO_HOME=/tmp/b12-demo-home
case "$(uname -s)" in
  Darwin) DB="$DEMO_HOME/Library/Application Support/mcp-memory/sqlite_vec.db" ;;
  *)      DB="$DEMO_HOME/.local/share/mcp-memory/sqlite_vec.db" ;;
esac
mkdir -p "$(dirname "$DB")" /tmp/b12-demo-work /tmp/b12-demo-record
rm -f "$DB" "$DB-wal" "$DB-shm"   # idempotent: each render starts from a clean DB

sqlite3 "$DB" <<'SQL'
CREATE TABLE memories (id INTEGER PRIMARY KEY, content TEXT, content_hash TEXT UNIQUE,
                       memory_type TEXT DEFAULT 'general', deleted_at REAL DEFAULT NULL);
INSERT INTO memories (content, content_hash, memory_type) VALUES
 ('B12 stores memories in a local SQLite database with sqlite-vec.', 'h1', 'architecture'),
 ('The B12 MCP server is defined in scripts/b12_mcp_server.py — host applications spawn it as a child process.', 'h2', 'architecture'),
 ('B12 hook scripts must exit 0; non-zero blocks the host tool call.', 'h3', 'convention'),
 ('Two FTS5 tables coexist: memory_fts unicode61 + memory_content_fts trigram.', 'h4', 'architecture'),
 ('B12_DATA_DIR controls state/log paths; B12_HOOK_DIR controls hook code.', 'h5', 'architecture');
SQL

# 2) Ink-based demo app — full source committed at assets/demo-app.js
mkdir -p /tmp/b12-demo-app
echo '{"name":"b12-demo","type":"module","main":"index.js"}' > /tmp/b12-demo-app/package.json
cp "$(git rev-parse --show-toplevel)/assets/demo-app.js" /tmp/b12-demo-app/index.js
( cd /tmp/b12-demo-app && npm install --silent ink ink-spinner react )

# 3) Render
cd "$(git rev-parse --show-toplevel)"
vhs assets/demo.tape -o assets/demo.gif
```

The `.tape` script aliases `claude` to `node /tmp/b12-demo-app/index.js`,
so when the recording shows `> claude` and presses Enter, the Ink app
launches and renders the simulated session. All writes during the render
stay in `/tmp` — `~/.B12` and `~/Library/Application Support/mcp-memory`
are not touched.
