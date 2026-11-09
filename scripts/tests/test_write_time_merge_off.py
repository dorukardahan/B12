"""Write-time semantic merge is off by default.

It appended a new memory to its nearest same-type neighbour as an undated bullet
whenever cosine passed 0.85, across project boundaries, and the re-embedded row
attracted further appends. A similar memory is now stored as its own row unless
B12_WRITE_TIME_MERGE=1; exact duplicates are still collapsed by content_hash.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import write_time_merge  # noqa: E402

EXISTING = "the deploy host is configured"


@pytest.fixture
def conn(monkeypatch):
    db = sqlite3.connect(":memory:")
    db.execute(
        """CREATE TABLE memories (
            id INTEGER PRIMARY KEY, content TEXT, content_hash TEXT UNIQUE,
            tags TEXT, memory_type TEXT, metadata TEXT, created_at REAL,
            updated_at REAL, created_at_iso TEXT, updated_at_iso TEXT,
            deleted_at REAL, valid_until TEXT, strength REAL)"""
    )
    h = write_time_merge._sha256_hex(EXISTING)
    db.execute(
        "INSERT INTO memories (id, content, content_hash, tags, memory_type, metadata, created_at, "
        "updated_at, created_at_iso, updated_at_iso, deleted_at, valid_until, strength) "
        "VALUES (1, ?, ?, 'proj:a', 'note', ?, 0, 0, '', '', NULL, NULL, 1.0)",
        (EXISTING, h, json.dumps({})),
    )
    # A near-identical neighbour (cosine 0.99) that the old rule would have merged into.
    monkeypatch.setattr(write_time_merge, "_ensure_sqlite_vec_loaded", lambda c: None)
    monkeypatch.setattr(write_time_merge, "_upsert_embedding", lambda *a, **k: None)
    monkeypatch.setattr(write_time_merge, "_encode_embedding_bytes", lambda *a, **k: b"")
    monkeypatch.setattr(write_time_merge, "_best_match", lambda *a, **k: (1, EXISTING, h, 1.0, 0.99))
    monkeypatch.setenv("B12_DISABLE_FRAGMENT_FILTER", "1")
    monkeypatch.delenv("B12_WRITE_TIME_MERGE", raising=False)
    return db


def _store(conn, content):
    return write_time_merge.merge_or_insert(
        conn, content=content, content_hash=None, tags="proj:b", memory_type="note",
        metadata={}, embedding_bytes=b"", now=1_779_379_200)


def test_similar_memory_is_inserted_not_appended(conn):
    result = _store(conn, "the deploy host is configured for staging")
    assert result.action == "inserted"
    assert conn.execute("SELECT content FROM memories WHERE id = 1").fetchone()[0] == EXISTING
    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 2


def test_exact_duplicate_is_still_collapsed(conn):
    assert _store(conn, EXISTING).action == "noop_duplicate"
    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1


@pytest.mark.parametrize("value", ["1", "true", "YES"])
def test_merge_can_be_opted_into(conn, monkeypatch, value):
    monkeypatch.setenv("B12_WRITE_TIME_MERGE", value)
    assert _store(conn, "the deploy host is configured for staging").action == "merged"
