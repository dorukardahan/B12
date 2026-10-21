"""The root conftest.py must isolate the suite before any test runs."""
from __future__ import annotations

import os
import pwd
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_home_is_not_the_login_home():
    assert os.environ["HOME"] != pwd.getpwuid(os.getuid()).pw_dir


def test_runtime_paths_point_into_the_isolated_tree():
    runtime = Path(os.environ["B12_EMBED_RUNTIME_DIR"])
    assert runtime != Path("/tmp") and runtime.is_dir()
    assert Path(os.environ["B12_MCP_DAEMON_SOCK"]).parent == runtime


def test_db_path_resolves_under_the_isolated_home():
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import shared_patterns

    login_home = pwd.getpwuid(os.getuid()).pw_dir
    assert shared_patterns.get_db_path().startswith(os.environ["HOME"])
    # DB_PATH is frozen at first import, possibly under another test's
    # monkeypatched HOME; it just must never be the live database.
    assert not shared_patterns.DB_PATH.startswith(login_home + os.sep), "tests must never open the live memory DB"


def test_launchctl_and_crontab_are_stubs():
    for name in ("launchctl", "crontab"):
        found = shutil.which(name)
        assert found and Path(found).parent.parent == Path(os.environ["B12_EMBED_RUNTIME_DIR"]).parent
    result = subprocess.run(["launchctl", "list", "com.b12.mcp.daemon"], capture_output=True)
    assert result.returncode != 0
