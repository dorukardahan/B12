"""Guards for memory_search near-duplicate collapsing.

With write-time merging off, near-identical memories are stored as separate rows
and could fill the top results with one fact. Search folds a hit into a
higher-ranked hit of the same type and project when their vectors have cosine
>= 0.95; nothing is deleted and the folded ids are shown.
"""
from __future__ import annotations

import array
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
M = pytest.importorskip("b12_mcp_server")


def _row(i, mtype="learning", tags="proj:b12"):
    return {"id": i, "memory_type": mtype, "tags": tags}


def _vec(*xs):
    return M._unit_vector(array.array("f", list(xs) + [0.0] * (8 - len(xs))).tobytes())


A, A2, B = _vec(1, 0), _vec(1, 0.05), _vec(0, 1)  # A2 ~ A (cos ~0.999), B orthogonal


def _ranked(*rows):
    return [(r, 1.0 - k * 0.01) for k, r in enumerate(rows)]


def test_near_duplicate_folds_into_higher_hit_and_frees_a_slot():
    ranked = _ranked(_row(1), _row(2), _row(3))
    kept, folded = M._collapse_ranked(ranked, {1: A, 2: A2, 3: B}, limit=2)
    assert [r["id"] for r, _ in kept] == [1, 3]
    assert folded == {1: [2]}


@pytest.mark.parametrize("other", [_row(2, mtype="decision"), _row(2, tags="proj:other")])
def test_other_type_or_project_is_never_folded(other):
    kept, folded = M._collapse_ranked(_ranked(_row(1), other), {1: A, 2: A2}, limit=5)
    assert [r["id"] for r, _ in kept] == [1, 2] and folded == {}


def test_hits_without_vectors_are_kept():
    kept, folded = M._collapse_ranked(_ranked(_row(1), _row(2)), {1: A}, limit=5)
    assert [r["id"] for r, _ in kept] == [1, 2] and folded == {}


def test_distinct_hits_are_unchanged():
    kept, folded = M._collapse_ranked(_ranked(_row(1), _row(3)), {1: A, 3: B}, limit=5)
    assert [r["id"] for r, _ in kept] == [1, 3] and folded == {}


def test_unit_vector_normalizes_and_rejects_empty():
    v = M._unit_vector(array.array("f", [3.0, 4.0]).tobytes())
    assert v == pytest.approx([0.6, 0.8])
    assert M._unit_vector(b"") is None
    assert M._unit_vector(array.array("f", [0.0, 0.0]).tobytes()) is None


def test_candidate_pool_is_deeper_than_the_limit_when_collapsing():
    # Folded hits must be replaced by distinct ones, so every candidate query
    # (exact, FTS, semantic, fallback) fetches from `pool`, not `limit` (Codex review P2).
    import inspect
    src = inspect.getsource(M.memory_search.fn if hasattr(M.memory_search, "fn") else M.memory_search)
    assert "pool = limit * 3 if _collapse_on else limit" in src
    for needle in ('[f"%{query}%"] + params + [pool]', "[fts_query] + params + [pool]",
                   "limit=pool * 2,", "params + [pool],"):
        assert needle in src, needle
    assert "+ [limit]" not in src


def test_folding_a_full_pool_refills_from_a_deeper_one(tmp_path):
    # 40 near-identical rows outrank 10 distinct ones. The first pool (3 x limit)
    # holds only the copies, so folding alone would return a single hit.
    import asyncio
    import hashlib
    import re
    import sqlite3

    if not M._HAS_VEC:
        pytest.skip("sqlite-vec not installed")
    db = str(tmp_path / "refill.db")
    conn = sqlite3.connect(db)
    conn.enable_load_extension(True)
    M.sqlite_vec.load(conn)
    M._ensure_schema(conn)

    def add(i, content, strength, axis):
        ch = hashlib.sha256(content.encode()).hexdigest()
        conn.execute("INSERT INTO memories (id, content, content_hash, tags, memory_type, metadata, created_at,"
                     " created_at_iso, updated_at, updated_at_iso, strength) VALUES (?, ?, ?, 'proj:b12', 'learning',"
                     " '{}', 1.7e9, '', 1.7e9, '', ?)", (i, content, ch, strength))
        v = [0.0] * 1024
        v[axis] = 1.0
        v[1023] = i * 1e-4  # copies differ slightly but stay far above 0.95
        conn.execute("INSERT INTO memory_embeddings (rowid, content_embedding) VALUES (?, ?)",
                     (i, array.array("f", v).tobytes()))

    for i in range(1, 41):
        add(i, f"alpha alpha alpha copy {i}", 5.0, 0)
    for k in range(10):
        add(100 + k, f"alpha distinct subject number {k} with several other words", 1.0, k + 1)
    conn.commit()
    conn.close()

    calls = []
    orig = (M.DB_PATH, M.daemon_request, M._daemon_lock, M._db_init_lock)
    M.DB_PATH, M.daemon_request = db, (lambda *a, **k: calls.append(a) or None)  # daemon not answering
    M._daemon_lock, M._db_init_lock = asyncio.Lock(), asyncio.Lock()

    async def body():
        await M._init_db()
        fn = M.memory_search.fn if hasattr(M.memory_search, "fn") else M.memory_search
        return await fn(query="alpha", mode="hybrid", limit=10)

    try:
        out = asyncio.run(body())
    finally:
        try:
            asyncio.run(M._shutdown_db())
        except Exception:
            pass
        (M.DB_PATH, M.daemon_request, M._daemon_lock, M._db_init_lock) = orig
    assert out.startswith("Found 10 memories")
    assert out.count("distinct subject") == 9
    assert re.search(r"\(\+\d+ near-identical: #", out)
    assert len(calls) == 1  # the refill pass does not wait for a daemon that did not answer
