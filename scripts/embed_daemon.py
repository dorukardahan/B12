#!/usr/bin/env python3
"""
B12 Embedding Daemon — persistent SentenceTransformer server over Unix socket.

Loads the embedding model once and serves operations over a Unix domain socket.
Started at SessionStart (background), communicates with hooks via line-delimited
JSON. Falls back gracefully — hooks check daemon_alive() before every request.

Protocol:
  Request:  {"op": "...", ...}\n
  Response: {"ok": true/false, ...}\n

Operations:
  semantic_search  {query, db_path, limit}      → {results: [{id, display, score}]}
  rerank           {query, db_path, ids}         → {ranked_ids: [int]}
  encode_batch     {texts}                       → {embeddings: [base64]}
  nli_check        {pairs: [[a,b], ...]}         → {results: [{label, scores}]}
  find_neighbors   {db_path, memory_id, k, min_sim} → {neighbors: [{id, similarity}]}
  find_cluster     {db_path, threshold, min_size, project} → {clusters: [[ids]], count}
  classify         {text}                        → {type: str, confidence: float}
  health           {}                            → {uptime, requests_served}
  shutdown         {}                            → {} + daemon exits

Lifecycle:
  - Socket created AFTER model load (daemon_alive = model ready)
  - Self-terminates after IDLE_TIMEOUT seconds of inactivity
  - SIGTERM/SIGINT → clean shutdown with atexit cleanup
  - Per-connection timeout prevents hung clients
"""

import atexit
import base64
import fcntl
import json
import os
import signal
import socket
import sqlite3
import struct
import sys
import time
import warnings

# B12 user config (~/.B12/config.toml). Stdlib-only reader; safe fallbacks
# when the file is missing — the daemon must work without any config at all.
try:
    _here = os.path.dirname(os.path.abspath(__file__))
    if _here not in sys.path:
        sys.path.insert(0, _here)
    from b12_config import get as _b12_cfg_get
    from shared_patterns import exact_tag_param, exact_tag_predicate
except Exception:  # pragma: no cover — never block daemon on config import
    def _b12_cfg_get(*_path, default=None):
        return default
    def exact_tag_predicate(column: str = "tags") -> str:
        normalized = f"replace(replace(COALESCE({column}, ''), ', ', ','), ' ,', ',')"
        return f"(',' || {normalized} || ',') LIKE ? ESCAPE '\\'"
    def exact_tag_param(tag: str) -> str:
        escaped = tag.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        return f"%,{escaped},%"

warnings.filterwarnings('ignore')
os.environ['TOKENIZERS_PARALLELISM'] = 'false'
os.environ.setdefault('WANDB_DISABLED', 'true')
os.environ.setdefault('WANDB_MODE', 'disabled')

_UID = os.getuid() if hasattr(os, 'getuid') else os.getpid()
_RUNTIME_DIR = os.environ.get('B12_EMBED_RUNTIME_DIR', '/tmp')
SOCKET_PATH = os.path.join(_RUNTIME_DIR, f"b12-embed-{_UID}.sock")
PID_PATH = os.path.join(_RUNTIME_DIR, f"b12-embed-{_UID}.pid")
LOCK_PATH = os.path.join(_RUNTIME_DIR, f"b12-embed-{_UID}.lock")
LOG_DIR = os.path.join(os.environ.get('B12_DATA_DIR', os.path.expanduser('~/.B12')), 'memory-logs')
LOG_PATH = os.path.join(LOG_DIR, "embed-daemon.log")
IDLE_TIMEOUT = 7200  # 2 hours
CONN_TIMEOUT = 15    # Per-connection read timeout (BGE-M3 batches can run >10s)
# Production default: BGE-M3 (1024-dim, multilingual, cls pooling). Override
# via MCP_EMBEDDING_MODEL for benchmarks or follow-up Q4_K_M GGUF rollout.
MODEL_NAME = os.environ.get('MCP_EMBEDDING_MODEL', 'BAAI/bge-m3')
# Embedding dim is derived from the loaded model at runtime — never assume 384.
EXPECTED_DIM = None  # set after model load
# Backend selector: sentence-transformers (default) | gguf (opt-in via env)
EMBED_BACKEND = os.environ.get('B12_EMBED_BACKEND', 'sentence-transformers').lower()


def log(msg):
    """Append timestamped message with PID to daemon log file."""
    try:
        with open(LOG_PATH, 'a') as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [{os.getpid()}] {msg}\n")
    except Exception:
        pass


def cleanup():
    """Remove socket and PID files (registered with atexit).
    NOTE: Lock file is NOT removed — flock is released automatically on process
    death. Removing the lock file creates a race where two daemons can each
    flock a different inode of the same path (10% of starts affected)."""
    for path in (SOCKET_PATH, PID_PATH):
        try:
            os.unlink(path)
        except OSError:
            pass


def _open_db(db_path):
    """Open SQLite DB with sqlite-vec extension loaded.
    Uses WAL mode + busy_timeout to avoid blocking MCP server writes."""
    import sqlite_vec
    conn = sqlite3.connect(db_path, timeout=10)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _ann_supported(conn):
    """Return (use_ann, count) given config + table size.
    `use_ann` is True only when both gates pass: [recall.ann].enabled = true
    in ~/.B12/config.toml AND active memory_embeddings count >= threshold_count.
    """
    enabled = bool(_b12_cfg_get("recall", "ann", "enabled", default=False))
    raw_threshold = _b12_cfg_get("recall", "ann", "threshold_count", default=10000)
    threshold = int(raw_threshold) if isinstance(raw_threshold, (int, float)) else 10000
    # P5: clamp to a sane range so a config typo (0, negative, or absurd) can't
    # either force ANN on for a near-empty table or wedge it off forever.
    threshold = max(100, min(threshold, 1_000_000))
    count = 0
    try:
        row = conn.execute("SELECT COUNT(*) FROM memory_embeddings").fetchone()
        if row:
            raw = row[0]
            if isinstance(raw, int):
                count = raw
    except sqlite3.Error:
        pass
    return (enabled and count >= threshold, count)


