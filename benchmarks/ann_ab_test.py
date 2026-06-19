#!/usr/bin/env python3
"""ANN A/B fidelity test — the gate for flipping recall.ann.enabled=true (P5).

Why this exists: benchmarks/locomo/eval_b12.py is self-contained (its own
retrieve_vector) and does NOT exercise embed_daemon's ANN path, so toggling
config + re-running it would not actually test the change. This harness drives
the real code path: sqlite-vec vec0 MATCH (the "ANN" path enabled by
recall.ann.enabled) vs the exact full-table cosine.

sqlite-vec's `WHERE content_embedding MATCH ? AND k = ?` is EXACT brute-force
KNN over normalized vectors, so enabling it should reproduce the exact-cosine
ranking while removing the `ORDER BY m.id DESC LIMIT 500` blind spot of the
default path. This test confirms that empirically on REAL production vectors.

Method (no embedding model load — it uses STORED embeddings as probe queries,
so it tests ranking fidelity on real vectors deterministically):
  * exact  : numpy cosine over ALL embeddings           (ground-truth top-k)
  * ann    : sqlite-vec vec0 MATCH top-k                 (the path P5 enables)
  * cap500 : exact cosine over the 500 highest rowids    (current default path)

PASS when:
  mean overlap@k(ann, exact)  >= ACCEPT (default 0.97)      # ANN is faithful
  AND mean overlap(ann,exact) >= mean overlap(cap500,exact) # never worse than cap
The reported (1 - overlap(cap500, exact)) is the recall the 500-cap costs today.

Read-only: opens the DB with mode=ro and never writes.

Usage:
  python3 benchmarks/ann_ab_test.py [--db PATH] [--k 5] [--probes 200] \
      [--seed 13] [--accept 0.97]
Exit 0 on PASS, 1 on FAIL/regression, 2 on setup error (e.g. sqlite-vec absent).
"""
from __future__ import annotations

import argparse
import os
import random
import sqlite3
import struct
import sys


def _default_db() -> str:
    home = os.path.expanduser("~")
    if sys.platform == "darwin":
        return os.path.join(home, "Library", "Application Support", "mcp-memory", "sqlite_vec.db")
    if sys.platform == "win32":
        return os.path.join(home, "AppData", "Local", "mcp-memory", "sqlite_vec.db")
    return os.path.join(home, ".local", "share", "mcp-memory", "sqlite_vec.db")


