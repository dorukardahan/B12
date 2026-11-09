"""Guards for check_vector_coverage: rows without a vector must not stay silent.

A memory without a vector is reachable by keyword search only. The checkpoint
hook never wrote vectors and other writers skip them during daemon outages; on
one install 56% of active memories had none and nothing reported it.
"""
from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import b12_health  # noqa: E402


def _coverage_db(path: Path, active: int, missing: int, age: float = 7200) -> None:
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE memories (id INTEGER PRIMARY KEY, deleted_at REAL, created_at REAL)")
    db.execute("CREATE TABLE memory_embeddings_rowids (rowid INTEGER PRIMARY KEY, id, chunk_id, chunk_offset)")
    for i in range(1, active + 1):
        db.execute("INSERT INTO memories (id, created_at) VALUES (?, ?)", (i, time.time() - age))
        if i > missing:
            db.execute("INSERT INTO memory_embeddings_rowids (rowid) VALUES (?)", (i,))
    db.execute("INSERT INTO memories (id, deleted_at) VALUES (?, 1.0)", (active + 1,))  # deleted, no vector
    db.commit()
    db.close()


@pytest.mark.parametrize("missing,status", [(0, "OK"), (1, "OK"), (10, "WARN")])
def test_health_reports_vector_coverage(tmp_path, monkeypatch, missing, status):
    db = tmp_path / "cov.db"
    _coverage_db(db, active=100, missing=missing)
    monkeypatch.setattr(b12_health, "DB_PATH", db)
    result = b12_health.check_vector_coverage()
    assert result.status == status
    assert f"{100 - missing}/100" in result.label


def test_health_warns_on_many_rows_waiting(tmp_path, monkeypatch):
    # 1% missing passes the percentage bar, but 60 rows unfilled for an hour do not.
    db = tmp_path / "cov.db"
    _coverage_db(db, active=6000, missing=60)
    monkeypatch.setattr(b12_health, "DB_PATH", db)
    result = b12_health.check_vector_coverage()
    assert result.status == "WARN" and "60 waiting over 1h" in result.label


def test_fresh_rows_are_not_counted_as_waiting(tmp_path, monkeypatch):
    db = tmp_path / "cov.db"
    _coverage_db(db, active=6000, missing=60, age=60)
    monkeypatch.setattr(b12_health, "DB_PATH", db)
    assert b12_health.check_vector_coverage().status == "OK"
