#!/usr/bin/env python3
"""S5 mini-eval — BGE-M3 quantization variant comparison.

10 hand-picked queries (5 EN, 5 TR/mixed) drawn from realistic B12 use:
"explain X", "what did we decide about Y", "error around Z". For each
configured backend we measure:

  - first-load latency (model init)
  - per-query encode latency p50 / p95
  - approximate MRR over the active production DB
  - on-disk model size

Backends auto-detected:
  - `st-fp32`   : sentence-transformers BAAI/bge-m3 (FP32, ~2.2GB)
  - `gguf-q8`  : llama-cpp-python + B12_EMBED_GGUF_Q8_PATH
  - `gguf-q4`  : llama-cpp-python + B12_EMBED_GGUF_Q4_PATH

When a backend is unavailable the row reports `unavailable` and the
script still emits a markdown table for the backends that DID run.

Output: writes the markdown table to docs/B12_embed_quant_eval_2026-05.md
(or path overridden via --out). Stdout shows the same table.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import struct
import sys
import time
from typing import Any


QUERIES = [
    # English
    "BGE-M3 multilingual embedding migration",
    "what did we decide about checkpoint trigger threshold",
    "error TS2300 duplicate identifier in routes",
    "RedactedProject RedactedProject benchmark methodology",
    "0G content marketing approved topics",
    # Turkish / mixed
    "B12 hatırlatıcı sistem nasıl çalışıyor",
    "AytuncYildizli/B12 PR-19 mahobrain port",
    "kimi cli mcp config drift",
    "Hermes session split context drift",
    "OpenCode GLM-5 optimization config",
]


def _anonymize_path(p: str) -> str:
    """Replace the running user's home prefix with ``~`` so the committed
    eval markdown doesn't leak ``/Users/<name>`` or ``/home/<name>``."""
    home = os.path.expanduser('~')
    if home and p.startswith(home):
        return '~' + p[len(home):]
    return p


def _default_db_path() -> str:
    if sys.platform == 'darwin':
        return os.path.expanduser(
            '~/Library/Application Support/mcp-memory/sqlite_vec.db')
    if os.path.isdir(os.path.expanduser('~/AppData')):
        return os.path.expanduser('~/AppData/Local/mcp-memory/sqlite_vec.db')
    return os.path.expanduser('~/.local/share/mcp-memory/sqlite_vec.db')


def _file_size_mb(p: str) -> float | None:
    try:
        return os.path.getsize(p) / (1024 * 1024)
    except OSError:
        return None


def _disk_for_st(model_name: str) -> float | None:
    """Best-effort: walk the HF cache for the model dir size."""
    cache = os.environ.get('HF_HOME') or os.environ.get('TRANSFORMERS_CACHE') \
        or os.path.expanduser('~/.cache/huggingface/hub')
    target = 'models--' + model_name.replace('/', '--')
    root = os.path.join(cache, target) if os.path.isdir(cache) else None
    if not root or not os.path.isdir(root):
        return None
    total = 0
    for dirpath, _, names in os.walk(root):
        for n in names:
            try:
                total += os.path.getsize(os.path.join(dirpath, n))
            except OSError:
                pass
    return total / (1024 * 1024) if total else None


def _load_st(model_name: str = 'BAAI/bge-m3'):
    from sentence_transformers import SentenceTransformer  # type: ignore
    t0 = time.time()
    m = SentenceTransformer(model_name, device='cpu')
    return m, time.time() - t0


def _load_gguf(path: str):
    from llama_cpp import Llama  # type: ignore
    t0 = time.time()
    m = Llama(model_path=path, embedding=True, n_ctx=8192, verbose=False)
    return m, time.time() - t0


def _encode_st(model, text: str):
    import numpy as np
    v = model.encode([text], normalize_embeddings=True, convert_to_numpy=True)[0]
    return np.asarray(v, dtype=np.float32)


