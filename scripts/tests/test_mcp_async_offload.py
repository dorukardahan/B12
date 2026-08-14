"""Regression test for SPD-1: blocking daemon calls are offloaded off the loop.

`daemon_request_async` must run the blocking Unix-socket round-trip in a worker
thread (via asyncio.to_thread) so the shared FastMCP event loop is never stalled
on daemon I/O. We assert it (a) returns the wrapped result and (b) executes in a
DIFFERENT thread than the caller.

Run via:  python3 -m pytest scripts/tests/test_mcp_async_offload.py -v
      or:  python3 scripts/tests/test_mcp_async_offload.py
"""
import asyncio
import os
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def test_daemon_request_async_offloads_to_worker_thread():
    try:
        import b12_mcp_server as M
    except Exception as e:  # mcp not installed in this env
        print(f"SKIP test_daemon_request_async_offloads_to_worker_thread ({e})")
        return

    caller_thread = threading.get_ident()
    seen = {}

    def fake_daemon_request(op, **kwargs):
        seen["thread"] = threading.get_ident()
        seen["op"] = op
        seen["kwargs"] = kwargs
        return {"ok": True, "op": op}

    orig = M.daemon_request
    M.daemon_request = fake_daemon_request
    try:
        result = asyncio.run(M.daemon_request_async("classify", text="hello"))
    finally:
        M.daemon_request = orig

    assert result == {"ok": True, "op": "classify"}, "wrapper did not return the result"
    assert seen.get("op") == "classify" and seen.get("kwargs") == {"text": "hello"}
    assert seen.get("thread") is not None, "blocking fn was never called"
    assert seen["thread"] != caller_thread, "blocking call ran on the event-loop thread (not offloaded)"


def test_daemon_request_async_serializes_concurrent_calls():
    """The embed daemon is single-connection-serial, so daemon_request_async
    must let at most ONE round-trip run at a time even when many coroutines
    call it concurrently (otherwise a fast op times out behind a slow one)."""
    try:
        import b12_mcp_server as M
    except Exception as e:
        print(f"SKIP test_daemon_request_async_serializes_concurrent_calls ({e})")
        return
    import asyncio
    import threading
    import time

    state = {"active": 0, "max": 0}
    guard = threading.Lock()

    def fake_daemon_request(op, **kwargs):
        with guard:
            state["active"] += 1
            state["max"] = max(state["max"], state["active"])
        time.sleep(0.05)
        with guard:
            state["active"] -= 1
        return {"ok": True, "op": op}

    orig = M.daemon_request
    M.daemon_request = fake_daemon_request
    try:
        async def run():
            return await asyncio.gather(
                *[M.daemon_request_async("classify", text=str(i)) for i in range(5)]
            )
        results = asyncio.run(run())
    finally:
        M.daemon_request = orig

    assert all(r["ok"] for r in results) and len(results) == 5
    assert state["max"] == 1, f"expected serial daemon access (max concurrency 1), got {state['max']}"


def test_daemon_request_async_finishes_worker_on_cancellation():
    """If the caller is cancelled mid-flight, the shielded worker (and its
    in-flight socket round-trip) must still complete before the lock releases —
    a worker thread can't be cancelled, and abandoning it would let the next
    request race the single-connection daemon."""
    try:
        import b12_mcp_server as M
    except Exception as e:
        print(f"SKIP test_daemon_request_async_finishes_worker_on_cancellation ({e})")
        return
    import asyncio
    import time

    completed = []

    def slow_daemon_request(op, **kwargs):
        time.sleep(0.1)
        completed.append(op)
        return {"ok": True, "op": op}

    orig = M.daemon_request
    M.daemon_request = slow_daemon_request
    try:
        async def run():
            t = asyncio.ensure_future(M.daemon_request_async("slow", text="x"))
            await asyncio.sleep(0.02)  # let it acquire the lock + start the worker
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass
            await asyncio.sleep(0.15)  # give the shielded worker time to finish
        asyncio.run(run())
    finally:
        M.daemon_request = orig

    assert "slow" in completed, "worker was abandoned on cancellation (socket round-trip not awaited)"


