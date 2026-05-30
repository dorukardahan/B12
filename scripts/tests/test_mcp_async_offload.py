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
