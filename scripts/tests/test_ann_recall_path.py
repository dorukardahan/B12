"""ANN recall-path contract tests (P5).

Guards two things about the sqlite-vec vec0 MATCH path that
`recall.ann.enabled=true` turns on:

  1. MATCH returns the EXACT nearest neighbours — enabling ANN must not change
     ranking vs an exact cosine over the same vectors (it is brute-force KNN,
     not an approximate index). This is the property the 2026-06-19 A/B
     (benchmarks/ann_ab_test.py) verified on production vectors; this test pins
     it on synthetic vectors so CI catches any sqlite-vec/declaration drift.
  2. embed_daemon._ann_supported clamps an absurd threshold_count into a sane
     range (P5 hardening) so a config typo can neither force ANN on for a
     near-empty table nor wedge it off forever.

Run:  python3 -m pytest scripts/tests/test_ann_recall_path.py -v
"""
import os
import sqlite3
import struct
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

np = pytest.importorskip("numpy")
try:
    import sqlite_vec  # noqa: F401
except ImportError:
    pytest.skip("sqlite_vec not installed", allow_module_level=True)

DIM = 16


def _make_vec_db(n=80, dim=DIM, seed=7):
    """In-memory vec0 table of `n` normalized random vectors; returns (conn, matrix).
    rowids are 1-based (i+1) so rowid == row index + 1."""
    rng = np.random.default_rng(seed)
    conn = sqlite3.connect(":memory:")
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute(
        f"CREATE VIRTUAL TABLE memory_embeddings USING vec0(content_embedding FLOAT[{dim}])"
    )
    mat = rng.standard_normal((n, dim)).astype(np.float32)
    mat /= np.linalg.norm(mat, axis=1, keepdims=True)
    for i in range(n):
        conn.execute(
            "INSERT INTO memory_embeddings(rowid, content_embedding) VALUES (?, ?)",
            (i + 1, struct.pack(f"{dim}f", *mat[i].tolist())),
        )
    conn.commit()
    return conn, mat


def _match_topk(conn, q, k):
    qb = struct.pack(f"{len(q)}f", *(float(x) for x in q))
    rows = conn.execute(
        "SELECT rowid FROM memory_embeddings "
        "WHERE content_embedding MATCH ? AND k = ? ORDER BY distance",
        (qb, int(k)),
    ).fetchall()
    return [int(r[0]) for r in rows]


def test_match_returns_exact_nearest():
    """vec0 MATCH top-k must equal exact-cosine top-k for normalized vectors."""
    conn, mat = _make_vec_db()
    n = mat.shape[0]
    k = 5
    mismatches = []
    for qi in range(0, n, 5):  # sample probes across the table
        q = mat[qi]
        sims = mat @ q
        order = np.argsort(-sims)
        exact = {int(i) + 1 for i in order if i != qi}  # set of nearest rowids
        # take the k nearest (exclude self) deterministically
        exact_top = set([int(i) + 1 for i in order if i != qi][:k])
        ann = [r for r in _match_topk(conn, q, k + 1) if r != qi + 1][:k]
        if set(ann) != exact_top:
            mismatches.append(qi)
    conn.close()
    assert not mismatches, f"vec0 MATCH disagreed with exact cosine on probes {mismatches}"


def test_threshold_clamp_in_ann_supported(monkeypatch):
    """_ann_supported must clamp an absurd threshold_count into [100, 1e6]."""
    embed_daemon = pytest.importorskip("embed_daemon")
    conn, _ = _make_vec_db(n=120)

    def cfg_factory(threshold):
        def _cfg(*path, default=None):
            if path and path[-1] == "enabled":
                return True
            if path and path[-1] == "threshold_count":
                return threshold
            return default
        return _cfg

    # raw=0 -> clamps to 100; 120 rows >= 100 -> ANN active
    monkeypatch.setattr(embed_daemon, "_b12_cfg_get", cfg_factory(0))
    use_ann, count = embed_daemon._ann_supported(conn)
    assert count == 120
    assert use_ann is True

    # raw=10_000_000 -> clamps to 1_000_000; 120 rows < that -> ANN inactive
    monkeypatch.setattr(embed_daemon, "_b12_cfg_get", cfg_factory(10_000_000))
    use_ann, _ = embed_daemon._ann_supported(conn)
    assert use_ann is False
    conn.close()