def test_daemon_request_async_queue_timeout_does_not_start_worker():
    """A queue timeout returns None without starting a late daemon worker."""
    try:
        import b12_mcp_server as M
    except Exception as e:
        print(f"SKIP test_daemon_request_async_queue_timeout_does_not_start_worker ({e})")
        return

    calls = []

    def fake_daemon_request(op, **kwargs):
        calls.append((op, kwargs))
        return {"ok": True, "op": op}

    orig_request, orig_lock = M.daemon_request, M._daemon_lock
    M.daemon_request = fake_daemon_request
    M._daemon_lock = asyncio.Lock()
    try:
        async def run():
            await M._daemon_lock.acquire()
            try:
                return await asyncio.wait_for(
                    M.daemon_request_async(
                        "semantic_search", queue_timeout=0.02, query="bounded"
                    ),
                    timeout=0.2,
                )
            finally:
                if M._daemon_lock.locked():
                    M._daemon_lock.release()

        result = asyncio.run(run())
    finally:
        M.daemon_request, M._daemon_lock = orig_request, orig_lock

    assert result is None
    assert calls == [], "queue timeout started a daemon worker after fallback"


def test_daemon_request_async_without_queue_timeout_still_waits():
    """Correctness-sensitive callers (stores/embeds) preserve the old wait."""
    try:
        import b12_mcp_server as M
    except Exception as e:
        print(f"SKIP test_daemon_request_async_without_queue_timeout_still_waits ({e})")
        return

    calls = []

    def fake_daemon_request(op, **kwargs):
        calls.append((op, kwargs))
        return {"ok": True, "op": op}

    orig_request, orig_lock = M.daemon_request, M._daemon_lock
    M.daemon_request = fake_daemon_request
    M._daemon_lock = asyncio.Lock()
    try:
        async def run():
            await M._daemon_lock.acquire()
            task = asyncio.create_task(M.daemon_request_async("encode_batch", texts=["x"]))
            await asyncio.sleep(0.04)
            assert not task.done(), "unbounded caller stopped waiting for the daemon lock"
            assert calls == [], "worker started before the daemon lock was available"
            M._daemon_lock.release()
            return await asyncio.wait_for(task, timeout=0.2)

        result = asyncio.run(run())
    finally:
        M.daemon_request, M._daemon_lock = orig_request, orig_lock

    assert result == {"ok": True, "op": "encode_batch"}
    assert calls == [("encode_batch", {"texts": ["x"]})]


def test_daemon_queue_wait_propagates_release_cancel_race():
    """External cancellation wins when lock release races the queue waiter."""
    try:
        import b12_mcp_server as M
    except Exception as e:
        print(f"SKIP test_daemon_queue_wait_propagates_release_cancel_race ({e})")
        return

    calls = []

    def fake_daemon_request(op, **kwargs):
        calls.append((op, kwargs))
        return {"ok": True, "op": op}

    orig_request, orig_lock = M.daemon_request, M._daemon_lock
    M.daemon_request = fake_daemon_request
    M._daemon_lock = asyncio.Lock()
    try:
        async def run():
            await M._daemon_lock.acquire()
            task = asyncio.create_task(
                M.daemon_request_async("semantic_search", queue_timeout=1.0, query="x")
            )
            await asyncio.sleep(0)  # queue the lock acquisition
            loop = asyncio.get_running_loop()
            loop.call_soon(M._daemon_lock.release)
            loop.call_soon(task.cancel)
            try:
                await task
            except asyncio.CancelledError:
                return True
            return False

        cancelled = asyncio.run(run())
    finally:
        M.daemon_request, M._daemon_lock = orig_request, orig_lock

    assert cancelled, "release/cancel race swallowed caller cancellation"
    assert calls == [], "cancelled queue waiter started a daemon worker"


# ── PR-B: rare admin handlers offloaded off the FastMCP loop ────────────────


def test_memory_import_offloads_plain_to_thread():
    """memory_import must run the blocking importer in a worker thread (PLAIN
    asyncio.to_thread) — never on the event loop and never routed through the
    BB1 single writer. import_memories opens its own connection + BEGIN + a 5s
    backfill socket call, so it must keep its standalone db_path/input_path/
    mode/source_name signature. We assert it runs off-thread with exactly that
    plain kwarg set."""
    try:
        import b12_mcp_server as M
    except Exception as e:
        print(f"SKIP test_memory_import_offloads_plain_to_thread ({e})")
        return
    if M._import_memories is None:
        print("SKIP test_memory_import_offloads_plain_to_thread (export_import unavailable)")
        return

    caller = threading.get_ident()
    seen = {}

    class _Res:
        memories_imported = 1
        memories_skipped = 0
        edges_imported = 0
        duration_seconds = 0.0
        errors = []

    def fake_import(**kwargs):
        seen["thread"] = threading.get_ident()
        seen["kwargs"] = set(kwargs)
        return _Res()

    orig = M._import_memories
    M._import_memories = fake_import
    try:
        out = asyncio.run(M.memory_import(input_path="x.b12", mode="merge", source_name="s"))
    finally:
        M._import_memories = orig

    assert "Import complete" in out
    assert seen.get("thread") is not None, "importer was never called"
    assert seen["thread"] != caller, "import ran on the event-loop thread (not offloaded)"
    assert seen["kwargs"] == {"db_path", "input_path", "mode", "source_name"}, (
        f"importer called with a non-plain signature {sorted(seen['kwargs'])} — "
        "it must NOT be wrapped/rewired through the writer")