def _ann_topk_rowids(conn, q_emb, k):
    """sqlite-vec MATCH top-k rowid+cosine lookup.
    Returns list of (rowid, cos_sim) with cos_sim = 1 - distance.
    Empty list on any error so the caller can fall back to full-scan."""
    try:
        q_bytes = struct.pack(f'{len(q_emb)}f', *(float(x) for x in q_emb))
        rows = conn.execute(
            "SELECT rowid, distance FROM memory_embeddings "
            "WHERE content_embedding MATCH ? AND k = ? ORDER BY distance",
            (q_bytes, int(k)),
        ).fetchall()
        return [(int(r[0]), 1.0 - float(r[1])) for r in rows]
    except sqlite3.Error as e:
        log(f"ann_topk error: {e}")
        return []


def _semantic_search(model, data):
    """Full-table cosine similarity search (matches cold path behavior)."""
    query = data.get('query', '')
    db_path = data.get('db_path', '')
    limit = data.get('limit', 5)

    if not query or not db_path:
        return {'ok': False, 'error': 'missing query or db_path'}

    q_emb = model.encode([query], normalize_embeddings=True)[0]
    conn = _open_db(db_path)

    use_ann, _ = _ann_supported(conn)
    if use_ann:
        # Codex review PR #43 rounds 2+3 P2: the ANN MATCH operator selects
        # the global top-k BEFORE active-memory filters apply (soft-delete,
        # expired, session_summary/progress, project-tag) AND before the
        # downstream similarity threshold / skip_ids ledger filter (in
        # _recall). Oversample 30× so all three layers of attrition still
        # leave enough candidates; if too few survive, take the full-scan.
        topk = _ann_topk_rowids(conn, q_emb, max(limit * 30, 150))
        if not topk:
            # P5: ANN gated on but MATCH returned nothing — a likely sqlite-vec
            # extension failure or empty vec0 table. Silent fall-through would
            # mask it, so surface it. (Full-scan below still serves the query.)
            log("ann(semantic): MATCH returned 0 rows (sqlite-vec failure or empty vec table); using full-scan")
        if topk:
            id_to_sim = {rid: sim for rid, sim in topk}
            ph = ",".join("?" for _ in id_to_sim)
            rows = conn.execute(
                f"""SELECT m.id, '[' || m.memory_type || '] ' || replace(substr(m.content, 1, 300), char(10), ' ')
                    FROM memories m WHERE m.id IN ({ph}) AND m.deleted_at IS NULL
                      AND (m.valid_until IS NULL OR m.valid_until > datetime('now'))
                      AND (m.memory_type IS NULL OR m.memory_type NOT IN ('session_summary', 'progress'))
                      AND (m.tags IS NULL OR m.tags NOT LIKE '%session-summary%')""",
                list(id_to_sim.keys()),
            ).fetchall()
            scores = [{'id': rid, 'display': disp, 'score': round(id_to_sim[rid], 3)}
                      for rid, disp in rows if id_to_sim.get(rid, 0.0) > 0.3]
            scores.sort(key=lambda x: x['score'], reverse=True)
            if len(scores) >= limit:
                conn.close()
                return {'ok': True, 'results': scores[:limit], 'path': 'ann'}
            # Too few filtered survivors — fall through to full-scan.

    # TODO: migrate to sqlite-vec kNN (vec_distance_cosine) for O(log N) search
    rows = conn.execute("""
        SELECT m.id,
               '[' || m.memory_type || '] ' || replace(substr(m.content, 1, 300), char(10), ' '),
               e.content_embedding
        FROM memories m
        JOIN memory_embeddings e ON m.id = e.rowid
        WHERE m.deleted_at IS NULL
          AND (m.valid_until IS NULL OR m.valid_until > datetime('now'))
          AND (m.memory_type IS NULL OR m.memory_type NOT IN ('session_summary', 'progress'))
          AND (m.tags IS NULL OR m.tags NOT LIKE '%session-summary%')
        ORDER BY m.id DESC
        LIMIT 500
    """).fetchall()

    scores = []
    skipped_dim = 0
    for row_id, display, emb_bytes in rows:
        if not emb_bytes:
            continue
        dim = len(emb_bytes) // 4
        # Mixed-dim cosines silently truncate via zip() and produce garbage
        # scores, so fail closed against mismatch until the migration finishes.
        if EXPECTED_DIM is not None and dim != EXPECTED_DIM:
            skipped_dim += 1
            continue
        stored = struct.unpack(f'{dim}f', emb_bytes)
        cos_sim = float(sum(a * b for a, b in zip(q_emb, stored)))
        if cos_sim > 0.3:
            scores.append({'id': row_id, 'display': display, 'score': round(cos_sim, 3)})

    scores.sort(key=lambda x: x['score'], reverse=True)
    conn.close()
    out = {'ok': True, 'results': scores[:limit]}
    if skipped_dim:
        out['dim_skipped'] = skipped_dim
    return out


