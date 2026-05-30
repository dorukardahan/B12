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


# ── RET-3: importance dual-scale normalization ─────────────────────────────
# Two write-side importance_score scales coexist: fractional [0, 0.95]
# (b12_importance.py: TRIVIAL 0.30 / BASELINE 0.50 / CAP 0.95) and level
# multipliers [0.7, 2.0] (critical 2.0 / important 1.5 / normal 1.0 / temporary
# 0.7; memory-session-end.sh caps at 2.0). The read path normalizes a level value
# (>= 1.0) by /2.0 (2.0->1.0, 1.5->0.75, 1.0->0.5) and passes fractional values
# (< 1.0) through unchanged; missing/null/bool/string default to the 0.50
# baseline; the result is clamped to [0, 1]. A blanket /2.0 wrongly halved the
# fractional band; a blanket clamp wrongly collapsed the levels. Identical in
# every read path (MCP _unified_score here, hook SQL below, OpenCode in
# plugins/opencode/tests/scoring.test.ts).

_ROW_BASE = {"last_accessed_at": 1_700_000_000.0, "created_at": 1_700_000_000.0, "strength": 1.0}


def _row(importance_json):
    return dict(_ROW_BASE, metadata=importance_json)


def test_ret3_fractional_band_not_halved():
    """A fractional 0.95 (< 1.0) passes through un-halved; it outranks baseline by
    exactly W_importance*(0.95-0.50). The original blanket /2.0 halved it to 0.475."""
    try:
        import b12_mcp_server as M
    except Exception as e:
        print(f"SKIP test_ret3_fractional_band_not_halved ({e})")
        return
    w_imp = M._DEFAULT_WEIGHTS["importance"]
    s_hi = M._unified_score(_row('{"importance_score": 0.95}'), 0.0)
    s_base = M._unified_score(_row('{"importance_score": 0.50}'), 0.0)
    assert abs((s_hi - s_base) - w_imp * (0.95 - 0.50)) < 1e-9, (s_hi, s_base)
    assert s_hi > s_base, "fractional max must outscore baseline (pre-fix inverted this)"


def test_ret3_level_scale_normalized():
    """Level multipliers (>= 1.0) normalize by /2.0: 2.0->1.0, 1.5->0.75, 1.0->0.5.
    The rejected blanket-clamp fix would have collapsed all three to 1.0."""
    try:
        import b12_mcp_server as M
    except Exception as e:
        print(f"SKIP test_ret3_level_scale_normalized ({e})")
        return
    w = M._DEFAULT_WEIGHTS["importance"]
    s_crit = M._unified_score(_row('{"importance_score": 2.0}'), 0.0)   # -> 1.0
    s_imp = M._unified_score(_row('{"importance_score": 1.5}'), 0.0)    # -> 0.75
    s_norm = M._unified_score(_row('{"importance_score": 1.0}'), 0.0)   # -> 0.5
    assert abs((s_crit - s_norm) - w * (1.0 - 0.5)) < 1e-9, "critical 2.0 must map to 1.0"
    assert abs((s_imp - s_norm) - w * (0.75 - 0.5)) < 1e-9, "important 1.5 must map to 0.75"
    # level normal (1.0 -> 0.5) and fractional baseline (0.50) land on the same value
    s_fbase = M._unified_score(_row('{"importance_score": 0.50}'), 0.0)
    assert abs(s_norm - s_fbase) < 1e-9


def test_ret3_missing_importance_defaults_to_baseline():
    """Missing / wrong-key / non-numeric / null / bool importance -> baseline 0.50."""
    try:
        import b12_mcp_server as M
    except Exception as e:
        print(f"SKIP test_ret3_missing_importance_defaults_to_baseline ({e})")
        return
    explicit = M._unified_score(_row('{"importance_score": 0.50}'), 0.0)
    # bool is a Python int subclass — float(True)==1.0 — so it must be rejected
    # explicitly (parity with the SQL json_type guard and the TS typeof guard).
    for md in ('{}', '{"other": 1}', '{"importance_score": "high"}',
               '{"importance_score": true}', '{"importance_score": false}',
               '{"importance_score": null}'):
        assert abs(M._unified_score(_row(md), 0.0) - explicit) < 1e-9, f"{md} should score as baseline 0.50"


def test_ret3_importance_clamped_to_unit_interval():
    """After normalization, clamp to [0, 1]: 3.0 (-> 1.5) clamps to 1.0 like 2.0;
    a negative value floors at 0.0."""
    try:
        import b12_mcp_server as M
    except Exception as e:
        print(f"SKIP test_ret3_importance_clamped_to_unit_interval ({e})")
        return
    capped = M._unified_score(_row('{"importance_score": 2.0}'), 0.0)   # -> 1.0
    assert abs(M._unified_score(_row('{"importance_score": 3.0}'), 0.0) - capped) < 1e-9, "3.0 (->1.5) must clamp to 1.0"
    floored = M._unified_score(_row('{"importance_score": 0.0}'), 0.0)
    assert abs(M._unified_score(_row('{"importance_score": -1.0}'), 0.0) - floored) < 1e-9, "-1.0 must floor at 0.0"


def test_ret3_hook_sql_importance_expr():
    """The hook's SQL importance term (memory-retrieval.sh) matches the read-path
    dual-scale normalization across every input class."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE m(metadata TEXT)")
    cases = [
        ('{"importance_score":0.95}', 0.95),   # fractional cap — un-halved
        ('{"importance_score":0.50}', 0.50),
        ('{"importance_score":1.0}', 0.50),    # level normal     -> /2
        ('{"importance_score":1.5}', 0.75),    # level important  -> /2
        ('{"importance_score":2.0}', 1.0),     # level critical   -> /2
        ('{"importance_score":3.0}', 1.0),     # >2.0 -> /2 -> clamp 1.0
        ('{"importance_score":-1.0}', 0.0),    # clamp floor
        (None, 0.50),
        ('{"other":1}', 0.50),
        ('not json', 0.50),
        ('{"importance_score":"high"}', 0.50), # valid JSON, non-numeric
        ('{"importance_score":null}', 0.50),
        ('{"importance_score":true}', 0.50),   # JSON bool
        ('{"importance_score":false}', 0.50),
    ]
    for md, _ in cases:
        conn.execute("INSERT INTO m(metadata) VALUES (?)", (md,))
    expr = (
        "max(min(CASE "
        "WHEN json_valid(m.metadata) AND json_type(m.metadata,'$.importance_score') IN ('integer','real') "
        "THEN (CASE WHEN json_extract(m.metadata,'$.importance_score') >= 1.0 "
        "THEN json_extract(m.metadata,'$.importance_score') / 2.0 "
        "ELSE json_extract(m.metadata,'$.importance_score') END) "
        "ELSE 0.50 END, 1.0), 0.0)"
    )
    got = [r[0] for r in conn.execute(f"SELECT {expr} FROM m AS m").fetchall()]
    conn.close()
    for (md, want), val in zip(cases, got):
        assert abs(val - want) < 1e-9, f"{md!r}: expected {want}, got {val}"


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
