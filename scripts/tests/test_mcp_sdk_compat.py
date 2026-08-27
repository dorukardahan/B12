"""Real startup coverage for every supported MCP SDK major.

CI installs this suite once with the latest supported v1 release and once with
v2. The tests exercise both B12 entry paths: direct stdio and the shared Unix
socket daemon used by the thin stdio proxy.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import tomllib
from importlib.metadata import version
from pathlib import Path

from packaging.requirements import Requirement
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"

_INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "b12-sdk-compat-test", "version": "1.0"},
    },
}
_INITIALIZED = {
    "jsonrpc": "2.0",
    "method": "notifications/initialized",
}


def _compat_env(tmp_path: Path) -> dict[str, str]:
    runtime = tmp_path / "runtime"
    data = tmp_path / "data"
    runtime.mkdir()
    data.mkdir()
    return {
        **os.environ,
        "B12_DATA_DIR": str(data),
        "B12_EMBED_RUNTIME_DIR": str(runtime),
        "B12_MCP_DAEMON_SOCK": str(runtime / "b12-mcp-test.sock"),
        "B12_MCP_FORCE_STDIO": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def _assert_initialize_response(payload: dict) -> None:
    assert payload["jsonrpc"] == "2.0"
    assert payload["id"] == 1
    assert "error" not in payload
    assert payload["result"]["serverInfo"]["name"] == "B12"


def test_manifest_accepts_installed_mcp_sdk() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    requirement = next(
        Requirement(item) for item in project["dependencies"] if Requirement(item).name == "mcp"
    )
    installed = Version(version("mcp"))
    assert installed in requirement.specifier, (
        f"installed MCP SDK {installed} is outside the published requirement "
        f"{requirement.specifier}"
    )


def test_real_stdio_server_initializes_with_installed_mcp_sdk(tmp_path: Path) -> None:
    env = _compat_env(tmp_path)
    wire = "\n".join((json.dumps(_INITIALIZE), json.dumps(_INITIALIZED), ""))
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "b12_mcp_server.py")],
        input=wire,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    responses = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    assert len(responses) == 1, (responses, result.stderr)
    _assert_initialize_response(responses[0])


def test_real_shared_daemon_initializes_with_installed_mcp_sdk(tmp_path: Path) -> None:
    env = _compat_env(tmp_path)
    socket_path = Path(env["B12_MCP_DAEMON_SOCK"])
    proc = subprocess.Popen(
        [sys.executable, str(SCRIPTS / "b12_mcp_daemon.py")],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    client: socket.socket | None = None
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not socket_path.exists():
            if proc.poll() is not None:
                stdout, stderr = proc.communicate()
                raise AssertionError(
                    f"daemon exited before binding its socket (rc={proc.returncode})\n"
                    f"stdout={stdout}\nstderr={stderr}"
                )
            time.sleep(0.02)
        assert socket_path.exists(), "daemon did not bind its socket"

        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(10)
        client.connect(str(socket_path))
        client.sendall((json.dumps(_INITIALIZE) + "\n").encode())

        response = b""
        while b"\n" not in response:
            chunk = client.recv(65536)
            assert chunk, "daemon closed the connection before initialize response"
            response += chunk
        _assert_initialize_response(json.loads(response.splitlines()[0]))
    finally:
        if client is not None:
            client.close()
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