def _recall(model, data):
    """Hook-side recall op: semantic search + threshold + dedup ledger.

    Single round-trip replacement for `semantic_search` followed by
    client-side filtering. Pushes the dedup logic to the daemon to avoid
    sending all candidate IDs over the socket.

    Input keys (additions over semantic_search):
      threshold : float, minimum cosine to return (default 0.55, Q3 spec)
      skip_ids  : list[int], memory IDs already injected this session (T3)
      limit     : int, max hits to return (default 5)
      project   : str, optional tag filter (e.g. 'B12' → tags LIKE '%proj:B12%')
    """
    # INTENT: ranking here is pure cosine BY DESIGN — not the 4-dim _unified_score
    # (decay/importance/relevance/strength) used by the MCP memory_search tool.
    # This is the cheap, automatic hot-path recall shared by 3 daemon consumers
    # (proactive-surface, working-context, session-start); rich multi-factor
    # ranking belongs to the explicit memory_search. The divergence is deliberate
    # — do not "reconcile" it.
    query = data.get('query', '')
    db_path = data.get('db_path', '')
    limit = int(data.get('limit', 5))
    threshold = float(data.get('threshold', 0.55))
    skip_ids = set(int(x) for x in (data.get('skip_ids') or []) if str(x).isdigit())
    project = (data.get('project') or '').strip()

    if not query or not db_path:
        return {'ok': False, 'error': 'missing query or db_path'}

    q_emb = model.encode([query], normalize_embeddings=True)[0]
    conn = _open_db(db_path)

    # source_session (from metadata.source_session, written by
    # memory-session-end.sh:907 regex pipeline) is surfaced as a tracing
    # anchor — Q4 of the proactive-recall design notes.
    base_sql = """
        SELECT m.id,
               '[' || m.memory_type || '] ' || replace(substr(m.content, 1, 300), char(10), ' ') AS display,
               m.content_hash,
               COALESCE(json_extract(m.metadata, '$.importance_score'), 0.5) AS importance,
               COALESCE(json_extract(m.metadata, '$.project'), '') AS project,
               COALESCE(json_extract(m.metadata, '$.source_session'), '') AS source_session,
               m.memory_type,
               m.content,
               e.content_embedding
        FROM memories m
        JOIN memory_embeddings e ON m.id = e.rowid
        WHERE m.deleted_at IS NULL
          AND (m.valid_until IS NULL OR m.valid_until > datetime('now'))
          AND (m.memory_type IS NULL OR m.memory_type NOT IN ('session_summary', 'progress'))
          AND (m.tags IS NULL OR m.tags NOT LIKE '%session-summary%')
    """
    params: list = []
    if project:
        base_sql += f" AND {exact_tag_predicate('m.tags')}"
        params.append(exact_tag_param(f"proj:{project}"))

    # ANN fast path: gate on config flag + table size. Below threshold or
    # on ANN error/under-fill, fall through to LIMIT-500 + numpy full-scan.
    # Codex review PR #43 rounds 2+3 P2: oversample 30× so the active-
    # memory filter + skip_ids ledger + similarity threshold (all applied
    # downstream in the numpy pass) leave enough candidates after
    # exclusion. Residual edge: a session that already injected all
    # top-150 nearest memories sees fewer than `limit` hits — that is
    # desirable signal (no fresh memories above threshold), not a bug.
    use_ann, _ = _ann_supported(conn)
    rows = None
    if use_ann:
        topk = _ann_topk_rowids(conn, q_emb, max(limit * 30, 150))
        if not topk:
            # P5: ANN gated on but MATCH returned nothing — likely sqlite-vec
            # failure or empty vec0 table. Surface it instead of silently
            # falling through. (Full-scan below still serves the recall.)
            log("ann(recall): MATCH returned 0 rows (sqlite-vec failure or empty vec table); using full-scan")
        if topk:
            rowid_set = {r for r, _ in topk}
            ph = ",".join("?" for _ in rowid_set)
            rows = conn.execute(base_sql + f" AND m.id IN ({ph})",
                                params + list(rowid_set)).fetchall()
            if rows is not None and len(rows) < limit:
                rows = None  # too few survived → fall through to full-scan
    if rows is None:
        rows = conn.execute(base_sql + " ORDER BY m.id DESC LIMIT 500", params).fetchall()
    conn.close()

    # C14 microopt (P-BURNIN-F): vectorise the cosine loop with numpy.
    # Profiling on 50 synthetic queries against the production DB found the
    # pure-Python `sum(a*b for a,b in zip(...))` over 500 × 1024-dim
    # embeddings was ~25 ms of the ~96 ms post-processing budget; numpy's
    # matmul brings the same work down to ~0.8 ms (33.8x), with max diff
    # 1.57e-08 vs the old path (well below float32 noise).
    import numpy as _np
    candidate_rows = []
    emb_bufs = []
    for row in rows:
        if row[0] in skip_ids or not row[8]:
            continue
        if EXPECTED_DIM is not None and len(row[8]) // 4 != EXPECTED_DIM:
            continue
        candidate_rows.append(row)
        emb_bufs.append(_np.frombuffer(row[8], dtype=_np.float32))
    if candidate_rows:
        stored_mat = _np.vstack(emb_bufs)
        q_arr = _np.asarray(q_emb, dtype=_np.float32)
        cos_arr = stored_mat @ q_arr
    else:
        cos_arr = []

    hits = []
    for idx, row in enumerate(candidate_rows):
        (mem_id, display, content_hash, importance, project_tag,
         source_session, memory_type, content, _emb_bytes) = row
        cos_sim = float(cos_arr[idx])
        if cos_sim < threshold:
            continue
        preview = (content or '').replace('\n', ' ').replace('\t', ' ').strip()
        if len(preview) > 80:
            preview = preview[:77] + '...'
        # Q4 hits carry the regex-pipeline source_session (12-char) so the
        # model can tell whether a memory came from this session or a prior
        # one. memory_type is duplicated outside `display` so a hook can
        # build a custom format without re-parsing the prefix.
        hits.append({
            'id': int(mem_id),
            'display': display,
            'score': round(cos_sim, 4),
            'content_hash': content_hash,
            'importance': round(float(importance or 0.5), 3),
            'project': project_tag,
            'source_session': (source_session or '')[:12],
            'memory_type': memory_type or '',
            'preview': preview,
        })

    hits.sort(key=lambda x: x['score'], reverse=True)
    return {'ok': True, 'results': hits[:limit], 'threshold': threshold}


def _rerank(model, data):
    """Rerank specific memory IDs by cosine similarity to query."""
    query = data.get('query', '')
    db_path = data.get('db_path', '')
    ids = data.get('ids', [])

    if not query or not db_path or not ids:
        return {'ok': False, 'error': 'missing query, db_path, or ids'}

    q_emb = model.encode([query], normalize_embeddings=True)[0]
    conn = _open_db(db_path)

    scores = []
    for mem_id in ids:
        row = conn.execute(
            'SELECT content_embedding FROM memory_embeddings WHERE rowid = ?',
            (int(mem_id),)
        ).fetchone()
        if row and row[0]:
            dim = len(row[0]) // 4
            if EXPECTED_DIM is not None and dim != EXPECTED_DIM:
                scores.append((int(mem_id), 0.0))
                continue
            stored = struct.unpack(f'{dim}f', row[0])
            cos_sim = float(sum(a * b for a, b in zip(q_emb, stored)))
            scores.append((int(mem_id), max(0.0, cos_sim)))
        else:
            scores.append((int(mem_id), 0.0))
    conn.close()

    scores.sort(key=lambda x: x[1], reverse=True)
    return {'ok': True, 'ranked_ids': [s[0] for s in scores]}


