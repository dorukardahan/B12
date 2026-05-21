#!/usr/bin/env python3
"""
B12 Graph Enrichment — batch discovery of embedding-based and NLI edges.

Discovers related/supports/contradicts relationships between memories using
the embedding daemon. Runs daily via launchd (com.b12.graph-enrich).

Usage:
  python3 graph_enrich.py                # Dry-run (report only)
  python3 graph_enrich.py --apply        # Create embedding-based edges
  python3 graph_enrich.py --nli          # Include NLI contradiction/support detection
  python3 graph_enrich.py --apply --nli  # Full enrichment

Phase A — Embedding similarity (always):
  For each active memory (skip session_summary), find top-5 neighbors with
  cosine >= 0.5. Skip if edge already exists. Write relationship_type='related'.

Phase B — NLI classification (--nli):
  For neighbor pairs with cosine 0.5-0.85, run NLI to detect:
  - contradiction score > 0.7 → write 'contradicts' edge
  - entailment score > 0.8 → write 'supports' edge
  Also flags contradicting memories with 'needs-review' in metadata.

Requires the B12 embedding daemon to be running.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone

try:
    from shared_patterns import get_db_path
    DB_PATH = get_db_path()
except ImportError:
    _home = os.path.expanduser("~")
    if sys.platform == "darwin":
        DB_PATH = os.path.join(_home, "Library", "Application Support", "mcp-memory", "sqlite_vec.db")
    elif sys.platform == "win32":
        DB_PATH = os.path.join(_home, "AppData", "Local", "mcp-memory", "sqlite_vec.db")
    else:
        DB_PATH = os.path.join(_home, ".local", "share", "mcp-memory", "sqlite_vec.db")
LOG_DIR = os.path.join(os.environ.get('B12_DATA_DIR', os.path.expanduser('~/.B12')), 'memory-logs')
_UID = os.getuid() if hasattr(os, 'getuid') else os.getpid()
# Hardcode /tmp/ — macOS TMPDIR varies per session
DAEMON_SOCK = f"/tmp/b12-embed-{_UID}.sock"
DAEMON_PID = f"/tmp/b12-embed-{_UID}.pid"

MAX_MEMORIES_PER_RUN = 50
NEIGHBOR_K = 5
NEIGHBOR_MIN_SIM = 0.5
NLI_CONTRADICTION_THRESHOLD = 0.8  # Raised from 0.7 — reduces ~67% false positive rate
NLI_ENTAILMENT_THRESHOLD = 0.8
NLI_SIM_RANGE = (0.5, 0.85)  # Only NLI-check pairs in this cosine range
MIN_CONTENT_LEN = 30  # Skip very short memories for NLI
# Content patterns that indicate session metadata (not real knowledge)
_SKIP_NLI_PATTERNS = (
    '[Progress]',
    '# Session Summary',
    '## Session Summary',
)


def _is_session_metadata(content):
    """Check if content is session metadata that shouldn't get NLI checked."""
    return any(content.startswith(p) for p in _SKIP_NLI_PATTERNS)


def log(msg, quiet=False):
    if not quiet:
        print(msg)


def daemon_alive():
    if not os.path.exists(DAEMON_SOCK) or not os.path.exists(DAEMON_PID):
        return False
    try:
        pid = int(open(DAEMON_PID).read().strip())
        os.kill(pid, 0)
        return True
    except (ValueError, ProcessLookupError, PermissionError, FileNotFoundError):
        return False


def daemon_request(payload, timeout=30):
    """Send JSON request to daemon, return parsed response or None."""
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(DAEMON_SOCK)
        s.sendall((json.dumps(payload) + '\n').encode())
        data = b''
        while True:
            chunk = s.recv(1048576)
            if not chunk:
                break
            data += chunk
            if b'\n' in data:
                break
        s.close()
        return json.loads(data.decode().strip())
    except Exception:
        return None