def test_memory_consolidate_dry_run_offloads_apply_stays_inline():
    """dry_run (read-only) is offloaded to a worker thread; the apply path
    (dry_run=False) stays synchronous on the loop (scoped-out: its own-connection
    writes would race the BB1 single writer)."""
    try:
        import b12_mcp_server as M
    except Exception as e:
        print(f"SKIP test_memory_consolidate_dry_run_offloads_apply_stays_inline ({e})")
        return
    if M._consolidate is None:
        print("SKIP test_memory_consolidate_dry_run_offloads_apply_stays_inline (engine unavailable)")
        return

    caller = threading.get_ident()

    class _R:
        memories_processed = 0
        clusters_found = 0
        memories_deduplicated = 0
        memories_merged = 0
        contradictions_flagged = 0
        dry_run_report = []

    rec = []

    def fake_consolidate(**kwargs):
        rec.append((kwargs.get("dry_run"), threading.get_ident()))
        return _R()

    orig = M._consolidate
    M._consolidate = fake_consolidate
    try:
        asyncio.run(M.memory_consolidate(dry_run=True))
        asyncio.run(M.memory_consolidate(dry_run=False))
    finally:
        M._consolidate = orig

    by = dict(rec)
    assert by.get(True) is not None and by.get(False) is not None, f"both paths must run: {rec}"
    assert by[True] != caller, "dry_run consolidate ran on the event-loop thread (not offloaded)"
    assert by[False] == caller, "apply consolidate must stay inline per the scoped-out caveat"


def test_memory_dashboard_stop_offloads_to_worker_thread():
    """memory_dashboard(action='stop') offloads the blocking _stop() (SIGTERM +
    os.kill poll loop with sleeps) to a worker thread."""
    try:
        import b12_mcp_server as M
    except Exception as e:
        print(f"SKIP test_memory_dashboard_stop_offloads_to_worker_thread ({e})")
        return
    import tempfile

    caller = threading.get_ident()
    d = tempfile.mkdtemp()
    with open(os.path.join(d, "dashboard.pid"), "w") as f:
        f.write("424242")
    old_env = os.environ.get("B12_DATA_DIR")
    os.environ["B12_DATA_DIR"] = d

    seen = {}
    state = {"sigterm": False}
    real_kill = os.kill

    def fake_kill(pid, sig):
        if sig == 0:  # existence probe
            if state["sigterm"]:
                raise ProcessLookupError()
            return None
        state["sigterm"] = True  # SIGTERM — this is the offloaded work
        seen["thread"] = threading.get_ident()
        return None

    os.kill = fake_kill
    try:
        out = asyncio.run(M.memory_dashboard(action="stop"))
    finally:
        os.kill = real_kill
        if old_env is None:
            os.environ.pop("B12_DATA_DIR", None)
        else:
            os.environ["B12_DATA_DIR"] = old_env

    assert "stopped" in out.lower(), out
    assert seen.get("thread") is not None, "_stop never sent SIGTERM"
    assert seen["thread"] != caller, "_stop ran on the event-loop thread (not offloaded)"


# ── audit #5/#6/#7: complete the PR #110 sweep — refine/export/surface ────────

def test_memory_export_offloads_to_thread():
    """memory_export must run the full-table fetchall + gzip in a worker thread
    (plain asyncio.to_thread; no _daemon_lock — no embed socket). audit #6."""
    try:
        import b12_mcp_server as M
    except Exception as e:
        print(f"SKIP ({e})"); return
    if M._export_memories is None:
        print("SKIP (export_import unavailable)"); return

    caller = threading.get_ident()
    seen = {}

    class _Res:
        memories_exported = 1; edges_exported = 0
        output_path = "/x/y.b12"; file_size_bytes = 10; duration_seconds = 0.0

    def fake_export(**kwargs):
        seen["thread"] = threading.get_ident(); seen["kwargs"] = set(kwargs)
        seen["locked"] = M._daemon_lock.locked()
        return _Res()

    orig = M._export_memories
    M._export_memories = fake_export
    try:
        out = asyncio.run(M.memory_export())
    finally:
        M._export_memories = orig
    assert "Export complete" in out
    assert seen.get("thread") is not None, "exporter never called"
    assert seen["thread"] != caller, "export ran on the event-loop thread"
    assert seen["kwargs"] == {"db_path", "output_path", "project", "tags", "after", "before"}
    assert seen.get("locked") is False, "export must NOT hold _daemon_lock (it does no embed socket I/O)"