def _encode_batch(model, data):
    """Encode texts to float32 embeddings, returned as base64."""
    texts = data.get('texts', [])
    if not texts:
        return {'ok': False, 'error': 'missing texts'}

    import numpy as np
    embeddings = model.encode(texts, normalize_embeddings=True, convert_to_numpy=True)
    result = []
    for emb in embeddings:
        emb_bytes = emb.astype(np.float32).tobytes()
        result.append(base64.b64encode(emb_bytes).decode('ascii'))
    return {'ok': True, 'embeddings': result}


# ── NLI-lite (embedding-based contradiction detection) ───────
# Uses the ALREADY-LOADED SentenceTransformer to detect contradictions.
# Approach: high cosine(A, B) + low cosine(A, neg_B) = likely contradiction.
# No extra model needed — zero additional RAM, works on Python 3.14 + arm64.
#
# Heuristic: for each (text_a, text_b) pair:
#   sim_ab   = cosine(embed(A), embed(B))           — topic similarity
#   neg_b    = "This is not true: " + B
#   sim_neg  = cosine(embed(A), embed(neg_b))        — negation similarity
#   contradiction_score = sim_ab - sim_neg            — divergence
#
# High sim_ab + low sim_neg → texts are about same topic but disagree.
# Label thresholds: contradiction > 0.15, entailment if sim_ab > 0.85
NLI_MAX_PAIRS = 20
_NEG_PREFIXES = [
    "This is not true: ",     # English
    "Bu doğru değil: ",       # Turkish
]

# ── ONNX NLI (lazy-loaded, ~83MB ARM64 quantized DeBERTa v3 xsmall) ──
_NLI_SESSION = None
_NLI_TOKENIZER = None
_NLI_LABELS = None
_NLI_ONNX_AVAILABLE = None  # None = not checked yet
_NLI_INPUT_NAMES = None
NLI_ONNX_MODEL_DIR = os.path.expanduser("~/.cache/b12-nli-onnx")
NLI_ONNX_MODEL_PATH = os.path.join(NLI_ONNX_MODEL_DIR, "onnx/model_qint8_arm64.onnx")


def _load_onnx_nli():
    """Lazy-load ONNX NLI model. Returns True if available."""
    global _NLI_SESSION, _NLI_TOKENIZER, _NLI_LABELS, _NLI_ONNX_AVAILABLE, _NLI_INPUT_NAMES

    if _NLI_ONNX_AVAILABLE is not None:
        return _NLI_ONNX_AVAILABLE

    try:
        import onnxruntime as ort
        from tokenizers import Tokenizer as HFTokenizer

        if not os.path.exists(NLI_ONNX_MODEL_PATH):
            log("ONNX NLI: model file not found, using NLI-lite fallback")
            _NLI_ONNX_AVAILABLE = False
            return False

        t0 = time.time()
        _NLI_SESSION = ort.InferenceSession(
            NLI_ONNX_MODEL_PATH,
            providers=["CPUExecutionProvider"]
        )
        _NLI_INPUT_NAMES = [i.name for i in _NLI_SESSION.get_inputs()]
        _NLI_TOKENIZER = HFTokenizer.from_file(
            os.path.join(NLI_ONNX_MODEL_DIR, "tokenizer.json")
        )
        _NLI_TOKENIZER.enable_padding()
        _NLI_TOKENIZER.enable_truncation(max_length=512)

        config = json.load(open(os.path.join(NLI_ONNX_MODEL_DIR, "config.json")))
        _NLI_LABELS = config.get("id2label",
                                  {"0": "contradiction", "1": "entailment", "2": "neutral"})

        _NLI_ONNX_AVAILABLE = True
        log(f"ONNX NLI loaded in {time.time()-t0:.1f}s ({NLI_ONNX_MODEL_PATH})")
        return True
    except Exception as e:
        log(f"ONNX NLI load failed: {e}, using NLI-lite fallback")
        _NLI_ONNX_AVAILABLE = False
        return False


def _nli_check_onnx(pairs):
    """ONNX-based NLI (DeBERTa v3 xsmall, ARM64 int8 quantized).

    ~10ms per pair, 83MB model, 3-class output.
    """
    import numpy as np

    results = []
    for text_a, text_b in pairs:
        encoded = _NLI_TOKENIZER.encode(text_a, text_b)
        inputs = {
            "input_ids": np.array([encoded.ids], dtype=np.int64),
            "attention_mask": np.array([encoded.attention_mask], dtype=np.int64),
        }
        if "token_type_ids" in _NLI_INPUT_NAMES:
            inputs["token_type_ids"] = np.array([encoded.type_ids], dtype=np.int64)

        logits = _NLI_SESSION.run(None, inputs)[0][0]
        exp_l = np.exp(logits - np.max(logits))
        probs = exp_l / exp_l.sum()

        pred_idx = int(np.argmax(probs))
        label = _NLI_LABELS[str(pred_idx)]

        results.append({
            'label': label,
            'scores': {
                'contradiction': round(float(probs[0]), 4),
                'entailment': round(float(probs[1]), 4),
                'neutral': round(float(probs[2]), 4),
            }
        })

    log(f"NLI-onnx: processed {len(pairs)} pairs")
    return {'ok': True, 'results': results, 'engine': 'onnx'}


def _nli_check(model, data):
    """NLI dispatcher — ONNX model primary, NLI-lite fallback."""
    pairs = data.get('pairs', [])
    if not pairs:
        return {'ok': False, 'error': 'missing pairs'}
    if len(pairs) > NLI_MAX_PAIRS:
        pairs = pairs[:NLI_MAX_PAIRS]

    if _load_onnx_nli():
        return _nli_check_onnx(pairs)
    return _nli_check_lite(model, pairs)