def start_daemon_if_needed():
    """Attempt to start daemon if not running."""
    if daemon_alive():
        return True
    venv_python = os.path.expanduser(
        "~/.local/b12-venv/bin/python3"
    )
    _hook_dir = os.environ.get('B12_HOOK_DIR', os.path.expanduser('~/.B12/hooks'))
    daemon_script = os.path.join(_hook_dir, 'scripts', 'embed_daemon.py')
    if not os.path.exists(daemon_script):
        # Try B12 source location
        daemon_script = os.path.join(os.path.dirname(__file__), "embed_daemon.py")
    if not os.path.exists(daemon_script) or not os.path.exists(venv_python):
        return False
    subprocess.Popen(
        [venv_python, daemon_script],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    # Wait up to 30s for daemon to be ready (model load takes ~12s)
    for _ in range(60):
        time.sleep(0.5)
        if daemon_alive():
            return True
    return False


def edge_exists(conn, src_hash, tgt_hash):
    """Check if edge exists in either direction."""
    row = conn.execute(
        "SELECT 1 FROM memory_graph WHERE "
        "(source_hash = ? AND target_hash = ?) OR "
        "(source_hash = ? AND target_hash = ?) LIMIT 1",
        (src_hash, tgt_hash, tgt_hash, src_hash)
    ).fetchone()
    return row is not None


def edge_exists_typed(conn, src_hash, tgt_hash, rel_type):
    """Check if edge of specific type exists in either direction."""
    row = conn.execute(
        "SELECT 1 FROM memory_graph WHERE "
        "((source_hash = ? AND target_hash = ?) OR "
        " (source_hash = ? AND target_hash = ?)) AND "
        "relationship_type = ? LIMIT 1",
        (src_hash, tgt_hash, tgt_hash, src_hash, rel_type)
    ).fetchone()
    return row is not None


def main():
    parser = argparse.ArgumentParser(description="B12 Graph Enrichment")
    parser.add_argument('--apply', action='store_true', help='Write edges to DB (default: dry-run)')
    parser.add_argument('--nli', action='store_true', help='Include NLI contradiction/support detection')
    parser.add_argument('--quiet', action='store_true', help='Minimal output')
    args = parser.parse_args()

    os.makedirs(LOG_DIR, exist_ok=True)

    if not os.path.exists(DB_PATH):
        log("ERROR: Database not found", args.quiet)
        return 1

    if not start_daemon_if_needed():
        log("ERROR: Embedding daemon not available", args.quiet)
        return 1

    try:
        import sqlite_vec
    except ImportError:
        log("ERROR: sqlite_vec not installed", args.quiet)
        return 1

    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)

    now = datetime.now(timezone.utc)
    now_ts = now.timestamp()

    # Get active memories (skip session_summary)
    memories = conn.execute("""
        SELECT id, content_hash, content, memory_type
        FROM memories
        WHERE deleted_at IS NULL
          AND (memory_type IS NULL OR memory_type NOT IN ('session_summary', 'progress'))
        ORDER BY updated_at DESC
        LIMIT ?
    """, (MAX_MEMORIES_PER_RUN,)).fetchall()

    log(f"Graph enrichment — {len(memories)} memories to process", args.quiet)
    if not args.apply:
        log("DRY-RUN mode (use --apply to write edges)", args.quiet)

    # ── Phase A: Embedding-based 'related' edges ─────────────
    new_related = 0
    skipped_existing = 0
    nli_candidates = []  # (mem_hash, mem_content, neighbor_hash, neighbor_content, similarity)

    for mem_id, mem_hash, mem_content, mem_type in memories:
        resp = daemon_request({
            'op': 'find_neighbors',
            'db_path': DB_PATH,
            'memory_id': mem_id,
            'k': NEIGHBOR_K,
            'min_sim': NEIGHBOR_MIN_SIM,
        })
        if not resp or not resp.get('ok'):
            continue

        for neighbor in resp.get('neighbors', []):
            n_id = neighbor['id']
            n_sim = neighbor['similarity']

            # Sanity check: cosine similarity must be in valid range
            # Allow small epsilon for IEEE 754 floating point rounding
            if n_sim > 1.001 or n_sim < -0.001:
                continue
            n_sim = min(n_sim, 1.0)  # Clamp to valid range

            # Get neighbor's hash, content, and type
            n_row = conn.execute(
                "SELECT content_hash, content, memory_type FROM memories WHERE id = ? AND deleted_at IS NULL",
                (n_id,)
            ).fetchone()
            if not n_row:
                continue
            n_hash, n_content, n_type = n_row

            # Skip session summaries and progress as NLI neighbors
            if n_type in ('session_summary', 'progress'):
                # Still create related edge but skip NLI
                if not edge_exists_typed(conn, mem_hash, n_hash, 'related'):
                    if args.apply:
                        conn.execute("""
                            INSERT OR IGNORE INTO memory_graph
                            (source_hash, target_hash, similarity, connection_types,
                             metadata, created_at, relationship_type)
                            VALUES (?, ?, ?, '["embedding_similarity"]', ?, ?, 'related')
                        """, (mem_hash, n_hash, n_sim,
                              json.dumps({"source": "graph_enrich"}), now_ts))
                    new_related += 1
                else:
                    skipped_existing += 1
                continue

            # Skip self-edges
            if mem_hash == n_hash:
                continue

            # Phase A: related edge
            if not edge_exists_typed(conn, mem_hash, n_hash, 'related'):
                if args.apply:
                    conn.execute("""
                        INSERT OR IGNORE INTO memory_graph
                        (source_hash, target_hash, similarity, connection_types,
                         metadata, created_at, relationship_type)
                        VALUES (?, ?, ?, '["embedding_similarity"]', ?, ?, 'related')
                    """, (mem_hash, n_hash, n_sim,
                          json.dumps({"source": "graph_enrich"}), now_ts))
                new_related += 1
            else:
                skipped_existing += 1

            # Collect NLI candidates (cosine in the right range, content long enough)
            if args.nli and NLI_SIM_RANGE[0] <= n_sim <= NLI_SIM_RANGE[1]:
                if (len(mem_content) >= MIN_CONTENT_LEN and len(n_content) >= MIN_CONTENT_LEN
                        and not _is_session_metadata(mem_content)
                        and not _is_session_metadata(n_content)):
                    nli_candidates.append((mem_hash, mem_content, n_hash, n_content, n_sim))

    if args.apply:
        conn.commit()

    log(f"Phase A: {new_related} new 'related' edges ({skipped_existing} already existed)", args.quiet)

    # ── Phase B: NLI contradiction/support edges ─────────────
    new_contradicts = 0
    new_supports = 0

    if args.nli and nli_candidates:
        # Deduplicate pairs (check both directions)
        seen_pairs = set()
        unique_candidates = []
        for mh, mc, nh, nc, sim in nli_candidates:
            pair_key = tuple(sorted([mh, nh]))
            if pair_key not in seen_pairs:
                seen_pairs.add(pair_key)
                unique_candidates.append((mh, mc, nh, nc, sim))

        log(f"Phase B: {len(unique_candidates)} unique pairs for NLI analysis", args.quiet)

        # Batch NLI in chunks of 20
        for i in range(0, len(unique_candidates), 20):
            batch = unique_candidates[i:i+20]
            pairs = [[mc, nc] for _, mc, _, nc, _ in batch]

            resp = daemon_request({'op': 'nli_check', 'pairs': pairs}, timeout=60)
            if not resp or not resp.get('ok'):
                log(f"  NLI batch {i//20+1} failed, skipping", args.quiet)
                continue

            for j, result in enumerate(resp.get('results', [])):
                mh, mc, nh, nc, sim = batch[j]
                scores = result.get('scores', {})
                label = result.get('label', 'neutral')

                if scores.get('contradiction', 0) > NLI_CONTRADICTION_THRESHOLD:
                    if not edge_exists_typed(conn, mh, nh, 'contradicts'):
                        if args.apply:
                            # INSERT OR REPLACE: contradiction upgrades any existing
                            # 'related' edge (PK is source_hash+target_hash only)
                            conn.execute("""
                                INSERT OR REPLACE INTO memory_graph
                                (source_hash, target_hash, similarity, connection_types,
                                 metadata, created_at, relationship_type)
                                VALUES (?, ?, ?, '["nli","graph_enrich"]', ?, ?, 'contradicts')
                            """, (mh, nh, scores['contradiction'],
                                  json.dumps({
                                      "source": "graph_enrich",
                                      "cosine_sim": sim,
                                      "nli_scores": scores
                                  }), now_ts))
                            # Flag both memories with needs-review
                            for h in (mh, nh):
                                meta_row = conn.execute(
                                    "SELECT metadata FROM memories WHERE content_hash = ? AND deleted_at IS NULL",
                                    (h,)
                                ).fetchone()
                                if meta_row:
                                    try:
                                        meta = json.loads(meta_row[0]) if meta_row[0] else {}
                                    except (json.JSONDecodeError, TypeError):
                                        meta = {}
                                    flags = meta.get('flags', [])
                                    if 'needs-review' not in flags:
                                        flags.append('needs-review')
                                        meta['flags'] = flags
                                        conn.execute(
                                            "UPDATE memories SET metadata = ? WHERE content_hash = ? AND deleted_at IS NULL",
                                            (json.dumps(meta, ensure_ascii=False), h)
                                        )
                        new_contradicts += 1
                        if not args.quiet:
                            log(f"  CONTRADICTS [{scores['contradiction']:.2f}]: "
                                f"`{mc[:50]}` vs `{nc[:50]}`")

                elif scores.get('entailment', 0) > NLI_ENTAILMENT_THRESHOLD:
                    if not edge_exists_typed(conn, mh, nh, 'supports'):
                        if args.apply:
                            # INSERT OR REPLACE: supports upgrades any existing
                            # 'related' edge (PK is source_hash+target_hash only)
                            conn.execute("""
                                INSERT OR REPLACE INTO memory_graph
                                (source_hash, target_hash, similarity, connection_types,
                                 metadata, created_at, relationship_type)
                                VALUES (?, ?, ?, '["nli","graph_enrich"]', ?, ?, 'supports')
                            """, (mh, nh, scores['entailment'],
                                  json.dumps({
                                      "source": "graph_enrich",
                                      "cosine_sim": sim,
                                      "nli_scores": scores
                                  }), now_ts))
                        new_supports += 1

        if args.apply:
            conn.commit()

        log(f"Phase B: {new_contradicts} contradicts, {new_supports} supports", args.quiet)

    # ── Summary ──────────────────────────────────────────────
    total_new = new_related + new_contradicts + new_supports
    log(f"\nTotal: {total_new} new edges discovered", args.quiet)
    if not args.apply and total_new > 0:
        log("Run with --apply to write these edges to the database", args.quiet)

    # Write log entry
    log_entry = {
        "ts": int(now_ts),
        "type": "graph_enrich",
        "memories_processed": len(memories),
        "new_related": new_related,
        "new_contradicts": new_contradicts,
        "new_supports": new_supports,
        "skipped_existing": skipped_existing,
        "nli_enabled": args.nli,
        "applied": args.apply,
    }
    log_file = os.path.join(LOG_DIR, "graph-enrich.jsonl")
    try:
        with open(log_file, 'a') as f:
            f.write(json.dumps(log_entry) + '\n')
    except Exception:
        pass

    conn.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
