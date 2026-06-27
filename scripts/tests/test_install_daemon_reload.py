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


def test_upgrade_path_calls_reload():
    src = _src()
    # Called on --all/--full, but not when --daemon already (re)loads it.
    m = re.search(r"\(\s*\$INSTALL_ALL\s*\|\|\s*\$FULL_SETUP\s*\).*?reload_daemon_if_running", src, re.DOTALL)
    assert m, "reload_daemon_if_running not called on the --all/--full upgrade path"
    assert "! $INSTALL_DAEMON" in m.group(0), "upgrade reload must be skipped when --daemon already handles it"


def test_reload_called_after_copy_scripts():
    src = _src()
    # The reload must run AFTER copy_scripts (which writes the new daemon code to
    # disk); reloading before the copy would preserve the very bug this fixes.
    copy_at = src.index("\ncopy_scripts\n")
    call_at = src.index("reload_daemon_if_running || warn")
    assert copy_at < call_at, "reload must be invoked after copy_scripts deploys the new code"
