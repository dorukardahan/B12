"""Hook smoke-test fixtures.

The hook smoke suite verifies that each B12 hook script gracefully accepts
a minimal JSON payload on stdin, exits 0 within a small timeout, and (when
it produces stdout) emits valid JSON. The suite does NOT exercise the full
B12 functionality — that requires a populated SQLite database, embedding
daemon, MCP server, and live AI host. These tests catch the most common
regressions: bash syntax bugs that only surface at runtime, unguarded
references to missing env vars, hooks that crash on a fresh install.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
HOOKS_DIR = REPO_ROOT / "hooks"


@pytest.fixture(scope="session")
def hooks_dir() -> Path:
    return HOOKS_DIR


@pytest.fixture
def isolated_b12_home(tmp_path: Path) -> Path:
    """Return a fresh $HOME with a minimal ~/.B12/ skeleton.

    The skeleton matches what install.sh would have created on a clean
    machine — empty staging / logs / summaries / hooks dirs and no
    database. Hooks should detect the absence of DB / daemon / MCP and
    degrade silently rather than crash.
    """
    b12_dir = tmp_path / ".B12"
    for sub in ("hooks", "memory-staging", "memory-logs", "memory-summaries"):
        (b12_dir / sub).mkdir(parents=True)
    return tmp_path


@pytest.fixture
def hook_env(isolated_b12_home: Path) -> dict[str, str]:
    """Return a sanitized env block suitable for running a hook under test."""
    env = os.environ.copy()
    env["HOME"] = str(isolated_b12_home)
    env["B12_DATA_DIR"] = str(isolated_b12_home / ".B12")
    # Point B12_HOOK_DIR at the real source-of-truth hooks/ dir so the
    # hook under test can source sibling helpers from the same tree it
    # ships in. This mirrors how the deployed system resolves helpers
    # (both source and deployed locations match in a real install).
    env["B12_HOOK_DIR"] = str(HOOKS_DIR)
    # Keep tests deterministic: never let an outer SESSION_ID / CWD bleed in.
    env.pop("CLAUDE_SESSION_ID", None)
    env.pop("CLAUDE_PROJECT_DIR", None)
    return env


def run_hook(
    hook_path: Path,
    payload: str,
    env: dict[str, str],
    timeout: float = 15.0,
) -> subprocess.CompletedProcess:
    """Invoke a hook with the given JSON payload on stdin."""
    return subprocess.run(
        [str(hook_path)],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
    )
