"""Keep the whole test suite away from a developer's live B12 install.

The memory DB (~/Library/Application Support/mcp-memory), ~/.B12 and
~/Library/LaunchAgents all derive from $HOME, while the launchd job domain,
the daemon sockets and PID files under /tmp and the user crontab are shared by
the whole account. Running the suite on a machine with B12 installed once
rebound the live com.b12.mcp.daemon to a pytest temp plist, stopped the live
embed daemon and wrote a session summary into the live database.

pytest_configure runs before any test module is imported, so module-level
paths (DB_PATH, socket paths) already see the isolated values:

- HOME, the embed runtime dir and the MCP daemon socket point into a private
  temp tree; inherited B12_* settings are dropped.
- No-op `launchctl` and `crontab` stubs come first on PATH, so an installer run
  can never load, unload or schedule anything for the real account.
- The Hugging Face cache keeps its real location so model files are not
  downloaded again.
"""
from __future__ import annotations

import os
import pathlib
import shutil
import tempfile

_LAUNCHCTL = """#!/bin/sh
# Test isolation stub: never touch the real launchd domain.
echo "launchctl $*" >> "{log}"
# `launchctl list <label>` reports "not loaded" so callers never reload a job.
if [ "$1" = list ] && [ -n "$2" ]; then exit 113; fi
exit 0
"""

_CRONTAB = """#!/bin/sh
# Test isolation stub: never read or replace the real user crontab.
echo "crontab $*" >> "{log}"
if [ "$1" = - ]; then cat >/dev/null; fi
exit 0
"""


_ISOLATION_BASE: pathlib.Path | None = None


def pytest_configure(config):
    global _ISOLATION_BASE
    real_home = os.path.expanduser("~")
    # Short base under /tmp keeps AF_UNIX socket paths below the 104-byte limit.
    base = pathlib.Path(tempfile.mkdtemp(prefix="b12t-", dir="/tmp" if os.path.isdir("/tmp") else None))
    _ISOLATION_BASE = base
    home, runtime, stubs = base / "home", base / "run", base / "bin"
    for path in (home, runtime, stubs):
        path.mkdir()

    log = base / "stub-calls.log"
    for name, body in (("launchctl", _LAUNCHCTL), ("crontab", _CRONTAB)):
        stub = stubs / name
        stub.write_text(body.format(log=log))
        stub.chmod(0o755)

    hf_cache = os.path.join(real_home, ".cache", "huggingface")
    if "HF_HOME" not in os.environ and os.path.isdir(hf_cache):
        os.environ["HF_HOME"] = hf_cache

    for key in [k for k in os.environ if k.startswith("B12_")]:
        del os.environ[key]
    os.environ["HOME"] = str(home)
    os.environ["B12_EMBED_RUNTIME_DIR"] = str(runtime)
    os.environ["B12_MCP_DAEMON_SOCK"] = str(runtime / "b12-mcp.sock")
    os.environ["B12_TEST_STUB_LOG"] = str(log)
    os.environ["PATH"] = f"{stubs}{os.pathsep}{os.environ.get('PATH', '')}"


def pytest_unconfigure(config):
    if _ISOLATION_BASE is not None:
        shutil.rmtree(_ISOLATION_BASE, ignore_errors=True)
