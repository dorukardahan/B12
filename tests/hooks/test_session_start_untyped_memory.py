"""SessionStart prefetch must keep memories that have no memory_type.

Two SQL patterns lost them: `memory_type != 'session_summary'` is NULL for an
untyped row (so the row is filtered out), and `'[' || memory_type || '] '` is
NULL (so the row renders as an empty line).
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOKS = REPO_ROOT / "hooks"

pytestmark = pytest.mark.skipif(shutil.which("sqlite3") is None, reason="sqlite3 CLI not installed")


def _db_path(home: Path) -> Path:
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "mcp-memory" / "sqlite_vec.db"
    return home / ".local" / "share" / "mcp-memory" / "sqlite_vec.db"


def test_untyped_project_and_universal_memories_are_prefetched(tmp_path):
    home = tmp_path / "home"
    (home / ".B12").mkdir(parents=True)
    project = tmp_path / "untypedproj"
    project.mkdir()
    db = _db_path(home)
    db.parent.mkdir(parents=True)
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE memories (id INTEGER PRIMARY KEY, content TEXT, content_hash TEXT, tags TEXT,
            memory_type TEXT, metadata TEXT, created_at REAL, updated_at REAL, created_at_iso TEXT,
            updated_at_iso TEXT, deleted_at REAL, strength REAL, valid_until TEXT);
        CREATE VIRTUAL TABLE memory_fts USING fts5(content);
        """
    )
    rows = [
        ("Untyped project note about the deploy script", "proj:untypedproj", None),
        ("Untyped universal lesson about shell pipes", "user:universal", None),
        ("Typed universal lesson", "user:universal", "learning"),
    ]
    for i, (content, tags, mtype) in enumerate(rows, 1):
        conn.execute(
            "INSERT INTO memories (id, content, content_hash, tags, memory_type, metadata, created_at)"
            " VALUES (?, ?, ?, ?, ?, '{}', strftime('%s','now'))",
            (i, content, f"h{i}", tags, mtype),
        )
    conn.commit()
    conn.close()

    # Only the hook and its helper: no scripts/ dir, so no embed daemon is spawned.
    hookroot = tmp_path / "hookroot"
    hookroot.mkdir()
    for name in ("_b12_common.sh", "memory-session-start.sh"):
        (hookroot / name).symlink_to(HOOKS / name)

    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "B12_DATA_DIR": str(home / ".B12"),
        "B12_HOOK_DIR": str(hookroot),
        "B12_EMBED_RUNTIME_DIR": str(tmp_path),
    }
    payload = json.dumps({"session_id": "untyped-1", "cwd": str(project), "hook_event_name": "SessionStart", "source": "startup"})
    result = subprocess.run(["bash", str(hookroot / "memory-session-start.sh")], input=payload, env=env,
                            cwd=str(project), capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "[general] Untyped project note about the deploy script" in context
    assert "[general] Untyped universal lesson about shell pipes" in context
    assert "[learning] Typed universal lesson" in context
