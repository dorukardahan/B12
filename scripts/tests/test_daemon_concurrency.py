"""BB1 concurrency proof: per-connection SQLite in the MCP daemon.

The server replaced the global `async with _db_lock: <sync _db.execute>` pattern
(which serialized every CLI tab on one lock and blocked the event loop on
synchronous sqlite) with: reads on a thread pool of thread-owned connections
(WAL concurrent readers) and ALL writes through ONE serialized writer thread
(BEGIN IMMEDIATE per op). These tests drive that path concurrently and assert:

  • no thread/ProgrammingError (check_same_thread) and no leaked SQLITE_BUSY,
  • transactional atomicity — dedup-then-insert never double-inserts, no partial
    writes — at both the low-level (_read/_write) and the REAL-handler layer,
  • final state == a serial baseline,
  • a deliberately slow read does NOT block other reads OR the writer (and a slow
    write does NOT block reads).

Everything runs against a freshly-built POPULATED temp DB in pytest's tmp dir —
the live DB at ~/Library/Application Support/mcp-memory/sqlite_vec.db is NEVER
touched. The embed daemon is stubbed out (daemon_request -> None) so the tests
are deterministic and need no external process.

Run via:  python3 -m pytest scripts/tests/test_daemon_concurrency.py -v
      or:  python3 scripts/tests/test_daemon_concurrency.py
"""
import asyncio
import hashlib
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    import pytest
except Exception:  # pragma: no cover - pytest always present in CI
    pytest = None


def _load():
    """Import the server module, or signal skip if mcp isn't installed."""
    try:
        import b12_mcp_server as M
        return M
    except Exception as e:  # mcp not installed in this env
        return e


def _make_populated_db(M, path, n):
    """Build a populated DB at `path` via the real schema + N memories."""
    conn = sqlite3.connect(path)
    try:
        M._ensure_schema(conn)
        now = 1_700_000_000.0
        for i in range(n):
            content = f"[note] seed memory number {i} for concurrency testing"
            ch = hashlib.sha256(content.encode()).hexdigest()
            conn.execute(
                "INSERT OR IGNORE INTO memories "
                "(content, content_hash, tags, memory_type, metadata, "
                " created_at, created_at_iso, updated_at, updated_at_iso, strength) "
                "VALUES (?, ?, ?, ?, '{}', ?, '', ?, '', 1.0)",
                (content, ch, "proj:bb1test", "note", now + i, now + i),
            )
        conn.commit()
    finally:
        conn.close()


def _setup(M, tmp_db):
    """Point the module at the temp DB + stub the embed daemon. Returns a restore
    callable that reverts globals and shuts the pools down.

    Each test runs in its own asyncio.run() loop. The module-level asyncio.Locks
    bind to the first loop that *contends* on them, so reset them to fresh
    instances per test (constructed with no running loop → they bind lazily to
    this test's loop). In production the server runs ONE loop for its lifetime, so
    the locks bind once and this reset is a test-harness concern only."""
    orig = (M.DB_PATH, M.daemon_request, M._daemon_lock, M._db_init_lock)
    M.DB_PATH = tmp_db
    M.daemon_request = lambda *a, **k: None  # no embed daemon in tests
    M._daemon_lock = asyncio.Lock()
    M._db_init_lock = asyncio.Lock()

    def restore():
        try:
            asyncio.run(M._shutdown_db())
        except Exception:
            pass
        (M.DB_PATH, M.daemon_request, M._daemon_lock, M._db_init_lock) = orig

    return restore


def _run(M, tmp_path, coro_factory):
    """Build a populated DB, init the pools inside one event loop, run the async
    body, then restore + tear down. coro_factory(M, db_path) -> coroutine."""
    db = os.path.join(str(tmp_path), "concurrency.db")
    _make_populated_db(M, db, 60)
    restore = _setup(M, db)
    try:
        async def _body():
            await M._init_db()
            return await coro_factory(M, db)
        return asyncio.run(_body())
    finally:
        restore()


