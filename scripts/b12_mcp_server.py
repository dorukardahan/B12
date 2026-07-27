#!/usr/bin/env python3
"""
B12 Mini MCP Server — minimal memory CRUD with daemon-delegated ML.

Replaces the 804MB mcp-memory-service with 4 tools, zero ML deps.
All embedding/search ops delegated to embed_daemon via Unix socket.
"""

import asyncio, base64, hashlib, json, os, socket, sqlite3, threading, time
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
try:
    import sqlite_vec
    _HAS_VEC = True
except ImportError:
    _HAS_VEC = False
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from mcp.server.fastmcp import FastMCP

# Consolidation engine (lazy import path — scripts/ is on sys.path)
try:
    from consolidation_engine import consolidate as _consolidate, ConsolidationResult
except ImportError:
    _consolidate = None
    ConsolidationResult = None

# Refinement module (lazy import path — scripts/ is on sys.path)
try:
    from memory_refine import refine_candidates as _refine_candidates
except ImportError:
    _refine_candidates = None

# ── Paths ────────────────────────────────────────────────────────
import sys as _sys
_home = os.path.expanduser("~")
if _sys.platform == "darwin":
    DB_PATH = os.path.join(_home, "Library", "Application Support",
                           "mcp-memory", "sqlite_vec.db")
elif _sys.platform == "win32":
    DB_PATH = os.path.join(_home, "AppData", "Local",
                           "mcp-memory", "sqlite_vec.db")
else:
    DB_PATH = os.path.join(_home, ".local", "share",
                           "mcp-memory", "sqlite_vec.db")

_UID = os.getuid() if hasattr(os, 'getuid') else os.getpid()
# Hardcode /tmp/ — macOS TMPDIR varies per session, causing socket mismatch
SOCK_PATH = f"/tmp/b12-embed-{_UID}.sock"

# Optional pre-warmed daemon socket. When present and reachable, b12_mcp_server
# runs as a thin stdio proxy that forwards the MCP wire protocol to the shared
# daemon process (see scripts/b12_mcp_daemon.py). When unreachable, the server
# falls back to legacy in-process stdio mode below — non-Claude-Code consumers
# (Codex, Gemini, Kimi, OpenCode, Grok) see zero behaviour change either way.
MCP_DAEMON_SOCK = os.environ.get("B12_MCP_DAEMON_SOCK", f"/tmp/b12-mcp-{_UID}.sock")
B12_VERSION = "v11.81.5"

# Fix C — resilient proxy reconnect. When the daemon-side socket closes while the
# host CLI's stdin is still open (daemon restart by launchd/RSS-guard, redeploy,
# MAX_CONNECTIONS eviction, or a crash), the proxy re-dials the daemon and replays
# the cached MCP `initialize` + `notifications/initialized` so the host never sees
# the break. Set B12_MCP_PROXY_RECONNECT=0 to disable (exit-on-EOF legacy behavior).
_PROXY_RECONNECT = os.environ.get("B12_MCP_PROXY_RECONNECT", "1") != "0"
try:
    _RECONNECT_BUDGET_S = max(0.0, float(os.environ.get("B12_MCP_RECONNECT_BUDGET", "30")))
except (TypeError, ValueError):
    _RECONNECT_BUDGET_S = 30.0

# ── SQLite access (BB1: per-connection, off the event loop) ──────
# Reads run concurrently on a thread pool — WAL allows many concurrent readers —
# each on a connection OWNED by its worker thread. ALL writes + SELECT→write
# transactional ops run through a SINGLE serialized writer thread (one connection
# pinned to it, BEGIN IMMEDIATE per op). This replaces the former
# `async with _db_lock: <sync _db.execute>` pattern, which serialized every CLI
# tab on one asyncio lock AND blocked the shared event loop on synchronous sqlite
# (a contended write could stall every tab up to busy_timeout=30s).
#
# Invariants (the BB1 landmines):
#   • No connection is EVER shared across threads (no check_same_thread crash).
#   • The unit of offload is a WHOLE transactional op on ONE conn / ONE thread
#     (SELECT→conditional INSERT/UPDATE→commit stays atomic — never split).
#   • Exactly one writer thread ⇒ writes are serialized with no asyncio lock.
# Mirrors the ownership model of b12_mcp_daemon.py:_checkpoint_wal_blocking.
_DB_READY = False
_db_init_lock = asyncio.Lock()           # guards one-time pool/schema init
_read_pool: "ThreadPoolExecutor | None" = None
_writer_pool: "ThreadPoolExecutor | None" = None
_tls = threading.local()                 # per-thread sqlite connection cache
# Floor of 4 read workers so "a slow read never blocks other reads" holds even on
# small (1-2 core) CI boxes; env-overridable. WAL readers are cheap.
READ_POOL_SIZE = (int(os.environ.get("B12_MCP_READ_POOL", "0"))
                  or max(4, min(8, (os.cpu_count() or 4))))
# Serializes embed-daemon socket round-trips. The daemon accepts ONE connection
# at a time (embed_daemon.py main loop), so without this, concurrent sessions
# would each open a socket at once and a fast op could hit its 5s client timeout
# waiting in the listen backlog behind a slow >10s encode_batch. See R&D SPD-1.
_daemon_lock = asyncio.Lock()

# ── Session tracker (MCP-only platform support) ─────────────────
# Tracks tool calls during a session so we can generate a summary
# on shutdown. Solves the #13 gap: MCP-only platforms have no
# SessionEnd hook, so without this, session context is lost.
def _new_session_tracker() -> dict:
    return {
        "search_queries": [],     # recent search queries (topic signals)
        "stored_count": 0,        # memories explicitly stored this session
        "tool_calls": 0,          # total MCP tool invocations
        "start_time": time.time(),  # session start timestamp
        "project": None,          # detected project name
    }


_session_tracker = _new_session_tracker()
_session_tracker_var: ContextVar[dict | None] = ContextVar("b12_session_tracker", default=None)


def _current_session_tracker() -> dict:
    return _session_tracker_var.get() or _session_tracker


def _reset_session_tracker(tracker: dict | None = None) -> None:
    target = tracker or _current_session_tracker()
    target.clear()
    target.update(_new_session_tracker())


def _ensure_fts_sync_triggers(db, table, cols, names):
    """Install/repair the soft-delete-aware sync triggers for external-content FTS5
    `table`.

    `cols` is the ordered FTS column tuple (mapped content→new.content,
    tags→COALESCE(new.tags, '')); `names` maps insert/delete/update/softdel/restore to
    trigger names. insert/delete are idempotent (`CREATE TRIGGER IF NOT EXISTS`).

    The UPDATE logic is SPLIT by deleted_at transition into three guarded triggers —
    active→active (re-index), active→deleted (remove), deleted→active (re-insert). The
    restore case must INSERT ONLY: issuing an FTS5 'delete' for a row already absent
    from an external-content index corrupts it ('database disk image is malformed').
    An older B12 build shipped a single `update` trigger guarded only on
    `new.deleted_at IS NULL`, which DID issue that fatal delete on restore. We recreate
    the trio whenever the corrected update guard (`old.deleted_at IS NULL`) or the
    restore trigger is missing, so such DBs self-heal on the next start. (Audit #20 +
    Codex review.)
    """
    def _src(prefix):
        return ", ".join(
            f"COALESCE({prefix}.tags, '')" if c == "tags" else f"{prefix}.{c}"
            for c in cols
        )
    col_csv = ", ".join(cols)
    ins, dele = names["insert"], names["delete"]
    upd, sdl, res = names["update"], names["softdel"], names["restore"]
    db.execute(f"""
        CREATE TRIGGER IF NOT EXISTS {ins} AFTER INSERT ON memories
        WHEN new.deleted_at IS NULL BEGIN
            INSERT INTO {table}(rowid, {col_csv}) VALUES (new.id, {_src('new')});
        END
    """)
    db.execute(f"""
        CREATE TRIGGER IF NOT EXISTS {dele} AFTER DELETE ON memories BEGIN
            INSERT INTO {table}({table}, rowid, {col_csv})
            VALUES('delete', old.id, {_src('old')});
        END
    """)
    _have = {r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger'").fetchall()}
    _u = db.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (upd,)
    ).fetchone()
    if res not in _have or not _u or "old.deleted_at IS NULL" not in (_u[0] or ""):
        for _t in (upd, sdl, res):
            db.execute(f"DROP TRIGGER IF EXISTS {_t}")
        db.execute(f"""
            CREATE TRIGGER {upd} AFTER UPDATE ON memories
            WHEN new.deleted_at IS NULL AND old.deleted_at IS NULL BEGIN
                INSERT INTO {table}({table}, rowid, {col_csv})
                VALUES('delete', old.id, {_src('old')});
                INSERT INTO {table}(rowid, {col_csv}) VALUES (new.id, {_src('new')});
            END
        """)
        db.execute(f"""
            CREATE TRIGGER {sdl} AFTER UPDATE ON memories
            WHEN new.deleted_at IS NOT NULL AND old.deleted_at IS NULL BEGIN
                INSERT INTO {table}({table}, rowid, {col_csv})
                VALUES('delete', old.id, {_src('old')});
            END
        """)
        db.execute(f"""
            CREATE TRIGGER {res} AFTER UPDATE ON memories
            WHEN new.deleted_at IS NULL AND old.deleted_at IS NOT NULL BEGIN
                INSERT INTO {table}(rowid, {col_csv}) VALUES (new.id, {_src('new')});
            END
        """)