def _nli_check_lite(model, pairs):
    """Embedding+keyword contradiction detection (fallback when ONNX unavailable).

    Combines cosine similarity with lexical negation signals.
    For each pair (A, B), checks:
    1. Topic similarity via cosine(A, B)
    2. Negation contrast: cosine(A, "not B") vs cosine(A, B)
    3. Lexical opposition: antonym patterns, conflicting values
    4. Direct negation: "not", "never", "don't" etc.
    """
    import numpy as np
    import re

    # ── Lexical contradiction signals ────────────────────────
    _NEGATION_WORDS = {
        'not', 'never', "don't", "doesn't", "didn't", "won't", "can't",
        "isn't", "aren't", "wasn't", "weren't", 'no', 'none', 'nobody',
        'nothing', 'nowhere', 'neither', 'nor', 'hate', 'avoid', 'refuse',
        # Smart quote variants (U+2019 right single quotation mark)
        "don\u2019t", "doesn\u2019t", "didn\u2019t", "won\u2019t", "can\u2019t",
        "isn\u2019t", "aren\u2019t", "wasn\u2019t", "weren\u2019t",
        'değil', 'asla', 'hiçbir', 'yok', 'olmaz', 'yapma', 'yapmaz',
    }
    _POSITIVE_WORDS = {
        'prefer', 'like', 'love', 'use', 'always', 'choose', 'want',
        'enjoy', 'recommend', 'best', 'should', 'must', 'correct',
        'tercih', 'sev', 'kullan', 'her zaman', 'doğru', 'iyi',
    }

    def _has_negation(text):
        words = set(text.lower().split())
        return bool(words & _NEGATION_WORDS)

    def _has_positive(text):
        tl = text.lower()
        return any(w in tl for w in _POSITIVE_WORDS)

    def _extract_numbers(text):
        # Remove date-like patterns first (YYYY-MM-DD, DD/MM, HH:MM, etc.)
        cleaned = re.sub(r'\b\d{4}[-/]\d{1,2}[-/]\d{1,2}\b', '', text)
        cleaned = re.sub(r'\b\d{1,2}[-/]\d{1,2}[-/]\d{2,4}\b', '', cleaned)
        cleaned = re.sub(r'\b\d{1,2}:\d{2}(?::\d{2})?\b', '', cleaned)
        return set(re.findall(r'\b\d+(?:\.\d+)?\b', cleaned))

    def _lexical_contradiction_score(text_a, text_b):
        """Return 0.0-1.0 score for lexical contradiction signals."""
        score = 0.0
        a_neg = _has_negation(text_a)
        b_neg = _has_negation(text_b)
        a_pos = _has_positive(text_a)
        b_pos = _has_positive(text_b)

        # Opposite polarity: one positive, one negative
        if (a_neg and b_pos) or (b_neg and a_pos):
            score += 0.4
        if a_neg != b_neg:
            score += 0.2

        # Conflicting numbers in similar context — strong signal
        nums_a = _extract_numbers(text_a)
        nums_b = _extract_numbers(text_b)
        if nums_a and nums_b and nums_a != nums_b:
            # Check they share context words (same topic, different values)
            words_a = set(text_a.lower().split()) - _NEGATION_WORDS - nums_a
            words_b = set(text_b.lower().split()) - _NEGATION_WORDS - nums_b
            overlap = words_a & words_b
            if len(overlap) >= 2:
                score += 0.7  # Stronger: different numbers + shared context

        # Substitution pattern: identical sentence except 1 key word swapped
        # "I use tabs" vs "I use spaces" — same structure, different key word
        # Only 1-word diff (strict) to avoid false positives on context changes
        # Skip multiline/structured content (session summaries, etc.)
        if '\n' not in text_a and '\n' not in text_b:
            words_a_list = text_a.lower().split()
            words_b_list = text_b.lower().split()
            if len(words_a_list) == len(words_b_list) and 4 <= len(words_a_list) <= 20:
                diffs = [(a, b) for a, b in zip(words_a_list, words_b_list) if a != b]
                if len(diffs) == 1 and len(words_a_list) - 1 >= 3:
                    score += 0.6  # Strong: near-identical with single word substitution

        return min(score, 1.0)

    # ── Encode all texts in one batch ────────────────────────
    all_texts = []
    for text_a, text_b in pairs:
        all_texts.append(text_a)
        all_texts.append(text_b)
        all_texts.append(_NEG_PREFIXES[0] + text_b)

    embeddings = model.encode(all_texts, normalize_embeddings=True,
                              convert_to_numpy=True)

    results = []
    for i, (text_a, text_b) in enumerate(pairs):
        emb_a = embeddings[i * 3]
        emb_b = embeddings[i * 3 + 1]
        emb_neg_b = embeddings[i * 3 + 2]

        sim_ab = float(np.dot(emb_a, emb_b))
        sim_neg = float(np.dot(emb_a, emb_neg_b))
        neg_divergence = sim_neg - sim_ab  # Higher = B's negation closer to A
        lex_score = _lexical_contradiction_score(text_a, text_b)

        # Combined contradiction score
        c_signal = max(0.0, neg_divergence * 3) + lex_score * 0.6

        # Classification
        if sim_ab > 0.85 and lex_score < 0.2:
            label = 'entailment'
            e_score = min(1.0, sim_ab)
            c_score = max(0.0, c_signal * 0.3)
            n_score = max(0.0, 1.0 - e_score - c_score)
        elif c_signal > 0.3 and sim_ab > 0.3:
            label = 'contradiction'
            c_score = min(1.0, 0.5 + c_signal * 0.5)
            e_score = max(0.0, sim_ab * 0.2)
            n_score = max(0.0, 1.0 - c_score - e_score)
        else:
            label = 'neutral'
            n_score = max(0.3, 1.0 - sim_ab)
            e_score = max(0.0, sim_ab - 0.3)
            c_score = max(0.0, c_signal * 0.5)

        # Normalize scores to sum to 1
        total = max(e_score + n_score + c_score, 0.001)
        results.append({
            'label': label,
            'scores': {
                'entailment': round(e_score / total, 4),
                'neutral': round(n_score / total, 4),
                'contradiction': round(c_score / total, 4),
            }
        })

    log(f"NLI-lite: processed {len(pairs)} pairs")
    return {'ok': True, 'results': results, 'engine': 'lite'}


