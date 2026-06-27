#!/usr/bin/env python3
"""B12 MCP Daemon — long-running shared MCP host over Unix socket.

Hosts the FastMCP server instance in a single launchd-managed process so
multiple Claude Code sessions share one FastMCP boot (saves ~7-8s cold
start per session at 4 parallel sessions = ~28s reclaimed).

Each Claude Code session spawns the thin stdio proxy (`b12_mcp_server.py`
in proxy mode), which forwards the MCP wire protocol over this daemon's
Unix socket. If the daemon is not running, the proxy auto-falls-back to
the legacy in-process stdio server so Codex / Gemini / Kimi / OpenCode /
Grok consumers keep working without any change.

Protocol on the wire: line-delimited JSON-RPC (same bytes the MCP stdio
transport already produces — we just re-route them through a Unix socket
instead of stdin/stdout).

Sockets:
  /tmp/b12-mcp-<UID>.sock   — this daemon's listening socket
  /tmp/b12-mcp-<UID>.pid    — PID file (best-effort)

Concurrency:
  Each accepted connection runs an independent FastMCP session against
  the SHARED `server._mcp_server` instance. The module-level
  `_session_tracker` in b12_mcp_server.py is read/written without a lock;
  concurrent sessions may produce slightly skewed counts but no crashes.
  This is documented as a known limitation; per-session tracker is a
  follow-up refactor.
"""

import asyncio
import os
import signal
import sqlite3
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

# Make sibling scripts importable
_SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPTS_DIR))

import anyio
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream

import mcp.types as types
from mcp.shared.message import SessionMessage

# Importing b12_mcp_server runs module-level setup (DB_PATH, server = FastMCP(...),
# all @server.tool() registrations) but does NOT call server.run() — that only
# happens under `if __name__ == "__main__":`.
import b12_mcp_server as srv

try:
    from shared_patterns import rss_exceeds
except Exception:  # pragma: no cover — never block daemon on import
    def rss_exceeds(ceiling_mb):  # fail-open
        return 0

_UID = os.getuid() if hasattr(os, "getuid") else os.getpid()
SOCK_PATH = os.environ.get("B12_MCP_DAEMON_SOCK", f"/tmp/b12-mcp-{_UID}.sock")
PID_PATH = f"/tmp/b12-mcp-{_UID}.pid"

_B12_DATA_DIR = os.environ.get("B12_DATA_DIR", os.path.expanduser("~/.B12"))
LOG_DIR = os.path.join(_B12_DATA_DIR, "memory-logs")
LOG_PATH = os.path.join(LOG_DIR, "mcp-daemon.log")

# ── P2: idle-connection reaping + connection cap ─────────────────
# IMPORTANT (2026-06-27): idle-reaping is DISABLED BY DEFAULT (IDLE_TIMEOUT=0).
# The original premise — "a reaped connection is client-invisible because the
# host proxy reconnects on next use (Claude Code does so)" — is FALSE for Claude
# Code: when the per-session stdio proxy process exits (which it does on socket
# EOF, see b12_mcp_server.py:_run_as_proxy), Claude Code marks the MCP server
# "failed/disconnected" and does NOT auto-respawn it; the user must run /mcp to
# reconnect. So reaping a LIVE-but-idle session (no B12 tool call for 30 min — a
# routine coding stretch) silently dropped B12 mid-session. Reconnecting just
# reset the idle clock, so it dropped again. (Root cause confirmed by GPT-5.5,
# GLM-5.2 and Grok review + the daemon log's "cancelled (idle-reap)" lines.)
#
# Reaping was never needed for cleanup: a closed tab (host exit), a crashed host
# (pipe torn down) and a SIGKILL'd proxy (kernel closes the FD) ALL deliver
# socket EOF to the daemon, so handle_client completes normally and the
# connection is removed. Connections therefore track open editors 1:1 and
# self-clean. MAX_CONNECTIONS stays as an emergency backstop ONLY — note it
# evicts via the same task.cancel() path, so it too is a client-visible drop;
# keep it well above realistic concurrency. Env-overridable; set
# B12_MCP_IDLE_TIMEOUT>0 to re-enable reaping (not recommended for Claude Code).
IDLE_TIMEOUT = float(os.environ.get("B12_MCP_IDLE_TIMEOUT", "0"))         # 0 = disabled
MAX_CONNECTIONS = int(os.environ.get("B12_MCP_MAX_CONN", "256"))          # emergency backstop only
REAP_INTERVAL = float(os.environ.get("B12_MCP_REAP_INTERVAL", "60"))      # 1 min
# ── P7: periodic WAL checkpoint ──────────────────────────────────
# wal_autocheckpoint=100 only fires on writes, so an idle daemon (or a
# long-lived legacy reader) can let the WAL grow unbounded and block checkpoint.
# A periodic TRUNCATE checkpoint under the shared DB lock covers the idle case.
WAL_CHECKPOINT_INTERVAL = float(os.environ.get("B12_MCP_WAL_CHECKPOINT_INTERVAL", "300"))  # 5 min
# ── Memory self-guard ────────────────────────────────────────────
# Log + exit if peak RSS exceeds this (MB) so a runaway leak can't starve the
# host (the failure mode behind the 2026-06 OOM panic). This daemon is small
# (~tens of MB live), so the default trips only on a genuine leak; launchd
# KeepAlive respawns a fresh process. getrusage-based (works on macOS, where
# RLIMIT_AS does not). Set B12_MCP_MAX_RSS_MB<=0 to disable.
try:
    MCP_MAX_RSS_MB = int(os.environ.get("B12_MCP_MAX_RSS_MB", "2048"))