def _ensure_schema(db: sqlite3.Connection) -> None:
    """Create all required tables if they don't exist. Safe on existing DBs."""
    db.execute("""
        CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT NOT NULL,
            content_hash TEXT UNIQUE,
            memory_type TEXT DEFAULT 'general',
            tags TEXT DEFAULT '',
            metadata TEXT DEFAULT '{}',
            created_at REAL,
            updated_at REAL,
            created_at_iso TEXT,
            updated_at_iso TEXT,
            deleted_at REAL DEFAULT NULL,
            strength REAL DEFAULT 1.0,
            last_accessed_at REAL DEFAULT NULL,
            valid_until TEXT DEFAULT NULL,
            difficulty REAL DEFAULT 5.0,
            due_date TEXT DEFAULT NULL
        )
    """)
    # Migrate existing tables: add FSRS columns if missing
    existing_cols = {r[1] for r in db.execute("PRAGMA table_info(memories)")}
    if "difficulty" not in existing_cols:
        db.execute("ALTER TABLE memories ADD COLUMN difficulty REAL DEFAULT 5.0")
    if "due_date" not in existing_cols:
        db.execute("ALTER TABLE memories ADD COLUMN due_date TEXT")
    # B12 FTS5 table (unicode61 tokenizer, used by hooks)
    # Includes tags column to match existing DB schema from mcp-memory-service
    db.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
            content,
            tags,
            content='memories',
            content_rowid='id',
            tokenize='unicode61'
        )
    """)
    # FTS5 sync triggers for memory_fts.
    # B12's memory_fts_* set (soft-delete aware) OWNS memory_fts. The legacy upstream
    # mcp-memory-service set (fts_insert/update/softdel/hardel) targets the SAME table
    # but is NOT soft-delete aware, so when both exist every write fires twice (audit
    # #20): redundant write work, and the guard-less legacy fts_insert indexes rows that
    # are soft-deleted at insert time (the search JOIN filters them by deleted_at, so
    # results stay correct, but the index carries entries B12's WHEN-clause deliberately
    # excludes). The former guard skipped creating B12's set whenever ANY of those names
    # existed — exactly how a DB ended up carrying BOTH across upgrades.
    #
    # Fix: (a) ensure the FULL B12 set unconditionally (every statement is CREATE
    # TRIGGER IF NOT EXISTS, so this is idempotent AND heals a DB that somehow has only
    # a partial B12 set), then (b) drop the legacy set, so a dual-trigger DB self-heals
    # on the next start. `_trig_names` is captured BEFORE (a) so the drop loop still sees
    # the pre-existing legacy triggers.
    #
    # No FTS5 'rebuild' is issued: a bare rebuild re-indexes soft-deleted rows (undoing
    # the softdel trigger's invariant), and — verified empirically — FTS5 collapses the
    # duplicate-rowid inserts the two trigger sets produced, so there is no accumulated
    # term-frequency inflation to clean up; only the redundant writes, which stop here.
    # _ensure_fts_sync_triggers also SPLITS the update logic so restoring a soft-deleted
    # row never corrupts the index (see its docstring); `_trig_names` is captured BEFORE
    # it so the legacy-drop loop still sees the pre-existing legacy triggers.
    _trig_names = {r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger'").fetchall()}
    _ensure_fts_sync_triggers(db, "memory_fts", ("content", "tags"), {
        "insert": "memory_fts_insert", "delete": "memory_fts_delete",
        "update": "memory_fts_update", "softdel": "memory_fts_softdel",
        "restore": "memory_fts_restore"})
    for _legacy in ("fts_insert", "fts_update", "fts_softdel", "fts_hardel"):
        if _legacy in _trig_names:
            db.execute(f"DROP TRIGGER IF EXISTS {_legacy}")
    # B12 FTS5 stemmed table (porter unicode61 tokenizer, for morphological matching)
    # "running" matches "run", "configured" matches "config", etc.
    db.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts_stemmed USING fts5(
            content,
            tags,
            content='memories',
            content_rowid='id',
            tokenize='porter unicode61'
        )
    """)
    # FTS5 sync triggers for memory_fts_stemmed — same split-by-transition contract as
    # memory_fts (the former single-guard update corrupted this index on restore too).
    _ensure_fts_sync_triggers(db, "memory_fts_stemmed", ("content", "tags"), {
        "insert": "memory_fts_stemmed_insert", "delete": "memory_fts_stemmed_delete",
        "update": "memory_fts_stemmed_update", "softdel": "memory_fts_stemmed_softdel",
        "restore": "memory_fts_stemmed_restore"})

    # Native FTS5 table (trigram tokenizer, used by MCP server search)
    db.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS memory_content_fts USING fts5(
            content,
            content='memories',
            content_rowid='id',
            tokenize='trigram'
        )
    """)
    # Triggers for memory_content_fts (with soft-delete guard). FTS5
    # external-content tables require the special 'delete' command; plain
    # DELETE leaves stale terms searchable after updates/deletes.
    _content_trigger_rows = db.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='trigger' AND sql LIKE '%memory_content_fts%'"
    ).fetchall()
    for _trigger_name, _trigger_sql in _content_trigger_rows:
        if "delete from memory_content_fts" in ((_trigger_sql or "").lower()):
            db.execute(f"DROP TRIGGER IF EXISTS {_trigger_name}")
    # Single-column (content only) FTS; same split-by-transition contract — the former
    # memories_fts_au also corrupted this index when restoring a soft-deleted row.
    _ensure_fts_sync_triggers(db, "memory_content_fts", ("content",), {
        "insert": "memories_fts_ai", "delete": "memories_fts_ad",
        "update": "memories_fts_au", "softdel": "memories_fts_softdel",
        "restore": "memories_fts_ar"})

    # Memory graph (edges between memories)
    # Uses composite PK matching existing DB from mcp-memory-service.
    # One edge per (source, target) pair — INSERT OR REPLACE upgrades type.
    db.execute("""
        CREATE TABLE IF NOT EXISTS memory_graph (
            source_hash TEXT NOT NULL,
            target_hash TEXT NOT NULL,
            similarity REAL NOT NULL,
            connection_types TEXT NOT NULL DEFAULT '[]',
            metadata TEXT,
            created_at REAL NOT NULL,
            relationship_type TEXT DEFAULT 'related',
            PRIMARY KEY (source_hash, target_hash)
        )
    """)
    # Vector embeddings table (requires sqlite-vec)
    # Dim is derived from B12_EMBED_DIM env var (default 1024 for BGE-M3 since
    # v11.34 / P-FOUNDATION). Pre-BGE-M3 DBs at 384-dim keep working because
    # the migration script (scripts/migrate_embed_to_bge_m3.py) drops + recreates
    # this table with the new dim. We only CREATE if missing.
    if _HAS_VEC:
        _embed_dim = int(os.environ.get("B12_EMBED_DIM", "1024"))
        try:
            db.execute(f"""
                CREATE VIRTUAL TABLE IF NOT EXISTS memory_embeddings USING vec0(
                    content_embedding FLOAT[{_embed_dim}] distance_metric=cosine
                )
            """)
        except Exception:
            pass  # vec0 may already exist with different params

    # Metadata JSON validation trigger — last safety net against invalid JSON
    db.execute("""
        CREATE TRIGGER IF NOT EXISTS b12_metadata_json_check_insert
        BEFORE INSERT ON memories
        WHEN NEW.metadata IS NOT NULL AND NEW.metadata != '' AND NEW.metadata != '{}'
             AND json_valid(NEW.metadata) = 0
        BEGIN
            SELECT RAISE(ABORT, 'B12: metadata must be valid JSON');
        END
    """)
    db.execute("""
        CREATE TRIGGER IF NOT EXISTS b12_metadata_json_check_update
        BEFORE UPDATE OF metadata ON memories
        WHEN NEW.metadata IS NOT NULL AND NEW.metadata != '' AND NEW.metadata != '{}'
             AND json_valid(NEW.metadata) = 0
        BEGIN
            SELECT RAISE(ABORT, 'B12: metadata must be valid JSON');
        END
    """)
    db.commit()


# Set to True by b12_mcp_daemon.py before serving connections so the lifespan
# below stays idempotent: DB is opened once per daemon process and remains
# open across every client connection. In legacy in-process mode (any non-
# daemon consumer: Codex, Gemini, Kimi, OpenCode, Grok, OR Claude Code when
# the daemon is down) this stays False and the lifespan behaves as before —
# closing the DB on session end.
_DAEMON_MODE = False


def _configure_connection(conn: sqlite3.Connection) -> None:
    """Apply the shared B12 pragmas + extensions to a fresh connection. Mirrors
    the former single-`_db` setup and the daemon's checkpoint connection.

    P10 (owner-approved 2026-06-19): synchronous=NORMAL is the recommended
    durability mode for WAL — corruption-safe, and committed transactions survive
    any app/process crash (daemon restart, kill, terminal close). The only loss
    window is an OS-level crash or power loss, which can roll back the most-recent
    commit(s); the database is NOT corrupted. Accepted as a deliberate
    write-latency trade for the shared memory DB across all 9+ CLI runtimes.
    temp_store=MEMORY keeps temp b-trees / sort scratch in RAM (pure speed, no
    durability cost)."""
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA wal_autocheckpoint=100")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.row_factory = sqlite3.Row
    if _HAS_VEC:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)


def _get_read_conn() -> sqlite3.Connection:
    """Thread-local read connection (autocommit; _run_read wraps each op in an
    explicit BEGIN for a consistent snapshot). One per read-pool worker thread."""
    conn = getattr(_tls, "read_conn", None)
    if conn is None:
        conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
        _configure_connection(conn)
        _tls.read_conn = conn
    return conn


def _get_writer_conn() -> sqlite3.Connection:
    """The single writer connection — created on, and only ever touched by, the
    lone writer thread. isolation_level=None: _run_write[_raw] control txns."""
    conn = getattr(_tls, "writer_conn", None)
    if conn is None:
        conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
        _configure_connection(conn)
        _tls.writer_conn = conn
    return conn


def _run_read(fn):
    """Run a read op inside one deferred read transaction (consistent snapshot)."""
    conn = _get_read_conn()
    conn.execute("BEGIN")
    try:
        result = fn(conn)
        conn.execute("COMMIT")
        return result
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise


def _run_write(fn):
    """Run a write op inside one BEGIN IMMEDIATE transaction (atomic; the op must
    NOT commit itself and must NOT call external-connection writers — see
    _run_write_raw)."""
    conn = _get_writer_conn()
    conn.execute("BEGIN IMMEDIATE")
    try:
        result = fn(conn)
        conn.execute("COMMIT")
        return result
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise


def _run_write_raw(fn):
    """Run a write op WITHOUT an outer transaction: fn manages its own statements
    in autocommit. For ops that embed an external-connection writer mid-op
    (memory_delete hard → b12_gc.collect_one opens its OWN connection) — holding
    BEGIN IMMEDIATE on the writer conn here would self-deadlock against that conn.
    Still globally serialized: runs on the single writer thread."""
    return fn(_get_writer_conn())


async def _read(fn):
    """Dispatch a read op to the read pool (concurrent; off the event loop)."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_read_pool, _run_read, fn)


async def _write(fn):
    """Dispatch a write op to the single serialized writer (BEGIN IMMEDIATE)."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_writer_pool, _run_write, fn)


async def _write_raw(fn):
    """Dispatch an autocommit write op to the single serialized writer."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_writer_pool, _run_write_raw, fn)


def _writer_init() -> None:
    """Runs on the writer thread: create the writer conn + ensure the schema."""
    _ensure_schema(_get_writer_conn())


def _close_writer_conn() -> None:
    """Runs on the writer thread: close + drop the pinned writer connection."""
    conn = getattr(_tls, "writer_conn", None)
    if conn is not None:
        try:
            conn.close()
        except sqlite3.Error:
            pass
        _tls.writer_conn = None


def _close_read_conn(barrier=None) -> None:
    """Runs on a read-pool thread: close + drop THIS thread's read connection.
    A barrier (sized to the pool) is submitted once per worker so every distinct
    read thread runs this exactly once — closing each conn on its OWNER thread
    avoids a cross-thread GC close (which leaks FDs until gc + emits
    ResourceWarning). No-op on threads that never opened a read conn."""
    if barrier is not None:
        try:
            barrier.wait(timeout=10)
        except (threading.BrokenBarrierError, Exception):
            pass
    conn = getattr(_tls, "read_conn", None)
    if conn is not None:
        try:
            conn.close()
        except sqlite3.Error:
            pass
        _tls.read_conn = None


def _atexit_flush() -> None:
    """Flush the ACTIVE session tracker on a SHORT-LIVED own connection — no event
    loop / pool reliance. Resolves the tracker via _current_session_tracker() so it
    works from BOTH the daemon's RSS-guard task (the contextvar tracker is set by
    lifespan and inherited by tasks — audit #12) AND the real atexit hook after the
    loop is gone (contextvar unset → global, a no-op once lifespan already flushed).
    Best-effort; never raises."""
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5, isolation_level="IMMEDIATE")
        try:
            _flush_session_tracker(conn, _current_session_tracker())
        finally:
            conn.close()
    except Exception:
        pass


async def _init_db() -> None:
    """Idempotent one-time init: create the read pool + single-writer pool, build
    the writer connection and run _ensure_schema ONCE (on the writer thread), and
    register the atexit flush. Called by lifespan; reusable by tests."""
    global _DB_READY, _read_pool, _writer_pool
    async with _db_init_lock:
        if _DB_READY:
            return
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        _read_pool = ThreadPoolExecutor(
            max_workers=READ_POOL_SIZE, thread_name_prefix="b12-read"
        )
        _writer_pool = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="b12-write"
        )
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(_writer_pool, _writer_init)
        import atexit
        atexit.register(_atexit_flush)
        _DB_READY = True


async def _shutdown_db() -> None:
    """Legacy-mode (non-daemon) teardown: close every pinned connection on its
    OWNER thread, then shut both pools. The daemon never calls this — it keeps the
    pools warm across client connections (OS reclaims at process exit). Closing on
    the owner thread (vs. dropping the refs) avoids leaking FDs / ResourceWarnings
    when a long-lived process tears the DB layer down more than once."""
    global _DB_READY, _read_pool, _writer_pool
    if not _DB_READY:
        return
    wp, rp = _writer_pool, _read_pool
    _DB_READY = False
    _writer_pool = None
    _read_pool = None
    loop = asyncio.get_running_loop()
    if wp is not None:
        try:
            await loop.run_in_executor(wp, _close_writer_conn)
        except Exception:
            pass
        wp.shutdown(wait=True)
    if rp is not None:
        # Close each worker's thread-local read conn on its own thread. A barrier
        # sized to the pool forces all workers live simultaneously so each
        # distinct thread runs _close_read_conn exactly once.
        try:
            n = READ_POOL_SIZE
            barrier = threading.Barrier(n)
            await asyncio.gather(*[
                loop.run_in_executor(rp, _close_read_conn, barrier)
                for _ in range(n)
            ])
        except Exception:
            pass
        rp.shutdown(wait=True)


@asynccontextmanager
async def lifespan(server: FastMCP):
    await _init_db()
    tracker = _new_session_tracker()
    token = _session_tracker_var.set(tracker)
    try:
        yield
    finally:
        # Per-session MCP summary flush, on the writer thread (correct connection
        # affinity). _flush_session_tracker commits internally, so route it
        # through _write_raw (autocommit), NOT _write (which owns the txn).
        try:
            await _write_raw(lambda db: _flush_session_tracker(db, tracker))
        except Exception:
            pass
        _session_tracker_var.reset(token)
        # Teardown only in legacy mode — the daemon keeps the pools warm across
        # connections and lets the OS clean up at process exit.
        if not _DAEMON_MODE:
            await _shutdown_db()


def _flush_session_tracker(db: sqlite3.Connection | None, tracker: dict | None = None) -> None:
    """Store a session summary from tracked MCP tool calls.

    This is the ONLY way MCP-only platforms (Cursor, VS Code, Windsurf,
    Cline, Kimi, OpenCode) get automatic session-end memories.
    Claude Code and Gemini CLI have dedicated hooks and don't need this.
    """
    tracker = tracker or _current_session_tracker()
    if not db or tracker["tool_calls"] < 3:
        _reset_session_tracker(tracker)
        return  # Too few calls — not a real session

    try:
        parts = []

        # Topics searched (reveals what user was working on)
        if tracker["search_queries"]:
            unique = list(dict.fromkeys(tracker["search_queries"][-10:]))
            parts.append(f"Searched: {', '.join(unique[:5])}")

        if tracker["stored_count"] > 0:
            parts.append(f"Stored {tracker['stored_count']} memories")

        parts.append(f"Tool calls: {tracker['tool_calls']}")

        # Duration
        if tracker["start_time"]:
            dur_min = (time.time() - tracker["start_time"]) / 60
            if dur_min >= 1:
                parts.append(f"Duration: {dur_min:.0f}min")

        content = f"[Progress] {' • '.join(parts)}"
        project = tracker["project"] or "unknown"
        tags = f"proj:{project},type:session_summary,source:mcp,platform:mcp-only"
        meta_dict = {
            "type": "session_summary",
            "importance_score": 0.6,
            "project": project,
            "source": "mcp_session_tracker",
            "tool_calls": tracker["tool_calls"],
        }

        now_ts = int(time.time())
        ch = hashlib.sha256(content.strip().lower().encode()).hexdigest()

        db.execute(
            """INSERT OR IGNORE INTO memories
               (content, content_hash, metadata, tags, memory_type,
                created_at, updated_at, strength)
               VALUES (?, ?, ?, ?, 'session_summary', ?, ?, 1.0)""",
            (content, ch, json.dumps(meta_dict), tags, now_ts, now_ts)
        )
        db.commit()
    except Exception:
        pass  # Never block shutdown
    finally:
        _reset_session_tracker(tracker)

server = FastMCP("B12", lifespan=lifespan)


# ── Helpers ──────────────────────────────────────────────────────

# Client read timeout for embed-daemon round-trips. Must cover the daemon's own
# encode budget (CONN_TIMEOUT=15; BGE-M3 batches can run >10s). The former 5s
# timed out mid-encode and silently dropped the embedding — the memory was stored
# but NOT vector-searchable (audit #9). The daemon serializes connections, so a
# generous client timeout is safe. Tradeoff: semantic_search now waits up to this
# long under daemon contention (was 5s) instead of failing fast to FTS — a correct
# result beats a fast fallback. Env-overridable; parsed defensively so a malformed
# value can't crash module import (which would take down every memory tool).
try:
    _DAEMON_CLIENT_TIMEOUT = float(os.environ.get("B12_DAEMON_CLIENT_TIMEOUT", "20"))
except (TypeError, ValueError):
    _DAEMON_CLIENT_TIMEOUT = 20.0


