"""Fix C — resilient MCP proxy (reconnect + initialize replay).

Unit-tests the handshake observers and drift check, and integration-tests
_proxy_session against a mock daemon Unix socket: a mid-session drop is absorbed
by a transparent reconnect + handshake replay; an in-flight request gets a
synthesized retryable error; a daemon that stays down exits within budget without
busy-looping.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import b12_mcp_server as srv  # noqa: E402

INIT_ID = 1
PROTO = "2025-06-18"


def _line(obj) -> bytes:
    return (json.dumps(obj) + "\n").encode()


def _init_req() -> bytes:
    return _line({"jsonrpc": "2.0", "id": INIT_ID, "method": "initialize",
                  "params": {"protocolVersion": PROTO, "capabilities": {},
                             "clientInfo": {"name": "t", "version": "0"}}})


def _initialized() -> bytes:
    return _line({"jsonrpc": "2.0", "method": "notifications/initialized"})


def _init_resp(caps=None) -> dict:
    return {"jsonrpc": "2.0", "id": INIT_ID,
            "result": {"protocolVersion": PROTO,
                       "capabilities": caps if caps is not None else {"tools": {}},
                       "serverInfo": {"name": "mock", "version": "1"}}}


# ── Unit: observers + drift check ──────────────────────────────────────────

def _fresh_state():
    return {"init_line": None, "init_id": None, "initialized_line": None,
            "init_response": None, "outstanding": set()}


def test_observe_client_captures_handshake_and_tracks_ids():
    st = _fresh_state()
    init = _init_req()
    srv._observe_client_line(init, st)
    assert st["init_line"] == init and st["init_id"] == INIT_ID
    assert INIT_ID in st["outstanding"]
    srv._observe_client_line(_initialized(), st)            # notification: no id
    assert st["initialized_line"] is not None
    srv._observe_client_line(_line({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                                    "params": {}}), st)
    assert 7 in st["outstanding"]


def test_observe_server_captures_init_response_and_clears_ids():
    st = _fresh_state()
    srv._observe_client_line(_init_req(), st)
    srv._observe_client_line(_line({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                                    "params": {}}), st)
    srv._observe_server_line(_line(_init_resp()), st)
    assert st["init_response"] is not None
    assert INIT_ID not in st["outstanding"]                 # init response cleared its id
    srv._observe_server_line(_line({"jsonrpc": "2.0", "id": 7, "result": {}}), st)
    assert 7 not in st["outstanding"]


def test_init_responses_compatible():
    base = _init_resp()
    assert srv._init_responses_compatible(_init_resp(), base) is True
    # different protocol version → incompatible
    drift = {"jsonrpc": "2.0", "id": INIT_ID,
             "result": {"protocolVersion": "1999-01-01", "capabilities": {"tools": {}}}}
    assert srv._init_responses_compatible(drift, base) is False
    # different capability surface → incompatible
    capdrift = _init_resp(caps={"tools": {}, "prompts": {}})
    assert srv._init_responses_compatible(capdrift, base) is False
    # missing data → don't block
    assert srv._init_responses_compatible(None, base) is True


# ── Integration harness ────────────────────────────────────────────────────

class _MockDaemon:
    """Unix-socket server emulating the B12 daemon. `script` is a list of
    per-connection behaviors (extra connections default to 'serve'):
      - 'serve': handshake, then answer every request with a result.
      - 'drop' : handshake, then close the connection.
      - 'silent': handshake, then read requests but never answer (orphans them).
    stop() force-closes any live connection so a blocked-on-read handler can't
    wedge wait_closed()."""

    def __init__(self, sock_path, script):
        self.sock_path = sock_path
        self.script = script
        self.conns = 0
        self.server = None
        self._writers: set = set()

    async def start(self):
        self.server = await asyncio.start_unix_server(self._handle, path=self.sock_path)

    async def stop(self):
        for w in list(self._writers):
            try:
                w.close()
            except Exception:
                pass
        self._writers.clear()
        if self.server is not None:
            self.server.close()
            try:
                await self.server.wait_closed()
            except Exception:
                pass
            self.server = None

    async def _handle(self, reader, writer):
        idx = self.conns
        self.conns += 1
        behavior = self.script[idx] if idx < len(self.script) else "serve"
        self._writers.add(writer)
        try:
            init = json.loads(await reader.readline())
            writer.write(_line(_init_resp() | {"id": init.get("id")}))
            await writer.drain()
            await reader.readline()  # notifications/initialized
            if behavior == "drop":
                return
            while True:
                line = await reader.readline()
                if not line:
                    break
                msg = json.loads(line)
                if behavior == "silent":
                    continue  # read the request but never answer it
                if msg.get("method") and msg.get("id") is not None:
                    writer.write(_line({"jsonrpc": "2.0", "id": msg["id"],
                                        "result": {"ok": True, "served_by": idx}}))
                    await writer.drain()
        except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError, ValueError):
            pass
        finally:
            self._writers.discard(writer)
            try:
                writer.close()
            except Exception:
                pass


def _short_sock(tag):
    """A short Unix-socket path under /tmp — AF_UNIX paths are capped near 104
    chars and pytest's tmp_path on macOS (/var/folders/...) blows that limit."""
    d = tempfile.mkdtemp(prefix="b12t", dir="/tmp")
    return os.path.join(d, f"{tag}.sock")


