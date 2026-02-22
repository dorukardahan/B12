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

warnings.filterwarnings('ignore')
os.environ['TOKENIZERS_PARALLELISM'] = 'false'
os.environ.setdefault('WANDB_DISABLED', 'true')
os.environ.setdefault('WANDB_MODE', 'disabled')

_UID = os.getuid() if hasattr(os, 'getuid') else os.getpid()
# Hardcode /tmp/ — macOS TMPDIR varies per session (/var/folders/...),
# causing socket path mismatch between daemon and hooks.
SOCKET_PATH = f"/tmp/b12-embed-{_UID}.sock"
PID_PATH = f"/tmp/b12-embed-{_UID}.pid"
LOCK_PATH = f"/tmp/b12-embed-{_UID}.lock"
LOG_DIR = os.path.expanduser("~/.claude/memory-logs")
LOG_PATH = os.path.join(LOG_DIR, "embed-daemon.log")
IDLE_TIMEOUT = 7200  # 2 hours
CONN_TIMEOUT = 10    # Per-connection read timeout (increased for NLI batches)
MODEL_NAME = os.environ.get('MCP_EMBEDDING_MODEL', 'paraphrase-multilingual-MiniLM-L12-v2')


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
    """Open SQLite DB with sqlite-vec extension loaded."""
    import sqlite_vec
    conn = sqlite3.connect(db_path, timeout=5)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    return conn


def _semantic_search(model, data):
    """Full-table cosine similarity search (matches cold path behavior)."""
    query = data.get('query', '')
    db_path = data.get('db_path', '')
    limit = data.get('limit', 5)

    if not query or not db_path:
        return {'ok': False, 'error': 'missing query or db_path'}

    q_emb = model.encode([query], normalize_embeddings=True)[0]
    conn = _open_db(db_path)

    rows = conn.execute("""
        SELECT m.id,
               '[' || m.memory_type || '] ' || replace(substr(m.content, 1, 300), char(10), ' '),
               e.content_embedding
        FROM memories m
        JOIN memory_embeddings e ON m.id = e.rowid
        WHERE m.deleted_at IS NULL
          AND m.valid_until IS NULL
          AND m.memory_type NOT IN ('session_summary', 'progress')
          AND m.tags NOT LIKE '%session-summary%'
    """).fetchall()

    scores = []
    for row_id, display, emb_bytes in rows:
        if not emb_bytes:
            continue
        dim = len(emb_bytes) // 4
        stored = struct.unpack(f'{dim}f', emb_bytes)
        cos_sim = float(sum(a * b for a, b in zip(q_emb, stored)))
        if cos_sim > 0.3:
            scores.append({'id': row_id, 'display': display, 'score': round(cos_sim, 3)})

    scores.sort(key=lambda x: x['score'], reverse=True)
    conn.close()
    return {'ok': True, 'results': scores[:limit]}


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
    target_emb = struct.unpack(f'{dim}f', target_bytes)

    # Scan all active non-deleted memories
    rows = conn.execute("""
        SELECT m.id, e.content_embedding
        FROM memories m
        JOIN memory_embeddings e ON m.id = e.rowid
        WHERE m.deleted_at IS NULL AND m.id != ?
    """, (int(memory_id),)).fetchall()

    import math
    # Pre-compute target norm
    target_norm = math.sqrt(sum(x * x for x in target_emb))

    neighbors = []
    for row_id, emb_bytes in rows:
        if not emb_bytes:
            continue
        d = len(emb_bytes) // 4
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
        elif op == 'rerank':
            return _rerank(model, data)
        elif op == 'encode_batch':
            return _encode_batch(model, data)
        elif op == 'nli_check':
            return _nli_check(model, data)
        elif op == 'find_neighbors':
            return _find_neighbors(model, data)
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

    log(f"Daemon starting (PID {os.getpid()})")

    # Load model FIRST (expensive, ~12s) — socket not created yet
    model = None
    try:
        from sentence_transformers import SentenceTransformer
        model = SentenceTransformer(MODEL_NAME, device='cpu')
        log(f"Model loaded: {MODEL_NAME}")
    except Exception as e:
        log(f"Model load FAILED: {e}")
        sys.exit(1)  # atexit handles cleanup

    # Check if we were killed during model load
    if not running[0]:
        sys.exit(0)  # atexit handles cleanup

    # Create socket AFTER model load (so daemon_alive() = model ready)
    if os.path.exists(SOCKET_PATH):
        os.unlink(SOCKET_PATH)

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(SOCKET_PATH)
    os.chmod(SOCKET_PATH, 0o600)
    server.listen(2)
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