def daemon_request(op: str, **kwargs) -> dict | None:
    """Send JSON to embed_daemon via Unix socket. Returns None on failure."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.settimeout(_DAEMON_CLIENT_TIMEOUT)
        s.connect(SOCK_PATH)
        s.sendall((json.dumps({"op": op, **kwargs}) + "\n").encode())
        data = b""
        while b"\n" not in data:
            chunk = s.recv(65536)
            if not chunk: break
            data += chunk
        resp = json.loads(data.decode().strip())
        return resp if resp.get("ok") else None
    except Exception:
        return None
    finally:
        s.close()


async def daemon_request_async(op: str, **kwargs) -> dict | None:
    """Async wrapper for daemon_request: runs the blocking Unix-socket round-trip
    in a worker thread so the shared asyncio event loop is never stalled on
    daemon I/O. In daemon mode a single FastMCP loop serves every connected
    session, so a synchronous socket call here would freeze ALL sessions for the
    duration of the call. See R&D plan SPD-1 (b12_mcp_server.py).

    Guarded by _daemon_lock so only one round-trip is in flight at a time: the
    embed daemon is single-connection-serial, so concurrent offloaded calls would
    otherwise race the listen backlog and a fast op could time out (5s) behind a
    slow encode_batch (>10s). The lock keeps the loop non-blocking AND serial.

    The worker is shielded so that if the caller is cancelled (e.g. the client
    disconnects mid-`encode_batch`), we still wait for the in-flight socket
    round-trip to finish before releasing _daemon_lock — a worker thread can't
    be cancelled, and releasing early would let the next request race the
    single-connection daemon. Cancellation then propagates as normal."""
    async with _daemon_lock:
        worker = asyncio.ensure_future(asyncio.to_thread(daemon_request, op, **kwargs))
        try:
            return await asyncio.shield(worker)
        finally:
            # Drain the worker even across REPEATED cancellations: a second
            # cancellation while awaiting would otherwise free _daemon_lock with the
            # thread still using the single-connection embed daemon (Codex review).
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except BaseException:
                    pass


async def _run_locked_offthread(fn, *args, **kwargs):
    """Offload a blocking embed-daemon helper to a worker thread UNDER _daemon_lock,
    cancellation-safe. Same shape as daemon_request_async: shield the worker so a
    caller cancellation (client disconnect mid-call) still waits for the in-flight
    socket round-trip to finish BEFORE releasing _daemon_lock — otherwise the lock
    frees while the worker keeps using the single-connection embed daemon, racing
    the next request into its 5s client timeout (SPD-1). For handlers whose sync
    helper does its own embed socket I/O (memory_refine -> encode_batch,
    memory_surface -> _daemon_search)."""
    async with _daemon_lock:
        worker = asyncio.ensure_future(asyncio.to_thread(fn, *args, **kwargs))
        try:
            return await asyncio.shield(worker)
        finally:
            # Drain the worker even across REPEATED cancellations: a second
            # cancellation while awaiting would otherwise free _daemon_lock with the
            # thread still using the single-connection embed daemon (Codex review).
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except BaseException:
                    pass


def compute_content_hash(content: str) -> str:
    """Content-only hash — matches upstream mcp-memory-service for backward compat."""
    return hashlib.sha256(content.strip().lower().encode()).hexdigest()


def _normalize_tags(tags) -> str:
    if tags is None: return ""
    if isinstance(tags, list): return ",".join(t.strip() for t in tags if t)
    return str(tags)


def _now():
    ts = time.time()
    return ts, datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _require_db() -> None:
    """Raise if the DB layer isn't initialized (pre-lifespan or post-shutdown).
    Connections are obtained per-op via _read/_write; handlers call this only as
    a readiness guard."""
    if not _DB_READY:
        raise RuntimeError("Database not initialized")


def _validate_metadata(value) -> str:
    """Ensure metadata is valid JSON. Gracefully handles legacy f-string format."""
    if value is None:
        return "{}"
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return "{}"
        try:
            json.loads(s)
            return s
        except (json.JSONDecodeError, ValueError):
            # Legacy f-string: "type:x, importance:0.6"
            result = {}
            for part in s.split(","):
                part = part.strip()
                if ":" not in part:
                    continue
                k, _, v = part.partition(":")
                k, v = k.strip(), v.strip()
                if k == "importance":
                    k = "importance_score"
                try:
                    v = float(v)
                    if v == int(v):
                        v = int(v)
                except (ValueError, TypeError):
                    pass
                result[k] = v
            return json.dumps(result, ensure_ascii=False) if result else "{}"
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return "{}"


_DEFAULT_WEIGHTS = {
    "decay": float(os.environ.get("B12_WEIGHT_DECAY", "0.25")),
    "importance": float(os.environ.get("B12_WEIGHT_IMPORTANCE", "0.25")),
    "relevance": float(os.environ.get("B12_WEIGHT_RELEVANCE", "0.40")),
    "strength": float(os.environ.get("B12_WEIGHT_STRENGTH", "0.10")),
}
_AGING_ALPHA = float(os.environ.get("B12_AGING_ALPHA", "4.0"))


def _unified_score(row, relevance: float) -> float:
    """Compute unified retrieval score across four dimensions.

    score = decay*W_decay + importance*W_importance + relevance*W_relevance
          + strength*W_strength

    Defaults (overridable via B12_WEIGHT_* env vars):
      decay      0.25  — FSRS retention curve, 1/(1+age_days/(9*eff_stability))
                         where eff_stability = strength*(1+_AGING_ALPHA*importance)
      importance 0.25  — user/system-tagged importance from metadata
      relevance  0.40  — BM25 (FTS path) or cosine (semantic path); single
                         slot because each candidate is found via exactly one
                         method here. Hybrid in `memory_search` comes from the
                         cross-method overlap bonus (BM25 ∩ semantic = +30%
                         min-side boost in `memory_search`).
      strength   0.10  — independent boost from spaced-repetition reinforcement
                         (previously only altered the decay time constant; now
                         also contributes directly so frequently-accessed
                         memories rank higher even when their content age
                         outpaces their relevance score).

    The four defaults sum to 1.0. Override behavior is intentionally permissive
    — callers may set any subset (e.g. only `B12_WEIGHT_STRENGTH=0` to suppress
    strength) without renormalizing the remaining weights; this mirrors how
    Mahmory's tuned weight vector (`semantic 0.42 / bm25 0.22 / recency 0.18 /
    strength 0.10 / importance 0.08`) was tuned empirically without enforced
    normalization. A future PR may add an explicit normalizer flag.
    """
    now_ts = time.time()
    # Explicit None checks — 0.0 is a valid value for both fields
    accessed = row["last_accessed_at"] if row["last_accessed_at"] is not None else row["created_at"]
    if accessed is None:
        accessed = now_ts
    age_days = max((now_ts - accessed) / 86400.0, 0.001)
    strength = row["strength"] if row["strength"] is not None else 1.0
    # Guard against zero strength (would cause ZeroDivisionError)
    if strength <= 0:
        strength = 0.01
    # Strength as an independent dimension, normalized to 0..1 against the
    # spaced-repetition boost cap of 5.0 (`strength` is FSRS stability, reinforced
    # on access and capped at 5.0).
    strength_score = min(strength / 5.0, 1.0)

    meta = {}
    try:
        meta = json.loads(row["metadata"] or "{}")
    except (json.JSONDecodeError, TypeError):
        pass
    # RET-3: two write-side importance_score scales coexist —
    #   - fractional [0, 0.95]  (b12_importance.py: TRIVIAL .30 / BASELINE .50 / CAP .95)
    #   - level multipliers [0.7, 2.0]  (critical 2.0 / important 1.5 / normal 1.0 /
    #     temporary 0.7; memory-session-end.sh caps at 2.0, precompact writes 1.5)
    # Fractional values are always < 1.0, so a value >= 1.0 is a level multiplier:
    # normalize it by /2.0 (2.0->1.0, 1.5->0.75, 1.0->0.5) while passing fractional
    # values through unchanged. A blanket /2.0 wrongly halved the fractional band
    # (0.95->0.475); a blanket clamp wrongly collapsed the levels (2.0/1.5/1.0->1.0).
    # KNOWN LIMIT: the overlap zone [0.7, 0.95] is ambiguous — `temporary` (level
    # 0.7) is indistinguishable from a fractional 0.7, so it passes through as 0.7
    # rather than 0.35 and can out-rank a `normal` (1.0 -> 0.5) on the importance
    # axis. This deliberately protects the high-value fractional 0.95 (the original
    # bug) over the low-value temporary 0.7; a complete fix needs write-side scale
    # unification + a data migration (deferred — see RET-3 follow-up).
    # Accept ONLY a genuine number (a JSON bool is a Python `bool`/`int` subclass, so
    # `float(True)` would score 1.0); missing/null/boolean/string fall back to the
    # baseline 0.50 — parity with the hook-SQL `json_type` guard and the OpenCode
    # `typeof === "number"` guard. Then clamp to [0, 1].
    raw_importance = meta.get("importance_score", 0.50)
    if isinstance(raw_importance, bool) or not isinstance(raw_importance, (int, float)):
        raw_importance = 0.50
    raw_importance = float(raw_importance)
    norm = raw_importance / 2.0 if raw_importance >= 1.0 else raw_importance
    importance = max(0.0, min(norm, 1.0))
    # Effective stability: both reinforcement (strength, bumped on access) AND
    # importance flatten the FSRS aging curve, so a valuable old memory (important
    # and/or reused) fades slowly while a trivial untouched one decays. This is the
    # cheap, intentional cure for "exp floored everything old to 0.01" — see the
    # Phase 1 design. importance ∈ [0,1] computed just above.
    eff_stability = strength * (1.0 + _AGING_ALPHA * importance)
    decay = max(1.0 / (1.0 + age_days / (9.0 * eff_stability)), 0.01)

    w = _DEFAULT_WEIGHTS
    return (
        w["decay"] * decay
        + w["importance"] * importance
        + w["relevance"] * relevance
        + w["strength"] * strength_score
    )


def _fmt_memory(row, score=None) -> str:
    p = [f"[{row['memory_type'] or 'general'}] {row['content'][:500]}"]
    if row["tags"]: p.append(f"  Tags: {row['tags']}")
    p.append(f"  Hash: {row['content_hash']}  Created: {row['created_at_iso'] or '?'}")
    if score is not None: p.append(f"  Score: {score:.3f}")
    return "\n".join(p)


# ── Tool: memory_store ───────────────────────────────────────────

@server.tool()
async def memory_store(content: str, metadata: dict | None = None) -> str:
    """Store a new memory with optional metadata, tags, and type."""
    _require_db()
    tracker = _current_session_tracker()
    tracker["tool_calls"] += 1

    # Fragment gate — short/incomplete utterances ("ok.", "evet.", lowercase
    # snippets without a [Label] prefix, unbalanced quotes) pollute search.
    # Allow [Label]-prefixed entries through even when short (handled inside
    # `is_fragment`). Escape hatch: B12_DISABLE_FRAGMENT_FILTER=1.
    # Codex review PR #59 P2: stored_count is incremented AFTER this gate so
    # rejected writes don't pollute session summaries with phantom counts.
    if os.environ.get("B12_DISABLE_FRAGMENT_FILTER", "").lower() not in ("1", "true", "yes"):
        try:
            from shared_patterns import is_fragment as _is_fragment
            if _is_fragment(content):
                return (
                    f"Rejected as fragment (len={len(content)}). "
                    "Add a [Label] prefix or expand the note. "
                    "Set B12_DISABLE_FRAGMENT_FILTER=1 to bypass."
                )
        except ImportError:
            pass
    tracker["stored_count"] += 1

    metadata = metadata or {}

    # PII / secret scrub — single chokepoint so EVERY MCP write is redacted
    # (mirrors write_time_merge.py:644 & codex_session_end.py:171). All MCP
    # clients — Claude Code, Cursor, Codex, Gemini, Kimi, OpenCode, Grok —
    # share this path. Honors B12_DISABLE_PII_SCRUB=1 for explicit raw capture.
    # Scrubs the content AND user-supplied metadata values + tags: a client can
    # smuggle secrets via metadata={"token":"sk-ant-..."} or tags="password=...".
    # Runs after the fragment gate (gate sees the original) and before
    # classify/hash/insert/embed so the redacted text is what gets stored.
    try:
        from b12_pii_scrubber import scrub as _pii_scrub
    except ImportError:
        _pii_scrub = None
    if _pii_scrub is not None:
        content = _pii_scrub(content)
        metadata = {
            k: (_pii_scrub(v) if isinstance(v, str) else v)
            for k, v in metadata.items()
        }

    # Detect project from tags
    if not tracker["project"]:
        t = metadata.get("tags", "") or ""
        for part in (t if isinstance(t, list) else t.split(",")):
            p = str(part).strip()
            if p.startswith("proj:"):
                tracker["project"] = p[5:]
                break
    tags_raw = metadata.pop("tags", None)
    memory_type = metadata.pop("type", metadata.pop("memory_type", "general"))
    valid_until = metadata.pop("valid_until", None)
    tags = _normalize_tags(tags_raw)

    # Auto-classify: prefix first, then ML head via daemon (v12.2+)
    if memory_type in ("general", "note", ""):
        try:
            from shared_patterns import classify_by_prefix
            prefix_result = classify_by_prefix(content)
            if prefix_result:
                memory_type = prefix_result["type"]
        except ImportError:
            pass

    if memory_type in ("general", "note", ""):
        resp = await daemon_request_async("classify", text=content)
        if resp and resp.get("type"):
            memory_type = resp["type"]

    content_hash = compute_content_hash(content)
    now_ts, now_iso = _now()

    # Default metadata fields
    base_meta = {
        "quality_score": 0.5, "quality_provider": "implicit",
        "access_count": 0, "source_type": "user", "credibility": 1.0,
    }
    base_meta.update(metadata)
    # Phase-2: resolve importance through the single finalize_importance chokepoint
    # — secret-cap + memory_type floor + the strongest of caller/heuristic — so the
    # MCP write path (Cursor/Cline/Gemini/…) gets the same scoring as hook capture.
    # Guarded; content is already PII/secret-scrubbed above.
    try:
        import b12_importance as _b12_imp
        base_meta["importance_score"] = _b12_imp.finalize_importance(
            content, base_meta.get("importance_score"), memory_type)
    except Exception:
        pass
    meta_json = _validate_metadata(base_meta)

    # One atomic writer op: dedup-check → undelete OR INSERT OR IGNORE → read id.
    # The id is read back within the same BEGIN IMMEDIATE txn (sees its own write).
    def _store_op(db):
        existing = db.execute(
            "SELECT id, deleted_at FROM memories WHERE content_hash = ?",
            (content_hash,),
        ).fetchone()
        if existing and existing["deleted_at"] is not None:
            db.execute(
                """UPDATE memories SET deleted_at = NULL, strength = 1.0,
                   tags = ?, memory_type = ?, metadata = ?,
                   updated_at = ?, updated_at_iso = ?, valid_until = ?
                   WHERE content_hash = ?""",
                (tags, memory_type, meta_json, now_ts, now_iso,
                 valid_until, content_hash),
            )
        else:
            db.execute(
                """INSERT OR IGNORE INTO memories
                   (content_hash, content, tags, memory_type, metadata,
                    strength, created_at, created_at_iso, updated_at, updated_at_iso,
                    valid_until)
                   VALUES (?, ?, ?, ?, ?, 1.0, ?, ?, ?, ?, ?)""",
                (content_hash, content, tags, memory_type, meta_json,
                 now_ts, now_iso, now_ts, now_iso, valid_until),
            )
        row = db.execute(
            "SELECT id FROM memories WHERE content_hash = ?", (content_hash,)
        ).fetchone()
        return row["id"] if row else None

    mem_id = await _write(_store_op)
    if mem_id is None:
        return f"Stored (hash: {content_hash[:16]}) but could not retrieve ID"

    # Embed via daemon (graceful degradation) — outside lock, daemon call is slow
    resp = await daemon_request_async("encode_batch", texts=[content])
    if not (resp and resp.get("embeddings")):
        # The memory IS stored (committed above) but has no vector embedding, so
        # it's FTS-searchable but not semantic/find_neighbors-searchable. This was
        # silent before the timeout fix (audit #9); surface it so a daemon outage
        # or a too-short timeout is visible. (embedding_backfill.py can re-embed.)
        _sys.stderr.write(
            f"[b12_mcp_server] embedding skipped for id={mem_id} "
            f"(daemon down or encode timed out) — memory stored but not vector-searchable\n"
        )
    if resp and resp.get("embeddings"):
        emb_bytes = base64.b64decode(resp["embeddings"][0])

        def _embed_op(db):
            try:
                db.execute(
                    "INSERT OR REPLACE INTO memory_embeddings (rowid, content_embedding) VALUES (?, ?)",
                    (mem_id, emb_bytes),
                )
            except sqlite3.OperationalError as e:
                # Only the schema-missing case is expected (minimal/test DBs).
                # Other OperationalErrors (database is locked, readonly db,
                # sqlite-vec insert errors) are REAL failures that degrade vector
                # recall — log them instead of swallowing the whole class.
                _m = str(e).lower()
                if "no such table" not in _m and "no such column" not in _m:
                    _sys.stderr.write(
                        f"[b12_mcp_server] embedding write failed (id={mem_id}): {e}\n"
                    )
            except Exception as e:
                # Real embedding-write failure → memory stored but not searchable
                # by vector. Don't fail the store, but surface it (was silently
                # swallowed, masking retrieval-quality regressions).
                _sys.stderr.write(
                    f"[b12_mcp_server] embedding write failed (id={mem_id}): {e}\n"
                )

        # Embedding write never fails the store (it's already committed above).
        try:
            await _write(_embed_op)
        except Exception as e:
            _sys.stderr.write(
                f"[b12_mcp_server] embedding write failed (id={mem_id}): {e}\n"
            )

    return f"Stored memory (hash: {content_hash[:16]}, id: {mem_id})"


# ── Tool: memory_search ─────────────────────────────────────────

def _iso_to_utc_epoch(s: str) -> float:
    """Parse an ISO-8601 string to a UTC epoch. Naive inputs are assumed UTC
    because created_at is stored as a UTC epoch — without this, a naive
    after/before bound was interpreted in the server's LOCAL timezone, skewing
    date-range filters by the local UTC offset."""
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


@server.tool()
async def memory_search(
    query: str = "",
    mode: str = "hybrid",
    tags: list[str] | str | None = None,
    limit: int = 10,
    after: str | None = None,
    before: str | None = None,
    stemmed: bool = False,
    max_response_chars: int = int(os.environ.get("MCP_MAX_RESPONSE_CHARS", "40000")),
) -> str:
    """Search memories by semantic similarity, full-text, or hybrid.
    Set stemmed=True to use porter-stemmed FTS (matches morphological variants: run/running/ran)."""
    _require_db()
    tracker = _current_session_tracker()
    tracker["tool_calls"] += 1
    if query:
        tracker["search_queries"].append(query[:80])
    results: dict[str, tuple[dict, float]] = {}  # content_hash -> (row, score)

    tag_list = ([t.strip() for t in tags.split(",")] if isinstance(tags, str)
                else tags if tags else [])

    # Build WHERE clause fragments
    wheres = ["m.deleted_at IS NULL",
              "(m.valid_until IS NULL OR m.valid_until > datetime('now'))"]
    params: list = []
    for t in tag_list:
        wheres.append("m.tags LIKE ?")
        params.append(f"%{t}%")
    if after:
        try:
            ts = _iso_to_utc_epoch(after)
            wheres.append("m.created_at >= ?")
            params.append(ts)
        except ValueError:
            pass
    if before:
        try:
            ts = _iso_to_utc_epoch(before)
            wheres.append("m.created_at <= ?")
            params.append(ts)
        except ValueError:
            pass

    where_sql = " AND ".join(wheres)

    # Exact + FTS reads run off the loop on a read-pool connection. The closure
    # mutates `results` (loop-local) on the read thread; the `await` is a
    # happens-before barrier and nothing else touches `results` until it returns.
    def _exact_fts_reads(db):
        # ── Exact substring search ─────────────────────────────
        if mode == "exact" and query:
            exact_rows = db.execute(
                f"""SELECT * FROM memories m
                    WHERE m.content LIKE ? AND {where_sql}
                    ORDER BY m.created_at DESC LIMIT ?""",
                [f"%{query}%"] + params + [limit],
            ).fetchall()
            for r in exact_rows:
                results[r["content_hash"]] = (r, _unified_score(r, 0.9))

        # ── FTS search (hybrid only) ─────────────────────────
        if mode == "hybrid" and query:
            # Choose FTS table: stemmed (porter) or default (trigram)
            _fts_table = "memory_fts_stemmed" if stemmed else "memory_content_fts"
            # memory_fts_stemmed has (content, tags); memory_content_fts has (content)
            _fts_join_col = "fts.rowid"
            # Try phrase match first, fall back to OR match for broader results
            for fts_attempt in ("phrase", "or"):
                try:
                    if fts_attempt == "phrase":
                        fts_query = '"' + query.replace('"', '""') + '"'
                    else:
                        # Split into words, join with OR for broader matching.
                        # Trigram FTS (memory_content_fts) produces NO tokens for
                        # <3-char strings, so a 2-char OR term matches 0 rows; only
                        # the stemmed/unicode table handles 2-char. Drop sub-min
                        # tokens for the active table so a pure-2-char query skips a
                        # guaranteed-empty FTS query and relies on semantic. (audit #19)
                        _min_tok = 2 if stemmed else 3
                        words = [w.strip() for w in query.split() if len(w.strip()) >= _min_tok]
                        if not words:
                            break
                        fts_query = " OR ".join(
                            '"' + w.replace('"', '""') + '"' for w in words
                        )
                    fts_rows = db.execute(
                        f"""SELECT m.*, rank
                            FROM {_fts_table} fts
                            JOIN memories m ON m.id = {_fts_join_col}
                            WHERE fts.content MATCH ? AND {where_sql}
                            ORDER BY rank LIMIT ?""",
                        [fts_query] + params + [limit],
                    ).fetchall()
                    for r in fts_rows:
                        h = r["content_hash"]
                        # FTS5 rank is negative BM25 (more negative = better match).
                        # Normalize to 0..1 where higher = better relevance.
                        bonus = 0.1 if fts_attempt == "phrase" else 0.0
                        raw_relevance = min(abs(r["rank"]) / 20.0, 1.0) + bonus
                        score = _unified_score(r, raw_relevance)
                        if h not in results or results[h][1] < score:
                            results[h] = (r, score)
                except Exception:
                    continue  # FTS match syntax error, try next

    await _read(_exact_fts_reads)

    # ── Semantic search via daemon (off the writer — daemon call is slow) ──
    if mode in ("semantic", "hybrid") and query:
        resp = await daemon_request_async(
            "semantic_search", query=query, db_path=DB_PATH, limit=limit * 2
        )
        if resp and resp.get("results"):
            def _semantic_merge(db):
                for hit in resp["results"]:
                    row = db.execute(
                        f"SELECT * FROM memories m WHERE m.id = ? AND {where_sql}",
                        [hit["id"]] + params,
                    ).fetchone()
                    if row:
                        ch = row["content_hash"]
                        old_score = results.get(ch, (None, 0))[1]
                        new_score = _unified_score(row, hit["score"])
                        # Intentional overlap bonus: memories found by BOTH FTS and semantic
                        # search get a 30% boost from the weaker score. This rewards cross-method
                        # agreement and is distinct from the hook's single-path scoring formula.
                        combined = max(old_score, new_score)
                        if ch in results:
                            combined = min(1.0, max(old_score, new_score) + min(old_score, new_score) * 0.3)
                        results[ch] = (row, combined)
            await _read(_semantic_merge)

    def _fallback_reads(db):
        # ── Fallback: recent memories if no query ────────────────
        if not query:
            rows = db.execute(
                f"""SELECT * FROM memories m
                    WHERE {where_sql}
                    ORDER BY m.created_at DESC LIMIT ?""",
                params + [limit],
            ).fetchall()
            for r in rows:
                results[r["content_hash"]] = (r, 0.5)

    await _read(_fallback_reads)

    # ── Sort and format ──────────────────────────────────────
    sorted_results = sorted(results.values(), key=lambda x: x[1], reverse=True)[:limit]

    if not sorted_results:
        return "No memories found."

    # ── Spaced repetition: boost strength + access_count for returned memories ──
    if query and sorted_results:
        def _boost_op(db):
            for row, _sc in sorted_results:
                try:
                    db.execute(
                        """UPDATE memories
                           SET strength = min(COALESCE(strength, 1.0) + 0.2, 5.0),
                               last_accessed_at = ?,
                               metadata = json_set(COALESCE(metadata, '{}'),
                                 '$.access_count',
                                 COALESCE(CASE WHEN json_valid(metadata) THEN json_extract(metadata, '$.access_count') END, 0) + 1)
                           WHERE content_hash = ?""",
                        (int(time.time()), row["content_hash"]),
                    )
                except sqlite3.OperationalError as e:
                    # Only schema-missing is expected (minimal/test DBs); log
                    # other OperationalErrors (locked/readonly) rather than
                    # swallowing the whole class.
                    _m = str(e).lower()
                    if "no such table" not in _m and "no such column" not in _m:
                        _sys.stderr.write(
                            f"[b12_mcp_server] strength boost failed "
                            f"({str(row['content_hash'])[:16]}): {e}\n"
                        )
                except Exception as e:
                    # Spaced-repetition boost failed — non-critical (don't fail
                    # the search) but log instead of swallowing silently.
                    _sys.stderr.write(
                        f"[b12_mcp_server] strength boost failed "
                        f"({str(row['content_hash'])[:16]}): {e}\n"
                    )
        await _write(_boost_op)

    output_parts = [f"Found {len(sorted_results)} memories:\n"]
    total_chars = 0
    for row, score in sorted_results:
        entry = _fmt_memory(row, score) + "\n"
        if total_chars + len(entry) > max_response_chars:
            output_parts.append(f"\n... truncated ({len(sorted_results)} total)")
            break
        output_parts.append(entry)
        total_chars += len(entry)

    return "\n".join(output_parts)


# ── Tool: memory_update ─────────────────────────────────────────

@server.tool()
async def memory_update(
    content_hash: str,
    updates: dict,
) -> str:
    """Update metadata, tags, or type of an existing memory. Content and hash are immutable."""
    _require_db()
    _current_session_tracker()["tool_calls"] += 1

    def _update_op(db):
        # Codex review PR #58 P1: when caller is restoring a soft-deleted
        # row via deleted_at=None, the lookup must match rows whose
        # deleted_at is non-null. Otherwise every soft delete is
        # effectively irreversible through the documented API flow.
        allow_soft_deleted = (
            isinstance(updates, dict) and "deleted_at" in updates
        )
        if allow_soft_deleted:
            row = db.execute(
                "SELECT * FROM memories WHERE content_hash = ?",
                (content_hash,),
            ).fetchone()
        else:
            row = db.execute(
                "SELECT * FROM memories WHERE content_hash = ? AND deleted_at IS NULL",
                (content_hash,),
            ).fetchone()
        if not row:
            return f"Memory not found: {content_hash[:16]}"

        # Check protected fields first
        protected = {"content", "content_hash", "embedding", "id"}
        bad = set(updates.keys()) & protected
        if bad:
            return f"Cannot update protected fields: {bad}"

        sets: list[str] = []
        vals: list = []

        if "tags" in updates:
            sets.append("tags = ?")
            vals.append(_normalize_tags(updates["tags"]))
        if "memory_type" in updates or "type" in updates:
            sets.append("memory_type = ?")
            vals.append(updates.get("memory_type", updates.get("type")))
        if "metadata" in updates:
            try:
                existing = json.loads(row["metadata"] or "{}")
            except (json.JSONDecodeError, TypeError):
                existing = {}
            existing.update(updates["metadata"])
            sets.append("metadata = ?")
            vals.append(_validate_metadata(existing))
        if "strength" in updates:
            # Clamp to [0.3, 5.0] to prevent poisoning the Ebbinghaus decay formula
            strength = max(0.3, min(5.0, float(updates["strength"])))
            sets.append("strength = ?")
            vals.append(strength)
        if "valid_until" in updates:
            # None clears dormancy/TTL, string sets it (ISO datetime or epoch)
            sets.append("valid_until = ?")
            vals.append(updates["valid_until"])
        if "deleted_at" in updates:
            # Soft-delete: set to epoch timestamp; None to un-delete
            sets.append("deleted_at = ?")
            vals.append(updates["deleted_at"])

        if not sets:
            return "No valid fields to update."

        now_ts, now_iso = _now()
        sets.append("updated_at = ?")
        vals.append(now_ts)
        sets.append("updated_at_iso = ?")
        vals.append(now_iso)

        vals.append(content_hash)
        db.execute(
            f"UPDATE memories SET {', '.join(sets)} WHERE content_hash = ?", vals
        )
        return f"Updated memory {content_hash[:16]}"

    return await _write(_update_op)


# ── Tool: memory_delete ─────────────────────────────────────────

@server.tool()
async def memory_delete(
    content_hash: str,
    hard: bool = False,
    reason: str | None = None,
) -> str:
    """Delete a memory by content_hash.

    Two modes:

      soft (default, reversible)
        Sets `deleted_at` to the current epoch. The row is invisible to
        recall but stays on disk until the GC job ages it out (see
        `b12_gc.py --age-days`; current default is configurable per
        install). Restoring is a single
        `memory_update(content_hash, {"deleted_at": None})`.

      hard (irreversible)
        Removes the row + its embedding immediately via
        `b12_gc.collect_one(memory_id)`. No undo. Use for secrets or
        credential leaks where the content must be unrecoverable.

    An audit row is written in either mode under the `delete-audit` tag,
    mirroring `memory_forget`'s audit trail.

    Overlap note: `memory_forget(mode='hard_delete')` does the same thing
    as `memory_delete(hard=True)`. The two surfaces will be consolidated
    in a follow-up (memory_forget kept for its privatize / forget_session
    modes which don't fit the delete shape). For new code, prefer
    `memory_delete`.
    """
    _require_db()
    _current_session_tracker()["tool_calls"] += 1

    now_ts, now_iso = _now()
    audit_payload: dict = {
        "mode": "hard" if hard else "soft",
        "target": content_hash[:64],
        "reason": reason or "",
        "ts_iso": now_iso,
    }

    def _delete_op(db):
        row = db.execute(
            "SELECT id FROM memories WHERE content_hash = ? AND deleted_at IS NULL",
            (content_hash,),
        ).fetchone()
        if not row:
            return f"Memory not found: {content_hash[:16]}"
        mem_id = int(row["id"])

        if hard:
            # Codex review PR #58 round 6 P2: even when collect_one
            # raises, we must persist a forensic-trail audit row.
            # Otherwise operators investigating a failed hard-delete have
            # no record of the attempt.
            audit_payload["affected_ids"] = [mem_id]
            audit_payload["target_hash_short"] = content_hash[:16]
            result = None
            hard_error: str | None = None
            try:
                import logging as _logging
                import b12_gc as _gc
                _logger = _logging.getLogger("b12_mcp_server.memory_delete")
                result = _gc.collect_one(DB_PATH, mem_id, _logger)
                audit_payload["gc_result"] = result
            except Exception as e:
                hard_error = str(e)
                audit_payload["gc_error"] = hard_error
            if hard_error is not None:
                # Write the audit row now, then return — the merged
                # post-block audit insert handles the success path.
                _audit_content = (
                    f"[delete-audit] mode=hard target={content_hash[:16]} "
                    f"id={mem_id} ERROR={hard_error[:120]} reason={(reason or '').strip()[:120]}"
                )
                _audit_hash = hashlib.sha256(
                    f"{_audit_content}|{now_iso}".encode("utf-8")
                ).hexdigest()
                try:
                    db.execute(
                        "INSERT OR IGNORE INTO memories "
                        "(content, content_hash, tags, memory_type, metadata, "
                        " created_at, created_at_iso, updated_at, updated_at_iso) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (_audit_content, _audit_hash, "delete-audit,system",
                         "system",
                         json.dumps(audit_payload, ensure_ascii=False),
                         now_ts, now_iso, now_ts, now_iso),
                    )
                    db.commit()
                except sqlite3.Error:
                    pass
                return f"hard delete failed: {hard_error}"
            # Codex review PR #58 round 2 P1: distinguish actual delete from
            # the collect_one safety-refuse path (sqlite_vec missing →
            # `deleted_memories: 0` returned with no DB mutation). Reporting
            # "Hard-deleted" in that case misleads operators into thinking
            # sensitive content is gone when it remains in `memories`.
            assert result is not None  # guarded by hard_error return above
            if int(result.get("deleted_memories", 0)) > 0:
                summary = (
                    f"Hard-deleted memory {content_hash[:16]} "
                    f"(id={mem_id}, embedding={result.get('deleted_embeddings', 0)})"
                )
            else:
                summary = (
                    f"Hard delete REFUSED for {content_hash[:16]} (id={mem_id}). "
                    f"GC returned deleted_memories=0 — see daemon log "
                    f"(likely sqlite_vec missing with memory_embeddings present). "
                    f"Row REMAINS in DB."
                )
        else:
            db.execute(
                "UPDATE memories SET deleted_at = ?, updated_at = ?, "
                "updated_at_iso = ? WHERE id = ?",
                (now_ts, now_ts, now_iso, mem_id),
            )
            db.commit()
            audit_payload["affected_ids"] = [mem_id]
            audit_payload["target_hash_short"] = content_hash[:16]
            summary = (
                f"Soft-deleted memory {content_hash[:16]} "
                f"(id={mem_id}); restore with "
                f"memory_update(..., {{'deleted_at': None}})"
            )

        # Audit row (same pattern as memory_forget). Tagged `delete-audit`
        # + system memory_type so it never decays into recall noise.
        audit_content = (
            f"[delete-audit] mode={'hard' if hard else 'soft'} "
            f"target={content_hash[:16]} id={mem_id} "
            f"reason={(reason or '').strip()[:120]}"
        )
        audit_hash = hashlib.sha256(
            f"{audit_content}|{now_iso}".encode("utf-8")
        ).hexdigest()
        try:
            db.execute(
                "INSERT OR IGNORE INTO memories "
                "(content, content_hash, tags, memory_type, metadata, "
                " created_at, created_at_iso, updated_at, updated_at_iso) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    audit_content, audit_hash, "delete-audit,system",
                    "system", json.dumps(audit_payload, ensure_ascii=False),
                    now_ts, now_iso, now_ts, now_iso,
                ),
            )
            db.commit()
        except sqlite3.Error:
            pass  # never block the delete on audit failure

        return summary

    # Hard delete embeds b12_gc.collect_one, which opens its OWN connection;
    # _write_raw runs this on the single writer thread in autocommit so we never
    # hold a BEGIN IMMEDIATE write lock across that external-connection write
    # (doing so would self-deadlock the writer against collect_one's conn). The
    # in-op db.commit() calls become no-ops in autocommit — the original
    # per-statement transaction boundaries are preserved exactly.
    return await _write_raw(_delete_op)


# ── Tool: memory_forget ─────────────────────────────────────────

@server.tool()
async def memory_forget(
    target: str,
    mode: str = "privatize",
    reason: str | None = None,
) -> str:
    """Forget operations — privacy + cleanup.

    Three modes:

      privatize (default, soft, reversible)
        Flip the row's tags to include `private` so cross-project
        recall excludes it. The content stays in the DB; running
        memory_update with tags removal undoes the privatize.

      hard_delete (irreversible)
        DELETE the row + embedding outright. Use only when the
        content must be unrecoverable (accidentally captured secret,
        credential leak). No undo.

      forget_session (bulk, soft)
        Walks every row whose metadata.session_id == target and
        privatizes the whole batch. Useful for "this whole exchange
        was sensitive — keep it local only".

    Every operation writes a single audit row tagged `forget-audit`
    with memory_type='system' so the trail survives both privatize
    and hard_delete. Caller-supplied `reason` is stored verbatim in
    the audit row's metadata.

    Parameters:
      target: content_hash for privatize/hard_delete; session_id for
              forget_session
      mode:   "privatize" | "hard_delete" | "forget_session"
      reason: free-text reason recorded in the audit row metadata
    """
    _require_db()
    _current_session_tracker()["tool_calls"] += 1

    valid_modes = {"privatize", "hard_delete", "forget_session"}
    if mode not in valid_modes:
        return f"Invalid mode '{mode}'. Use one of: {sorted(valid_modes)}"

    now_ts, now_iso = _now()
    audit_payload: dict = {
        "mode": mode,
        "target": target[:64],  # truncate long session_ids in metadata
        "reason": reason or "",
        "ts_iso": now_iso,
    }

    def _forget_op(db):
        affected_ids: list[int] = []

        if mode == "hard_delete":
            row = db.execute(
                "SELECT id FROM memories WHERE content_hash = ? AND deleted_at IS NULL",
                (target,),
            ).fetchone()
            if not row:
                return f"Memory not found: {target[:16]}"
            mem_id = int(row["id"])
            # Remove embedding first (FK-style cleanup; sqlite-vec
            # tables don't enforce FKs but consistency matters).
            try:
                db.execute("DELETE FROM memory_embeddings WHERE rowid = ?", (mem_id,))
            except sqlite3.OperationalError:
                pass  # table may not exist on older installs
            db.execute("DELETE FROM memories WHERE id = ?", (mem_id,))
            affected_ids = [mem_id]
            audit_payload["affected_ids"] = affected_ids
            audit_payload["target_hash_short"] = target[:16]

        elif mode == "privatize":
            row = db.execute(
                "SELECT id, tags FROM memories WHERE content_hash = ? AND deleted_at IS NULL",
                (target,),
            ).fetchone()
            if not row:
                return f"Memory not found: {target[:16]}"
            mem_id = int(row["id"])
            existing_tags = _normalize_tags(row["tags"] or "")
            tag_list = [t.strip() for t in existing_tags.split(",") if t.strip()]
            if "private" not in tag_list:
                tag_list.append("private")
            new_tags = ",".join(tag_list)
            db.execute(
                "UPDATE memories SET tags = ?, updated_at = ?, updated_at_iso = ? WHERE id = ?",
                (new_tags, now_ts, now_iso, mem_id),
            )
            affected_ids = [mem_id]
            audit_payload["affected_ids"] = affected_ids
            audit_payload["target_hash_short"] = target[:16]

        elif mode == "forget_session":
            # session_id can live under metadata.session_id or tags
            # ("session:<id>"). We search both surfaces.
            rows = db.execute(
                """
                SELECT id, tags FROM memories
                WHERE deleted_at IS NULL
                  AND (
                    (json_valid(metadata) AND json_extract(metadata, '$.session_id') = ?)
                    OR tags LIKE ?
                  )
                """,
                (target, f"%session:{target}%"),
            ).fetchall()
            if not rows:
                return f"No rows match session_id {target[:16]}"
            for r in rows:
                mem_id = int(r["id"])
                existing_tags = _normalize_tags(r["tags"] or "")
                tag_list = [t.strip() for t in existing_tags.split(",") if t.strip()]
                if "private" not in tag_list:
                    tag_list.append("private")
                new_tags = ",".join(tag_list)
                db.execute(
                    "UPDATE memories SET tags = ?, updated_at = ?, updated_at_iso = ? WHERE id = ?",
                    (new_tags, now_ts, now_iso, mem_id),
                )
                affected_ids.append(mem_id)
            audit_payload["affected_ids"] = affected_ids
            audit_payload["session_id_short"] = target[:16]

        # Audit row — system-type, tagged `forget-audit`. Survives both
        # privatize (tagged forget-audit, not private — operator can
        # still see the trail) and hard_delete (the target row is gone
        # but the audit lives).
        audit_content = (
            f"[forget-audit] mode={mode} target={target[:16]} "
            f"affected={len(affected_ids)} reason={(reason or '').strip()[:120]}"
        )
        audit_hash = hashlib.sha256(
            f"{audit_content}|{now_iso}".encode("utf-8")
        ).hexdigest()
        try:
            db.execute(
                """
                INSERT INTO memories (
                    content, content_hash, tags, memory_type, metadata,
                    created_at, created_at_iso, updated_at, updated_at_iso,
                    strength
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    audit_content,
                    audit_hash,
                    "forget-audit,system",
                    "system",
                    json.dumps(audit_payload, ensure_ascii=False),
                    now_ts, now_iso,
                    now_ts, now_iso,
                    1.0,
                ),
            )
        except sqlite3.IntegrityError:
            # extremely unlikely — same audit hash within the same
            # iso-second. Skip the audit row rather than failing the
            # forget op.
            pass

        return (
            f"Forget complete: mode={mode}, affected={len(affected_ids)} row(s), "
            f"audit recorded as memory_type=system tag=forget-audit"
        )

    return await _write(_forget_op)


# ── Tool: memory_quality ─────────────────────────────────────────

@server.tool()
async def memory_quality(
    action: str,
    content_hash: str | None = None,
    rating: str | None = None,
    feedback: str | None = None,
) -> str:
    """Rate, get, or analyze memory quality scores."""
    _require_db()

    if action == "rate":
        if not content_hash or rating is None:
            return "Need content_hash and rating (-1, 0, or 1)"
        def _rate_op(db):
            row = db.execute(
                "SELECT metadata FROM memories WHERE content_hash = ? AND deleted_at IS NULL",
                (content_hash,),
            ).fetchone()
            if not row:
                return f"Memory not found: {content_hash[:16]}"

            try:
                meta = json.loads(row["metadata"] or "{}")
            except (json.JSONDecodeError, TypeError):
                meta = {}
            user_score = {"1": 1.0, "0": 0.5, "-1": 0.0}.get(str(rating), 0.5)
            existing = float(meta.get("quality_score", 0.5))
            new_score = round(0.6 * user_score + 0.4 * existing, 4)

            meta["quality_score"] = new_score
            meta["quality_provider"] = "user"
            if feedback:
                meta["quality_feedback"] = feedback

            now_ts, now_iso = _now()
            db.execute(
                "UPDATE memories SET metadata = ?, updated_at = ?, updated_at_iso = ? WHERE content_hash = ?",
                (json.dumps(meta, ensure_ascii=False), now_ts, now_iso, content_hash),
            )
            return f"Quality updated to {new_score} for {content_hash[:16]}"
        return await _write(_rate_op)

    elif action == "get":
        if not content_hash:
            return "Need content_hash"
        def _get_op(db):
            row = db.execute(
                "SELECT content, metadata, tags FROM memories WHERE content_hash = ? AND deleted_at IS NULL",
                (content_hash,),
            ).fetchone()
            if not row:
                return f"Memory not found: {content_hash[:16]}"
            try:
                meta = json.loads(row["metadata"] or "{}")
            except (json.JSONDecodeError, TypeError):
                meta = {}
            return (f"Quality: {meta.get('quality_score', 'N/A')}\n"
                    f"Provider: {meta.get('quality_provider', 'N/A')}\n"
                    f"Content: {row['content'][:200]}\n"
                    f"Tags: {row['tags'] or 'none'}")
        return await _read(_get_op)

    elif action == "analyze":
        def _analyze_op(db):
            stats = db.execute("""
                SELECT COUNT(*) as total,
                       AVG(json_extract(metadata, '$.quality_score')) as avg_q,
                       MIN(json_extract(metadata, '$.quality_score')) as min_q,
                       MAX(json_extract(metadata, '$.quality_score')) as max_q
                FROM memories WHERE deleted_at IS NULL AND json_valid(metadata)
            """).fetchone()
            type_counts = db.execute(
                "SELECT memory_type, COUNT(*) as cnt FROM memories WHERE deleted_at IS NULL GROUP BY memory_type"
            ).fetchall()
            return stats, type_counts
        stats, type_counts = await _read(_analyze_op)
        total = stats["total"] or 0
        if total == 0:
            return "No memories in database."
        avg_q = float(stats["avg_q"]) if stats["avg_q"] is not None else 0.0
        min_q = float(stats["min_q"]) if stats["min_q"] is not None else 0.0
        max_q = float(stats["max_q"]) if stats["max_q"] is not None else 0.0
        lines = [
            f"Total memories: {total}",
            f"Quality — avg: {avg_q:.3f}, min: {min_q:.3f}, max: {max_q:.3f}",
            "By type:",
        ]
        for tc in type_counts:
            lines.append(f"  {tc['memory_type'] or 'none'}: {tc['cnt']}")
        return "\n".join(lines)

    return f"Unknown action: {action}. Use rate, get, or analyze."


# ── Tool: memory_session_context ─────────────────────────────────

@server.tool()
async def memory_session_context(
    project_name: str = "",
    cwd: str = "",
) -> str:
    """Get session start context: pre-fetched project memories, last session summary,
    and behavioral instructions. Call this FIRST in every new session.

    Returns a Markdown-formatted block. Per-row content is trimmed at
    RENDER_CONTENT_CAP_CHARS (600) with a "…(truncated)" breadcrumb so
    operators can see when a memory was clipped. Timestamps are
    rendered as `(2026-05-17 14:08)` next to each row. Pattern ported
    from AytuncYildizli/B12 PR 11 (787a6b8 session_context renderer)
    rendering style.
    """
    _require_db()
    now_ts = time.time()
    sections: list[str] = []

    # Per-row content cap. Mirrors the AytuncYildizli/B12 session_context renderer
    # RENDER_CONTENT_CAP_CHARS — keeps the MCP response under a reasonable
    # token budget when a session has 10x 4KB tool outputs.
    RENDER_CONTENT_CAP_CHARS = 600

    def _trim(text: str | None, cap: int = RENDER_CONTENT_CAP_CHARS) -> str:
        if text is None:
            return ""
        if len(text) <= cap:
            return text
        return text[:cap].rstrip() + "…(truncated)"

    def _short_iso(epoch: float | None, fallback_iso: str | None = None) -> str:
        if epoch:
            try:
                from datetime import datetime as _dt, timezone as _tz
                return _dt.fromtimestamp(float(epoch), tz=_tz.utc).strftime("%Y-%m-%d %H:%M")
            except (OverflowError, OSError, ValueError, TypeError):
                pass
        if fallback_iso:
            return str(fallback_iso)[:16].replace("T", " ")
        return ""

    # Detect project from cwd if not provided
    if not project_name and cwd:
        project_name = os.path.basename(cwd)

    # Phase MX (R8) — Gemini bench / temp-dir project_name normalization.
    # Some hosts (Gemini in particular when running under bench scripts
    # or temp dirs) append a run-id hash suffix like `-qe2y1r7g` to the
    # project basename, so the exact-tag lookup misses every real proj:
    # row in the DB. We build a small list of candidate project_name
    # forms — exact name first (preserves existing behavior), then a
    # trailing-suffix-strip, then the cwd parent's basename — and pick
    # the first one that returns project rows.
    import re as _re_local
    candidates: list[str] = []
    if project_name:
        candidates.append(project_name)
        # Strip trailing -[6-10 char alnum hash] suffix
        stripped = _re_local.sub(r"-[a-z0-9]{6,10}$", "", project_name)
        if stripped and stripped != project_name:
            candidates.append(stripped)
        # Cwd parent fallback (the temp-dir-with-meaningful-parent case)
        if cwd:
            parent = os.path.basename(os.path.dirname(cwd))
            if parent and parent not in candidates and parent not in ("/", "T", "tmp", "private"):
                candidates.append(parent)

    # Reads + the once-per-session strength boost run as one writer op (BEGIN
    # IMMEDIATE). The closure mutates sections/project_name (loop-local) on the
    # writer thread; the await below is a happens-before barrier with no
    # concurrent access. nonlocal lets the op rebind the resolved project_name.
    def _ctx_op(db):
        nonlocal project_name
        # 1. Pre-fetched project memories (top 3 by importance x strength)
        # Try each candidate in order, stop on first hit.
        proj_memories = []
        matched_project_name = ""
        for cand in candidates:
            proj_memories = db.execute("""
                SELECT id, content, memory_type, tags, metadata, strength,
                       created_at, created_at_iso
                FROM memories
                WHERE deleted_at IS NULL
                  AND (valid_until IS NULL OR valid_until > datetime('now'))
                  AND tags LIKE ?
                  AND (memory_type IS NULL OR memory_type NOT IN ('session_summary', 'progress'))
                ORDER BY max(min(CASE WHEN json_valid(metadata) AND json_type(metadata, '$.importance_score') IN ('integer','real') THEN (CASE WHEN json_extract(metadata, '$.importance_score') >= 1.0 THEN json_extract(metadata, '$.importance_score') / 2.0 ELSE json_extract(metadata, '$.importance_score') END) ELSE 0.50 END, 1.0), 0.0)
                         * COALESCE(strength, 1.0) DESC
                LIMIT 3
            """, (f"%proj:{cand}%",)).fetchall()
            if proj_memories:
                matched_project_name = cand
                break
        if proj_memories or matched_project_name:
            # Keep the legacy code path below working — substitute the
            # matched candidate name back into project_name so the
            # downstream Markdown heading is accurate.
            project_name = matched_project_name or project_name
        if proj_memories:
            sections.append(f"## Project Memories ({project_name})")
            boost_ids = []
            for m in proj_memories:
                ts = _short_iso(m["created_at"] if "created_at" in m.keys() else None,
                                m["created_at_iso"] if "created_at_iso" in m.keys() else None)
                ts_suffix = f"  _({ts})_" if ts else ""
                sections.append(f"- [{m['memory_type']}] {_trim(m['content'])}{ts_suffix}")
                boost_ids.append(m['id'])
            # Spaced repetition: boost retrieved memories
            if boost_ids:
                placeholders = ",".join("?" * len(boost_ids))
                db.execute(f"""
                    UPDATE memories
                    SET strength = min(COALESCE(strength, 1.0) + 0.2, 5.0),
                        last_accessed_at = ?,
                        metadata = json_set(COALESCE(metadata, '{{}}'),
                          '$.access_count',
                          COALESCE(CASE WHEN json_valid(metadata) THEN json_extract(metadata, '$.access_count') END, 0) + 1)
                    WHERE id IN ({placeholders})
                """, [now_ts] + boost_ids)

        # 2. Universal memories (top 2, not project-specific)
        universal = db.execute("""
            SELECT content, memory_type, created_at, created_at_iso FROM memories
            WHERE deleted_at IS NULL
              AND (valid_until IS NULL OR valid_until > datetime('now'))
              AND (tags NOT LIKE '%proj:%' OR tags IS NULL OR tags = '')
              AND (memory_type IS NULL OR memory_type NOT IN ('session_summary', 'progress'))
            ORDER BY max(min(CASE WHEN json_valid(metadata) AND json_type(metadata, '$.importance_score') IN ('integer','real') THEN (CASE WHEN json_extract(metadata, '$.importance_score') >= 1.0 THEN json_extract(metadata, '$.importance_score') / 2.0 ELSE json_extract(metadata, '$.importance_score') END) ELSE 0.50 END, 1.0), 0.0)
                     * COALESCE(strength, 1.0) DESC
            LIMIT 2
        """).fetchall()
        if universal:
            sections.append("## Universal Memories")
            for m in universal:
                ts = _short_iso(m["created_at"] if "created_at" in m.keys() else None,
                                m["created_at_iso"] if "created_at_iso" in m.keys() else None)
                ts_suffix = f"  _({ts})_" if ts else ""
                sections.append(f"- [{m['memory_type']}] {_trim(m['content'])}{ts_suffix}")

        # 3. Last session summary for this project
        if project_name:
            last_summary = db.execute("""
                SELECT content, created_at, created_at_iso FROM memories
                WHERE memory_type = 'session_summary'
                  AND deleted_at IS NULL
                  AND tags LIKE ?
                ORDER BY created_at DESC LIMIT 1
            """, (f"%proj:{project_name}%",)).fetchone()
            if last_summary:
                ts = _short_iso(
                    last_summary["created_at"] if "created_at" in last_summary.keys() else None,
                    last_summary["created_at_iso"] if "created_at_iso" in last_summary.keys() else None,
                )
                ts_suffix = f"  _({ts})_" if ts else ""
                sections.append(f"## Last Session Summary{ts_suffix}")
                sections.append(_trim(last_summary['content'], cap=800))

    await _write(_ctx_op)

    # 4. User profile (from templates directory) — no DB, off the writer
    profile_path = os.path.join(
        os.environ.get("B12_DATA_DIR", os.path.expanduser("~/.B12")),
        "user-profile.md"
    )
    if os.path.exists(profile_path):
        try:
            with open(profile_path) as f:
                profile = f.read().strip()
            if profile:
                sections.append("## User Profile")
                sections.append(profile[:500])
        except Exception:
            pass

    # 5. Behavioral instructions
    sections.append("## Instructions")
    sections.append(
        "- Search memory before answering questions about past work\n"
        "- Store decisions, errors/fixes, and learnings as memories\n"
        "- Include tags (proj:<name>) and metadata (scope, importance_score) when storing\n"
        "- Update existing memories instead of creating duplicates"
    )

    if not sections:
        return "No context available. Start storing memories to build your knowledge base."

    return "\n\n".join(sections)


# ── Tool: memory_consolidate ─────────────────────────────────────

@server.tool()
async def memory_consolidate(
    project: str = "",
    dry_run: bool = True,
    min_cluster_size: int = 3,
) -> str:
    """Consolidate similar memories: deduplicate near-identical entries,
    merge related memories, and flag contradictions for review.
    Uses HDBSCAN clustering on embeddings for semantic grouping.
    Defaults to dry_run=True (report only, no changes)."""
    if _consolidate is None:
        return "Error: consolidation_engine not available. Check scripts/ directory."

    try:
        if dry_run:
            # Dry-run is read-only (clustering + report, no writes), so offload the
            # CPU-heavy HDBSCAN pass to a worker thread — in daemon mode one FastMCP
            # loop serves every session, so it must never block on this rare admin
            # op. Precedent: daemon_request_async.
            #
            # Scoped-out caveat (same engine _daemon_request bypass noted in the
            # apply branch below): if the dry-run reaches merge candidates, the
            # engine's NLI check opens its OWN embed-daemon socket
            # (consolidation_engine.py ~:171) that does NOT go through
            # daemon_request_async / _daemon_lock. Off-loop, that socket call can
            # now overlap a concurrent session's classify/encode on the
            # single-connection-serial daemon — worst case a 5s client times out
            # and degrades (the hook paths already produce this contention), NOT
            # data loss. Routing the engine through daemon_request_async is the
            # deferred fix; offloading is still a net win (loop stays responsive
            # instead of frozen for the whole pass).
            result = await asyncio.to_thread(
                _consolidate,
                db_path=DB_PATH,
                project=project or None,
                dry_run=True,
                min_cluster_size=min_cluster_size,
            )
        else:
            # Apply path kept synchronous DELIBERATELY (scoped-out follow-ups, do NOT
            # "fix" by offloading here):
            #   1. the engine opens its OWN connection and writes outside the BB1
            #      single-writer, so a concurrent worker would race the serialized
            #      writer on SQLite's reserved lock; and
            #   2. consolidation_engine._daemon_request (~:150) is a synchronous
            #      socket call that bypasses this loop's daemon_request_async
            #      serialization.
            # Both must be addressed before the apply path can move off-loop safely.
            result = _consolidate(
                db_path=DB_PATH,
                project=project or None,
                dry_run=False,
                min_cluster_size=min_cluster_size,
            )
    except FileNotFoundError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Consolidation error: {e}"

    # Build human-readable summary
    lines = []
    mode = "DRY RUN" if dry_run else "APPLIED"
    lines.append(f"Consolidation ({mode}):")
    lines.append(f"  Memories processed:    {result.memories_processed}")
    lines.append(f"  Clusters found:        {result.clusters_found}")
    lines.append(f"  Deduplicated:          {result.memories_deduplicated}")
    lines.append(f"  Merged:                {result.memories_merged}")
    lines.append(f"  Contradictions flagged: {result.contradictions_flagged}")

    if dry_run and result.dry_run_report:
        lines.append("")
        lines.append("Cluster details:")
        for entry in result.dry_run_report:
            action = entry['type'].upper()
            ids = entry['ids']
            sim = entry.get('similarity', 0)
            nli = entry.get('nli_score', '')
            nli_str = f" NLI:{nli}" if nli else ''
            lines.append(f"  [{action}] #{ids[0]} <-> #{ids[1]}  "
                         f"(cosine: {sim:.3f}{nli_str})")
            for snippet in entry.get('snippets', []):
                lines.append(f"    {snippet}")

    if dry_run and (result.memories_deduplicated or result.memories_merged):
        lines.append("")
        lines.append("Run with dry_run=False to apply these changes.")
    elif not dry_run and not result.memories_deduplicated and not result.memories_merged:
        lines.append("")
        lines.append("No consolidation needed — database looks clean.")

    return "\n".join(lines)


# ── Tool: memory_refine ─────────────────────────────────────────

@server.tool()
async def memory_refine(
    candidates: str = "[]",
    project: str = "",
    similarity_threshold: float = 0.85,
) -> str:
    """Refine and deduplicate raw memory candidates.

    Accepts a JSON array of candidate memories, groups near-duplicates by
    semantic similarity, picks the best representative from each group,
    and scores quality. Returns refined candidates ready for storage.

    Args:
        candidates: JSON array of objects with {content, memory_type, tags}
        project: Project name for tag generation
        similarity_threshold: Cosine similarity threshold for grouping (0.0-1.0)
    """
    if _refine_candidates is None:
        return "Error: memory_refine module not available. Check scripts/ directory."

    try:
        candidate_list = json.loads(candidates)
    except (json.JSONDecodeError, TypeError):
        return "Error: candidates must be a valid JSON array"

    if not isinstance(candidate_list, list):
        return "Error: candidates must be a JSON array"

    if not candidate_list:
        return "No candidates provided"

    # Validate each candidate has at least 'content'
    valid = []
    for c in candidate_list:
        if isinstance(c, dict) and c.get("content"):
            valid.append({
                "content": str(c["content"]),
                "memory_type": str(c.get("memory_type", "general")),
                "tags": str(c.get("tags", "")),
            })

    if not valid:
        return "Error: no valid candidates (each must have 'content' field)"

    # Offload off the FastMCP event loop (in daemon mode one loop serves every
    # connected tab) under _daemon_lock: _refine_candidates does its own embed
    # encode_batch round-trip, which must serialize with the main path's daemon
    # I/O or it races the single-connection daemon (audit #5; completes the PR
    # #110 sweep). Cancellation-safe (shields the worker — see the helper).
    refined = await _run_locked_offthread(_refine_candidates, valid, similarity_threshold)

    # Format output
    lines = [f"Refined {len(valid)} candidates \u2192 {len(refined)} unique memories:\n"]
    for r in refined:
        quality_bar = "\u2588" * int(r["quality_score"] * 10) + "\u2591" * (10 - int(r["quality_score"] * 10))
        lines.append(f"[{quality_bar}] {r['quality_score']:.2f} | {r['memory_type']} | {r['content'][:120]}")
        if r["group_size"] > 1:
            lines.append(f"  \u21b3 merged {r['group_size']} near-duplicates")

    lines.append(f"\nTo store refined memories, call memory_store for each candidate above.")
    lines.append(f"\nJSON output:\n{json.dumps(refined, ensure_ascii=False, indent=2)}")

    return "\n".join(lines)


# ── Tool: memory_surface ─────────────────────────────────────────

# Surfacing engine (lazy import path — scripts/ is on sys.path)
try:
    from surfacing_engine import surface as _surface, format_for_context as _format_surface
except ImportError:
    _surface = None
    _format_surface = None


@server.tool()
async def memory_surface(
    context: str = "",
    trigger_type: str = "topic",
) -> str:
    """Proactively surface relevant memories based on context.

    Searches for memories related to the given context (file path, error message,
    or topic keywords) and returns relevant past knowledge. Useful on platforms
    without hook support (Gemini CLI, Codex CLI, etc.).

    Args:
        context: The context to search for (file path, error text, or topic)
        trigger_type: One of "file", "error", or "topic"
    """
    if _surface is None:
        return "Error: surfacing_engine module not available. Check scripts/ directory."

    if not context:
        return "Error: context is required"

    if trigger_type not in ("file", "error", "topic"):
        return "Error: trigger_type must be 'file', 'error', or 'topic'"

    # Offload off the loop + serialize the embed socket round-trip (_surface ->
    # _daemon_search) under _daemon_lock so it can't freeze other tabs or race a
    # concurrent encode (audit #7). Cancellation-safe (shields the worker).
    result = await _run_locked_offthread(_surface, trigger_type=trigger_type, context=context)

    if not result.surfaced:
        return f"No relevant memories found. ({result.reason})"

    formatted = _format_surface(result)
    if formatted:
        return formatted

    return "No relevant memories found."


# ── Tool: memory_export ─────────────────────────────────────────

# Export/Import engine (lazy import)
try:
    from export_import import (
        export_memories as _export_memories,
        import_memories as _import_memories,
        ExportResult as _ExportResult,
        ImportResult as _ImportResult,
    )
except ImportError:
    _export_memories = None
    _import_memories = None


@server.tool()
async def memory_export(
    output_path: str = "",
    project: str = "",
    tags: str = "",
    after: str = "",
    before: str = "",
) -> str:
    """Export memories to a portable .b12 archive file.

    Creates a gzip-compressed JSONL archive containing memories and graph edges.
    Excludes embeddings (regenerated on import). Safe to run while B12 is active.

    Args:
        output_path: Output file path (auto-generated in ~/.B12/exports/ if empty)
        project: Filter by project name
        tags: Filter by tags (comma-separated)
        after: Only memories created after this ISO date
        before: Only memories created before this ISO date
    """
    if _export_memories is None:
        return "Error: export_import module not available."

    # Validate output_path: must be within ~/.B12/exports/ if specified
    if output_path:
        b12_exports = os.path.join(os.path.expanduser("~"), ".B12", "exports")
        resolved = os.path.realpath(os.path.expanduser(output_path))
        if not resolved.startswith(os.path.realpath(b12_exports)):
            return f"Error: output_path must be within {b12_exports}"

    # Offload the full-table fetchall + gzip off the loop so a large corpus
    # doesn't freeze other connected tabs (audit #6). No embed socket here, so no
    # _daemon_lock — mirrors the memory_import offload (it opens its own conn).
    result = await asyncio.to_thread(
        _export_memories,
        db_path=DB_PATH,
        output_path=output_path,
        project=project,
        tags=tags,
        after=after,
        before=before,
    )

    size_kb = result.file_size_bytes / 1024
    return (
        f"Export complete:\n"
        f"  Memories: {result.memories_exported}\n"
        f"  Edges:    {result.edges_exported}\n"
        f"  File:     {result.output_path}\n"
        f"  Size:     {size_kb:.1f} KB\n"
        f"  Time:     {result.duration_seconds}s"
    )


@server.tool()
async def memory_import(
    input_path: str = "",
    mode: str = "merge",
    source_name: str = "",
) -> str:
    """Import memories from a .b12 archive file.

    Reads a .b12 archive and imports memories into the database. In merge mode
    (default), existing memories are skipped. In replace mode, all existing
    memories are soft-deleted before import.

    Args:
        input_path: Path to the .b12 archive file
        mode: "merge" (skip duplicates) or "replace" (soft-delete existing first)
        source_name: Name of source machine for provenance tracking
    """
    if _import_memories is None:
        return "Error: export_import module not available."

    if not input_path:
        return "Error: input_path is required"

    if not input_path.endswith(".b12"):
        return "Error: file must have .b12 extension"

    if ".." in input_path:
        return "Error: directory traversal not allowed"

    if mode not in ("merge", "replace"):
        return "Error: mode must be 'merge' or 'replace'"

    # PLAIN to_thread — do NOT route through the BB1 single writer (_write/_write_raw).
    # import_memories opens its OWN connection (export_import.py:280) with its own
    # BEGIN/commit/rollback (~:291) and an embedded post-commit embedding-backfill
    # (~:341, _request_embedding_backfill — scoped to the imported rows' hashes, so
    # bounded encode_batch socket work). Nesting that inside the
    # writer's BEGIN IMMEDIATE would throw "cannot start a transaction within a
    # transaction", and holding the single writer across the socket wait would stall
    # every connected tab. The standalone CLI path (export_import.py __main__) also
    # calls import_memories directly with no daemon. So we only move the blocking
    # call off the shared event loop and leave its transaction handling untouched.
    result = await asyncio.to_thread(
        _import_memories,
        db_path=DB_PATH,
        input_path=input_path,
        mode=mode,
        source_name=source_name,
    )

    lines = [
        f"Import complete ({mode} mode):",
        f"  Imported: {result.memories_imported}",
        f"  Skipped:  {result.memories_skipped}",
        f"  Edges:    {result.edges_imported}",
        f"  Time:     {result.duration_seconds}s",
    ]
    if result.errors:
        lines.append(f"  Errors ({len(result.errors)}):")
        for e in result.errors[:5]:
            lines.append(f"    - {e}")
    return "\n".join(lines)


# ── Tool: memory_dashboard ──────────────────────────────────────

import subprocess
import signal


@server.tool()
async def memory_dashboard(
    action: str = "start",
) -> str:
    """Start, stop, or check the B12 Web Dashboard.

    The dashboard provides a browser-based UI for browsing memories,
    visualizing the memory graph, viewing health stats, and managing
    contradictions. Runs at http://127.0.0.1:8742 (localhost only).

    Args:
        action: "start", "stop", "status", or "restart"
    """
    if action not in ("start", "stop", "status", "restart"):
        return "Error: action must be start, stop, status, or restart"

    b12_data = os.environ.get("B12_DATA_DIR", os.path.expanduser("~/.B12"))
    pid_file = os.path.join(b12_data, "dashboard.pid")
    token_file = os.path.join(b12_data, "dashboard.token")
    server_script = os.path.join(os.path.dirname(__file__), "dashboard_server.py")

    # Load config
    config_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "config", "dashboard.json"
    )
    port = 8742
    if os.path.exists(config_path):
        try:
            with open(config_path) as f:
                cfg = json.load(f)
            port = cfg.get("port", 8742)
        except Exception:
            pass

    def _is_running():
        """Check if dashboard process is alive."""
        if not os.path.exists(pid_file):
            return False
        try:
            pid = int(open(pid_file).read().strip())
            os.kill(pid, 0)  # signal 0 = check existence
            return True
        except (ProcessLookupError, ValueError, OSError):
            # Stale PID file
            try:
                os.remove(pid_file)
            except OSError:
                pass
            return False

    def _stop():
        """Stop the dashboard process."""
        if not os.path.exists(pid_file):
            return False
        try:
            pid = int(open(pid_file).read().strip())
            os.kill(pid, signal.SIGTERM)
            # Wait briefly for clean shutdown
            for _ in range(10):
                try:
                    os.kill(pid, 0)
                    time.sleep(0.2)
                except ProcessLookupError:
                    break
        except (ProcessLookupError, ValueError, OSError):
            pass
        # Clean up files
        for f in (pid_file, token_file):
            try:
                os.remove(f)
            except OSError:
                pass
        return True

    if action == "stop":
        if _is_running():
            # _stop() blocks up to ~2s (SIGTERM + os.kill poll loop with sleeps);
            # offload it so the shared FastMCP loop is not stalled. Precedent:
            # daemon_request_async.
            await asyncio.to_thread(_stop)
            return "Dashboard stopped."
        return "Dashboard is not running."

    if action == "status":
        if _is_running():
            token = ""
            if os.path.exists(token_file):
                token = open(token_file).read().strip()
            url = f"http://127.0.0.1:{port}?token={token}"
            pid = open(pid_file).read().strip()
            return f"Dashboard is running (PID {pid}).\nURL: {url}"
        return "Dashboard is not running."

    if action == "restart":
        # Offloaded for the same reason as the "stop" branch above.
        await asyncio.to_thread(_stop)
        # Fall through to start

    # ── Start ──
    if _is_running():
        token = ""
        if os.path.exists(token_file):
            token = open(token_file).read().strip()
        url = f"http://127.0.0.1:{port}?token={token}"
        return f"Dashboard already running.\nURL: {url}"

    if not os.path.exists(server_script):
        return f"Error: dashboard_server.py not found at {server_script}"

    # Generate auth token
    import secrets as _secrets
    token = _secrets.token_urlsafe(32)

    # Launch as background process
    proc = subprocess.Popen(
        [_sys.executable, server_script, "--port", str(port), "--token", token],
        stdout=subprocess.DEVNULL,
        stderr=open(os.path.join(b12_data, "dashboard.log"), "a"),
        start_new_session=True,
    )

    # Save PID and token
    os.makedirs(b12_data, exist_ok=True)
    with open(pid_file, "w") as f:
        f.write(str(proc.pid))
    fd = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token)

    url = f"http://127.0.0.1:{port}?token={token}"
    return f"Dashboard started (PID {proc.pid}).\nURL: {url}"


# ── MCP Resources (b12:// URIs) ─────────────────────────────────

@server.resource("b12://context/project/{name}")
async def resource_project_context(name: str) -> str:
    """Pre-fetched project context: top memories, last session summary, instructions."""
    _require_db()
    sections: list[str] = []

    def _ctx_reads(db):
        # Top 3 project memories by importance x strength
        proj_memories = db.execute("""
            SELECT content, memory_type, tags FROM memories
            WHERE deleted_at IS NULL
              AND (valid_until IS NULL OR valid_until > datetime('now'))
              AND tags LIKE ?
              AND (memory_type IS NULL OR memory_type NOT IN ('session_summary', 'progress'))
            ORDER BY max(min(CASE WHEN json_valid(metadata) AND json_type(metadata, '$.importance_score') IN ('integer','real') THEN (CASE WHEN json_extract(metadata, '$.importance_score') >= 1.0 THEN json_extract(metadata, '$.importance_score') / 2.0 ELSE json_extract(metadata, '$.importance_score') END) ELSE 0.50 END, 1.0), 0.0)
                     * COALESCE(strength, 1.0) DESC
            LIMIT 3
        """, (f"%proj:{name}%",)).fetchall()
        if proj_memories:
            sections.append(f"## Project Memories ({name})")
            for m in proj_memories:
                sections.append(f"- [{m['memory_type']}] {m['content'][:300]}")

        # Last session summary
        last_summary = db.execute("""
            SELECT content FROM memories
            WHERE memory_type = 'session_summary'
              AND deleted_at IS NULL
              AND tags LIKE ?
            ORDER BY created_at DESC LIMIT 1
        """, (f"%proj:{name}%",)).fetchone()
        if last_summary:
            sections.append("## Last Session Summary")
            sections.append(last_summary['content'][:800])

    await _read(_ctx_reads)

    # Behavioral instructions (no DB needed)
    sections.append("## Instructions")
    sections.append(
        "- Search memory before answering questions about past work\n"
        "- Store decisions, errors/fixes, and learnings as memories\n"
        "- Include tags (proj:<name>) and metadata when storing\n"
        "- Update existing memories instead of creating duplicates"
    )

    return "\n\n".join(sections) if sections else "No project context available."


@server.resource("b12://stats")
async def resource_stats() -> str:
    """Memory statistics: counts by status, type, and graph edges."""
    _require_db()

    def _stats_reads(db):
        active = db.execute(
            "SELECT COUNT(*) FROM memories WHERE deleted_at IS NULL"
        ).fetchone()[0]
        deleted = db.execute(
            "SELECT COUNT(*) FROM memories WHERE deleted_at IS NOT NULL"
        ).fetchone()[0]
        # Counts by memory_type
        type_rows = db.execute("""
            SELECT memory_type, COUNT(*) as cnt FROM memories
            WHERE deleted_at IS NULL
            GROUP BY memory_type ORDER BY cnt DESC
        """).fetchall()
        # Graph edges
        edge_count = db.execute("SELECT COUNT(*) FROM memory_graph").fetchone()[0]
        edge_types = db.execute("""
            SELECT relationship_type, COUNT(*) as cnt FROM memory_graph
            GROUP BY relationship_type ORDER BY cnt DESC
        """).fetchall()
        # Embedding coverage
        emb_count = 0
        if _HAS_VEC:
            try:
                emb_count = db.execute(
                    "SELECT COUNT(*) FROM memory_embeddings"
                ).fetchone()[0]
            except Exception:
                pass
        return active, deleted, type_rows, edge_count, edge_types, emb_count

    active, deleted, type_rows, edge_count, edge_types, emb_count = await _read(_stats_reads)

    lines = [
        "# B12 Memory Statistics",
        f"Active memories: {active}",
        f"Deleted memories: {deleted}",
        f"Embeddings: {emb_count}",
        f"Graph edges: {edge_count}",
        "",
        "## By Type",
    ]
    for r in type_rows:
        lines.append(f"- {r['memory_type']}: {r['cnt']}")
    if edge_types:
        lines.append("")
        lines.append("## Edge Types")
        for r in edge_types:
            lines.append(f"- {r['relationship_type']}: {r['cnt']}")

    return "\n".join(lines)


@server.resource("b12://profile")
async def resource_profile() -> str:
    """User profile from user-profile.md."""
    profile_path = os.path.join(
        os.environ.get("B12_DATA_DIR", os.path.expanduser("~/.B12")),
        "user-profile.md"
    )
    if not os.path.exists(profile_path):
        return "No user profile found. Create ~/.B12/user-profile.md to set one up."
    try:
        with open(profile_path) as f:
            return f.read().strip() or "User profile is empty."
    except Exception as e:
        return f"Error reading profile: {e}"


@server.resource("b12://health")
async def resource_health() -> str:
    """Quick health check: embedding coverage, stale count, recent growth."""
    _require_db()

    def _health_reads(db):
        active = db.execute(
            "SELECT COUNT(*) FROM memories WHERE deleted_at IS NULL"
        ).fetchone()[0]
        # Embedding coverage
        emb_count = 0
        if _HAS_VEC:
            try:
                emb_count = db.execute(
                    "SELECT COUNT(*) FROM memory_embeddings"
                ).fetchone()[0]
            except Exception:
                pass
        emb_pct = (emb_count / active * 100) if active > 0 else 0
        # Stale memories (not accessed in 30+ days)
        stale_threshold = time.time() - (30 * 86400)
        stale = db.execute("""
            SELECT COUNT(*) FROM memories
            WHERE deleted_at IS NULL
              AND COALESCE(last_accessed_at, created_at) < ?
        """, (stale_threshold,)).fetchone()[0]
        stale_pct = (stale / active * 100) if active > 0 else 0
        # Recent growth (last 7 days)
        week_ago = time.time() - (7 * 86400)
        new_7d = db.execute("""
            SELECT COUNT(*) FROM memories
            WHERE deleted_at IS NULL AND created_at > ?
        """, (week_ago,)).fetchone()[0]
        # Graph edges
        edges = db.execute("SELECT COUNT(*) FROM memory_graph").fetchone()[0]
        return active, emb_count, emb_pct, stale, stale_pct, new_7d, edges

    active, emb_count, emb_pct, stale, stale_pct, new_7d, edges = await _read(_health_reads)

    lines = [
        "# B12 Health Check",
        f"Active memories: {active}",
        f"Embedding coverage: {emb_count}/{active} ({emb_pct:.0f}%)",
        f"Stale memories (30d+): {stale} ({stale_pct:.0f}%)",
        f"New this week: {new_7d}",
        f"Graph edges: {edges}",
        "",
        f"Status: {'healthy' if emb_pct > 80 and stale_pct < 30 else 'needs attention'}",
    ]

    return "\n".join(lines)


# ── Proxy mode (when shared daemon is running) ───────────────────

def _daemon_alive(sock_path: str, timeout: float = 0.5) -> bool:
    """Quick check: is the B12 MCP daemon listening on `sock_path`?

    Returns True iff the socket exists AND accepts a TCP-style connect within
    `timeout` seconds. False on any failure (no socket file, permission denied,
    daemon stuck mid-shutdown). The caller falls back to in-process mode on False.
    """
    if not os.path.exists(sock_path):
        return False
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(sock_path)
        s.close()
        return True
    except (OSError, socket.timeout):
        return False


def _json_or_none(line: bytes):
    """Parse a JSON-RPC wire line, or None if it isn't valid JSON (partial frame,
    blank line). Observational only — callers still forward the raw bytes verbatim."""
    try:
        return json.loads(line)
    except (ValueError, TypeError):
        return None


def _observe_client_line(line: bytes, state: dict) -> None:
    """Capture the MCP handshake (the `initialize` request line + its id, and the
    `notifications/initialized` line) so the proxy can replay it on reconnect.
    Request-id tracking is done by the caller AFTER a successful write (see
    _client_request_id) — recording an id here would orphan requests queued
    mid-reconnect that never reached the dead daemon and double-answer them."""
    msg = _json_or_none(line)
    if not isinstance(msg, dict):
        return
    method = msg.get("method")
    if method == "initialize":
        state["init_line"] = line
        state["init_id"] = msg.get("id")
    elif method == "notifications/initialized":
        state["initialized_line"] = line


def _client_request_id(line: bytes):
    """The JSON-RPC id of a client->daemon REQUEST (has both a method and an id),
    or None for notifications/responses/non-JSON. Recorded as outstanding ONLY once
    the request has actually been written to the daemon."""
    msg = _json_or_none(line)
    if isinstance(msg, dict) and msg.get("method") is not None and msg.get("id") is not None:
        return msg["id"]
    return None


def _observe_server_line(line: bytes, state: dict) -> None:
    """Capture the FIRST `initialize` response (for capability-drift comparison on
    reconnect) and clear request ids as their responses come back."""
    msg = _json_or_none(line)
    if not isinstance(msg, dict):
        return
    if (state.get("init_response") is None and msg.get("id") == state.get("init_id")
            and isinstance(msg.get("result"), dict)
            and "protocolVersion" in msg["result"]):
        state["init_response"] = msg
    if msg.get("method") is None and "id" in msg:
        state["outstanding"].discard(msg.get("id"))


def _init_responses_compatible(new_resp, cached_resp) -> bool:
    """True if a reconnect's `initialize` response negotiates the same protocol
    version + capability surface the client already saw. A material difference
    (protocol/capability drift after a redeploy) means we must NOT silently splice
    the new session onto the old client — fall back to manual reconnect instead.
    Capabilities are compared by VALUE (canonical JSON), not just top-level keys, so
    a nested change (e.g. tools.listChanged flipping) is caught too (Codex PR #141)."""
    if not isinstance(new_resp, dict) or not isinstance(cached_resp, dict):
        return True  # can't compare → don't block the reconnect
    rn = new_resp.get("result") or {}
    rc = cached_resp.get("result") or {}
    if rn.get("protocolVersion") != rc.get("protocolVersion"):
        return False
    return (json.dumps(rn.get("capabilities") or {}, sort_keys=True)
            == json.dumps(rc.get("capabilities") or {}, sort_keys=True))


async def _run_as_proxy(sock_path: str) -> None:
    """Bidirectional JSON-RPC pipe (stdin <-> daemon socket <-> stdout) that
    transparently RECONNECTS to the daemon when the socket drops mid-session.

    Lines are forwarded verbatim; a thin observational parse captures the MCP
    handshake and tracks outstanding request ids (see _observe_*).

    Shutdown / reconnect semantics:
      - stdin EOF (host CLI exited): half-close the daemon, drain pending
        responses, exit. Normal case.
      - socket EOF while stdin is still open (daemon restart by launchd/RSS-guard,
        redeploy, MAX_CONNECTIONS eviction, crash): Fix C re-dials the daemon with
        backoff, replays the cached `initialize` (swallowing the new response),
        replays `notifications/initialized`, synthesizes a retryable error for each
        in-flight request, and resumes — so Claude Code never sees the break. If
        reconnection can't succeed within B12_MCP_RECONNECT_BUDGET seconds (default
        30, ~one launchd respawn), or the reconnect negotiates a different protocol/
        capability set, the proxy exits (legacy behavior: B12 shows disconnected
        until a manual /mcp). Set B12_MCP_PROXY_RECONNECT=0 to disable entirely.
    """
    import sys as _sys
    loop = asyncio.get_running_loop()

    # Wrap stdin as a proper asyncio StreamReader so cancellation actually works.
    stdin_reader = asyncio.StreamReader()
    stdin_protocol = asyncio.StreamReaderProtocol(stdin_reader)
    await loop.connect_read_pipe(lambda: stdin_protocol, _sys.stdin)

    def _write_stdout_real(data: bytes) -> None:
        _sys.stdout.buffer.write(data)
        _sys.stdout.buffer.flush()

    await _proxy_session(stdin_reader, _write_stdout_real, sock_path)


async def _proxy_session(stdin_reader, write_stdout, sock_path: str) -> None:
    """Core reconnect-capable JSON-RPC pipe, parameterized on `stdin_reader` (an
    asyncio.StreamReader) and `write_stdout` (a bytes->None sink) so tests can drive
    it against a mock daemon socket. See _run_as_proxy for the wire/reconnect contract."""
    import sys as _sys  # local: the drift-exit branch logs to stderr (Codex PR #141 retro)
    loop = asyncio.get_running_loop()

    sock_reader, sock_writer = await asyncio.open_unix_connection(sock_path)
    conn = {"reader": sock_reader, "writer": sock_writer}
    state: dict = {"init_line": None, "init_id": None, "initialized_line": None,
                   "init_response": None, "outstanding": set()}

    stdin_closed = asyncio.Event()
    give_up = asyncio.Event()
    connected = asyncio.Event()
    connected.set()
    budget = _RECONNECT_BUDGET_S if _PROXY_RECONNECT else 0.0

    async def _reconnect() -> bool:
        """Re-dial + replay the handshake. Owned solely by socket_to_stdout (no
        concurrent socket read). Returns True on success, False on budget/drift."""
        connected.clear()
        try:
            conn["writer"].close()
        except Exception:
            pass
        deadline = loop.time() + budget
        backoff = 0.1

        def _remaining() -> float:
            return deadline - loop.time()

        while budget > 0 and _remaining() > 0 and not stdin_closed.is_set():
            try:
                new_r, new_w = await asyncio.open_unix_connection(sock_path)
            except (ConnectionRefusedError, FileNotFoundError, OSError):
                await asyncio.sleep(min(backoff, max(0.0, _remaining())))
                backoff = min(backoff * 2, 2.0)
                continue
            try:
                if state["init_line"]:
                    new_w.write(state["init_line"])
                    await new_w.drain()
                    new_resp = None
                    new_resp_line = None
                    while True:
                        # clamp the per-read wait to the remaining budget so a daemon
                        # that accepts but never answers can't overshoot the cap.
                        _to = min(5.0, _remaining())
                        if _to <= 0:
                            raise asyncio.TimeoutError
                        rline = await asyncio.wait_for(new_r.readline(), timeout=_to)
                        if not rline:
                            raise ConnectionError("eof during initialize replay")
                        m = _json_or_none(rline)
                        if isinstance(m, dict) and m.get("id") == state["init_id"] and "result" in m:
                            new_resp = m
                            new_resp_line = rline
                            break
                    if state["init_response"] is None:
                        # The drop happened DURING the initial handshake — the host
                        # never got an initialize response. FORWARD this one (and
                        # capture it + clear its outstanding id) so startup completes,
                        # rather than swallowing it and hanging the client. No drift
                        # baseline exists yet to compare against.
                        state["init_response"] = new_resp
                        write_stdout(new_resp_line)
                        state["outstanding"].discard(state["init_id"])
                    elif not _init_responses_compatible(new_resp, state["init_response"]):
                        # Host already has an init response — a materially different
                        # one means a divergent session; bail to manual reconnect.
                        _sys.stderr.write("B12 proxy: capability drift on reconnect; exiting\n")
                        _sys.stderr.flush()
                        try:
                            new_w.close()
                        except Exception:
                            pass
                        return False
                    # else: host already has an equivalent init response — swallow this one.
                if state["initialized_line"]:
                    new_w.write(state["initialized_line"])
                    await new_w.drain()
            except (asyncio.TimeoutError, ConnectionError, ConnectionResetError,
                    BrokenPipeError, OSError):
                try:
                    new_w.close()
                except Exception:
                    pass
                await asyncio.sleep(min(backoff, max(0.0, _remaining())))
                backoff = min(backoff * 2, 2.0)
                continue
            # Success: answer the orphaned in-flight requests so the host fails fast
            # (retryable) instead of hanging until MCP_TIMEOUT. NB: a response the
            # daemon wrote into the socket buffer just before the drop is unobserved,
            # so its id is still outstanding and gets -32001 instead of the real
            # result — recoverable (host retries), inherent to any reconnect proxy.
            # Guard the write: if the host already closed stdout, give up cleanly
            # rather than raising an unretrieved task exception.
            try:
                for rid in list(state["outstanding"]):
                    err = {"jsonrpc": "2.0", "id": rid,
                           "error": {"code": -32001, "message": "B12 reconnected; please retry"}}
                    write_stdout((json.dumps(err) + "\n").encode())
            except (BrokenPipeError, OSError):
                try:
                    new_w.close()
                except Exception:
                    pass
                return False
            state["outstanding"].clear()
            conn["reader"], conn["writer"] = new_r, new_w
            connected.set()
            return True
        return False

    async def stdin_to_socket() -> None:
        try:
            while True:
                line = await stdin_reader.readline()
                if not line:
                    return  # stdin EOF — host CLI closed
                _observe_client_line(line, state)
                req_id = _client_request_id(line)
                while True:  # write, retrying across a reconnect if the socket is down
                    await connected.wait()
                    if give_up.is_set():
                        return
                    w = conn["writer"]
                    try:
                        w.write(line)
                        await w.drain()
                        # Track as outstanding ONLY now that it actually reached the
                        # daemon — so a request queued while connected was cleared
                        # (and thus re-sent to the NEW daemon) is never both
                        # synthesized-errored AND answered for the same id.
                        if req_id is not None:
                            state["outstanding"].add(req_id)
                        break
                    except (ConnectionResetError, BrokenPipeError, OSError):
                        # Clear `connected` ONLY if the writer we failed on is still
                        # current. If _reconnect already swapped in a fresh writer (and
                        # set `connected`), clearing here would wedge stdin waiting for
                        # a set that never comes; instead just retry on the new writer.
                        if conn["writer"] is w:
                            connected.clear()  # socket_to_stdout will see EOF + reconnect
                        try:
                            await asyncio.wait_for(connected.wait(), timeout=budget + 2.0)
                        except asyncio.TimeoutError:
                            return
                        if give_up.is_set():
                            return
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            stdin_closed.set()
            try:
                if conn["writer"].can_write_eof():
                    conn["writer"].write_eof()
            except Exception:
                pass

    async def socket_to_stdout() -> None:
        while True:
            try:
                line = await conn["reader"].readline()
            except (ConnectionResetError, OSError):
                line = b""
            if line:
                _observe_server_line(line, state)
                try:
                    write_stdout(line)
                except (BrokenPipeError, OSError):
                    return
                continue
            # Socket EOF.
            if stdin_closed.is_set():
                return  # normal shutdown — host already gone
            if not await _reconnect():
                give_up.set()
                connected.set()  # unblock a stdin writer waiting on reconnect so it exits
                return
            # Reconnected — keep reading from the fresh conn["reader"].

    stdin_task = asyncio.create_task(stdin_to_socket())
    socket_task = asyncio.create_task(socket_to_stdout())

    done, _pending = await asyncio.wait(
        {stdin_task, socket_task}, return_when=asyncio.FIRST_COMPLETED
    )
    if stdin_task in done:
        # Host closed stdin first — give the socket up to 2s to drain in-flight
        # responses before tearing down.
        if not socket_task.done():
            try:
                await asyncio.wait_for(socket_task, timeout=2.0)
            except asyncio.TimeoutError:
                socket_task.cancel()
                try:
                    await socket_task
                except (asyncio.CancelledError, Exception):
                    pass
    else:
        # socket_to_stdout finished (reconnect gave up, or stdout closed): stop the
        # stdin reader and exit. give_up unblocks any in-progress write retry.
        give_up.set()
        connected.set()
        stdin_task.cancel()
        try:
            await stdin_task
        except (asyncio.CancelledError, Exception):
            pass

    try:
        conn["writer"].close()
        await conn["writer"].wait_closed()
    except Exception:
        pass


# ── Entry point ──────────────────────────────────────────────────

if __name__ == "__main__":
    # Daemon-aware launch:
    #   1. If B12_MCP_FORCE_STDIO=1 is set, always run in-process (escape hatch).
    #   2. Else if the shared daemon socket is reachable, become a thin
    #      stdio<->socket proxy (saves ~7-8s per Claude Code session cold start).
    #   3. Else fall back to legacy in-process FastMCP stdio — this is the
    #      behaviour every non-Claude-Code consumer (Codex, Gemini, Kimi,
    #      OpenCode, Grok) has always used. No regression risk.
    _force_stdio = os.environ.get("B12_MCP_FORCE_STDIO") == "1"
    if not _force_stdio and _daemon_alive(MCP_DAEMON_SOCK):
        try:
            asyncio.run(_run_as_proxy(MCP_DAEMON_SOCK))
        except KeyboardInterrupt:
            pass
        except Exception as exc:
            # Proxy failure: log to stderr (visible to host CLI) and fall back
            # to in-process so this Claude Code session stays usable.
            import sys as _sys
            _sys.stderr.write(
                f"[B12] daemon proxy failed ({type(exc).__name__}: {exc}); "
                f"falling back to in-process stdio\n"
            )
            server.run("stdio")
    else:
        server.run("stdio")