def _find_neighbors(model, data):
    """Find top-K similar memories by cosine similarity."""
    db_path = data.get('db_path', '')
    memory_id = data.get('memory_id')
    k = data.get('k', 5)
    min_sim = data.get('min_sim', 0.5)

    if not db_path or memory_id is None:
        return {'ok': False, 'error': 'missing db_path or memory_id'}

    conn = _open_db(db_path)

    # Get the target memory's embedding
    target_row = conn.execute(
        'SELECT content_embedding FROM memory_embeddings WHERE rowid = ?',
        (int(memory_id),)
    ).fetchone()
    if not target_row or not target_row[0]:
        conn.close()
        return {'ok': False, 'error': f'no embedding for memory_id={memory_id}'}

    target_bytes = target_row[0]
    dim = len(target_bytes) // 4
    if EXPECTED_DIM is not None and dim != EXPECTED_DIM:
        conn.close()
        return {'ok': False, 'error': 'dim_mismatch',
                'expected': EXPECTED_DIM, 'got': dim}
    target_emb = struct.unpack(f'{dim}f', target_bytes)

    # Scan all active non-deleted memories
    # TODO: migrate to sqlite-vec kNN (vec_distance_cosine) for O(log N) search
    rows = conn.execute("""
        SELECT m.id, e.content_embedding
        FROM memories m
        JOIN memory_embeddings e ON m.id = e.rowid
        WHERE m.deleted_at IS NULL AND m.id != ?
        ORDER BY m.id DESC
        LIMIT 500
    """, (int(memory_id),)).fetchall()

    import math
    # Pre-compute target norm
    target_norm = math.sqrt(sum(x * x for x in target_emb))

    neighbors = []
    for row_id, emb_bytes in rows:
        if not emb_bytes:
            continue
        d = len(emb_bytes) // 4
        if EXPECTED_DIM is not None and d != EXPECTED_DIM:
            continue
        stored = struct.unpack(f'{d}f', emb_bytes)
        dot = float(sum(a * b for a, b in zip(target_emb, stored)))
        stored_norm = math.sqrt(sum(x * x for x in stored))
        denom = target_norm * stored_norm
        cos_sim = dot / denom if denom > 0 else 0.0
        if cos_sim >= min_sim:
            neighbors.append({'id': row_id, 'similarity': round(cos_sim, 4)})

    neighbors.sort(key=lambda x: x['similarity'], reverse=True)
    conn.close()
    return {'ok': True, 'neighbors': neighbors[:k]}


# ── Classifier head (lazy-loaded LogisticRegression over embeddings) ──
_CLASSIFIER_HEAD = None
_CLASSIFIER_LABELS = None
_CLASSIFIER_AVAILABLE = None  # None = not checked yet
_CLASSIFIER_PATH = os.path.join(
    os.environ.get('B12_DATA_DIR', os.path.expanduser('~/.B12')),
    'models', 'classifier-head.pkl'
)


def _load_classifier():
    """Lazy-load the LogReg classifier head. Returns True if available.

    Honors B12_CLASSIFIER_BACKEND=off as an escape hatch (skip load entirely,
    classify ops return classifier_not_available). On a dim mismatch between
    the pickled head and the current embedding dim, log a one-time warning
    so the operator sees the cause instead of a silent per-call error.
    """
    global _CLASSIFIER_HEAD, _CLASSIFIER_LABELS, _CLASSIFIER_AVAILABLE

    if _CLASSIFIER_AVAILABLE is not None:
        return _CLASSIFIER_AVAILABLE

    if os.environ.get('B12_CLASSIFIER_BACKEND', '').lower() == 'off':
        log("Classifier disabled via B12_CLASSIFIER_BACKEND=off")
        _CLASSIFIER_AVAILABLE = False
        return False

    try:
        import pickle
        if not os.path.exists(_CLASSIFIER_PATH):
            log(f"Classifier head not found: {_CLASSIFIER_PATH}")
            _CLASSIFIER_AVAILABLE = False
            return False

        t0 = time.time()
        with open(_CLASSIFIER_PATH, 'rb') as f:
            data = pickle.load(f)
        head_model = data.get('base_model')
        allow_model_mismatch = os.environ.get(
            'B12_CLASSIFIER_ALLOW_MODEL_MISMATCH', ''
        ).lower() in ('1', 'true', 'yes')
        if head_model and head_model != MODEL_NAME and not allow_model_mismatch:
            log(f"Classifier head trained for model={head_model} but daemon "
                f"model={MODEL_NAME}; refusing to load. Retrain the head with "
                "the daemon model or set B12_CLASSIFIER_BACKEND=off.")
            _CLASSIFIER_AVAILABLE = False
            return False
        _CLASSIFIER_HEAD = data['head']
        _CLASSIFIER_LABELS = data['labels']
        _CLASSIFIER_AVAILABLE = True
        log(f"Classifier head loaded in {time.time()-t0:.3f}s "
            f"({len(_CLASSIFIER_LABELS)} classes, "
            f"cv_acc={data.get('cv_accuracy', 'N/A'):.3f})")

        # One-time loud warning on dim mismatch — otherwise every classify
        # op returns classifier_dim_mismatch with no operator-visible cause.
        head_dim = getattr(_CLASSIFIER_HEAD, 'n_features_in_', None)
        if head_dim is not None and EXPECTED_DIM is not None \
                and int(head_dim) != int(EXPECTED_DIM):
            log(f"WARNING: classifier head trained at dim={head_dim} but "
                f"daemon embedding dim={EXPECTED_DIM}. Classify ops will "
                f"return classifier_dim_mismatch. Retrain the head at "
                f"dim={EXPECTED_DIM} or set B12_CLASSIFIER_BACKEND=off "
                f"to silence.")
        return True
    except Exception as e:
        log(f"Classifier head load failed: {e}")
        _CLASSIFIER_AVAILABLE = False
        return False


