"""Tests for the read-only importance-gap audit (PR-2c).

Uses a synthetic temp DB only — never touches the real corpus. Verifies the gap
arithmetic (high-value memories the heuristic would score baseline), the RET-3
importance normalization, and that the audit never writes to the DB.
"""
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import audit_importance_gap as A  # noqa: E402


def _mk_db(rows):
    d = tempfile.mkdtemp()
    db = os.path.join(d, "sqlite_vec.db")
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE memories (id INTEGER PRIMARY KEY, content TEXT, "
              "metadata TEXT, deleted_at REAL)")
    for content, meta in rows:
        c.execute("INSERT INTO memories(content, metadata, deleted_at) VALUES (?, ?, NULL)",
                  (content, meta))
    c.commit()
    c.close()
    return db


def test_gap_arithmetic():
    db = _mk_db([
        ("we decided to ship on Friday", '{"importance_score":0.75}'),   # high + decision -> not gap
        ("the quarterly target is fixed", '{"importance_score":2.0}'),   # high (2.0->1.0) + baseline -> GAP
        ("our anniversary matters most", '{"importance_score":1.5}'),    # high (->0.75) + baseline -> GAP
        ("just chatting about lunch", '{"importance_score":0.5}'),       # not high
        ("remember this important thing", '{"importance_score":0.9}'),   # high + memorable -> not gap
    ])
    r = A.audit(db, high=0.75, samples=10)
    assert r["total_memories"] == 5
    assert r["high_value"] == 4
    assert r["gap"] == 2
    assert r["gap_pct_of_high_value"] == 50.0
    assert len(r["gap_samples"]) == 2


def test_normalization_and_missing_importance():
    # level multipliers halve; missing/bool/string default to baseline (not high).
    db = _mk_db([
        ("plain content one", "{}"),                              # missing -> 0.50, not high
        ("plain content two", '{"importance_score":true}'),       # bool -> 0.50, not high
        ("plain content three", '{"importance_score":"high"}'),   # string -> 0.50, not high
        ("plain content four", '{"importance_score":1.0}'),       # level 1.0 -> 0.50, not high
    ])
    r = A.audit(db, high=0.75, samples=10)
    assert r["high_value"] == 0
    assert r["gap"] == 0


def test_missing_db_returns_error():
    r = A.audit("/nonexistent/path/sqlite_vec.db", high=0.75, samples=5)
    assert "error" in r


def test_audit_is_read_only():
    # Opening mode=ro must reject writes; the audit must not mutate the DB.
    db = _mk_db([("we decided to ship", '{"importance_score":0.75}')])
    before = os.path.getmtime(db)
    A.audit(db, high=0.75, samples=5)
    assert os.path.getmtime(db) == before


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