def _encode_gguf(model, text: str):
    import numpy as np
    emb = model.create_embedding(text)
    v = np.asarray(emb['data'][0]['embedding'], dtype=np.float32)
    n = float(np.linalg.norm(v))
    if n > 0:
        v = v / n
    return v


def _load_db_vectors(db_path: str, limit: int = 1000):
    """Return [(id, embedding_np, content)] for evaluation.

    Pulls only memories that already carry an embedding in the active
    production DB so all backends are scored against the same recall
    surface (we don't re-encode the entire DB per backend).
    """
    import sqlite3
    import numpy as np
    try:
        import sqlite_vec  # type: ignore
    except ImportError:
        return []
    conn = sqlite3.connect(db_path, timeout=15)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    rows = conn.execute(
        """
        SELECT m.id,
               SUBSTR(replace(m.content, char(10), ' '), 1, 200),
               e.content_embedding
        FROM memories m
        JOIN memory_embeddings e ON e.rowid = m.id
        WHERE m.deleted_at IS NULL
          AND (m.memory_type IS NULL OR m.memory_type NOT IN ('session_summary', 'progress'))
        LIMIT ?
        """,
        (int(limit),),
    ).fetchall()
    conn.close()
    out = []
    for mid, content, emb_bytes in rows:
        if not emb_bytes:
            continue
        dim = len(emb_bytes) // 4
        v = np.asarray(struct.unpack(f'{dim}f', emb_bytes), dtype=np.float32)
        out.append((int(mid), v, content or ''))
    return out


def _top_n_ids(query_vec, db_vecs, n: int = 5):
    """Return top-N memory IDs by cosine similarity to query_vec."""
    import numpy as np
    qv = np.asarray(query_vec, dtype=np.float32)
    qn = float(np.linalg.norm(qv))
    if qn == 0:
        return []
    qv = qv / qn
    scored = []
    for mid, dv, _ in db_vecs:
        if dv.shape[0] != qv.shape[0]:
            continue
        dn = float(np.linalg.norm(dv))
        if dn == 0:
            continue
        cos = float(qv @ (dv / dn))
        scored.append((mid, cos))
    scored.sort(key=lambda x: x[1], reverse=True)
    return [s[0] for s in scored[:n]]


def _reciprocal_rank_overlap(ids_a, ids_b):
    """MRR proxy: reciprocal rank of GOLD's top-1 in the candidate list.

    1.0 if candidate's top-1 matches gold's top-1; 1/N if gold[0] appears
    at position N in candidates; 0 if gold[0] is absent. This measures
    whether two backends AGREE on the single best result — the metric
    S5's "≤5% quality dip" gate actually needs. The previous overlap-
    anywhere variant returned 1.0 whenever candidate's top-1 hit any
    gold member, which inflated agreement.

    Returns ``None`` when gold is empty (e.g., the gold backend itself
    produced no results for that query because of a dim mismatch or
    encode failure). ``None`` signals "invalid benchmark input — skip
    this query in the average" rather than the false-low signal of 0.0.
    """
    if not ids_b:
        return None
    target = ids_b[0]
    for rank, mid in enumerate(ids_a, start=1):
        if mid == target:
            return 1.0 / rank
    return 0.0


