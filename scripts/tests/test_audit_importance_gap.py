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
    # rows: (content, metadata) or (content, metadata, valid_until)
    d = tempfile.mkdtemp()
    db = os.path.join(d, "sqlite_vec.db")
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE memories (id INTEGER PRIMARY KEY, content TEXT, "
              "metadata TEXT, deleted_at REAL, valid_until TEXT)")
    for row in rows:
        content, meta = row[0], row[1]
        valid_until = row[2] if len(row) > 2 else None
        c.execute("INSERT INTO memories(content, metadata, deleted_at, valid_until) "
                  "VALUES (?, ?, NULL, ?)", (content, meta, valid_until))
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
    assert r["gap_pct_of_eligible"] == 50.0
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


def test_ttl_expired_excluded():
    # Codex: TTL-expired rows must be excluded (match the retrieval paths).
    db = _mk_db([
        ("expired high baseline memory", '{"importance_score":2.0}', "2000-01-01 00:00:00"),
        ("live high baseline memory", '{"importance_score":2.0}', "2999-01-01 00:00:00"),
    ])
    r = A.audit(db, high=0.75, samples=10)
    assert r["total_memories"] == 1      # the expired row is filtered out
    assert r["high_value"] == 1
    assert r["gap"] == 1                 # only the live one counts


def test_legacy_metadata_format_parsed():
    # Codex: legacy "key:val, ..." metadata must be parsed, not dropped to {}.
    db = _mk_db([
        ("the quarterly target is fixed", "type:progress, importance:1.5"),  # ->0.75 high, baseline -> GAP
        ("we decided to proceed", "type:decision, importance:0.75"),         # high, decision -> not gap
    ])
    r = A.audit(db, high=0.75, samples=10)
    assert r["high_value"] == 2
    assert r["gap"] == 1


def test_secret_suppressed_not_counted_as_gap():
    # Codex: a high-importance credential-bearing row is deliberately baselined
    # (secret cap) and must be reported separately, NOT counted as a gap/miss.
    db = _mk_db([
        ("api key is token=ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789012345",
         '{"importance_score":2.0}'),                       # high + secret -> suppressed, not gap
        ("the quarterly target is fixed", '{"importance_score":2.0}'),  # high baseline -> GAP
    ])
    r = A.audit(db, high=0.75, samples=10)
    assert r["high_value"] == 2
    assert r["gap"] == 1
    assert r["secret_suppressed"] == 1
    # gap % is over the ELIGIBLE set (high_value - secret_suppressed = 1), so the
    # one eligible miss is 100% — not 50% diluted by the secret-suppressed row.
    assert r["eligible"] == 1
    assert r["gap_pct_of_eligible"] == 100.0


def test_missing_db_returns_error():
    r = A.audit("/nonexistent/path/sqlite_vec.db", high=0.75, samples=5)
    assert "error" in r


def test_scrub_forces_redaction_despite_optout(monkeypatch):
    # Even with B12_DISABLE_PII_SCRUB=1, audit samples must be redacted, not raw.
    monkeypatch.setenv("B12_DISABLE_PII_SCRUB", "1")
    out = A._scrub("token=ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789012345")
    assert "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ" not in out
    # the env opt-out is restored after the call
    assert os.environ.get("B12_DISABLE_PII_SCRUB") == "1"


def test_audit_is_read_only():
    # Opening mode=ro must reject writes; the audit must not mutate the DB.
    db = _mk_db([("we decided to ship", '{"importance_score":0.75}')])
    before = os.path.getmtime(db)
    A.audit(db, high=0.75, samples=5)
    assert os.path.getmtime(db) == before


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