# ─────────────────────────────────────────────────────────────────
# 1. N parallel readers + M parallel writers through _read/_write:
#    no thread/ProgrammingError, no leaked SQLITE_BUSY, dedup atomic.
# ─────────────────────────────────────────────────────────────────
def test_parallel_readers_writers_no_thread_or_busy_error(tmp_path):
    M = _load()
    if not hasattr(M, "_init_db"):
        if pytest:
            pytest.skip(f"b12_mcp_server unavailable ({M})")
        return

    async def body(M, db):
        # 24 readers (COUNT) + 24 writers (distinct inserts) all at once.
        def _reader(_):
            def op(c):
                return c.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            return M._read(op)

        def _writer(i):
            content = f"[decision] concurrent writer row {i}"
            ch = hashlib.sha256(content.encode()).hexdigest()

            def op(c):
                c.execute(
                    "INSERT OR IGNORE INTO memories "
                    "(content, content_hash, created_at, updated_at, strength) "
                    "VALUES (?, ?, 1, 1, 1.0)",
                    (content, ch),
                )
                return ch
            return M._write(op)

        tasks = [_reader(i) for i in range(24)] + [_writer(i) for i in range(24)]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        excs = [r for r in results if isinstance(r, BaseException)]
        assert not excs, f"unexpected exceptions on concurrent r/w: {excs!r}"
        # readers all returned an int count
        reader_results = results[:24]
        assert all(isinstance(r, int) for r in reader_results)
        # all 24 distinct writer rows landed
        def count_new(c):
            return c.execute(
                "SELECT COUNT(*) FROM memories WHERE content LIKE 'concurrent writer row%'"
                " OR content LIKE '[decision] concurrent writer row%'"
            ).fetchone()[0]
        n_new = await M._read(count_new)
        assert n_new == 24, f"expected 24 new rows, got {n_new}"

    _run(M, tmp_path, body)


# ─────────────────────────────────────────────────────────────────
# 2. Atomicity: M writers racing the SAME dedup-then-insert never
#    double-insert (single-writer serialization replaces the lock).
# ─────────────────────────────────────────────────────────────────
def test_dedup_then_insert_never_double_inserts(tmp_path):
    M = _load()
    if not hasattr(M, "_init_db"):
        if pytest:
            pytest.skip(f"b12_mcp_server unavailable ({M})")
        return

    async def body(M, db):
        content = "[decision] the one and only deduped row"
        ch = hashlib.sha256(content.encode()).hexdigest()

        def make_op():
            # SELECT-then-conditional-INSERT (TOCTOU shape). Atomic because the
            # single writer thread can't interleave two ops, and each runs in one
            # BEGIN IMMEDIATE txn.
            def op(c):
                row = c.execute(
                    "SELECT id FROM memories WHERE content_hash = ?", (ch,)
                ).fetchone()
                if row is None:
                    c.execute(
                        "INSERT INTO memories "
                        "(content, content_hash, created_at, updated_at, strength) "
                        "VALUES (?, ?, 1, 1, 1.0)",
                        (content, ch),
                    )
                return None
            return M._write(op)

        results = await asyncio.gather(
            *[make_op() for _ in range(32)], return_exceptions=True
        )
        excs = [r for r in results if isinstance(r, BaseException)]
        assert not excs, f"writer raced into an error: {excs!r}"

        def count(c):
            return c.execute(
                "SELECT COUNT(*) FROM memories WHERE content_hash = ?", (ch,)
            ).fetchone()[0]
        n = await M._read(count)
        assert n == 1, f"dedup-then-insert double-inserted: {n} rows"

    _run(M, tmp_path, body)