def _bench_backend(name: str, model, encode_fn, queries: list[str],
                   db_vecs, gold_per_query: list[list[int]] | None):
    latencies_ms: list[float] = []
    ids_per_query: list[list[int]] = []
    # Dimension-compatibility probe — if the backend produces vectors
    # whose shape doesn't match the DB's stored embeddings, every
    # `_top_n_ids` call would silently return [] (the loop drops every
    # row at the `dv.shape[0] != qv.shape[0]` filter). Flag the whole
    # backend as invalid so the report can tell the reader "this
    # backend is mispointed" instead of just blanking the MRR cell.
    if queries and db_vecs:
        import numpy as _np
        probe_t0 = time.time()
        try:
            probe_v = _np.asarray(encode_fn(model, queries[0]), dtype=_np.float32)
        except Exception as e:
            return {
                'name': name,
                'p50_ms': 0.0, 'p95_ms': 0.0, 'mrr_vs_gold': None,
                'mrr_valid_queries': 0, 'mrr_skipped_queries': len(queries),
                'error': f'encode_failed: {type(e).__name__}',
                'ids_per_query': [],
            }
        latencies_ms.append((time.time() - probe_t0) * 1000)
        ids_per_query.append(_top_n_ids(probe_v, db_vecs, n=5))
        db_dim = int(db_vecs[0][1].shape[0])
        probe_dim = int(probe_v.shape[0])
        if probe_dim != db_dim:
            return {
                'name': name,
                'p50_ms': round(latencies_ms[0], 1),
                'p95_ms': round(latencies_ms[0], 1),
                'mrr_vs_gold': None,
                'mrr_valid_queries': 0,
                'mrr_skipped_queries': len(queries),
                'error': f'dim_mismatch: backend={probe_dim} db={db_dim}',
                'ids_per_query': [],
            }
        # First query already encoded; continue from index 1.
        queries = queries[1:]
    for q in queries:
        t0 = time.time()
        v = encode_fn(model, q)
        latencies_ms.append((time.time() - t0) * 1000)
        ids = _top_n_ids(v, db_vecs, n=5)
        ids_per_query.append(ids)
    p50 = statistics.median(latencies_ms) if latencies_ms else 0.0
    if not latencies_ms:
        p95 = 0.0
    elif len(latencies_ms) == 1:
        p95 = latencies_ms[0]
    else:
        sorted_lat = sorted(latencies_ms)
        idx = min(len(sorted_lat) - 1, math.ceil(0.95 * len(sorted_lat)) - 1)
        p95 = sorted_lat[max(0, idx)]
    mrr = None
    valid_queries = 0
    skipped_queries = 0
    if gold_per_query is not None:
        scored: list[float] = []
        for ids, gold in zip(ids_per_query, gold_per_query):
            rr = _reciprocal_rank_overlap(ids, gold)
            if rr is None:
                skipped_queries += 1
                continue
            scored.append(rr)
            valid_queries += 1
        mrr = sum(scored) / len(scored) if scored else None
    return {
        'name': name,
        'p50_ms': round(p50, 1),
        'p95_ms': round(p95, 1),
        'mrr_vs_gold': round(mrr, 3) if mrr is not None else None,
        'mrr_valid_queries': valid_queries,
        'mrr_skipped_queries': skipped_queries,
        'ids_per_query': ids_per_query,
    }