def _responses(out):
    msgs = []
    for b in out:
        try:
            msgs.append(json.loads(b))
        except ValueError:
            pass
    return msgs


def _with_budget(budget, coro_factory):
    """Run an async scenario with srv._RECONNECT_BUDGET_S temporarily set."""
    async def runner():
        old = srv._RECONNECT_BUDGET_S
        srv._RECONNECT_BUDGET_S = budget
        try:
            return await coro_factory()
        finally:
            srv._RECONNECT_BUDGET_S = old
    return asyncio.run(runner())


def test_reconnect_after_midsession_drop(tmp_path):
    sock = _short_sock("d")

    async def scenario():
        daemon = _MockDaemon(sock, ["drop", "serve"])
        await daemon.start()
        stdin = asyncio.StreamReader()
        out: list[bytes] = []
        task = asyncio.create_task(srv._proxy_session(stdin, out.append, sock))
        try:
            stdin.feed_data(_init_req())
            stdin.feed_data(_initialized())
            await asyncio.sleep(0.4)        # handshake on conn #1, which then drops
            await asyncio.sleep(0.6)        # proxy reconnects to conn #2
            stdin.feed_data(_line({"jsonrpc": "2.0", "id": 42, "method": "tools/call",
                                   "params": {"name": "memory_search"}}))
            await asyncio.sleep(0.6)
            stdin.feed_eof()
            await asyncio.wait_for(task, timeout=5.0)
        finally:
            await daemon.stop()
        return out, daemon.conns

    out, conns = _with_budget(5.0, scenario)
    msgs = _responses(out)
    # exactly ONE initialize response reached the client (conn #1's; conn #2's swallowed)
    init_resps = [m for m in msgs if m.get("id") == INIT_ID and "result" in m]
    assert len(init_resps) == 1, f"expected 1 init response forwarded, got {len(init_resps)}"
    # the post-reconnect tool call was answered by conn #2
    tool = [m for m in msgs if m.get("id") == 42]
    assert tool and tool[0]["result"]["served_by"] == 1, f"tool not served by reconnect: {msgs}"
    assert conns >= 2, "proxy did not reconnect"


def test_inflight_request_gets_synthesized_error(tmp_path):
    sock = _short_sock("d2")

    async def scenario():
        daemon = _MockDaemon(sock, ["silent", "serve"])
        await daemon.start()
        stdin = asyncio.StreamReader()
        out: list[bytes] = []
        task = asyncio.create_task(srv._proxy_session(stdin, out.append, sock))
        try:
            stdin.feed_data(_init_req())
            stdin.feed_data(_initialized())
            await asyncio.sleep(0.4)
            # send a tool call, then kill the daemon so the in-flight id #99 orphans.
            stdin.feed_data(_line({"jsonrpc": "2.0", "id": 99, "method": "tools/call",
                                   "params": {"name": "slow"}}))
            await asyncio.sleep(0.05)
            await daemon.stop()             # daemon gone → socket EOF, id 99 outstanding
            await asyncio.sleep(0.2)
            await daemon.start()            # daemon back → proxy reconnects
            await asyncio.sleep(0.8)
            stdin.feed_eof()
            await asyncio.wait_for(task, timeout=6.0)
        finally:
            await daemon.stop()
        return out

    out = _with_budget(5.0, scenario)
    msgs = _responses(out)
    errs = [m for m in msgs if m.get("id") == 99 and "error" in m]
    assert errs, f"no synthesized error for in-flight request: {msgs}"
    assert errs[0]["error"]["code"] == -32001


def test_budget_exhaustion_exits_without_busyloop(tmp_path):
    sock = _short_sock("d3")

    async def scenario():
        daemon = _MockDaemon(sock, ["serve"])
        await daemon.start()
        stdin = asyncio.StreamReader()
        out: list[bytes] = []
        task = asyncio.create_task(srv._proxy_session(stdin, out.append, sock))
        try:
            stdin.feed_data(_init_req())
            stdin.feed_data(_initialized())
            await asyncio.sleep(0.4)        # handshake on conn #1, then it drops
            await daemon.stop()            # daemon stays DOWN → no reconnect possible
            t0 = asyncio.get_running_loop().time()
            await asyncio.wait_for(task, timeout=4.0)   # must exit on its own (budget=1s)
            return asyncio.get_running_loop().time() - t0
        finally:
            await daemon.stop()

    elapsed = _with_budget(1.0, scenario)
    # exited after roughly the budget: not instantly (it really retried with backoff),
    # not forever (no busy-loop / hang).
    assert 0.3 <= elapsed <= 3.0, f"unexpected exit timing: {elapsed:.2f}s"