except (TypeError, ValueError):
    MCP_MAX_RSS_MB = 2048
RSS_GUARD_INTERVAL = float(os.environ.get("B12_MCP_RSS_GUARD_INTERVAL", "60"))  # 1 min


def log(msg: str) -> None:
    """Append a timestamped line to the daemon log (best-effort, never raises)."""
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(LOG_PATH, "a") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [{os.getpid()}] {msg}\n")
    except Exception:
        pass


def cleanup_socket_files() -> None:
    """Remove socket and PID files. Safe to call multiple times."""
    for path in (SOCK_PATH, PID_PATH):
        try:
            os.unlink(path)
        except OSError:
            pass


@asynccontextmanager
async def _socket_streams(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, on_activity=None):
    """Bridge an asyncio (reader, writer) pair to anyio memory streams that
    speak `SessionMessage`. Mirrors `mcp.server.stdio.stdio_server` but over a
    Unix socket instead of stdin/stdout.

    Yields (read_stream, write_stream) suitable for `Server.run(...)`.
    """
    in_send: MemoryObjectSendStream[SessionMessage | Exception]
    in_recv: MemoryObjectReceiveStream[SessionMessage | Exception]
    out_send: MemoryObjectSendStream[SessionMessage]
    out_recv: MemoryObjectReceiveStream[SessionMessage]

    in_send, in_recv = anyio.create_memory_object_stream(0)
    out_send, out_recv = anyio.create_memory_object_stream(0)

    async def socket_reader() -> None:
        """Read newline-delimited JSON from socket, wrap as SessionMessage."""
        try:
            async with in_send:
                while True:
                    line = await reader.readline()
                    if not line:
                        return  # peer closed
                    # P2: mark inbound activity so the idle reaper doesn't cancel
                    # a connection that is still actively serving requests.
                    if on_activity is not None:
                        try:
                            on_activity()
                        except Exception:
                            pass
                    try:
                        msg = types.JSONRPCMessage.model_validate_json(
                            line.decode("utf-8").rstrip()
                        )
                        await in_send.send(SessionMessage(message=msg))
                    except Exception as exc:
                        await in_send.send(exc)
        except anyio.ClosedResourceError:
            pass
        except Exception as e:
            log(f"socket_reader error: {e}")

    async def socket_writer() -> None:
        """Read SessionMessages from out_recv, serialize to socket as JSON lines."""
        try:
            async with out_recv:
                async for sm in out_recv:
                    line = (
                        sm.message.model_dump_json(by_alias=True, exclude_none=True)
                        + "\n"
                    )
                    writer.write(line.encode("utf-8"))
                    await writer.drain()
        except (anyio.ClosedResourceError, ConnectionResetError, BrokenPipeError):
            pass
        except Exception as e:
            log(f"socket_writer error: {e}")

    async with anyio.create_task_group() as tg:
        tg.start_soon(socket_reader)
        tg.start_soon(socket_writer)
        try:
            yield in_recv, out_send
        finally:
            # Cancel the bridging tasks once the session ends
            tg.cancel_scope.cancel()


_active_connections = 0
_conn_seq = 0
# conn_id -> {"task": asyncio.Task, "last_activity": float, "writer": StreamWriter}
_connections: dict[int, dict] = {}


