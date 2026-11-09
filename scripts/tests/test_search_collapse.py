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
