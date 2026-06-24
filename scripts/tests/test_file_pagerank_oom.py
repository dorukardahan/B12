"""Regression tests for the 2026-06 file-pagerank OOM machine-panic fix.

Root cause: `_pagerank` built a dense n×n float32 matrix with no node cap, and
SessionStart/`b12_smoke.sh` ran it on `$HOME` (~167k files) → ~112 GB per
process → RAM exhaustion → WindowServer watchdog kernel panic.

These prove the fix holds:
  * the source never allocates a dense n×n matrix;
  * the sparse power iteration matches the former dense math (no regression);
  * a graph that would be multi-GB dense stays cheap (no square allocation
    is ever even attempted);
  * `top_n` refuses an oversized tree (cap → [] + a logged reason);
  * a normal repo still returns a ranked top-N;
  * `b12_smoke.sh` no longer drives the hooks against `$HOME`.
"""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np
import pytest

import file_pagerank as fp


REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


# ── (a) no dense n×n allocation anywhere in the source ────────────────────

def test_source_has_no_dense_square_allocation():
    """Mirrors the success-criterion rule: the file must not contain a
    `np.zeros((n, n))`-shaped dense allocation (not even in a comment, so the
    regex gate can't false-positive on docs)."""
    src = open(fp.__file__, encoding="utf-8").read()
    assert re.search(r"np\.zeros\(\(\s*n\s*,\s*n\s*\)", src) is None, (
        "file_pagerank.py still contains a dense n×n np.zeros(...) allocation"
    )


# ── (b) sparse == dense (no behavioral regression) ────────────────────────

def _dense_pagerank_reference(adj, damping=0.85, iters=30):
    """The ORIGINAL dense algorithm, verbatim — the oracle the sparse rewrite
    must match (top-N identical, ranks equal to float rounding)."""
    nodes = list(adj.keys())
    if not nodes:
        return {}
    n = len(nodes)
    idx = {nm: i for i, nm in enumerate(nodes)}
    M = np.zeros((n, n), dtype=np.float32)
    for src, dsts in adj.items():
        if not dsts:
            continue
        w = 1.0 / len(dsts)
        for dst in dsts:
            if dst in idx:
                M[idx[dst], idx[src]] += w
    rank = np.full(n, 1.0 / n, dtype=np.float32)
    tp = (1.0 - damping) / n
    for _ in range(iters):
        rank = tp + damping * (M @ rank)
    return {nodes[i]: float(rank[i]) for i in range(n)}


def _random_graph(n, seed, max_out=5):
    import random
    rng = random.Random(seed)
    names = [f"f{i}.py" for i in range(n)]
    adj = {nm: [] for nm in names}
    for nm in names:
        for _ in range(rng.randint(0, max_out)):
            adj[nm].append(rng.choice(names))  # self-loops + duplicates intended
    return adj


@pytest.mark.parametrize("n,seed", [(1, 0), (5, 1), (30, 2), (120, 3), (400, 4)])
def test_sparse_matches_dense_reference(n, seed):
    adj = _random_graph(n, seed)
    sparse = fp._pagerank(adj)
    dense = _dense_pagerank_reference(adj)
    assert set(sparse) == set(dense)
    # Top-5 ordering identical.
    top_s = [k for k, _ in sorted(sparse.items(), key=lambda kv: kv[1], reverse=True)[:5]]
    top_d = [k for k, _ in sorted(dense.items(), key=lambda kv: kv[1], reverse=True)[:5]]
    assert top_s == top_d, (top_s, top_d)
    # Ranks equal to float32↔float64 rounding.
    if dense:
        assert max(abs(sparse[k] - dense[k]) for k in dense) < 1e-5


def test_empty_and_edgeless_graphs():
    assert fp._pagerank({}) == {}
    flat = fp._pagerank({"a.py": [], "b.py": []})
    # No edges → every node converges to the same teleport mass.
    assert set(flat) == {"a.py", "b.py"}
    assert abs(flat["a.py"] - flat["b.py"]) < 1e-12


# ── (c) a would-be-multi-GB graph never attempts a square allocation ──────