def main() -> int:
    ap = argparse.ArgumentParser(description="ANN vs exact-cosine fidelity gate (P5)")
    ap.add_argument("--db", default=_default_db(), help="sqlite_vec.db path (read-only)")
    ap.add_argument("--k", type=int, default=5, help="top-k to compare")
    ap.add_argument("--probes", type=int, default=200, help="number of probe queries")
    ap.add_argument("--seed", type=int, default=13, help="RNG seed (reproducible)")
    ap.add_argument("--accept", type=float, default=0.97, help="min mean ANN/exact overlap@k")
    args = ap.parse_args()

    try:
        import numpy as np
    except ImportError:
        print("FAIL(setup): numpy not available", file=sys.stderr)
        return 2

    if not os.path.isfile(args.db):
        print(f"FAIL(setup): DB not found: {args.db}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True, timeout=10)
    try:
        conn.enable_load_extension(True)
        import sqlite_vec
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
    except Exception as e:  # noqa: BLE001
        print(f"FAIL(setup): sqlite-vec load failed: {e}", file=sys.stderr)
        return 2

    # Load all stored embeddings into a matrix. rowid == memories.id (the join
    # key embed_daemon uses), so highest rowids == newest memories.
    try:
        rows = conn.execute(
            "SELECT rowid, content_embedding FROM memory_embeddings ORDER BY rowid"
        ).fetchall()
    except sqlite3.Error as e:
        print(f"FAIL(setup): cannot read memory_embeddings: {e}", file=sys.stderr)
        return 2

    rowids: list[int] = []
    vecs: list = []
    dim = None
    for rid, blob in rows:
        if not blob:
            continue
        d = len(blob) // 4
        if dim is None:
            dim = d
        if d != dim:
            continue  # skip mixed-dim rows (mid-migration)
        rowids.append(int(rid))
        vecs.append(np.frombuffer(blob, dtype=np.float32))

    n = len(rowids)
    if n < 600:
        print(f"SKIP: only {n} usable embeddings (need >=600 so the 500-cap is exercised). "
              f"Cannot run a meaningful A/B.", file=sys.stderr)
        return 2

    M = np.vstack(vecs)                      # (n, dim)
    # Normalize defensively (stored vectors are already normalized at encode).
    norms = np.linalg.norm(M, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    M = M / norms
    rid_to_idx = {rid: i for i, rid in enumerate(rowids)}
    newest_500 = set(sorted(rowids, reverse=True)[:500])

    rng = random.Random(args.seed)
    probe_idxs = rng.sample(range(n), min(args.probes, n))

    k = args.k
    ann_overlaps = []
    cap_overlaps = []
    ann_eq_exact = 0           # probes where ANN top-k == exact top-k exactly
    cap_misses_true_top1 = 0   # probes whose true nearest is outside the 500-cap

    for qi in probe_idxs:
        q = M[qi]
        self_rid = rowids[qi]  # exclude self by ROWID (MATCH returns rowids, not indices)
        # exact cosine over ALL (normalized → dot product), exclude self
        sims = M @ q
        order = np.argsort(-sims)
        exact_top = [rowids[i] for i in order if i != qi][:k]
        exact_set = set(exact_top)

        # cap500: exact cosine over the 500 newest only, exclude self
        cap_order = [i for i in order if i != qi and rowids[i] in newest_500][:k]
        cap_top = set(rowids[i] for i in cap_order)

        # true nearest neighbour outside the cap?
        if exact_top and exact_top[0] not in newest_500:
            cap_misses_true_top1 += 1

        # ann: sqlite-vec MATCH (exact brute-force KNN), exclude self
        q_bytes = struct.pack(f"{dim}f", *(float(x) for x in q))
        try:
            mrows = conn.execute(
                "SELECT rowid FROM memory_embeddings "
                "WHERE content_embedding MATCH ? AND k = ? ORDER BY distance",
                (q_bytes, k + 1),
            ).fetchall()
        except sqlite3.Error as e:
            print(f"FAIL: vec0 MATCH errored mid-run: {e}", file=sys.stderr)
            return 1
        ann_top = [int(r[0]) for r in mrows if int(r[0]) != self_rid and int(r[0]) in rid_to_idx][:k]
        ann_set = set(ann_top)

        ann_overlaps.append(len(ann_set & exact_set) / float(k))
        cap_overlaps.append(len(cap_top & exact_set) / float(k))
        if ann_top == exact_top:
            ann_eq_exact += 1

    conn.close()

    mean_ann = sum(ann_overlaps) / len(ann_overlaps)
    mean_cap = sum(cap_overlaps) / len(cap_overlaps)
    p = len(probe_idxs)

    print("=" * 64)
    print(f"ANN A/B fidelity test  (DB={args.db})")
    print(f"  embeddings: {n}  dim: {dim}  probes: {p}  k: {k}  seed: {args.seed}")
    print("-" * 64)
    print(f"  ANN (vec0 MATCH)   mean overlap@{k} vs exact : {mean_ann:.4f}")
    print(f"  cap500 (current)   mean overlap@{k} vs exact : {mean_cap:.4f}")
    print(f"  ANN exactly == exact top-{k}                 : {ann_eq_exact}/{p} "
          f"({100.0*ann_eq_exact/p:.1f}%)")
    print(f"  probes whose TRUE nearest is beyond the 500-cap: {cap_misses_true_top1}/{p} "
          f"({100.0*cap_misses_true_top1/p:.1f}%)  <- recall the cap loses today")
    print("-" * 64)

    ok_fidelity = mean_ann >= args.accept
    ok_not_worse = mean_ann >= mean_cap - 1e-9
    passed = ok_fidelity and ok_not_worse
    print(f"  fidelity   ANN/exact >= {args.accept}: {'PASS' if ok_fidelity else 'FAIL'}")
    print(f"  no-regress ANN >= cap500           : {'PASS' if ok_not_worse else 'FAIL'}")
    print(f"  RESULT: {'PASS' if passed else 'FAIL'}")
    print("=" * 64)
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
