"""Guard for the install.sh daemon-reload-on-upgrade fix (2026-06-27 audit #2).

`./install.sh --all`/`--full` copied new daemon code to disk but never restarted
the long-lived launchd daemon, so daemon-code fixes (e.g. the #127 idle-reaper
fix) silently did NOT activate for existing installs until a manual launchctl
reload. These static checks assert the upgrade-reload wiring is present so it
can't be silently dropped (the daemon is macOS-only and absent in CI, so a
behavioral test isn't feasible on the ubuntu runner).
"""
from __future__ import annotations

import re
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALL = REPO_ROOT / "install.sh"


def _src() -> str:
    return INSTALL.read_text()


def test_reload_function_defined():
    src = _src()
    assert "reload_daemon_if_running()" in src, "reload_daemon_if_running not defined"


def test_reload_checks_running_daemon_and_reloads():
    src = _src()
    m = re.search(r"reload_daemon_if_running\(\)\s*\{(.*?)\n\}", src, re.DOTALL)
    assert m, "reload_daemon_if_running body not found"
    body = m.group(1)
    assert 'launchctl list' in body and "com.b12.mcp.daemon" in body, "reload must gate on a running daemon"
    assert "install_mcp_daemon" in body, "reload must call install_mcp_daemon (re-render plist + unload/load)"
    # macOS-only gate — CI is ubuntu so the body never runs there; a static test is
    # the only thing guarding the Darwin check from being silently deleted.
    assert "uname" in body and "Darwin" in body, "reload must early-return on non-macOS"
    # PR #129 P1: must preserve a custom B12_DATA_DIR from the existing plist so a
    # bare upgrade doesn't re-point the daemon to the default ~/.B12 DB.
    assert "B12_DATA_DIR" in body and "PlistBuddy" in body, "reload must preserve a custom B12_DATA_DIR (PR #129 P1)"
    # PR #129 P2: must propagate install_mcp_daemon's exit status (the trailing
    # echo would otherwise mask it, so `|| warn` never fires).
    assert "return $_rc" in body, "reload must propagate install_mcp_daemon's exit status (PR #129 P2)"


def test_upgrade_path_calls_reload():
    src = _src()
    # copy_scripts runs for EVERY invocation, so the reload must fire for any
    # script-copying install EXCEPT the explicit --daemon / --daemon-uninstall
    # paths — not be restricted to --all/--full (Codex review on PR #129).
    m = re.search(r"if ! \$INSTALL_DAEMON && ! \$UNINSTALL_DAEMON.*?\nfi", src, re.DOTALL)
    assert m, "reload not invoked under `if ! $INSTALL_DAEMON && ! $UNINSTALL_DAEMON`"
    block = m.group(0)
    assert "reload_daemon_if_running" in block, "guard present but reload not called inside it"
    # Must NOT be narrowed back to only --all/--full (would miss --codex/bare installs).
    assert "$INSTALL_ALL || $FULL_SETUP" not in block, "reload gate wrongly restricted to --all/--full"


def test_reload_called_after_copy_scripts():
    src = _src()
    # The reload must run AFTER copy_scripts (which writes the new daemon code to
    # disk); reloading before the copy would preserve the very bug this fixes.
    copy_at = src.index("\ncopy_scripts\n")
    call_at = src.index("reload_daemon_if_running || warn")
    assert copy_at < call_at, "reload must be invoked after copy_scripts deploys the new code"


def test_embed_daemon_upgrade_restart_is_pid_and_command_guarded():
    src = _src()
    m = re.search(r"restart_embed_daemon_if_running\(\)\s*\{(.*?)\n\}", src, re.DOTALL)
    assert m, "restart_embed_daemon_if_running body not found"
    body = m.group(1)
    assert "B12_EMBED_RUNTIME_DIR" in body and "b12-embed-" in body and ".pid" in body
    assert "kill -0" in body, "restart must require a live PID"
    assert "ps -p" in body and "embed_daemon.py" in body, "restart must reject PID reuse"
    assert "kill -TERM" in body, "verified embed daemon must receive a graceful TERM"


def test_embed_daemon_restart_runs_after_support_script_copy():
    src = _src()
    copy_at = src.index("\ncopy_scripts\n")
    restart_at = src.index("restart_embed_daemon_if_running || warn")
    assert copy_at < restart_at, "embed daemon restart must happen after new code is deployed"


def _invoke_embed_restart(runtime: Path) -> subprocess.CompletedProcess[str]:
    src = _src()
    m = re.search(r"restart_embed_daemon_if_running\(\)\s*\{(.*?)\n\}", src, re.DOTALL)
    assert m
    function = "restart_embed_daemon_if_running() {" + m.group(1) + "\n}"
    return subprocess.run(
        ["bash", "-c", f"info() {{ :; }}\n{function}\nrestart_embed_daemon_if_running"],
        env=os.environ | {"B12_EMBED_RUNTIME_DIR": str(runtime)},
        text=True,
        capture_output=True,
        timeout=5,
    )


def test_embed_daemon_upgrade_restart_terminates_verified_process(tmp_path: Path):
    if shutil.which("bash") is None or not hasattr(os, "getuid"):
        return
    daemon = tmp_path / "embed_daemon.py"
    daemon.write_text("import time\ntime.sleep(30)\n")
    proc = subprocess.Popen([sys.executable, str(daemon)])
    (tmp_path / f"b12-embed-{os.getuid()}.pid").write_text(str(proc.pid))
    try:
        result = _invoke_embed_restart(tmp_path)
        assert result.returncode == 0, result.stderr
        proc.wait(timeout=3)
        assert proc.returncode is not None
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=3)


def test_embed_daemon_upgrade_restart_rejects_reused_pid(tmp_path: Path):
    if shutil.which("bash") is None or not hasattr(os, "getuid"):
        return
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    (tmp_path / f"b12-embed-{os.getuid()}.pid").write_text(str(proc.pid))
    try:
        result = _invoke_embed_restart(tmp_path)
        assert result.returncode == 0, result.stderr
        assert proc.poll() is None, "unrelated process was terminated through a stale PID file"
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=3)
