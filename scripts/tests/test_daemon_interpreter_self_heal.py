"""Regression coverage for daemon interpreter self-healing and shutdown latency.

The subprocess tests launch the real daemon entry points with a tiny fake embedding
model / MCP server.  No user database, Homebrew install, or network access is used.
"""
from __future__ import annotations

import asyncio
import fcntl
import json
import os
import plistlib
import shlex
import signal
import socket
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import cast

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import b12_health
import b12_mcp_daemon
import embed_daemon
import shared_patterns


def _wait_for_path(path: Path, proc: subprocess.Popen, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        if proc.poll() is not None:
            stdout, stderr = proc.communicate()
            raise AssertionError(
                f"daemon exited before creating {path} (rc={proc.returncode})\n"
                f"stdout={stdout}\nstderr={stderr}"
            )
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {path}")


def _wait_for_exit(proc: subprocess.Popen, timeout: float) -> float:
    started = time.monotonic()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        raise AssertionError(f"daemon did not exit within {timeout:.1f}s")
    return time.monotonic() - started


def _stop_process(proc: subprocess.Popen, socket_path: Path | None = None) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    # Old embed-daemon behavior only notices SIGTERM after accept() wakes.  Nudge
    # the socket so a RED test never leaks a process for sixty seconds.
    if socket_path and socket_path.exists():
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(0.2)
                client.connect(str(socket_path))
                client.sendall(b'{"op":"health"}\n')
        except OSError:
            pass
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=2)


def _fake_sentence_transformers(tmp_path: Path) -> Path:
    package_root = tmp_path / "fake-modules"
    package = package_root / "sentence_transformers"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(
        textwrap.dedent(
            """
            class SentenceTransformer:
                def __init__(self, *args, **kwargs):
                    pass

                def encode(self, texts, normalize_embeddings=True):
                    return [[0.0, 1.0, 0.0] for _ in texts]
            """
        ),
        encoding="utf-8",
    )
    return package_root