# ─────────────────────────────────────────────────────────────────
# 3. Final state after concurrent writes == a serial baseline.
# ─────────────────────────────────────────────────────────────────
def test_concurrent_final_state_equals_serial_baseline(tmp_path):
    M = _load()
    if not hasattr(M, "_init_db"):
        if pytest:
            pytest.skip(f"b12_mcp_server unavailable ({M})")
        return

    # 100 ops over 20 distinct hashes (5x each). Content is a pure function of
    # the hash, so whichever duplicate "wins" the INSERT OR IGNORE, the surviving
    # row is identical → concurrent and serial runs must converge to the same set.
    def ops_spec():
        spec = []
        for i in range(100):
            key = i % 20
            content = f"[note] payload for key {key}"
            ch = hashlib.sha256(content.encode()).hexdigest()
            spec.append((ch, content))
        return spec

    spec = ops_spec()

    # Serial baseline in a separate plain DB.
    baseline_db = os.path.join(str(tmp_path), "baseline.db")
    _make_populated_db(M, baseline_db, 60)
    bconn = sqlite3.connect(baseline_db)
    try:
        for ch, content in spec:
            bconn.execute(
                "INSERT OR IGNORE INTO memories "
                "(content, content_hash, created_at, updated_at, strength) "
                "VALUES (?, ?, 1, 1, 1.0)",
                (content, ch),
            )
        bconn.commit()
        baseline = sorted(
            tuple(r) for r in bconn.execute(
                "SELECT content_hash, content FROM memories ORDER BY content_hash"
            ).fetchall()
        )
    finally:
        bconn.close()

    async def body(M, db):
        def make_op(ch, content):
            def op(c):
                c.execute(
                    "INSERT OR IGNORE INTO memories "
                    "(content, content_hash, created_at, updated_at, strength) "
                    "VALUES (?, ?, 1, 1, 1.0)",
                    (content, ch),
                )
                return None
            return M._write(op)

        await asyncio.gather(*[make_op(ch, c) for ch, c in spec])

        def snapshot(c):
            return sorted(
                tuple(r) for r in c.execute(
                    "SELECT content_hash, content FROM memories ORDER BY content_hash"
                ).fetchall()
            )
        return await M._read(snapshot)

    concurrent = _run(M, tmp_path, body)
    assert concurrent == baseline, "concurrent final state diverged from serial baseline"


# ─────────────────────────────────────────────────────────────────
# 4a. A slow read does NOT block other reads or the writer.
# ─────────────────────────────────────────────────────────────────
def test_slow_read_does_not_block_others(tmp_path):
    M = _load()
    if not hasattr(M, "_init_db"):
        if pytest:
            pytest.skip(f"b12_mcp_server unavailable ({M})")
        return

    SLOW = 1.5

    async def body(M, db):
        assert M.READ_POOL_SIZE >= 2, "read pool must allow concurrent reads"

        def slow_read_op(c):
            time.sleep(SLOW)  # occupy one read-pool thread
            return c.execute("SELECT COUNT(*) FROM memories").fetchone()[0]

        def fast_read_op(c):
            return c.execute("SELECT COUNT(*) FROM memories").fetchone()[0]

        def write_op(c):
            c.execute(
                "INSERT OR IGNORE INTO memories "
                "(content, content_hash, created_at, updated_at, strength) "
                "VALUES ('x', 'while-slow-read', 1, 1, 1.0)"
            )
            return "written"

        slow = asyncio.ensure_future(M._read(slow_read_op))
        await asyncio.sleep(0.05)  # ensure the slow read has grabbed its thread

        t0 = time.monotonic()
        others = await asyncio.gather(
            *[M._read(fast_read_op) for _ in range(6)], M._write(write_op)
        )
        elapsed = time.monotonic() - t0

        assert elapsed < SLOW * 0.5, (
            f"fast reads + writer were blocked by the slow read "
            f"(elapsed {elapsed:.2f}s, slow {SLOW}s)"
        )
        assert others[-1] == "written"
        assert all(isinstance(r, int) for r in others[:-1])

        await slow  # let the slow read finish cleanly

    _run(M, tmp_path, body)


# ─────────────────────────────────────────────────────────────────
# 4b. A slow write does NOT block reads (writer is its own thread).
# ─────────────────────────────────────────────────────────────────
def test_slow_write_does_not_block_reads(tmp_path):
    M = _load()
    if not hasattr(M, "_init_db"):
        if pytest:
            pytest.skip(f"b12_mcp_server unavailable ({M})")
        return

    SLOW = 1.5

    async def body(M, db):
        def slow_write_op(c):
            time.sleep(SLOW)  # occupy the lone writer thread
            c.execute(
                "INSERT OR IGNORE INTO memories "
                "(content, content_hash, created_at, updated_at, strength) "
                "VALUES ('slow', 'slow-writer-row', 1, 1, 1.0)"
            )
            return "written"

        def fast_read_op(c):
            return c.execute("SELECT COUNT(*) FROM memories").fetchone()[0]

        slow = asyncio.ensure_future(M._write(slow_write_op))
        await asyncio.sleep(0.05)

        t0 = time.monotonic()
        reads = await asyncio.gather(*[M._read(fast_read_op) for _ in range(6)])
        elapsed = time.monotonic() - t0

        assert elapsed < SLOW * 0.5, (
            f"reads were blocked by the slow write (elapsed {elapsed:.2f}s)"
        )
        assert all(isinstance(r, int) for r in reads)
        assert await slow == "written"

    _run(M, tmp_path, body)


