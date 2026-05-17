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

_UID = os.getuid() if hasattr(os, "getuid") else os.getpid()
SOCK_PATH = os.environ.get("B12_MCP_DAEMON_SOCK", f"/tmp/b12-mcp-{_UID}.sock")
PID_PATH = f"/tmp/b12-mcp-{_UID}.pid"

_B12_DATA_DIR = os.environ.get("B12_DATA_DIR", os.path.expanduser("~/.B12"))
LOG_DIR = os.path.join(_B12_DATA_DIR, "memory-logs")
LOG_PATH = os.path.join(LOG_DIR, "mcp-daemon.log")


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
async def _socket_streams(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
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


async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Serve one MCP session for one connected client (one Claude Code session)."""
    global _active_connections
    _active_connections += 1
    conn_id = _active_connections
    log(f"Connection #{conn_id} accepted (active={_active_connections})")
    try:
        async with _socket_streams(reader, writer) as (rs, ws):
            init_opts = srv.server._mcp_server.create_initialization_options()
            await srv.server._mcp_server.run(rs, ws, init_opts)
        log(f"Connection #{conn_id} completed normally")
    except (ConnectionResetError, BrokenPipeError):
        log(f"Connection #{conn_id} reset by peer")
    except asyncio.CancelledError:
        log(f"Connection #{conn_id} cancelled (daemon shutdown)")
        raise
    except Exception as e:
        log(f"Connection #{conn_id} error: {type(e).__name__}: {e}")
    finally:
        _active_connections -= 1
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

    # Tell b12_mcp_server.lifespan to keep the DB connection warm across
    # client disconnects — without this flag, the per-connection lifespan's
    # exit would close `_db`, breaking the next client.
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

        log(f"Listening on {SOCK_PATH}")
        try:
            async with server:
                await server.serve_forever()
        except asyncio.CancelledError:
            log("serve_forever cancelled, exiting")
            raise


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