def _classify(model, data):
    """Classify text using embedding + LogReg head.

    Input:  {"op": "classify", "text": "..."}
    Output: {"type": str, "confidence": float}
    """
    text = data.get('text', '')
    if not text:
        return {'ok': False, 'error': 'missing text'}

    if not _load_classifier():
        return {'ok': False, 'error': 'classifier_not_available'}

    import numpy as np

    embedding = model.encode([text], normalize_embeddings=True,
                              convert_to_numpy=True)[0]
    # Dim guard — classifier head is trained against a specific embed dim.
    # After a model swap the pickled head will silently produce garbage
    # without this check.
    expected_in = getattr(_CLASSIFIER_HEAD, 'n_features_in_', None)
    if expected_in is not None and int(expected_in) != int(len(embedding)):
        return {'ok': False, 'error': 'classifier_dim_mismatch',
                'expected': int(expected_in), 'got': int(len(embedding))}
    proba = _CLASSIFIER_HEAD.predict_proba(embedding.reshape(1, -1))[0]
    pred_idx = int(np.argmax(proba))
    confidence = float(proba[pred_idx])

    return {
        'ok': True,
        'type': _CLASSIFIER_LABELS[pred_idx],
        'confidence': round(confidence, 4),
    }


def _find_cluster(model, data):
    """Find connected components of similar memories above a threshold.

    Input:  {"op": "find_cluster", "threshold": 0.80, "min_size": 3,
             "db_path": "...", "project": ""}
    Output: {"clusters": [[id1, id2, id3], [id4, id5]], "count": 2}

    Uses stored embeddings, computes pairwise cosine similarity,
    groups into connected components where similarity >= threshold.
    """
    db_path = data.get('db_path', '')
    threshold = data.get('threshold', 0.80)
    min_size = data.get('min_size', 3)
    project = data.get('project', '')

    if not db_path:
        return {'ok': False, 'error': 'missing db_path'}

    conn = _open_db(db_path)

    # Load all active memory embeddings
    sql = """
        SELECT m.id, e.content_embedding
        FROM memories m
        JOIN memory_embeddings e ON m.id = e.rowid
        WHERE m.deleted_at IS NULL
    """
    params = []
    if project:
        sql += " AND m.tags LIKE ?"
        params.append(f"%proj:{project}%")
    sql += " ORDER BY m.id DESC LIMIT 500"

    rows = conn.execute(sql, params).fetchall()
    conn.close()

    if len(rows) < min_size:
        return {'ok': True, 'clusters': [], 'count': 0}

    # Parse embeddings
    import math
    mem_ids = []
    embeddings = []
    for row_id, emb_bytes in rows:
        if not emb_bytes:
            continue
        dim = len(emb_bytes) // 4
        if EXPECTED_DIM is not None and dim != EXPECTED_DIM:
            continue
        emb = struct.unpack(f'{dim}f', emb_bytes)
        mem_ids.append(row_id)
        embeddings.append(emb)

    n = len(mem_ids)
    if n < min_size:
        return {'ok': True, 'clusters': [], 'count': 0}

    # Precompute norms
    norms = []
    for emb in embeddings:
        norms.append(math.sqrt(sum(x * x for x in emb)))

    # Build adjacency list via pairwise cosine similarity
    adj = {i: set() for i in range(n)}
    for i in range(n):
        for j in range(i + 1, n):
            dot = sum(a * b for a, b in zip(embeddings[i], embeddings[j]))
            denom = norms[i] * norms[j]
            if denom > 0:
                cos_sim = dot / denom
                if cos_sim >= threshold:
                    adj[i].add(j)
                    adj[j].add(i)

    # Find connected components via BFS
    visited = set()
    clusters = []
    for start in range(n):
        if start in visited:
            continue
        # BFS
        component = []
        queue = [start]
        visited.add(start)
        while queue:
            node = queue.pop(0)
            component.append(mem_ids[node])
            for neighbor in adj[node]:
                if neighbor not in visited:
                    visited.add(neighbor)
                    queue.append(neighbor)
        if len(component) >= min_size:
            clusters.append(component)

    return {'ok': True, 'clusters': clusters, 'count': len(clusters)}


class _GGUFEmbedShim:
    """Thin shim around llama-cpp-python's embedding API.

    Exposes ``encode(texts, normalize_embeddings=True, convert_to_numpy=...)``
    so it is drop-in for the rest of the daemon. Lives behind ``B12_EMBED_BACKEND=gguf``
    so the default path stays on sentence-transformers (no new heavy dep).
    """
    def __init__(self, llama):
        self._llama = llama

    def encode(self, texts, normalize_embeddings=True, convert_to_numpy=False):
        import numpy as np
        out = []
        for t in texts:
            emb = self._llama.create_embedding(t)
            vec = emb['data'][0]['embedding']
            if normalize_embeddings:
                arr = np.asarray(vec, dtype=np.float32)
                norm = float(np.linalg.norm(arr))
                if norm > 0:
                    arr = arr / norm
                vec = arr.tolist()
            out.append(vec)
        if convert_to_numpy:
            return np.asarray(out, dtype=np.float32)
        return [np.asarray(v, dtype=np.float32) for v in out]


def _load_gguf_backend():
    """Load a BGE-M3 Q8_0 GGUF via llama-cpp-python (opt-in)."""
    from llama_cpp import Llama  # type: ignore
    gguf_path = os.environ.get('B12_EMBED_GGUF_PATH', '').strip()
    if not gguf_path or not os.path.exists(gguf_path):
        raise FileNotFoundError(
            f"B12_EMBED_GGUF_PATH not set or missing (got: {gguf_path!r}). "
            "Set it to a BGE-M3 Q8_0 GGUF file."
        )
    llama = Llama(model_path=gguf_path, embedding=True, n_ctx=8192, verbose=False)
    return _GGUFEmbedShim(llama)


