"""memory-browse.sh must show and delete memories that have no memory_type.

`' [' || memory_type || '] '` is NULL for an untyped row, so the delete preview
came back empty and the command reported "No active memory found", and the
stats line for the untyped group printed blank.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BROWSE = REPO_ROOT / "hooks" / "memory-browse.sh"

pytestmark = pytest.mark.skipif(shutil.which("sqlite3") is None, reason="sqlite3 CLI not installed")


def _db_path(home: Path) -> Path:
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "mcp-memory" / "sqlite_vec.db"
    return home / ".local" / "share" / "mcp-memory" / "sqlite_vec.db"


def _run(home: Path, *args: str, stdin: str = "") -> subprocess.CompletedProcess:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home), "B12_HOOK_DIR": str(REPO_ROOT / "hooks")}
    return subprocess.run(["bash", str(BROWSE), *args], input=stdin, env=env, capture_output=True, text=True, timeout=30)


def test_untyped_memory_is_previewed_deleted_and_counted(tmp_path):
    home = tmp_path / "home"
    db = _db_path(home)
    db.parent.mkdir(parents=True)
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE memories (id INTEGER PRIMARY KEY, content TEXT, content_hash TEXT, tags TEXT,"
        " memory_type TEXT, metadata TEXT, created_at REAL, updated_at REAL, created_at_iso TEXT,"
        " updated_at_iso TEXT, deleted_at REAL)"
    )
    conn.execute("INSERT INTO memories (id, content, content_hash, memory_type) VALUES (1, 'untyped note', 'abcdef0123456789', NULL)")
    conn.commit()
    conn.close()

    stats = _run(home, "stats")
    assert "  none: 1" in stats.stdout

    preview = _run(home, "delete", "abcdef01", stdin="y\n")
    assert preview.returncode == 0, preview.stdout + preview.stderr
    assert "abcdef0123456789 [general] untyped note" in preview.stdout
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT deleted_at IS NOT NULL FROM memories WHERE id = 1").fetchone() == (1,)
    conn.close()