def _start_embed_daemon(tmp_path: Path, python: Path) -> tuple[subprocess.Popen, Path, Path, Path]:
    runtime = tmp_path / "runtime"
    data = tmp_path / "data"
    runtime.mkdir()
    fake_modules = _fake_sentence_transformers(tmp_path)
    env = os.environ.copy()
    env.update(
        {
            "B12_EMBED_RUNTIME_DIR": str(runtime),
            "B12_DATA_DIR": str(data),
            "PYTHONPATH": str(fake_modules),
            "B12_INTERPRETER_CHECK_INTERVAL": "0.10",
        }
    )
    uid = os.getuid() if hasattr(os, "getuid") else os.getpid()
    sock = runtime / f"b12-embed-{uid}.sock"
    pid_file = runtime / f"b12-embed-{uid}.pid"
    log_file = data / "memory-logs" / "embed-daemon.log"
    proc = subprocess.Popen(
        [str(python), str(SCRIPTS / "embed_daemon.py")],
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    _wait_for_path(sock, proc)
    return proc, sock, pid_file, log_file


def test_embed_daemon_sigterm_exits_promptly_and_cleans_runtime_files(tmp_path: Path) -> None:
    proc, sock, pid_file, _ = _start_embed_daemon(tmp_path, Path(sys.executable))
    try:
        assert pid_file.exists()
        proc.send_signal(signal.SIGTERM)
        elapsed = _wait_for_exit(proc, timeout=3.0)
        assert proc.returncode == 0
        assert elapsed < 2.0, f"SIGTERM shutdown took {elapsed:.3f}s"
        assert not sock.exists()
        assert not pid_file.exists()
    finally:
        _stop_process(proc, sock)


def test_embed_daemon_deleted_executable_exits_cleanly_after_response(tmp_path: Path) -> None:
    stale_python = tmp_path / "python-stale"
    stale_python.symlink_to(sys.executable)
    proc, sock, pid_file, log_file = _start_embed_daemon(tmp_path, stale_python)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(1)
            client.connect(str(sock))
            client.sendall(b'{"op":"health"}\n')
            response = client.recv(4096)
        assert json.loads(response.decode())["ok"] is True

        stale_python.unlink()
        _wait_for_exit(proc, timeout=3.0)
        assert proc.returncode == 0
        assert not sock.exists()
        assert not pid_file.exists()
        assert "interpreter executable missing" in log_file.read_text(encoding="utf-8")
    finally:
        _stop_process(proc, sock)


def test_embed_daemon_detects_deleted_mapped_binary_when_launcher_still_exists(
    tmp_path: Path, monkeypatch
) -> None:
    launcher = tmp_path / "venv" / "bin" / "python3"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("stable launcher", encoding="utf-8")
    missing_runtime = tmp_path / "Cellar" / "python@3.14" / "3.14.6" / "Python"
    running = [True]
    monkeypatch.setattr(embed_daemon.sys, "executable", str(launcher))
    monkeypatch.setattr(
        embed_daemon, "process_executable_path", lambda: str(missing_runtime)
    )
    monkeypatch.setattr(embed_daemon, "_stale_interpreter_logged", False)

    assert embed_daemon._request_restart_for_stale_interpreter(running) is True
    assert running == [False]


def test_mcp_daemon_detects_deleted_mapped_binary_when_launcher_still_exists(
    tmp_path: Path, monkeypatch
) -> None:
    launcher = tmp_path / "venv" / "bin" / "python3"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("stable launcher", encoding="utf-8")
    missing_runtime = tmp_path / "Cellar" / "python@3.14" / "3.14.6" / "Python"
    monkeypatch.setattr(b12_mcp_daemon.sys, "executable", str(launcher))
    monkeypatch.setattr(
        b12_mcp_daemon, "process_executable_path", lambda: str(missing_runtime)
    )

    assert b12_mcp_daemon._missing_interpreter_path() == str(missing_runtime)


def test_mcp_boundary_client_is_closed_without_entering_server_during_drain(
    monkeypatch,
) -> None:
    called = {"run": False}

    class FakeWriter:
        closed = False

        def close(self) -> None:
            self.closed = True

        async def wait_closed(self) -> None:
            return None

        def write(self, data: bytes) -> None:
            pass

        async def drain(self) -> None:
            return None

    async def fake_run(*args, **kwargs) -> None:
        called["run"] = True

    mcp_server = b12_mcp_daemon._mcp_low_level_server()
    monkeypatch.setattr(mcp_server, "create_initialization_options", lambda: object())
    monkeypatch.setattr(mcp_server, "run", fake_run)
    monkeypatch.setattr(b12_mcp_daemon, "_draining_for_stale_interpreter", True)
    monkeypatch.setattr(b12_mcp_daemon, "_connections", {})
    monkeypatch.setattr(b12_mcp_daemon, "_active_connections", 0)
    writer = FakeWriter()

    async def exercise() -> None:
        await asyncio.wait_for(
            b12_mcp_daemon.handle_client(
                asyncio.StreamReader(), cast(asyncio.StreamWriter, writer)
            ),
            timeout=0.5,
        )

    asyncio.run(exercise())

    assert called["run"] is False
    assert writer.closed is True
    assert b12_mcp_daemon._connections == {}
    assert b12_mcp_daemon._active_connections == 0


def test_mcp_daemon_request_triggers_detached_embed_restart_on_connect_failure(
    monkeypatch,
) -> None:
    mcp_server = b12_mcp_daemon.srv
    ensure_calls = []

    class FailingSocket:
        def settimeout(self, timeout) -> None:
            pass

        def connect(self, path) -> None:
            raise ConnectionRefusedError(path)

        def close(self) -> None:
            pass

    monkeypatch.setattr(
        mcp_server,
        "_ensure_embed_daemon",
        lambda force=False: ensure_calls.append(force),
    )
    monkeypatch.setattr(mcp_server.socket, "socket", lambda *args, **kwargs: FailingSocket())

    assert mcp_server.daemon_request("classify", text="hello") is None
    assert ensure_calls == [True]


def test_mcp_embed_restart_launcher_is_detached_and_lock_guarded(
    tmp_path: Path, monkeypatch
) -> None:
    mcp_server = b12_mcp_daemon.srv
    spawned = []
    lock_path = tmp_path / "embed.lock"
    script_path = tmp_path / "embed_daemon.py"
    script_path.write_text("pass\n", encoding="utf-8")

    def fake_popen(argv, **kwargs):
        spawned.append((argv, kwargs))
        return object()

    monkeypatch.setattr(mcp_server, "SOCK_PATH", str(tmp_path / "missing.sock"))
    monkeypatch.setattr(mcp_server, "_EMBED_LOCK_PATH", str(lock_path))
    monkeypatch.setattr(mcp_server, "_EMBED_DAEMON_SCRIPT", str(script_path))
    monkeypatch.setattr(mcp_server, "_EMBED_PYTHON", sys.executable)
    monkeypatch.setattr(mcp_server.subprocess, "Popen", fake_popen)

    assert mcp_server._ensure_embed_daemon() is True
    assert spawned[0][0] == [sys.executable, str(script_path)]
    assert spawned[0][1]["stdin"] is subprocess.DEVNULL
    assert spawned[0][1]["stdout"] is subprocess.DEVNULL
    assert spawned[0][1]["stderr"] is subprocess.DEVNULL
    assert spawned[0][1]["close_fds"] is True
    assert spawned[0][1]["start_new_session"] is True

    spawned.clear()
    with lock_path.open("a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert mcp_server._ensure_embed_daemon() is False
    assert spawned == []


def test_embed_daemon_need_predicate_skips_keyword_only_queries() -> None:
    common = ROOT / "hooks" / "_b12_common.sh"

    def needed(mode: str, rerank: str, count: int) -> bool:
        return subprocess.run(
            [
                "bash",
                "-c",
                f". {shlex.quote(str(common))}; "
                f"b12_embed_daemon_needed {shlex.quote(mode)} {shlex.quote(rerank)} {count}",
            ],
            check=False,
            capture_output=True,
            text=True,
        ).returncode == 0

    assert needed("keyword", "false", 2) is False
    assert needed("hybrid", "false", 2) is True
    assert needed("keyword", "true", 2) is True
    hook = (ROOT / "hooks" / "memory-retrieval.sh").read_text(encoding="utf-8")
    assert 'b12_embed_daemon_needed "$QUERY_MODE" "$SHOULD_RERANK" "$RESULT_COUNT"' in hook


def test_mcp_daemon_deleted_executable_drains_inflight_request_before_exit(tmp_path: Path) -> None:
    stale_python = tmp_path / "python-stale"
    stale_python.symlink_to(sys.executable)
    sock = tmp_path / "mcp.sock"
    marker = tmp_path / "request-started"
    log_file = tmp_path / "mcp-daemon.log"

    harness = tmp_path / "run_mcp_harness.py"
    harness.write_text(
        textwrap.dedent(
            f"""
            import os, sys, types
            from contextlib import asynccontextmanager
            import anyio

            class FakeLowLevelServer:
                def create_initialization_options(self):
                    return None

                async def run(self, read_stream, write_stream, initialization_options):
                    async with write_stream:
                        async for _message in read_stream:
                            open({str(marker)!r}, "w").write("started")
                            await anyio.sleep(0.8)
                            from mcp import types
                            from mcp.shared.message import SessionMessage
                            request = getattr(_message.message, "root", _message.message)
                            response = types.JSONRPCResponse(
                                jsonrpc="2.0", id=request.id, result={{}}
                            )
                            try:
                                response = types.JSONRPCMessage(root=response)
                            except TypeError:
                                pass
                            await write_stream.send(SessionMessage(message=response))

            fake = types.ModuleType("b12_mcp_server")
            fake.server = types.SimpleNamespace(_mcp_server=FakeLowLevelServer())
            fake._DAEMON_MODE = False
            @asynccontextmanager
            async def lifespan(server):
                yield None
            fake.lifespan = lifespan
            sys.modules["b12_mcp_server"] = fake
            sys.path.insert(0, {str(SCRIPTS)!r})
            import b12_mcp_daemon as daemon
            daemon.SOCK_PATH = {str(sock)!r}
            daemon.PID_PATH = {str(tmp_path / 'mcp.pid')!r}
            daemon.LOG_PATH = {str(log_file)!r}
            daemon.LOG_DIR = {str(tmp_path)!r}
            anyio.run(daemon.main)
            """
        ),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["B12_INTERPRETER_CHECK_INTERVAL"] = "0.10"
    proc = subprocess.Popen(
        [str(stale_python), str(harness)],
        env={**env, "PYTHONPATH": os.pathsep.join(sys.path)},
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    _wait_for_path(sock, proc)
    client: socket.socket | None = None
    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(1)
        client.connect(str(sock))
        client.sendall(b'{"jsonrpc":"2.0","id":1,"method":"ping"}\n')
        _wait_for_path(marker, proc)

        stale_python.unlink()
        time.sleep(0.25)
        assert proc.poll() is None, "daemon exited while an accepted request was still running"
        client.sendall(b'{"jsonrpc":"2.0","id":2,"method":"ping"}\n')

        _wait_for_exit(proc, timeout=3.0)
        assert proc.returncode == 0
        response_lines = client.recv(4096).decode().splitlines()
        assert [json.loads(line) for line in response_lines] == [
            {"jsonrpc": "2.0", "id": 1, "result": {}}
        ]
        assert "interpreter executable missing" in log_file.read_text(encoding="utf-8")
        assert "deferred request 2" in log_file.read_text(encoding="utf-8")
    finally:
        if client is not None:
            client.close()
        _stop_process(proc, sock)


def test_launchd_template_respawns_clean_daemon_exit() -> None:
    with (ROOT / "config" / "com.b12.mcp.daemon.plist").open("rb") as stream:
        plist = plistlib.load(stream)
    assert plist["KeepAlive"] is True


def test_retrieval_hook_starts_embed_daemon_on_demand(tmp_path: Path) -> None:
    home = tmp_path / "home"
    hook_root = tmp_path / "hooks"
    data = tmp_path / "data"
    data.mkdir()
    python = home / ".local" / "b12-venv" / "bin" / "python3"
    daemon_script = hook_root / "scripts" / "embed_daemon.py"
    python.parent.mkdir(parents=True)
    daemon_script.parent.mkdir(parents=True)
    marker = data / "started"
    python.symlink_to(sys.executable)
    daemon_script.write_text(
        "from pathlib import Path\n"
        "import time\n"
        "time.sleep(1.5)\n"
        f"Path({str(marker)!r}).write_text(__file__, encoding='utf-8')\n",
        encoding="utf-8",
    )

    command = (
        f". {shlex.quote(str(ROOT / 'hooks' / '_b12_common.sh'))}; "
        "b12_ensure_embed_daemon"
    )
    started = time.monotonic()
    result = subprocess.run(
        ["bash", "-c", command],
        env={
            **os.environ,
            "HOME": str(home),
            "B12_DATA_DIR": str(data),
            "B12_HOOK_DIR": str(hook_root),
            "B12_EMBED_RUNTIME_DIR": str(tmp_path / "runtime"),
        },
        capture_output=True,
        text=True,
        timeout=3,
        check=False,
    )
    elapsed = time.monotonic() - started
    assert result.returncode == 0, result.stderr
    assert elapsed < 1.0, f"on-demand launcher blocked the hook for {elapsed:.3f}s"
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and not marker.exists():
        time.sleep(0.01)
    assert marker.read_text(encoding="utf-8").strip() == str(daemon_script)
    retrieval = (ROOT / "hooks" / "memory-retrieval.sh").read_text(encoding="utf-8")
    assert "b12_ensure_embed_daemon" in retrieval


def test_on_demand_embed_start_survives_retrieval_watchdog(tmp_path: Path) -> None:
    home = tmp_path / "home"
    hook_root = tmp_path / "hooks"
    data = tmp_path / "data"
    data.mkdir()
    python = home / ".local" / "b12-venv" / "bin" / "python3"
    daemon_script = hook_root / "scripts" / "embed_daemon.py"
    python.parent.mkdir(parents=True)
    daemon_script.parent.mkdir(parents=True)
    survived = data / "survived"
    killed = data / "killed"
    python.symlink_to(sys.executable)
    daemon_script.write_text(
        "from pathlib import Path\n"
        "import signal, sys, time\n"
        f"killed = Path({str(killed)!r})\n"
        f"survived = Path({str(survived)!r})\n"
        "def stop(signum, frame):\n"
        "    killed.write_text('killed', encoding='utf-8')\n"
        "    sys.exit(143)\n"
        "signal.signal(signal.SIGTERM, stop)\n"
        "time.sleep(1.5)\n"
        "survived.write_text('survived', encoding='utf-8')\n",
        encoding="utf-8",
    )

    command = (
        f". {shlex.quote(str(ROOT / 'hooks' / '_b12_common.sh'))}; "
        "b12_ensure_embed_daemon; "
        "b12_sync_watchdog 0.1 test-retrieval; while :; do :; done"
    )
    started = time.monotonic()
    result = subprocess.run(
        ["bash", "-c", command],
        env={
            **os.environ,
            "HOME": str(home),
            "B12_DATA_DIR": str(data),
            "B12_HOOK_DIR": str(hook_root),
            "B12_EMBED_RUNTIME_DIR": str(tmp_path / "runtime"),
        },
        capture_output=True,
        text=True,
        timeout=3,
        check=False,
    )
    elapsed = time.monotonic() - started
    assert result.returncode == 0
    assert elapsed < 1.0, f"retrieval watchdog fired too late ({elapsed:.3f}s)"
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and not survived.exists() and not killed.exists():
        time.sleep(0.01)
    assert survived.exists(), "retrieval watchdog killed the on-demand model loader"
    assert not killed.exists()


def test_on_demand_start_ignores_reused_pid_in_unlocked_stale_lock(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    hook_root = tmp_path / "hooks"
    data = tmp_path / "data"
    runtime = tmp_path / "runtime"
    data.mkdir()
    runtime.mkdir()
    python = home / ".local" / "b12-venv" / "bin" / "python3"
    daemon_script = hook_root / "scripts" / "embed_daemon.py"
    python.parent.mkdir(parents=True)
    daemon_script.parent.mkdir(parents=True)
    marker = data / "started"
    python.symlink_to(sys.executable)
    daemon_script.write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('started', encoding='utf-8')\n",
        encoding="utf-8",
    )
    lock = runtime / f"b12-embed-{os.getuid()}.lock"
    sock = runtime / f"b12-embed-{os.getuid()}.sock"
    pidfile = runtime / f"b12-embed-{os.getuid()}.pid"
    # A stale lock file can contain a PID now reused by this unrelated pytest.
    lock.write_text(str(os.getpid()), encoding="utf-8")
    pidfile.write_text(str(os.getpid()), encoding="utf-8")
    stale_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale_socket.bind(str(sock))
    stale_socket.close()

    command = (
        f". {shlex.quote(str(ROOT / 'hooks' / '_b12_common.sh'))}; "
        "b12_ensure_embed_daemon"
    )
    with lock.open("r+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        held_result = subprocess.run(
            ["bash", "-c", command],
            env={
                **os.environ,
                "HOME": str(home),
                "B12_DATA_DIR": str(data),
                "B12_HOOK_DIR": str(hook_root),
                "B12_EMBED_RUNTIME_DIR": str(runtime),
            },
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        assert held_result.returncode == 0, held_result.stderr
        time.sleep(0.1)
        assert not marker.exists(), "held singleton lock did not suppress a contender"

    result = subprocess.run(
        ["bash", "-c", command],
        env={
            **os.environ,
            "HOME": str(home),
            "B12_DATA_DIR": str(data),
            "B12_HOOK_DIR": str(hook_root),
            "B12_EMBED_RUNTIME_DIR": str(runtime),
        },
        capture_output=True,
        text=True,
        timeout=3,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline and not marker.exists():
        time.sleep(0.01)
    assert marker.exists(), "reused unrelated PID suppressed on-demand restart"


def test_health_process_scan_is_scoped_to_current_user(monkeypatch) -> None:
    seen = {}

    def missing_pid_file(self, *args, **kwargs):
        raise OSError("fixture: no pid file")

    def fake_run(args, **kwargs):
        seen["args"] = args
        return subprocess.CompletedProcess(args, 1, "", "")

    monkeypatch.setattr(b12_health.Path, "read_text", missing_pid_file)
    monkeypatch.setattr(b12_health.subprocess, "run", fake_run)

    assert b12_health._daemon_pids() == set()
    assert seen["args"][:4] == ["pgrep", "-u", str(b12_health._UID), "-f"]


@pytest.mark.parametrize(
    ("platform", "expected"),
    [
        ("darwin", "launchctl kickstart -k gui/501/com.b12.mcp.daemon"),
        ("linux", "kill -TERM 123  # restart it with your process supervisor"),
    ],
)
def test_health_mcp_restart_command_matches_the_host_platform(
    monkeypatch, platform: str, expected: str
) -> None:
    monkeypatch.setattr(b12_health.sys, "platform", platform)
    monkeypatch.setattr(b12_health, "_UID", 501)
    monkeypatch.setattr(b12_health, "_daemon_pids", lambda: {123})
    monkeypatch.setattr(
        b12_health,
        "_process_command",
        lambda pid: "/venv/bin/python /hooks/scripts/b12_mcp_daemon.py",
    )
    monkeypatch.setattr(
        b12_health, "_process_executable", lambda pid, command: "/venv/bin/python"
    )
    monkeypatch.setattr(b12_health, "_python_version", lambda executable: "3.14.7")

    rows = b12_health._running_daemon_interpreters()

    assert rows[0]["restart"] == expected


def test_health_warns_when_daemon_executable_was_deleted(tmp_path: Path, monkeypatch) -> None:
    missing = tmp_path / "Cellar" / "python@3.14" / "3.14.6" / "Python"
    monkeypatch.setattr(b12_health, "_venv_python_version", lambda: "3.14.7")
    monkeypatch.setattr(
        b12_health,
        "_running_daemon_interpreters",
        lambda: [
            {
                "name": "mcp",
                "pid": 123,
                "executable": str(missing),
                "python_version": "3.14.6",
                "restart": "launchctl kickstart -k gui/501/com.b12.mcp.daemon",
            }
        ],
    )

    result = b12_health.check_daemon_interpreters()

    assert result.status == b12_health.Status.WARN
    assert "stale interpreter — restart needed" in result.label
    assert str(missing) in result.detail
    assert "launchctl kickstart -k gui/501/com.b12.mcp.daemon" in result.detail


def test_health_prefers_live_process_binary_over_stable_argv_symlink(tmp_path: Path, monkeypatch) -> None:
    stable_venv_python = tmp_path / "venv" / "bin" / "python3"
    deleted_cellar_python = tmp_path / "Cellar" / "python@3.14" / "3.14.6" / "Python"
    stable_venv_python.parent.mkdir(parents=True)
    stable_venv_python.write_text("still present", encoding="utf-8")
    monkeypatch.setattr(
        b12_health, "process_executable_path", lambda pid: str(deleted_cellar_python)
    )

    executable = b12_health._process_executable(
        123, f"{stable_venv_python} /tmp/b12-hooks/scripts/b12_mcp_daemon.py"
    )

    assert executable == str(deleted_cellar_python)
    assert not os.path.exists(executable)


def test_process_executable_probe_does_not_use_own_runtime_for_another_pid(
    monkeypatch,
) -> None:
    monkeypatch.setattr(shared_patterns.sys, "platform", "darwin")
    monkeypatch.setattr(
        shared_patterns.ctypes,
        "CDLL",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("libproc unavailable")),
    )

    assert shared_patterns.process_executable_path(os.getpid() + 100_000) == ""


def test_process_executable_path_preserves_linux_deleted_marker(monkeypatch) -> None:
    deleted = "/opt/runtime/python3.14 (deleted)"
    monkeypatch.setattr(shared_patterns.sys, "platform", "linux")
    monkeypatch.setattr(shared_patterns.os, "readlink", lambda path: deleted)

    assert shared_patterns.process_executable_path(123) == deleted


def test_health_warns_when_running_daemon_version_differs_from_venv(tmp_path: Path, monkeypatch) -> None:
    executable = tmp_path / "python"
    executable.write_text("present", encoding="utf-8")
    monkeypatch.setattr(b12_health, "_venv_python_version", lambda: "3.14.7")
    monkeypatch.setattr(
        b12_health,
        "_running_daemon_interpreters",
        lambda: [
            {
                "name": "embed",
                "pid": 456,
                "executable": str(executable),
                "python_version": "3.14.6",
                "restart": "kill -TERM 456",
            }
        ],
    )

    result = b12_health.check_daemon_interpreters()

    assert result.status == b12_health.Status.WARN
    assert "stale interpreter — restart needed" in result.label
    assert "3.14.6" in result.detail and "3.14.7" in result.detail
    assert "kill -TERM 456" in result.detail


def test_health_stays_ok_when_running_daemons_match_venv(tmp_path: Path, monkeypatch) -> None:
    executable = tmp_path / "python"
    executable.write_text("present", encoding="utf-8")
    monkeypatch.setattr(b12_health, "_venv_python_version", lambda: "3.14.7")
    monkeypatch.setattr(
        b12_health,
        "_running_daemon_interpreters",
        lambda: [
            {
                "name": "mcp",
                "pid": 123,
                "executable": str(executable),
                "python_version": "3.14.7",
                "restart": "unused",
            },
            {
                "name": "embed",
                "pid": 456,
                "executable": str(executable),
                "python_version": "3.14.7",
                "restart": "unused",
            },
        ],
    )

    result = b12_health.check_daemon_interpreters()

    assert result.status == b12_health.Status.OK
    assert result.detail == ""


def test_daemon_interpreter_health_check_is_part_of_main_health_run() -> None:
    assert "check_daemon_interpreters" in b12_health.run_health_check.__code__.co_names