def test_memory_refine_offloads_under_daemon_lock():
    """memory_refine must offload _refine_candidates off the loop AND hold
    _daemon_lock during the call (its embed round-trip must serialize). audit #5."""
    try:
        import b12_mcp_server as M
    except Exception as e:
        print(f"SKIP ({e})"); return
    if M._refine_candidates is None:
        print("SKIP (memory_refine unavailable)"); return

    caller = threading.get_ident()
    seen = {}

    def fake_refine(valid, threshold):
        seen["thread"] = threading.get_ident()
        seen["locked"] = M._daemon_lock.locked()
        return [{"quality_score": 0.9, "memory_type": "general", "content": "x", "group_size": 1}]

    orig = M._refine_candidates
    M._refine_candidates = fake_refine
    try:
        out = asyncio.run(M.memory_refine(candidates='[{"content":"hello world fact"}]'))
    finally:
        M._refine_candidates = orig
    assert "Refined" in out
    assert seen.get("thread") is not None, "refiner never called"
    assert seen["thread"] != caller, "refine ran on the event-loop thread"
    assert seen.get("locked") is True, "refine did not hold _daemon_lock during its embed round-trip"


def test_memory_surface_offloads_under_daemon_lock():
    """memory_surface must offload _surface off the loop AND hold _daemon_lock
    during the call (its _daemon_search socket round-trip). audit #7."""
    try:
        import b12_mcp_server as M
    except Exception as e:
        print(f"SKIP ({e})"); return
    if M._surface is None:
        print("SKIP (surfacing_engine unavailable)"); return

    caller = threading.get_ident()
    seen = {}

    class _R:
        surfaced = False; reason = "test"

    def fake_surface(trigger_type, context):
        seen["thread"] = threading.get_ident()
        seen["locked"] = M._daemon_lock.locked()
        return _R()

    orig = M._surface
    M._surface = fake_surface
    try:
        out = asyncio.run(M.memory_surface(context="db migration", trigger_type="topic"))
    finally:
        M._surface = orig
    assert "No relevant memories" in out
    assert seen.get("thread") is not None, "surface never called"
    assert seen["thread"] != caller, "surface ran on the event-loop thread"
    assert seen.get("locked") is True, "surface did not hold _daemon_lock during its socket round-trip"


def test_memory_refine_finishes_worker_on_cancellation():
    """If the caller is cancelled mid-refine, the shielded worker (its embed
    round-trip) must still complete before _daemon_lock releases — else the next
    request races the single-connection daemon (SPD-1). audit #5."""
    try:
        import b12_mcp_server as M
    except Exception as e:
        print(f"SKIP ({e})"); return
    if M._refine_candidates is None:
        print("SKIP (memory_refine unavailable)"); return
    import time
    completed = []

    def slow_refine(valid, threshold):
        time.sleep(0.1)
        completed.append(True)
        return [{"quality_score": 0.9, "memory_type": "general", "content": "x", "group_size": 1}]

    orig = M._refine_candidates
    M._refine_candidates = slow_refine
    try:
        async def run():
            t = asyncio.ensure_future(M.memory_refine(candidates='[{"content":"hello world fact"}]'))
            await asyncio.sleep(0.02)   # let it acquire the lock + start the worker
            t.cancel()
            t0 = time.monotonic()
            try:
                await t
            except asyncio.CancelledError:
                pass
            elapsed = time.monotonic() - t0
            # The distinguishing signal: with the shield + finally-await, awaiting
            # the cancelled task BLOCKS until the worker drains (~the remaining
            # sleep). The buggy bare `async with _daemon_lock: await to_thread`
            # would propagate CancelledError immediately (lock freed mid-socket),
            # returning near-instantly. So a fast return == the regression.
            assert elapsed >= 0.05, f"cancelled refine returned in {elapsed:.3f}s — worker not awaited (lock freed mid-socket)"
            assert not M._daemon_lock.locked(), "lock still held after worker finished"
        asyncio.run(run())
    finally:
        M._refine_candidates = orig
    assert completed, "refine worker was abandoned on cancellation (lock would free mid-socket)"


if __name__ == "__main__":
    rc = 0
    fns = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    for fn in fns:
        try:
            fn()
            print(f"OK: {fn.__name__}")
        except AssertionError as e:
            print(f"FAIL: {fn.__name__}: {e}")
            rc = 1
    sys.exit(rc)
