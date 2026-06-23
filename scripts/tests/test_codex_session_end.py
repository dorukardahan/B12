"""PR-3b: Codex SessionEnd store_memory secret-cap test.

store_memory scrubs content (credential -> [REDACTED:...]) and must then cap a
credential-bearing memory's importance at baseline via b12_importance.is_secret,
while preserving the caller's deliberate per-category importance for non-secrets.

We pass content that already carries the scrubber's [REDACTED:...] marker (the
post-scrub shape) so no real secret literal lives in this file — the scrub step
itself is covered by test_b12_pii_scrubber.
"""
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import b12_importance as imp  # noqa: E402
import codex_session_end as cse  # noqa: E402


def _make_db(path):
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE memories (
            id INTEGER PRIMARY KEY, content TEXT, metadata TEXT, tags TEXT,
            content_hash TEXT, memory_type TEXT, created_at REAL, updated_at REAL,
            created_at_iso TEXT, updated_at_iso TEXT, strength REAL, deleted_at REAL
        )
        """
    )
    conn.commit()
    conn.close()


def _stored_importance(db_path, mem_id):
    conn = sqlite3.connect(db_path)
    md = conn.execute("SELECT metadata FROM memories WHERE id = ?", (mem_id,)).fetchone()[0]
    conn.close()
    return json.loads(md)["importance_score"]


def test_codex_store_caps_secret_importance(tmp_path):
    db = str(tmp_path / "codex.db")
    _make_db(db)
    # Content already carries the scrubber's marker (post-scrub shape).
    mid = cse.store_memory(
        db, "deploy token=[REDACTED:generic] keep secret",
        json.dumps({"type": "decision", "importance_score": 0.8}),
        "proj:test", embedding=None, memory_type="decision",
    )
    assert mid is not None
    assert _stored_importance(db, mid) == imp.IMPORTANCE_BASELINE


def test_codex_store_preserves_nonsecret_importance(tmp_path):
    db = str(tmp_path / "codex.db")
    _make_db(db)
    mid = cse.store_memory(
        db, "we shipped the migration cleanly this week",
        json.dumps({"type": "decision", "importance_score": 0.8}),
        "proj:test", embedding=None, memory_type="decision",
    )
    assert mid is not None
    assert _stored_importance(db, mid) == 0.8  # caller's deliberate value preserved


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
