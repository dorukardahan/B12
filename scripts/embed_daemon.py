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

_UID = os.getuid()
SOCKET_PATH = f"/tmp/b12-embed-{_UID}.sock"
PID_PATH = f"/tmp/b12-embed-{_UID}.pid"
LOG_DIR = os.path.expanduser("~/.claude/memory-logs")
LOG_PATH = os.path.join(LOG_DIR, "embed-daemon.log")
IDLE_TIMEOUT = 7200  # 2 hours
CONN_TIMEOUT = 5     # Per-connection read timeout
MODEL_NAME = os.environ.get('MCP_EMBEDDING_MODEL', 'paraphrase-multilingual-MiniLM-L12-v2')


def log(msg):
    """Append timestamped message to daemon log file."""
    try:
        with open(LOG_PATH, 'a') as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass


def cleanup():
    """Remove socket and PID files (registered with atexit)."""
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
    embeddings = model.encode(texts, convert_to_numpy=True)
    result = []
    for emb in embeddings:
        emb_bytes = emb.astype(np.float32).tobytes()
        result.append(base64.b64encode(emb_bytes).decode('ascii'))
    return {'ok': True, 'embeddings': result}


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
        else:
            return {'ok': False, 'error': f'unknown_op: {op}'}
    except Exception as e:
        return {'ok': False, 'error': str(e)}


def main():
    os.makedirs(LOG_DIR, exist_ok=True)

    # Write PID immediately (for stale daemon cleanup)
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
        cleanup()
        sys.exit(1)

    # Check if we were killed during model load
    if not running[0]:
        cleanup()
        sys.exit(0)

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