def _format_markdown(rows: list[dict], db_size: int, header: dict) -> str:
    lines = []
    lines.append('# B12 BGE-M3 quantization mini-eval')
    lines.append('')
    lines.append(f'- **Date:** {header["date"]}')
    lines.append(f'- **DB path:** `{header["db_path"]}`')
    lines.append(f'- **DB embedded rows scored:** {db_size}')
    lines.append(f'- **Queries:** {len(QUERIES)} (5 EN + 5 TR/mixed)')
    lines.append(f'- **Host:** {header["host"]}')
    lines.append(f'- **Plan reference:** docs/B12_proactive_recall_plan_2026-05-18.md (S5)')
    lines.append('')
    lines.append('| Backend | Load (s) | Disk (MB) | Encode p50 (ms) | Encode p95 (ms) | MRR vs gold | MRR coverage |')
    lines.append('|---------|---------:|----------:|---------------:|---------------:|------------:|--------------|')
    for r in rows:
        load = '—' if r.get('load_s') is None else f'{r["load_s"]:.1f}'
        disk = '—' if r.get('disk_mb') is None else f'{r["disk_mb"]:.0f}'
        if r.get('error'):
            lines.append(
                f'| `{r["name"]}` | unavailable | — | — | — | — | — '
                f' (`{r["error"][:60]}`) |'
            )
            continue
        mrr = '—' if r['mrr_vs_gold'] is None else f'{r["mrr_vs_gold"]:.3f}'
        # Coverage: `valid/total` queries that contributed to the MRR mean.
        # Queries skipped because gold backend produced empty top-k (e.g.
        # dim mismatch, encode failure) shouldn't drag the mean down with
        # a phantom 0; report them out-of-band so a reader can tell
        # "Q4 underperformed" from "FP32 gold itself failed on N queries".
        _valid = r.get('mrr_valid_queries')
        _skipped = r.get('mrr_skipped_queries')
        if _valid is None and _skipped is None:
            coverage = '—'
        else:
            _v = int(_valid or 0)
            _s = int(_skipped or 0)
            _total = _v + _s
            coverage = f'{_v}/{_total}'
            if _s > 0:
                coverage += f' (skipped {_s})'
        lines.append(
            f'| `{r["name"]}` | {load} | {disk} | {r["p50_ms"]:.1f} | {r["p95_ms"]:.1f} | {mrr} | {coverage} |'
        )
    lines.append('')
    lines.append('## Decision')
    lines.append('')
    lines.append('The plan (S5) says swap default to Q4_K_M only if `quality dip < 5% AND speed gain > 40%` vs Q8_0. If `gguf-q8` and `gguf-q4` rows above were both unavailable (no GGUFs on disk), this eval establishes the FP32 baseline and parks the quantization choice as a follow-up PR.')
    lines.append('')
    lines.append('Default stays at `BAAI/bge-m3` (FP32 via sentence-transformers) until a host runs this script with both GGUFs present.')
    lines.append('')
    return '\n'.join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--db', default=_default_db_path())
    p.add_argument('--out', default='docs/B12_embed_quant_eval_2026-05.md')
    p.add_argument('--limit', type=int, default=1000,
                   help='Max rows from DB to score against (default 1000).')
    p.add_argument('--self-test', action='store_true')
    args = p.parse_args(argv)

    if args.self_test:
        # Verify the harness mechanics on a tiny in-memory case.
        from types import SimpleNamespace
        import numpy as np
        dim = 4
        # Build a stub backend that returns deterministic vectors.
        def stub_encode(_m, t):
            seed = abs(hash(t)) % (2**31)
            return np.asarray(
                [((seed + i) * 1103515245 % 2**31) / 2**31 for i in range(dim)],
                dtype=np.float32,
            )
        db = [
            (1, np.asarray([1, 0, 0, 0], dtype=np.float32), 'one'),
            (2, np.asarray([0, 1, 0, 0], dtype=np.float32), 'two'),
            (3, np.asarray([0, 0, 1, 0], dtype=np.float32), 'three'),
        ]
        res = _bench_backend('stub', None, stub_encode, ['a', 'b'], db, None)
        assert res['p50_ms'] >= 0
        assert res['p95_ms'] >= 0
        # gold-top-1 MRR: candidates [1,2,3] vs gold[0]=3 → rank 3 → 1/3
        rr = _reciprocal_rank_overlap([1, 2, 3], [3, 1, 2])
        assert rr is not None and abs(rr - 1.0/3.0) < 1e-9, f'gold-top1 at rank 3 expected 1/3 got {rr}'
        # exact match
        rr_perfect = _reciprocal_rank_overlap([3, 1, 2], [3, 1, 2])
        assert rr_perfect == 1.0, f'perfect match expected 1.0 got {rr_perfect}'
        # gold[0] absent from candidates
        rr2 = _reciprocal_rank_overlap([99, 5], [1, 2, 3])
        assert rr2 == 0.0
        # regression guard: overlap-anywhere variant would score 1.0 here
        # (candidate top-1 = A is in gold) — we want 0.0 because gold[0] != A.
        rr_anti = _reciprocal_rank_overlap(['A', 'X', 'Y'], ['B', 'C', 'D', 'E', 'A'])
        assert rr_anti == 0.0, f'gold-top-1 = B not in cands expected 0.0 got {rr_anti}'
        # empty-gold guard: gold[] (e.g., dim-mismatch produced no candidates)
        # must return None, NOT 0.0 — 0.0 would silently lower the MRR mean.
        rr_empty = _reciprocal_rank_overlap([1, 2, 3], [])
        assert rr_empty is None, f'empty gold expected None got {rr_empty}'
        # p95 nearest-rank guard
        import math as _math
        n = 10
        idx = min(n - 1, _math.ceil(0.95 * n) - 1)
        assert idx == 9, f'n=10 p95 nearest-rank idx expected 9 got {idx}'
        # anonymize_path guard
        home = os.path.expanduser('~')
        if home:
            assert _anonymize_path(home + '/x.db') == '~/x.db'
        assert _anonymize_path('/var/abs.db') == '/var/abs.db'
        print('SELF-TEST OK (8 cases: stub bench, gold-top1 partial/perfect/miss, overlap-anywhere regression guard, empty-gold None guard, p95 nearest-rank, anonymize-path)')
        return 0

    db_vecs = _load_db_vectors(args.db, limit=args.limit)
    if not db_vecs:
        print(f'No vectors in {args.db} — abort.', file=sys.stderr)
        return 1

    backends: list[dict] = []
    # Always try FP32 first — it's the gold/baseline.
    try:
        m, load_s = _load_st()
        disk = _disk_for_st('BAAI/bge-m3')
        res = _bench_backend('st-fp32', m, _encode_st, QUERIES, db_vecs, None)
        res['load_s'] = round(load_s, 2)
        res['disk_mb'] = disk
        backends.append(res)
        gold_per_query = res['ids_per_query']
    except Exception as e:
        backends.append({'name': 'st-fp32', 'error': str(e),
                         'p50_ms': None, 'p95_ms': None, 'mrr_vs_gold': None,
                         'load_s': None, 'disk_mb': None})
        gold_per_query = None

    # gguf-q8
    q8 = os.environ.get('B12_EMBED_GGUF_Q8_PATH', '').strip()
    if q8 and os.path.exists(q8):
        try:
            m, load_s = _load_gguf(q8)
            res = _bench_backend('gguf-q8', m, _encode_gguf, QUERIES, db_vecs, gold_per_query)
            res['load_s'] = round(load_s, 2)
            res['disk_mb'] = _file_size_mb(q8)
            backends.append(res)
        except Exception as e:
            backends.append({'name': 'gguf-q8', 'error': str(e)})
    else:
        backends.append({'name': 'gguf-q8',
                         'error': 'B12_EMBED_GGUF_Q8_PATH unset or missing'})

    # gguf-q4
    q4 = os.environ.get('B12_EMBED_GGUF_Q4_PATH', '').strip()
    if q4 and os.path.exists(q4):
        try:
            m, load_s = _load_gguf(q4)
            res = _bench_backend('gguf-q4', m, _encode_gguf, QUERIES, db_vecs, gold_per_query)
            res['load_s'] = round(load_s, 2)
            res['disk_mb'] = _file_size_mb(q4)
            backends.append(res)
        except Exception as e:
            backends.append({'name': 'gguf-q4', 'error': str(e)})
    else:
        backends.append({'name': 'gguf-q4',
                         'error': 'B12_EMBED_GGUF_Q4_PATH unset or missing'})

    md = _format_markdown(
        backends,
        db_size=len(db_vecs),
        header={
            'date': time.strftime('%Y-%m-%d %H:%M %Z'),
            'db_path': _anonymize_path(args.db),
            'host': f'{sys.platform} python{sys.version.split()[0]}',
        },
    )
    os.makedirs(os.path.dirname(args.out) or '.', exist_ok=True)
    with open(args.out, 'w') as f:
        f.write(md)
    print(md)
    print(f'\nWrote {args.out}', file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
