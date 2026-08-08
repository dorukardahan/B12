"""Regression coverage for one-row-per-session summaries."""
from __future__ import annotations

import json
import sqlite3
import sys
import threading
from array import array
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

import b12_mcp_server as server  # noqa: E402
from write_time_merge import upsert_session_summary  # noqa: E402


def _db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=10)
    server._ensure_schema(conn)
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='memory_embeddings'"
    ).fetchone():
        conn.execute(
            "CREATE TABLE memory_embeddings("
            "rowid INTEGER PRIMARY KEY, content_embedding BLOB)"
        )
    conn.commit()
    return conn


def _store(conn: sqlite3.Connection, sid: str, content: str, now: float, emb: bytes | None):
    return upsert_session_summary(
        conn,
        session_id=sid,
        content=content,
        tags="proj:test,session-summary",
        metadata={"platform": "codex", "session_id": "wrong"},
        embedding_bytes=emb,
        now=now,
    )


def test_repeated_fires_update_one_row_and_all_indexes(tmp_path):
    conn = _db(tmp_path / "summary.db")
    sid = "session-one-123456789"

    first_id = _store(conn, sid, "initialunique session summary", 100.0, b"first")
    old_hash = conn.execute(
        "SELECT content_hash FROM memories WHERE id = ?", (first_id,)
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO memory_graph "
        "(source_hash,target_hash,similarity,connection_types,created_at) "
        "VALUES (?, 'target-hash', 0.5, '[]', 100)",
        (old_hash,),
    )
    conn.commit()

    for n in range(2, 6):
        row_id = _store(
            conn, sid, f"finalunique session summary version {n}",
            100.0 * n, f"embedding-{n}".encode(),
        )
        assert row_id == first_id

    row = conn.execute(
        "SELECT content, content_hash, metadata, created_at, updated_at "
        "FROM memories WHERE id = ?", (first_id,),
    ).fetchone()
    assert conn.execute(
        "SELECT COUNT(*) FROM memories WHERE memory_type='session_summary'"
    ).fetchone()[0] == 1
    assert row[0] == "finalunique session summary version 5"
    assert row[1] != old_hash
    assert json.loads(row[2])["session_id"] == sid
    assert row[3] == 100.0
    assert row[4] == 500.0
    assert conn.execute(
        "SELECT content_embedding FROM memory_embeddings WHERE rowid = ?", (first_id,)
    ).fetchone()[0] == b"embedding-5"

    for table in ("memory_fts", "memory_fts_stemmed", "memory_content_fts"):
        assert conn.execute(
            f"SELECT rowid FROM {table} WHERE {table} MATCH 'initialunique'"
        ).fetchall() == []
        assert conn.execute(
            f"SELECT rowid FROM {table} WHERE {table} MATCH 'finalunique'"
        ).fetchall() == [(first_id,)]

    edge = conn.execute(
        "SELECT source_hash FROM memory_graph WHERE target_hash='target-hash'"
    ).fetchone()
    assert edge == (row[1],)

    _store(conn, sid, "novector latest session summary", 600.0, None)
    assert conn.execute(
        "SELECT 1 FROM memory_embeddings WHERE rowid = ?", (first_id,)
    ).fetchone() is None
    source = (SCRIPTS / "b12_mcp_server.py").read_text()
    assert "COALESCE(updated_at, created_at) AS summary_at" in source
    assert 'last_summary["summary_at"]' in source
    conn.close()


def test_same_content_in_different_sessions_does_not_hash_collide(tmp_path):
    conn = _db(tmp_path / "hashes.db")
    first = _store(conn, "same-prefix1-alpha", "same summary content", 1.0, None)
    second = _store(conn, "same-prefix1-beta", "same summary content", 2.0, None)
    assert first != second
    assert conn.execute("SELECT COUNT(DISTINCT content_hash) FROM memories").fetchone()[0] == 2
    conn.close()


def test_upsert_replaces_and_removes_real_vec0_row(tmp_path, monkeypatch):
    sqlite_vec = pytest.importorskip("sqlite_vec")
    monkeypatch.setenv("B12_EMBED_DIM", "4")
    conn = sqlite3.connect(tmp_path / "vec.db")
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    server._ensure_schema(conn)
    first = array("f", [1.0, 0.0, 0.0, 0.0]).tobytes()
    second = array("f", [0.0, 1.0, 0.0, 0.0]).tobytes()
    row_id = _store(conn, "real-vec-session", "first vector", 1.0, first)
    _store(conn, "real-vec-session", "second vector", 2.0, second)
    stored = conn.execute(
        "SELECT content_embedding FROM memory_embeddings WHERE rowid=?", (row_id,)
    ).fetchone()[0]
    assert bytes(stored) == second
    _store(conn, "real-vec-session", "no vector now", 3.0, None)
    assert conn.execute(
        "SELECT 1 FROM memory_embeddings WHERE rowid=?", (row_id,)
    ).fetchone() is None
    conn.close()


def test_existing_duplicates_are_not_cleaned_and_newest_row_is_updated(tmp_path):
    conn = _db(tmp_path / "legacy.db")
    sid = "legacy-session"
    metadata = json.dumps({"session_id": sid[:12]})
    for content, updated in (("old duplicate", 10.0), ("new duplicate", 20.0)):
        conn.execute(
            "INSERT INTO memories(content,content_hash,memory_type,metadata,created_at,updated_at) "
            "VALUES (?,?, 'session_summary', ?, ?, ?)",
            (content, f"hash-{updated}", metadata, updated, updated),
        )
    conn.commit()
    newest_id = conn.execute("SELECT MAX(id) FROM memories").fetchone()[0]

    updated_id = _store(conn, sid, "latest legacy summary", 30.0, None)
    assert updated_id == newest_id
    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 2
    assert conn.execute(
        "SELECT content FROM memories WHERE id = ?", (newest_id,)
    ).fetchone()[0] == "latest legacy summary"
    conn.close()


def test_concurrent_first_fires_still_create_one_row(tmp_path):
    path = tmp_path / "concurrent.db"
    _db(path).close()
    barrier = threading.Barrier(5)
    errors: list[BaseException] = []

    def worker(n: int) -> None:
        conn = sqlite3.connect(path, timeout=10)
        try:
            barrier.wait()
            _store(conn, "concurrent-session", f"concurrent summary {n}", float(n), None)
        except BaseException as exc:  # surfaced after all workers join
            errors.append(exc)
        finally:
            conn.close()

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(1, 6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    conn = sqlite3.connect(path)
    assert conn.execute(
        "SELECT COUNT(*) FROM memories WHERE memory_type='session_summary'"
    ).fetchone()[0] == 1
    conn.close()