# ─────────────────────────────────────────────────────────────────
# 5. Atomicity on the REAL transactional handlers.
# ─────────────────────────────────────────────────────────────────
def test_real_handler_memory_store_concurrent_dedup(tmp_path):
    M = _load()
    if not hasattr(M, "_init_db"):
        if pytest:
            pytest.skip(f"b12_mcp_server unavailable ({M})")
        return

    async def body(M, db):
        content = "[decision] BB1 real-handler dedup proof — store me once"
        ch = hashlib.sha256(content.strip().lower().encode()).hexdigest()

        results = await asyncio.gather(
            *[M.memory_store(content, {"tags": "proj:bb1test", "type": "decision"})
              for _ in range(16)],
            return_exceptions=True,
        )
        excs = [r for r in results if isinstance(r, BaseException)]
        assert not excs, f"memory_store raced into an error: {excs!r}"

        def count(c):
            return c.execute(
                "SELECT COUNT(*) FROM memories WHERE content_hash = ?", (ch,)
            ).fetchone()[0]
        n = await M._read(count)
        assert n == 1, f"concurrent memory_store created {n} rows (expected 1)"

    _run(M, tmp_path, body)


def test_real_handlers_mixed_store_search_update_delete(tmp_path):
    M = _load()
    if not hasattr(M, "_init_db"):
        if pytest:
            pytest.skip(f"b12_mcp_server unavailable ({M})")
        return

    async def body(M, db):
        # Mix every handler family concurrently against the same DB.
        contents = [f"[decision] mixed-load row {i} payload" for i in range(12)]
        await asyncio.gather(*[
            M.memory_store(c, {"tags": "proj:bb1test", "type": "decision"})
            for c in contents
        ])
        hashes = [hashlib.sha256(c.strip().lower().encode()).hexdigest() for c in contents]

        tasks = []
        tasks += [M.memory_search(query="mixed-load", mode="hybrid", limit=10) for _ in range(6)]
        tasks += [M.memory_search(query="payload", mode="exact", limit=10) for _ in range(3)]
        tasks += [M.memory_update(h, {"tags": "proj:bb1test,touched"}) for h in hashes[:6]]
        tasks += [M.memory_delete(h, hard=False) for h in hashes[6:]]
        tasks += [M.memory_quality("analyze") for _ in range(3)]
        tasks += [M.memory_session_context(project_name="bb1test") for _ in range(2)]

        results = await asyncio.gather(*tasks, return_exceptions=True)
        excs = [r for r in results if isinstance(r, BaseException)]
        assert not excs, f"mixed concurrent handlers raised: {excs!r}"

        # The 6 soft-deleted rows are now invisible; the 6 updated rows remain.
        def live(c):
            rows = c.execute(
                "SELECT content_hash FROM memories "
                "WHERE deleted_at IS NULL AND content LIKE '%mixed-load row%'"
            ).fetchall()
            return {r[0] for r in rows}
        live_hashes = await M._read(live)
        for h in hashes[:6]:
            assert h in live_hashes, "an updated row went missing"
        for h in hashes[6:]:
            assert h not in live_hashes, "a soft-deleted row is still live"

    _run(M, tmp_path, body)


if __name__ == "__main__":
    import tempfile

    class _TmpPath:
        def __init__(self, p):
            self._p = p
        def __str__(self):
            return self._p

    rc = 0
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        d = tempfile.mkdtemp(prefix="bb1-")
        try:
            fn(_TmpPath(d))
            print(f"OK: {fn.__name__}")
        except AssertionError as e:
            print(f"FAIL: {fn.__name__}: {e}")
            rc = 1
        except Exception as e:
            print(f"ERROR: {fn.__name__}: {type(e).__name__}: {e}")
            rc = 1
    sys.exit(rc)