def _enforce_conn_cap(current_id: int) -> None:
    """P2: if over MAX_CONNECTIONS, cancel the most-idle connection(s) — never
    the one being accepted right now. A defensive bound against runaway
    accumulation; the reaped client's host respawns its proxy on next use."""
    if MAX_CONNECTIONS <= 0:
        return
    while len(_connections) > MAX_CONNECTIONS:
        now = time.time()
        victim_id = None
        victim_idle = -1.0
        for cid, rec in _connections.items():
            if cid == current_id:
                continue
            idle = now - rec.get("last_activity", now)
            if idle > victim_idle:
                victim_idle = idle
                victim_id = cid
        if victim_id is None:
            break
        rec = _connections.pop(victim_id, None)  # remove now so the loop terminates
        task = rec.get("task") if rec else None
        log(f"Connection #{victim_id} evicted (over cap {MAX_CONNECTIONS}, idle {victim_idle:.0f}s)")
        if task is not None and not task.done():
            task.cancel()


async def _reap_idle_connections() -> None:
    """P2: periodically cancel connections idle longer than IDLE_TIMEOUT.
    DISABLED by default (IDLE_TIMEOUT=0) — reaping a live-but-idle session is NOT
    client-invisible: the proxy exits on socket close and Claude Code does not
    auto-respawn it, so the user sees B12 drop until a manual /mcp reconnect.
    See the IDLE_TIMEOUT block above for the full rationale."""
    if IDLE_TIMEOUT <= 0:
        return
    while True:
        try:
            await asyncio.sleep(REAP_INTERVAL)
            now = time.time()
            for cid, rec in list(_connections.items()):
                idle = now - rec.get("last_activity", now)
                if idle > IDLE_TIMEOUT:
                    task = rec.get("task")
                    if task is not None and not task.done():
                        log(f"Connection #{cid} idle {idle:.0f}s > {IDLE_TIMEOUT:.0f}s — reaping")
                        task.cancel()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log(f"reaper error: {e}")


def _checkpoint_wal_blocking() -> tuple:
    """Run a TRUNCATE checkpoint on a SHORT-LIVED dedicated connection. Runs in a
    worker thread (see _wal_checkpoint_timer), so it owns its own connection and
    must NOT touch the server's read/writer connections (each pinned to another
    thread) or the event loop — the same per-connection ownership model the
    server's read pool / single-writer thread use. A short busy_timeout bounds the
    wait if a reader/writer is contending; on contention the PRAGMA returns busy=1
    (logged) and we retry next interval. Returns the
    (busy, log_frames, checkpointed_frames) row, or () on no-extension path."""
    cx = sqlite3.connect(srv.DB_PATH, timeout=5)
    try:
        cx.execute("PRAGMA busy_timeout=2000")  # bounded — off the event loop
        row = cx.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        cx.commit()
        return tuple(row) if row is not None else ()
    finally:
        cx.close()