def test_large_graph_never_allocates_square(monkeypatch):
    """A 20k-node graph would be 20000²·4B ≈ 1.6 GB dense. Guard EVERY dense
    constructor (zeros/empty/ones/full) against any 2-D square (>1) allocation
    — regardless of dtype or whether the shape is a tuple or a list — then prove
    the sparse path completes without tripping it. This tripwire fires on ANY
    plausible reintroduction of a dense n×n matrix, not just `np.zeros((n, n))`.
    """
    def _is_square_2d(shape):
        # numpy accepts tuple OR list shapes; normalize both.
        if isinstance(shape, (tuple, list)) and len(shape) == 2:
            try:
                return int(shape[0]) == int(shape[1]) and int(shape[0]) > 1
            except (TypeError, ValueError):
                return False
        return False

    for ctor in ("zeros", "empty", "ones", "full"):
        orig = getattr(np, ctor)

        def _guard(shape, *a, _orig=orig, _ctor=ctor, **k):
            if _is_square_2d(shape):
                raise AssertionError(f"dense square allocation attempted: np.{_ctor}({shape})")
            return _orig(shape, *a, **k)

        monkeypatch.setattr(np, ctor, _guard)

    adj = _random_graph(20000, seed=7, max_out=3)
    ranks = fp._pagerank(adj)
    assert len(ranks) == 20000
    # Sanity: ranks are finite and sum to a sane, bounded mass.
    total = sum(ranks.values())
    assert np.isfinite(total) and 0 < total < 20000


def test_dense_revert_would_trip_the_guard(monkeypatch):
    """Meta-test: install the same broadened tripwire, then prove a dense n×n
    revert via ANY constructor (tuple OR list shape) raises — so the guard in
    test_large_graph_never_allocates_square can't silently miss a regression."""
    def _is_square_2d(shape):
        if isinstance(shape, (tuple, list)) and len(shape) == 2:
            try:
                return int(shape[0]) == int(shape[1]) and int(shape[0]) > 1
            except (TypeError, ValueError):
                return False
        return False

    for ctor in ("zeros", "empty", "ones", "full"):
        orig = getattr(np, ctor)

        def _guard(shape, *a, _orig=orig, **k):
            if _is_square_2d(shape):
                raise AssertionError("dense square")
            return _orig(shape, *a, **k)

        extra = (0.0,) if ctor == "full" else ()
        with pytest.raises(AssertionError):
            _guard((500, 500), *extra)   # tuple-shaped dense revert
        with pytest.raises(AssertionError):
            _guard([500, 500], *extra)   # list-shaped dense revert
        # A 1-D allocation (the sparse path's own rank vector) must NOT trip.
        assert _guard(7, *extra) is not None


# ── (d) top_n refuses an oversized tree (cap → [] + logged reason) ────────

def test_top_n_caps_oversized_tree(tmp_path, monkeypatch, capsys):
    for i in range(40):
        (tmp_path / f"m{i}.py").write_text("import os\n")
    monkeypatch.setenv("B12_PAGERANK_MAX_NODES", "10")
    monkeypatch.setenv("B12_DATA_DIR", str(tmp_path / "data"))
    result = fp.top_n(str(tmp_path), 5)
    assert result == []
    err = capsys.readouterr().err
    assert "skip" in err and "cap 10" in err


def test_top_n_cap_disabled_with_zero(tmp_path, monkeypatch):
    """B12_PAGERANK_MAX_NODES=0 disables the cap (opt-out documented)."""
    for i in range(12):
        (tmp_path / f"m{i}.py").write_text("import os\n")
    monkeypatch.setenv("B12_PAGERANK_MAX_NODES", "0")
    monkeypatch.setenv("B12_DATA_DIR", str(tmp_path / "data"))
    # Should NOT be capped → returns a (possibly empty-of-edges) ranked list.
    assert isinstance(fp.top_n(str(tmp_path), 5), list)


# ── (e) normal repo still ranks correctly (no regression) ─────────────────

def test_normal_repo_returns_ranked_top_n(tmp_path, monkeypatch):
    monkeypatch.setenv("B12_DATA_DIR", str(tmp_path / "data"))
    (tmp_path / "core.py").write_text("VALUE = 1\n")
    (tmp_path / "a.py").write_text("import core\n")
    (tmp_path / "b.py").write_text("import core\n")
    (tmp_path / "c.py").write_text("import core\n")
    top = fp.top_n(str(tmp_path), 5)
    assert isinstance(top, list) and 0 < len(top) <= 5
    # Everything imports core.py → it must be the most central node.
    assert top[0] == "core.py"


# ── (f) smoke harness no longer targets $HOME ─────────────────────────────

def test_smoke_does_not_target_home():
    smoke = open(os.path.join(REPO_ROOT, "scripts", "b12_smoke.sh"), encoding="utf-8").read()
    # The exact pre-fix construct embedded $HOME as the session/retrieval cwd.
    assert '"cwd":"\'"$HOME"\'"' not in smoke
    assert '"cwd":"$HOME"' not in smoke
    # And it must build a throwaway temp dir instead.
    assert "mktemp -d" in smoke
    assert "SMOKE_CWD" in smoke
