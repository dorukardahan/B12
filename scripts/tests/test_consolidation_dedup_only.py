"""Guards for the nightly consolidation: strict dedup only, merging opt-in.

A dry run once proposed 10,200 merges (cosine 0.80-0.95, no type or project
check) over 15,962 memories. Merging now needs allow_merge=True and NLI; dedup
needs the same allowed type, the same project, cosine >= 0.97, similar length
and high token overlap; a run applies at most MAX_DEDUP_PER_RUN dedups and
changes nothing when the candidate count looks anomalous.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
ce = pytest.importorskip("consolidation_engine")

TEXT = "sqlite busy_timeout must be set before the first write on every new connection"


def _mem(i, content=TEXT, mtype="learning", tags="proj:b12", emb=None):
    return ce.MemoryRecord(id=i, content_hash=f"h{i}", content=content, tags=tags, memory_type=mtype,
                           metadata_str="{}", strength=1.0,
                           embedding=emb if emb is not None else np.ones(8, dtype=np.float32))


@pytest.mark.parametrize("b,expected", [
    (_mem(2), True),
    (_mem(2, mtype="decision"), False),                       # timeline type
    (_mem(2, tags="proj:other"), False),                       # other project
    (_mem(2, tags="checkpoint"), False),                       # no project
    (_mem(2, content="sqlite busy_timeout"), False),           # length ratio
    (_mem(2, content=TEXT.replace("before the first write", "after a failed read")), False),  # token overlap
])
def test_near_duplicate_rules(b, expected):
    a = _mem(1, mtype=b.memory_type if b.memory_type == "decision" else "learning")
    assert ce._is_near_duplicate(a, b) is expected


def _run(monkeypatch, clusters, nli=False, **kw):
    applied = {"dedup": 0, "merge": 0}
    conn = sqlite3.connect(":memory:")
    monkeypatch.setattr(ce, "_open_db", lambda path: conn)
    monkeypatch.setattr(ce.os.path, "exists", lambda path: True)
    monkeypatch.setattr(ce, "_load_memories", lambda c, project=None: [m for cl in clusters for m in cl])
    monkeypatch.setattr(ce, "_cluster_memories", lambda mems, min_cluster_size=3: dict(enumerate(clusters)))
    monkeypatch.setattr(ce, "_daemon_alive", lambda: nli)

    def dedup(c, a, b):
        applied["dedup"] += 1
        return min(a.id, b.id)

    def merge(c, group):
        applied["merge"] += 1
        return group[0].id

    monkeypatch.setattr(ce, "_apply_dedup", dedup)
    monkeypatch.setattr(ce, "_apply_merge", merge)
    return ce.consolidate(db_path="x.db", dry_run=False, min_cluster_size=2, **kw), applied


def _similar_pair(base, i):
    # cosine ~0.9: a merge candidate, not a duplicate
    e1 = np.zeros(8, dtype=np.float32); e1[0] = 1
    e2 = np.zeros(8, dtype=np.float32); e2[0] = 0.9; e2[1] = 0.43
    return [_mem(base + i, content=f"fact {i} about deploys", emb=e1),
            _mem(base + i + 1, content=f"another fact {i} about builds", emb=e2)]


def test_similar_pairs_are_reported_not_merged(monkeypatch):
    result, applied = _run(monkeypatch, [_similar_pair(0, 0)])
    assert applied["merge"] == 0 and result.memories_merged == 0
    assert result.merge_candidates == 1


def test_merge_needs_nli_even_when_allowed(monkeypatch):
    result, applied = _run(monkeypatch, [_similar_pair(0, 0)], allow_merge=True)
    assert applied["merge"] == 0  # _daemon_alive is False: no NLI, no merge


def test_per_run_limit_defers_the_rest(monkeypatch):
    monkeypatch.setattr(ce, "MAX_DEDUP_PER_RUN", 3)
    clusters = [[_mem(10 * k + 1), _mem(10 * k + 2)] for k in range(5)]
    result, applied = _run(monkeypatch, clusters)
    assert applied["dedup"] == 3 and result.memories_deduplicated == 3
    assert result.dedup_deferred == 2 and not result.aborted


def test_anomalous_candidate_count_changes_nothing(monkeypatch):
    monkeypatch.setattr(ce, "MAX_DEDUP_PER_RUN", 3)
    monkeypatch.setattr(ce, "DEDUP_ANOMALY", 4)
    clusters = [[_mem(10 * k + 1), _mem(10 * k + 2)] for k in range(5)]
    result, _ = _run(monkeypatch, clusters)
    assert result.aborted and result.memories_deduplicated == 0


def test_nightly_script_fails_when_aborted():
    src = (Path(__file__).resolve().parents[2] / "hooks" / "memory-consolidate.py").read_text()
    assert "if result.aborted:\n            sys.exit(1)" in src


def test_large_duplicate_cluster_defers_per_row_not_per_pair(monkeypatch):
    # 10 identical rows need 9 deletions. Counting every remaining pair as deferred
    # made this look like an anomaly and rolled back every night (Codex review P2).
    monkeypatch.setattr(ce, "MAX_DEDUP_PER_RUN", 3)
    monkeypatch.setattr(ce, "DEDUP_ANOMALY", 20)
    result, applied = _run(monkeypatch, [[_mem(i) for i in range(1, 11)]])
    assert not result.aborted
    assert applied["dedup"] == 3 and result.dedup_deferred == 6


def test_merge_only_for_pairs_with_an_nli_verdict(monkeypatch):
    # The daemon caps NLI batches at 20 pairs; a pair without a verdict must not merge.
    e1 = np.zeros(8, dtype=np.float32); e1[0] = 1
    e2 = np.zeros(8, dtype=np.float32); e2[0] = 0.9; e2[1] = 0.43
    cluster = [_mem(1, content="the staging deploy uses blue green rollout", emb=e1),
               _mem(2, content="the staging deploy uses canary rollout steps", emb=e2),
               _mem(3, content="the staging deploy uses rolling update batches", emb=e2)]
    verdicts = [{"scores": {"contradiction": 0.01}}]  # only the first pair was checked
    monkeypatch.setattr(ce, "_nli_check_pairs", lambda pairs: verdicts)
    result, applied = _run(monkeypatch, [cluster], nli=True, allow_merge=True)
    assert applied["merge"] == 1                # one group, from the verified pair only
    assert result.memories_merged == 1
    assert result.merge_candidates == 2         # the two unchecked pairs are only reported
