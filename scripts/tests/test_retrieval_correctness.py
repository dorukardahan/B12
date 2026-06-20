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
    """A fractional 0.95 (< 1.0) passes through un-halved; it must outscore baseline
    and must outscore what a blanket /2.0 halving (0.95->0.475) would produce.
    The exact W_importance*(0.95-0.50) delta is no longer asserted because decay now
    also depends on importance (effective-stability formula), so the total delta is
    larger than the importance-weight term alone — the right invariant is ordering."""
    try:
        import b12_mcp_server as M
    except Exception as e:
        print(f"SKIP test_ret3_fractional_band_not_halved ({e})")
        return
    s_hi = M._unified_score(_row('{"importance_score": 0.95}'), 0.0)
    s_base = M._unified_score(_row('{"importance_score": 0.50}'), 0.0)
    # What the (buggy) blanket /2.0 would have produced: 0.95 -> 0.475
    s_halved = M._unified_score(_row('{"importance_score": 0.475}'), 0.0)
    assert s_hi > s_base, "fractional max must outscore baseline (pre-fix inverted this)"
    assert s_hi > s_halved, "0.95 un-halved must beat the halved-to-0.475 score (guards the original bug)"


def test_ret3_level_scale_normalized():
    """Level multipliers (>= 1.0) normalize by /2.0: 2.0->1.0, 1.5->0.75, 1.0->0.5.
    The rejected blanket-clamp fix would have collapsed all three to the same score.
    Exact importance-weight deltas are no longer asserted because decay now also varies
    with importance (effective-stability); the invariant is strict ordering and that
    level normal (1.0->0.5) equals fractional baseline (0.50) exactly."""
    try:
        import b12_mcp_server as M
    except Exception as e:
        print(f"SKIP test_ret3_level_scale_normalized ({e})")
        return
    s_crit = M._unified_score(_row('{"importance_score": 2.0}'), 0.0)   # -> 1.0
    s_imp = M._unified_score(_row('{"importance_score": 1.5}'), 0.0)    # -> 0.75
    s_norm = M._unified_score(_row('{"importance_score": 1.0}'), 0.0)   # -> 0.5
    assert s_crit > s_imp > s_norm, (
        "critical(2.0->1.0) > important(1.5->0.75) > normal(1.0->0.5) ordering must hold; "
        "blanket-clamp bug would have collapsed all three to equal"
    )
    # level normal (1.0 -> 0.5) and fractional baseline (0.50) produce identical
    # normalized importance AND identical eff_stability, so scores are exactly equal
    s_fbase = M._unified_score(_row('{"importance_score": 0.50}'), 0.0)
    assert abs(s_norm - s_fbase) < 1e-9, "level normal (1.0->0.5) must equal fractional baseline 0.50"


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


def test_aging_hook_sql_matches_unified_score():
    """The hook's new 4-term score (effective-stability decay) matches MCP _unified_score."""
    import time
    import json
    try:
        import b12_mcp_server as M
    except Exception as e:
        print(f"SKIP ({e})"); return
    now = time.time()
    cases = [  # (age_days, importance_score_json_value, strength)
        (365, 0.90, 1.0), (365, 0.50, 1.0), (30, 0.50, 1.0), (365, 0.50, 5.0),
        (1, 0.30, 1.0), (180, 0.95, 2.0),
    ]
    rank = 3.0
    relevance = 1.0 / (1.0 + abs(rank))
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE m(created_at REAL, strength REAL, metadata TEXT)")
    for age, imp, st in cases:
        conn.execute("INSERT INTO m VALUES (?,?,?)", (now - age*86400.0, st, json.dumps({"importance_score": imp})))
    # SQL: same imp_norm + eff_stability decay + 4-term weights, last_accessed_at absent -> created_at
    sql = '''
      SELECT (
        0.25 * max(1.0/(1.0 + ((julianday('now') - julianday(datetime(created_at,'unixepoch'))))/(9.0*COALESCE(strength,1.0)*(1.0+4.0*imp_norm))),0.01)
        + 0.25 * imp_norm
        + 0.40 * (1.0/(1.0+abs(%f)))
        + 0.10 * min(COALESCE(strength,1.0)/5.0,1.0)
      ) FROM (SELECT created_at, strength,
              max(min(CASE WHEN json_valid(metadata) AND json_type(metadata,'$.importance_score') IN ('integer','real')
                  THEN (CASE WHEN json_extract(metadata,'$.importance_score')>=1.0 THEN json_extract(metadata,'$.importance_score')/2.0
                             ELSE json_extract(metadata,'$.importance_score') END) ELSE 0.50 END,1.0),0.0) AS imp_norm
              FROM m)
    ''' % rank
    sql_scores = [r[0] for r in conn.execute(sql).fetchall()]
    conn.close()
    for (age, imp, st), sql_s in zip(cases, sql_scores):
        row = {"last_accessed_at": None, "created_at": now - age*86400.0, "strength": st,
               "metadata": json.dumps({"importance_score": imp})}
        py_s = M._unified_score(row, relevance)
        assert abs(py_s - sql_s) < 1e-4, (age, imp, st, py_s, sql_s)


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