def handle_request(model, data, start_time, requests_served):
    """Route a JSON request to the appropriate handler."""
    op = data.get('op', '')

    if op == 'health':
        return {
            'ok': True,
            'uptime': int(time.time() - start_time),
            'requests_served': requests_served,
            'model_loaded': model is not None
        }

    if op == 'shutdown':
        return {'ok': True, '_shutdown': True}

    if model is None:
        return {'ok': False, 'error': 'model_not_loaded'}

    try:
        if op == 'semantic_search':
            return _semantic_search(model, data)
        elif op == 'recall':
            return _recall(model, data)
        elif op == 'rerank':
            return _rerank(model, data)
        elif op == 'encode_batch':
            return _encode_batch(model, data)
        elif op == 'nli_check':
            return _nli_check(model, data)
        elif op == 'find_neighbors':
            return _find_neighbors(model, data)
        elif op == 'find_cluster':
            return _find_cluster(model, data)
        elif op == 'classify':
            return _classify(model, data)
        else:
            return {'ok': False, 'error': f'unknown_op: {op}'}
    except Exception as e:
        return {'ok': False, 'error': str(e)}


def main():
    os.makedirs(LOG_DIR, exist_ok=True)

    # ── Singleton lock (prevents multiple daemons) ──────────────
    # flock is released automatically if process dies (crash/kill/OOM).
    # The file descriptor must stay open for the daemon's lifetime.
    lock_fd = open(LOCK_PATH, 'w')
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        # Another daemon holds the lock (running or loading model)
        lock_fd.close()
        sys.exit(0)

    # Write PID to lock file (before model load, so hook can check)
    lock_fd.write(str(os.getpid()))
    lock_fd.flush()

    # Also write PID file for backward compatibility
    with open(PID_PATH, 'w') as f:
        f.write(str(os.getpid()))

    atexit.register(cleanup)

    # Signal handlers for clean shutdown
    running = [True]

    def _signal_handler(signum, frame):
        running[0] = False

    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)
    # Ignore SIGHUP so daemon survives shell exits (even without disown)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)

    log(f"Daemon starting (PID {os.getpid()}, backend={EMBED_BACKEND})")

    # Load model FIRST (expensive, ~3-12s for BGE-M3) — socket not created yet
    # so daemon_alive() = model ready by definition.
    global EXPECTED_DIM
    model = None
    try:
        if EMBED_BACKEND == 'gguf':
            # Opt-in path for users who installed llama-cpp-python + a GGUF.
            # Path is conventional, override via B12_EMBED_GGUF_PATH.
            model = _load_gguf_backend()
            log(f"Model loaded (gguf): {os.environ.get('B12_EMBED_GGUF_PATH', '<auto>')}")
        else:
            from sentence_transformers import SentenceTransformer
            # Load from the local HF cache only. transformers 5.x makes a
            # network model_info() round-trip during tokenizer init for
            # repo-id loads (BGE-M3's mistral-regex patch path); skipping it
            # cuts cold load ~9.4s → ~4.7s (model_load 5.5s → 1.3s, measured
            # on Apple Silicon) and removes the HF network dependency from
            # startup. Fall back to a normal downloading load only when the
            # model isn't cached yet (fresh install, first run).
            try:
                model = SentenceTransformer(MODEL_NAME, device='cpu', local_files_only=True)
            except Exception:
                log(f"Model not in local cache — downloading {MODEL_NAME} (first run)")
                model = SentenceTransformer(MODEL_NAME, device='cpu')
            log(f"Model loaded: {MODEL_NAME}")
        # Probe dim once so subsequent ops can validate against drift.
        probe = model.encode(["dim_probe"], normalize_embeddings=True)
        EXPECTED_DIM = int(len(probe[0]))
        log(f"Embedding dim = {EXPECTED_DIM}")
    except Exception as e:
        log(f"Model load FAILED: {e}")
        sys.exit(1)  # atexit handles cleanup

    # Check if we were killed during model load
    if not running[0]:
        sys.exit(0)  # atexit handles cleanup

    # Create socket AFTER model load (so daemon_alive() = model ready)
    os.makedirs(_RUNTIME_DIR, mode=0o700, exist_ok=True)
    try:
        os.chmod(_RUNTIME_DIR, 0o700)
    except OSError:
        pass
    if os.path.exists(SOCKET_PATH):
        os.unlink(SOCKET_PATH)

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(SOCKET_PATH)
    os.chmod(SOCKET_PATH, 0o600)
    # Backlog 8 keeps a small burst of overlapping hook fires (e.g. SessionStart
    # prime + the first UserPromptSubmit, plus a PreCompact) from racing into
    # a `Connection refused`; Claude Code itself is sequential per session.
    server.listen(8)
    server.settimeout(60)  # Wakes up every 60s to check idle timeout

    start_time = time.time()
    last_request = time.time()
    requests_served = 0

    log("Listening for connections")

    while running[0]:
        # Idle timeout check
        if time.time() - last_request > IDLE_TIMEOUT:
            log(f"Idle timeout ({IDLE_TIMEOUT}s), shutting down")
            break

        try:
            conn, _ = server.accept()
            conn.settimeout(CONN_TIMEOUT)
        except socket.timeout:
            continue
        except OSError:
            break

        try:
            # Read line-delimited JSON request
            data = b''
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                data += chunk
                if b'\n' in data:
                    break

            if data:
                request = json.loads(data.decode('utf-8').strip())
                response = handle_request(model, request, start_time, requests_served)
                requests_served += 1
                last_request = time.time()

                should_shutdown = response.pop('_shutdown', False)
                conn.sendall((json.dumps(response) + '\n').encode('utf-8'))

                if should_shutdown:
                    running[0] = False
        except Exception as e:
            try:
                err_resp = json.dumps({'ok': False, 'error': str(e)}) + '\n'
                conn.sendall(err_resp.encode('utf-8'))
            except Exception:
                pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

    server.close()
    cleanup()
    log("Daemon shut down cleanly")


if __name__ == '__main__':
    main()
