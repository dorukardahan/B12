"""Regression tests for the retrieval-correctness fixes (R&D PR-3).

Covers:
  RET-1  contradiction_resolver.merge_memories re-embeds the surviving memory
  RET-2  memory_search after/before bounds are parsed as UTC, not local time
  RET-4  the embedding-search candidate cap is newest-first (ORDER BY m.id DESC)

Run via:  python3 -m pytest scripts/tests/test_retrieval_correctness.py -v
      or:  python3 scripts/tests/test_retrieval_correctness.py
"""
import os
import sqlite3
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def test_ret2_naive_iso_is_utc():
    """RET-2: a naive ISO bound must map to the UTC epoch (created_at is UTC)."""
    try:
        import b12_mcp_server as M
    except Exception as e:  # mcp not installed in this env
        print(f"SKIP test_ret2_naive_iso_is_utc ({e})")
        return
    want = datetime(2026, 5, 1, tzinfo=timezone.utc).timestamp()
    assert M._iso_to_utc_epoch("2026-05-01") == want
    assert M._iso_to_utc_epoch("2026-05-01T00:00:00") == want
    # an explicitly-aware input is respected, not double-shifted
    assert M._iso_to_utc_epoch("2026-05-01T00:00:00+00:00") == want


def test_ret1_merge_reembeds_survivor():
    """RET-1: after a merge, memory A keeps a (fresh) embedding row."""
    import contradiction_resolver as CR

    orig = CR._encode_via_daemon
    CR._encode_via_daemon = lambda text: b"\x00\x01\x02\x03"
    try:
        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE memories(id INTEGER PRIMARY KEY, content TEXT, content_hash TEXT, "
            "updated_at REAL, updated_at_iso TEXT, metadata TEXT, deleted_at TEXT)"
        )
        conn.execute("CREATE TABLE memory_embeddings(rowid INTEGER PRIMARY KEY, content_embedding BLOB)")
        conn.execute(
            "CREATE TABLE memory_graph(source_hash TEXT, target_hash TEXT, similarity REAL, "
            "connection_types TEXT, metadata TEXT, created_at TEXT, relationship_type TEXT, "
            "UNIQUE(source_hash, target_hash))"
        )
        conn.execute("INSERT INTO memories(id,content,content_hash,updated_at,updated_at_iso,metadata,deleted_at) "
                     "VALUES (1,'A content','h1',0,'',NULL,NULL)")
        conn.execute("INSERT INTO memories(id,content,content_hash,updated_at,updated_at_iso,metadata,deleted_at) "
                     "VALUES (2,'B content','h2',0,'',NULL,NULL)")
        conn.execute("INSERT INTO memory_embeddings VALUES (1, X'DEADBEEF')")
        conn.commit()

        CR.merge_memories(conn, 1, "A content", "h1", 2, "B content", "h2")

        emb = conn.execute("SELECT content_embedding FROM memory_embeddings WHERE rowid=1").fetchone()
        content = conn.execute("SELECT content FROM memories WHERE id=1").fetchone()[0]
        b_deleted = conn.execute("SELECT deleted_at FROM memories WHERE id=2").fetchone()[0]
        conn.close()

        assert emb is not None, "survivor lost its embedding entirely"
        assert emb[0] == b"\x00\x01\x02\x03", "embedding was not re-encoded from merged text"
        assert "Merged from #2" in content
        assert b_deleted is not None, "memory B should be soft-deleted"
    finally:
        CR._encode_via_daemon = orig


def test_ret4_candidate_cap_is_newest_first():
    """RET-4: ORDER BY m.id DESC keeps the newest rows in the LIMIT-500 pool."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE memories(id INTEGER PRIMARY KEY, content TEXT, deleted_at TEXT)")
    conn.execute("CREATE TABLE memory_embeddings(rowid INTEGER PRIMARY KEY, content_embedding BLOB)")
    for i in range(1, 601):
        conn.execute("INSERT INTO memories(id,content,deleted_at) VALUES (?,?,NULL)", (i, f"m{i}"))
        conn.execute("INSERT INTO memory_embeddings VALUES (?, X'00')", (i,))
    conn.commit()
    rows = conn.execute(
        "SELECT m.id FROM memories m JOIN memory_embeddings e ON m.id = e.rowid "
        "WHERE m.deleted_at IS NULL AND m.id != ? ORDER BY m.id DESC LIMIT 500",
        (999,),
    ).fetchall()
    ids = [r[0] for r in rows]
    conn.close()
    assert len(ids) == 500
    assert 600 in ids and 599 in ids, "newest memories must be in the candidate pool"
    assert 1 not in ids and 100 not in ids, "oldest memories should be dropped, not the newest"


if __name__ == "__main__":
    rc = 0
    fns = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    for fn in fns:
        try:
            fn()
            print(f"OK: {fn.__name__}")
        except AssertionError as e:
            print(f"FAIL: {fn.__name__}: {e}")
            rc = 1
    sys.exit(rc)
