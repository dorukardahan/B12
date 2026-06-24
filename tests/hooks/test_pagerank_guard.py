"""Behavioral guard tests for the SessionStart file-pagerank OOM fix.

These run the REAL `memory-session-start.sh` and assert it does NOT emit the
pagerank ("LIKELY-NEXT FILES") section when the CWD is unsafe to walk — the
$HOME / non-git / oversized-tree cases that previously made it allocate a
~112 GB matrix and panic the machine — while still emitting it for a small
git repo (so the guard doesn't over-block normal projects).

To make the guard the thing under test (not a missing interpreter), the
fixture provides a real venv-python symlink so the hook's pagerank block is
actually reached. `B12_HOOK_DIR` points at a tmp tree whose `scripts/` holds
ONLY `file_pagerank.py`, so the hook never spawns the heavy embedding daemon.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
HOOK = REPO_ROOT / "hooks" / "memory-session-start.sh"
SCRIPTS_DIR = REPO_ROOT / "scripts"
VENV_PYTHON = Path(os.path.expanduser("~/.local/b12-venv/bin/python3"))
PAGERANK_MARKER = "LIKELY-NEXT FILES (pagerank)"


def _numpy_available() -> bool:
    if not VENV_PYTHON.exists():
        return False
    try:
        return subprocess.run([str(VENV_PYTHON), "-c", "import numpy"],
                              capture_output=True, timeout=30).returncode == 0
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not VENV_PYTHON.exists(),
    reason="b12 venv python not present; pagerank block would be skipped for lack of an interpreter, not by the guard",
)


@pytest.fixture
def guard_env(tmp_path):
    home = tmp_path / "home"
    (home / ".B12").mkdir(parents=True)
    venv_bin = home / ".local" / "b12-venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "python3").symlink_to(VENV_PYTHON)

    # Only file_pagerank.py under hookroot/scripts → embed_daemon.py absent →
    # the hook's daemon-spawn guard short-circuits (no model load in tests).
    hookroot = tmp_path / "hookroot"
    (hookroot / "scripts").mkdir(parents=True)
    (hookroot / "scripts" / "file_pagerank.py").symlink_to(SCRIPTS_DIR / "file_pagerank.py")

    env = os.environ.copy()
    env["HOME"] = str(home)
    env["B12_DATA_DIR"] = str(home / ".B12")
    env["B12_HOOK_DIR"] = str(hookroot)
    env.pop("CLAUDE_SESSION_ID", None)
    env.pop("CLAUDE_PROJECT_DIR", None)
    return env, home


def _git_init(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=str(path), check=False,
                   capture_output=True)


def _run(cwd: Path, env: dict, timeout: float = 60.0) -> subprocess.CompletedProcess:
    payload = json.dumps({"source": "startup", "cwd": str(cwd), "session_id": "pg-guard"})
    return subprocess.run([str(HOOK)], input=payload, capture_output=True,
                          text=True, env=env, timeout=timeout)


def test_skips_pagerank_when_cwd_is_home(guard_env):
    """CWD == $HOME is the exact crash trigger — must never run pagerank even
    though $HOME here is a git repo full of candidate files."""
    env, home = guard_env
    _git_init(home)
    (home / "a.py").write_text("import b\n")
    (home / "b.py").write_text("x = 1\n")
    result = _run(home, env)
    assert result.returncode == 0, result.stderr[:500]
    assert PAGERANK_MARKER not in result.stdout


def test_skips_pagerank_when_not_a_git_repo(guard_env, tmp_path):
    env, _ = guard_env
    proj = tmp_path / "nogit"
    proj.mkdir()
    (proj / "a.py").write_text("import b\n")
    (proj / "b.py").write_text("x = 1\n")
    result = _run(proj, env)
    assert result.returncode == 0, result.stderr[:500]
    assert PAGERANK_MARKER not in result.stdout


def test_skips_pagerank_when_tree_exceeds_cap(guard_env, tmp_path):
    env, _ = guard_env
    env["B12_PAGERANK_MAX_NODES"] = "3"   # tiny cap so a handful of files trips it
    proj = tmp_path / "big"
    proj.mkdir()
    _git_init(proj)
    for i in range(12):
        (proj / f"m{i}.py").write_text("import os\n")
    result = _run(proj, env)
    assert result.returncode == 0, result.stderr[:500]
    assert PAGERANK_MARKER not in result.stdout


@pytest.mark.skipif(not _numpy_available(),
                    reason="numpy needed for the positive (ranking) path")
def test_runs_pagerank_on_small_git_repo(guard_env, tmp_path):
    """Guard must NOT over-block: a normal small git repo still gets ranked."""
    env, _ = guard_env
    proj = tmp_path / "small"
    proj.mkdir()
    _git_init(proj)
    (proj / "core.py").write_text("VALUE = 1\n")
    (proj / "a.py").write_text("import core\n")
    (proj / "b.py").write_text("import core\n")
    result = _run(proj, env)
    assert result.returncode == 0, result.stderr[:500]
    assert PAGERANK_MARKER in result.stdout
    assert "core.py" in result.stdout


@pytest.mark.skipif(not _numpy_available(),
                    reason="numpy needed for the positive (ranking) path")
def test_runs_pagerank_from_repo_subdirectory(guard_env, tmp_path):
    """Regression for PR #126 Codex P2: launching from a SUBDIRECTORY of a git
    repo (e.g. repo/packages/api) must still rank — the old `[ -e $CWD/.git ]`
    check only matched the repo root and wrongly skipped subdirs/worktrees."""
    env, _ = guard_env
    proj = tmp_path / "repo"
    proj.mkdir()
    _git_init(proj)
    sub = proj / "packages" / "api"
    sub.mkdir(parents=True)
    (sub / "core.py").write_text("VALUE = 1\n")
    (sub / "a.py").write_text("import core\n")
    (sub / "b.py").write_text("import core\n")
    result = _run(sub, env)   # CWD is the subdir, not the repo root
    assert result.returncode == 0, result.stderr[:500]
    assert PAGERANK_MARKER in result.stdout
    assert "core.py" in result.stdout


@pytest.mark.skipif(not _numpy_available(),
                    reason="numpy needed for the positive (ranking) path")
def test_timeout_disabled_with_zero_still_runs(guard_env, tmp_path):
    """Regression for PR #126 Codex P2: B12_PAGERANK_TIMEOUT_S=0 disables the
    wall-clock kill (documented opt-out). The bare-macOS fallback must NOT
    insta-SIGKILL the child (waited>=0); pagerank should still produce output."""
    env, _ = guard_env
    env["B12_PAGERANK_TIMEOUT_S"] = "0"
    proj = tmp_path / "notimeout"
    proj.mkdir()
    _git_init(proj)
    (proj / "core.py").write_text("VALUE = 1\n")
    (proj / "a.py").write_text("import core\n")
    result = _run(proj, env)
    assert result.returncode == 0, result.stderr[:500]
    assert PAGERANK_MARKER in result.stdout


@pytest.mark.skipif(not _numpy_available(),
                    reason="numpy needed for the positive (ranking) path")
def test_cap_disabled_with_zero_still_runs(guard_env, tmp_path):
    """Regression for PR #126 Codex P2: B12_PAGERANK_MAX_NODES=0 is the
    documented cap opt-out (honored by file_pagerank.top_n). The hook must NOT
    silently suppress pagerank in that mode — the old pre-count gate required
    `count > 0 AND count <= 0`, which can never hold, so it always skipped."""
    env, _ = guard_env
    env["B12_PAGERANK_MAX_NODES"] = "0"   # disable the cap
    proj = tmp_path / "uncapped"
    proj.mkdir()
    _git_init(proj)
    (proj / "core.py").write_text("VALUE = 1\n")
    (proj / "a.py").write_text("import core\n")
    (proj / "b.py").write_text("import core\n")
    result = _run(proj, env)
    assert result.returncode == 0, result.stderr[:500]
    assert PAGERANK_MARKER in result.stdout
    assert "core.py" in result.stdout
