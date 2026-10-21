"""install_mcp_daemon() must hand custom runtime paths to the launchd job.

It waits for ${B12_MCP_DAEMON_SOCK:-/tmp/b12-mcp-$UID.sock}. If that variable is
set but not written into the rendered plist, the daemon binds the default
socket and the installer times out. The function is extracted from install.sh
and run with the root conftest's launchctl stub and a temp HOME.
"""
from __future__ import annotations

import os
import plistlib
import re
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALL = REPO_ROOT / "install.sh"

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="launchd plist rendering uses PlistBuddy (macOS)")


def _function(name: str) -> str:
    m = re.search(rf"\n{name}\(\)\s*\{{.*?\n\}}\n", INSTALL.read_text(), re.DOTALL)
    assert m, f"{name}() not found in install.sh"
    return m.group(0)


def _render(tmp_path: Path, extra_env: dict[str, str]) -> dict:
    home = tmp_path / "home"
    scripts = home / ".B12" / "hooks" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "b12_mcp_daemon.py").write_text("")
    venv_python = home / ".local" / "b12-venv" / "bin" / "python3"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("#!/bin/sh\n")
    venv_python.chmod(0o755)

    # A bound socket at the expected path lets the wait loop finish at once.
    sock_path = extra_env.get("B12_MCP_DAEMON_SOCK") or str(tmp_path / "default.sock")
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(sock_path)
    try:
        script = (
            'info() { :; }; warn() { echo "$*" >&2; }\n'
            f'SCRIPT_DIR="{REPO_ROOT}"; VENV_PYTHON="{venv_python}"; SCRIPT_DEST="{scripts}"\n'
            + _function("install_mcp_daemon")
            + "install_mcp_daemon\n"
        )
        env = {"HOME": str(home), "PATH": os.environ["PATH"], **extra_env}
        if "B12_MCP_DAEMON_SOCK" not in extra_env:
            env["B12_MCP_DAEMON_SOCK"] = sock_path
        result = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, timeout=60)
    finally:
        listener.close()
    assert result.returncode == 0, result.stderr
    plist = home / "Library" / "LaunchAgents" / "com.b12.mcp.daemon.plist"
    return plistlib.loads(plist.read_bytes())["EnvironmentVariables"]


@pytest.fixture
def short_dir():
    # pytest's tmp_path on macOS is too long for an AF_UNIX socket path.
    path = Path(tempfile.mkdtemp(prefix="b12s-", dir="/tmp"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


def test_custom_socket_and_runtime_dir_reach_the_job(tmp_path, short_dir):
    env = _render(tmp_path, {"B12_MCP_DAEMON_SOCK": str(short_dir / "m.sock"), "B12_EMBED_RUNTIME_DIR": str(short_dir)})
    assert env["B12_MCP_DAEMON_SOCK"] == str(short_dir / "m.sock")
    assert env["B12_EMBED_RUNTIME_DIR"] == str(short_dir)


def test_unset_runtime_dir_is_not_written(tmp_path, short_dir):
    env = _render(tmp_path, {"B12_MCP_DAEMON_SOCK": str(short_dir / "m.sock")})
    assert "B12_EMBED_RUNTIME_DIR" not in env