async def _wal_checkpoint_timer() -> None:
    """P7: periodically checkpoint the WAL so an idle daemon (or a long-lived
    reader) can't let it grow unbounded (wal_autocheckpoint=100 only fires on
    writes). Runs the checkpoint OFF the event loop on its own connection: a
    TRUNCATE checkpoint can wait on a concurrent reader up to busy_timeout, so
    doing it synchronously on the event loop (as the first cut did) would freeze
    every MCP client for that whole window. The worker thread keeps
    the daemon responsive; a contended cycle just bails and retries.
    (Addresses PR #108 review r3441309261.)"""
    if WAL_CHECKPOINT_INTERVAL <= 0:
        return
    while True:
        try:
            await asyncio.sleep(WAL_CHECKPOINT_INTERVAL)
            row = await asyncio.to_thread(_checkpoint_wal_blocking)
            # row == (busy, log, checkpointed); busy != 0 means a reader/writer
            # blocked a full truncate — harmless, the WAL is reused regardless.
            log(f"WAL checkpoint (TRUNCATE, off-loop) done {row}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log(f"WAL checkpoint error: {e}")


async def _rss_self_guard_timer() -> None:
    """Periodically check peak RSS; if it exceeds MCP_MAX_RSS_MB, log and exit
    so launchd respawns a fresh daemon. A memory-runaway emergency — os._exit
    is intentional (WAL is durable; the periodic checkpoint timer keeps it
    truncated), avoiding a hang while the host's RAM is exhausted."""
    if MCP_MAX_RSS_MB <= 0 or RSS_GUARD_INTERVAL <= 0:
        return
    while True:
        try:
            await asyncio.sleep(RSS_GUARD_INTERVAL)
            over = rss_exceeds(MCP_MAX_RSS_MB)
            if over:
                log(f"RSS {over}MB > ceiling {MCP_MAX_RSS_MB}MB "
                    f"(B12_MCP_MAX_RSS_MB) — exiting to protect host; launchd respawns")
                # os._exit skips atexit + the lifespan finally, so flush the
                # (global) session tracker best-effort first — otherwise an
                # MCP-only-platform session summary is lost on this emergency exit
                # (audit #12). _atexit_flush resolves the active tracker via the
                # contextvar (set by lifespan, inherited by this task), so it
                # persists the daemon's real session summary. Best-effort: bounded
                # by the flush conn's 5s busy_timeout (can delay this emergency exit
                # up to ~5s under lock contention), never raises.
                try:
                    srv._atexit_flush()
                except Exception:
                    pass
                cleanup_socket_files()
                os._exit(1)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log(f"rss guard error: {e}")


async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Serve one MCP session for one connected client (one Claude Code session)."""
    global _active_connections, _conn_seq
    _conn_seq += 1
    conn_id = _conn_seq
    _active_connections += 1
    record = {"task": asyncio.current_task(), "last_activity": time.time(), "writer": writer}
    _connections[conn_id] = record
    log(f"Connection #{conn_id} accepted (active={_active_connections})")
    _enforce_conn_cap(conn_id)

    def _bump_activity() -> None:
        record["last_activity"] = time.time()

    try:
        async with _socket_streams(reader, writer, on_activity=_bump_activity) as (rs, ws):
            init_opts = srv.server._mcp_server.create_initialization_options()
            await srv.server._mcp_server.run(rs, ws, init_opts)
        log(f"Connection #{conn_id} completed normally")
    except (ConnectionResetError, BrokenPipeError):
        log(f"Connection #{conn_id} reset by peer")
    except asyncio.CancelledError:
        log(f"Connection #{conn_id} cancelled (idle-reap or daemon shutdown)")
        raise
    except Exception as e:
        log(f"Connection #{conn_id} error: {type(e).__name__}: {e}")
    finally:
        _active_connections -= 1
        _connections.pop(conn_id, None)
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


_main_task: asyncio.Task | None = None


def _install_signal_handlers() -> None:
    """Install POSIX signal handlers that cancel the main task for clean shutdown."""
    loop = asyncio.get_running_loop()

    def _on_signal(signum: int) -> None:
        log(f"Signal {signum} received, initiating shutdown")
        if _main_task is not None and not _main_task.done():
            _main_task.cancel()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _on_signal, sig)
        except NotImplementedError:
            # add_signal_handler not supported on Windows; fallback
            signal.signal(sig, lambda s, f: _on_signal(s))

    # Survive shell exits even without disown
    try:
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
    except (AttributeError, ValueError):
        pass


async def _serve() -> None:
    """Bind the Unix socket and serve forever (until cancelled)."""
    # Clean up stale socket from prior unclean shutdown
    if os.path.exists(SOCK_PATH):
        os.unlink(SOCK_PATH)

    # Write PID
    try:
        with open(PID_PATH, "w") as f:
            f.write(str(os.getpid()))
    except OSError as e:
        log(f"PID write failed: {e}")

    log(f"Starting B12 MCP daemon (PID {os.getpid()}, socket {SOCK_PATH})")

    # Tell b12_mcp_server.lifespan to keep the DB pools (read pool + single
    # writer) warm across client disconnects — without this flag, each
    # per-connection lifespan exit would tear the pools down, breaking the next
    # client.
    srv._DAEMON_MODE = True

    # Initialize DB + FastMCP lifespan ONCE — this is the cold-start cost we're amortizing.
    # The async generator from lifespan() needs to stay open for the daemon's lifetime.
    async with srv.lifespan(srv.server) as _state:
        log("FastMCP lifespan initialized; accepting connections")
        server = await asyncio.start_unix_server(handle_client, path=SOCK_PATH)
        try:
            os.chmod(SOCK_PATH, 0o600)
        except OSError:
            pass

        # P2 + P7: background maintenance — idle-connection reaper + WAL
        # checkpoint timer. Cancelled cleanly on daemon shutdown below.
        _maint_tasks = [
            asyncio.create_task(_reap_idle_connections()),
            asyncio.create_task(_wal_checkpoint_timer()),
            asyncio.create_task(_rss_self_guard_timer()),
        ]

        log(f"Listening on {SOCK_PATH}")
        try:
            async with server:
                await server.serve_forever()
        except asyncio.CancelledError:
            log("serve_forever cancelled, exiting")
            raise
        finally:
            for _t in _maint_tasks:
                _t.cancel()
            for _t in _maint_tasks:
                try:
                    await _t
                except (asyncio.CancelledError, Exception):
                    pass


async def main() -> None:
    global _main_task
    _install_signal_handlers()
    _main_task = asyncio.current_task()
    try:
        await _serve()
    except asyncio.CancelledError:
        log("Daemon main task cancelled")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log("KeyboardInterrupt")
    finally:
        cleanup_socket_files()
        log("Daemon exited")
