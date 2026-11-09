"""Guards for vector coverage: every active memory should be semantically searchable.

The checkpoint hook inserted memories without ever writing a vector, and other
writers skip the vector when the embed daemon is down. Those rows were reachable
by FTS only, silently, for months. Now the checkpoint flush embeds what it
inserts, SessionStart runs a bounded, rate-limited backfill as a safety net, and
the health check reports coverage.
"""
from __future__ import annotations

import os
import re
import sqlite3
import sys
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
REPO_ROOT = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))

import embedding_backfill  # noqa: E402


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    runtime = tmp_path / "run"
    runtime.mkdir()
    monkeypatch.setenv("B12_EMBED_RUNTIME_DIR", str(runtime))
    db = tmp_path / "db.sqlite"
    db.write_bytes(b"")
    calls = []
    monkeypatch.setattr(embedding_backfill, "backfill",
                        lambda path, limit=None, **kw: calls.append((path, limit)) or (limit, 0))
    sock = embedding_backfill._sock_path()
    # The tests stand in for a live daemon with the socket file; the real probe is tested below.
    monkeypatch.setattr(embedding_backfill, "_daemon_ready", lambda: Path(sock).exists())
    return {"state": tmp_path / "state", "sock": embedding_backfill._sock_path(), "db": str(db), "calls": calls}


def _run(rt, **kw):
    return embedding_backfill.scheduled_backfill(rt["db"], state_dir=str(rt["state"]), **kw)


def test_due_run_backfills_and_stamps(runtime):
    Path(runtime["sock"]).touch()
    assert _run(runtime, limit=7, min_interval=3600) == (7, 0)
    assert runtime["calls"] == [(runtime["db"], 7)]
    assert (runtime["state"] / ".embedding-backfill.last").exists()


def test_fresh_stamp_skips(runtime):
    Path(runtime["sock"]).touch()
    runtime["state"].mkdir()
    (runtime["state"] / ".embedding-backfill.last").touch()
    assert _run(runtime, min_interval=3600) is None
    assert runtime["calls"] == []


def test_stale_stamp_runs_again(runtime):
    Path(runtime["sock"]).touch()
    runtime["state"].mkdir()
    stamp = runtime["state"] / ".embedding-backfill.last"
    stamp.touch()
    old = time.time() - 7200
    os.utime(stamp, (old, old))
    assert _run(runtime, min_interval=3600) is not None


def test_daemon_down_does_not_consume_the_slot(runtime):
    assert _run(runtime, min_interval=3600, wait_daemon=0) is None
    assert runtime["calls"] == []
    assert not (runtime["state"] / ".embedding-backfill.last").exists()


@pytest.mark.skipif(embedding_backfill.fcntl is None, reason="no fcntl on this platform")
def test_concurrent_run_is_skipped(runtime):
    Path(runtime["sock"]).touch()
    runtime["state"].mkdir()
    holder = open(runtime["state"] / ".embedding-backfill.last.lock", "a+")
    embedding_backfill.fcntl.flock(holder.fileno(), embedding_backfill.fcntl.LOCK_EX)
    try:
        assert _run(runtime, min_interval=0) is None
        assert runtime["calls"] == []
    finally:
        holder.close()


def test_stale_content_gets_no_vector(tmp_path):
    # A row edited while its old text was being embedded must not get that vector.
    db = tmp_path / "s.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE memories (id INTEGER PRIMARY KEY, content_hash TEXT, deleted_at REAL)")
    conn.execute("INSERT INTO memories VALUES (1, 'new-hash', NULL)")
    conn.commit()
    conn.close()
    assert embedding_backfill.store_embedding(str(db), 1, "AAAA", expected_hash="old-hash") is False


def test_run_stops_at_its_deadline(monkeypatch):
    calls = []
    monkeypatch.setattr(embedding_backfill, "daemon_request", lambda texts: calls.append(texts) or None)
    rows = [(i, "text", "", "h") for i in range(30)]
    assert embedding_backfill.backfill("unused", missing=rows, deadline=time.time() - 1) == (0, 0)
    assert calls == []


def test_checkpoint_flush_embeds_inserted_rows():
    src = (REPO_ROOT / "hooks" / "memory-checkpoint.sh").read_text()
    assert '_inserted_hashes.append(item["hash"])' in src
    tail = src[src.index("_inserted_hashes.append"):]
    # Embedding happens after the flush, outside the buffer lock, scoped to this flush's rows.
    assert "LOCK_UN" in tail
    # The hook's python3 has no sqlite-vec, so the venv must store the vectors.
    assert "b12-venv/bin/python3" in tail and '"--hashes", ",".join(_inserted_hashes)' in tail


def test_session_start_runs_detached_safety_net():
    src = (REPO_ROOT / "hooks" / "memory-session-start.sh").read_text()
    m = re.search(r"embedding_backfill\.py\"[^\n]*\\\n([^\n]*)\n", src)
    assert m, "SessionStart does not start the backfill safety net"
    line = m.group(1)
    for flag in ("--if-due", "--limit", "--wait-daemon", "--quiet", "</dev/null", ">/dev/null", "2>&1", "&"):
        assert flag in line, f"safety net is missing {flag}"


def test_stale_socket_is_not_a_ready_daemon(tmp_path, monkeypatch):
    # A daemon killed without cleanup leaves its socket file behind; that must not
    # count as ready, or the hourly slot is spent on a dead daemon.
    monkeypatch.setenv("B12_EMBED_RUNTIME_DIR", str(tmp_path))
    Path(embedding_backfill._sock_path()).touch()
    assert embedding_backfill._daemon_ready() is False
