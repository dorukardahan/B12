"""SessionStart shows B12 health problems without anyone running b12_health.py.

Vectorless memories and failing launchd jobs stayed silent for months because the
health check only ran on demand. A detached run writes health-snapshot.json;
SessionStart turns its open issues, or a writer that failed, into one
[B12 HEALTH] line.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOKS = REPO_ROOT / "hooks"
HOUR = 3600

pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="jq not installed")


def _age(path: Path, seconds: int) -> None:
    t = time.time() - seconds
    os.utime(path, (t, t))


def _run(tmp_path: Path, snapshot: dict | None = None, snapshot_age: int = 0,
         spawned_age: int | None = None, writer: bool = False) -> tuple[str, Path]:
    home = tmp_path / "home"
    data = home / ".B12"
    data.mkdir(parents=True)
    db = (home / "Library" / "Application Support" / "mcp-memory" / "sqlite_vec.db" if sys.platform == "darwin"
          else home / ".local" / "share" / "mcp-memory" / "sqlite_vec.db")
    db.parent.mkdir(parents=True)
    sqlite3.connect(db).executescript(
        "CREATE TABLE memories (id INTEGER PRIMARY KEY, content TEXT, content_hash TEXT, tags TEXT,"
        " memory_type TEXT, metadata TEXT, created_at REAL, updated_at REAL, created_at_iso TEXT,"
        " updated_at_iso TEXT, deleted_at REAL, strength REAL, valid_until TEXT);"
        " CREATE VIRTUAL TABLE memory_fts USING fts5(content);")
    if snapshot is not None:
        snap = data / "health-snapshot.json"
        snap.write_text(json.dumps({"ts": int(time.time()) - snapshot_age, **snapshot}))
        _age(snap, snapshot_age)
    if spawned_age is not None:
        marker = data / ".health-snapshot.spawned"
        marker.touch()
        _age(marker, spawned_age)
    hookroot = tmp_path / "hookroot"
    hookroot.mkdir()
    for name in ("_b12_common.sh", "memory-session-start.sh"):
        (hookroot / name).symlink_to(HOOKS / name)
    calls = tmp_path / "writer-calls"
    if writer:  # a stand-in venv python that records how the snapshot writer was started
        (hookroot / "scripts").mkdir()
        (hookroot / "scripts" / "b12_health.py").write_text("")
        py = home / ".local" / "b12-venv" / "bin" / "python3"
        py.parent.mkdir(parents=True)
        py.write_text(f'#!/bin/sh\ncase "$1" in *b12_health.py) echo "$@" >> "{calls}";; esac\nexit 0\n')
        py.chmod(0o755)
    project = tmp_path / "proj"
    project.mkdir()
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home), "B12_DATA_DIR": str(data),
           "B12_HOOK_DIR": str(hookroot), "B12_EMBED_RUNTIME_DIR": str(tmp_path)}
    payload = json.dumps({"session_id": "h-1", "cwd": str(project), "hook_event_name": "SessionStart",
                          "source": "startup"})
    result = subprocess.run(["bash", str(hookroot / "memory-session-start.sh")], input=payload, env=env,
                            cwd=str(project), capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"], calls


def _health_line(ctx: str) -> str | None:
    return next((l for l in ctx.splitlines() if l.startswith("[B12 HEALTH]")), None)


def test_open_issues_become_one_health_line(tmp_path):
    ctx, _ = _run(tmp_path, {"issues": [
        "Vector coverage 90.0% (900/1000), 100 waiting over 1h",
        "B12 launchd jobs: 5 healthy, 1 failing: com.b12.graph-enrich: last run failed (exit code 1)"]})
    line = _health_line(ctx)
    assert line and "Vector coverage 90.0%" in line and "com.b12.graph-enrich" in line
    assert "b12_health.py" in line and "checked" not in line


def test_old_snapshot_after_a_quiet_day_is_not_an_alarm(tmp_path):
    # No session for two days: the writer last ran before the snapshot was written.
    ctx, _ = _run(tmp_path, {"issues": []}, snapshot_age=48 * HOUR, spawned_age=48 * HOUR + 60)
    assert _health_line(ctx) is None
    ctx, _ = _run(tmp_path / "b", {"issues": ["Vector coverage 90.0%"]}, snapshot_age=48 * HOUR,
                  spawned_age=48 * HOUR + 60)
    assert "(checked 48h ago) Vector coverage 90.0%" in _health_line(ctx)


def test_writer_that_left_no_snapshot_is_reported(tmp_path):
    ctx, _ = _run(tmp_path, {"issues": []}, snapshot_age=3 * HOUR, spawned_age=2 * HOUR, writer=True)
    assert "snapshot was not written after its last run" in _health_line(ctx)


def test_due_snapshot_starts_the_writer_and_marks_it(tmp_path):
    ctx, calls = _run(tmp_path, {"issues": []}, snapshot_age=2 * HOUR, writer=True)
    assert _health_line(ctx) is None
    deadline = time.time() + 10
    while not calls.exists() and time.time() < deadline:
        time.sleep(0.1)
    assert "--snapshot" in calls.read_text()
    assert (tmp_path / "home" / ".B12" / ".health-snapshot.spawned").exists()


def test_fresh_snapshot_does_not_start_the_writer(tmp_path):
    _, calls = _run(tmp_path, {"issues": []}, snapshot_age=60, writer=True)
    time.sleep(0.5)
    assert not calls.exists()


@pytest.mark.parametrize("snapshot", [None, {"issues": []}])
def test_no_line_without_issues_or_snapshot(tmp_path, snapshot):
    ctx, _ = _run(tmp_path, snapshot)
    assert _health_line(ctx) is None
