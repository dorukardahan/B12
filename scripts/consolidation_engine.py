#!/usr/bin/env python3
"""
B12 Smart Consolidation Engine — HDBSCAN clustering + semantic dedup/merge.

Replaces the Jaccard-based memory-consolidate.py with embedding-aware
clustering. Groups similar memories using HDBSCAN, then categorizes each
cluster pair as dedup / merge / contradiction / keep-separate.

Usage:
    python3 consolidation_engine.py --dry-run               # Report only
    python3 consolidation_engine.py --dry-run --project B12  # Single project
    python3 consolidation_engine.py                          # Apply changes
    python3 consolidation_engine.py --min-cluster-size 5     # Tuning

Algorithm:
  1. Load all active memory embeddings from SQLite
  2. Run HDBSCAN clustering (min_cluster_size configurable, default 3)
  3. For each cluster, compute pairwise cosine similarity:
     - Dedup  (cosine > 0.95 AND same tags): keep highest-strength memory
     - Merge  (cosine 0.80–0.95 AND same cluster): concatenate unique facts
     - Contradiction (NLI > 0.90): flag for user review, never auto-resolve
     - Keep separate: below thresholds
  4. Merged memories get provenance metadata + graph edge rewriting
  5. Originals soft-deleted (deleted_at set, recoverable)

Requires: sklearn (HDBSCAN), numpy, sqlite-vec (in b12-venv)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import sqlite3
import sys
import time
import warnings
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

warnings.filterwarnings("ignore", category=FutureWarning)

import numpy as np

try:
    from shared_patterns import get_db_path
except ImportError:
    # Fallback when running outside scripts/ directory
    _scripts_dir = os.path.dirname(os.path.abspath(__file__))
    if _scripts_dir not in sys.path:
        sys.path.insert(0, _scripts_dir)
    try:
        from shared_patterns import get_db_path
    except ImportError:
        def get_db_path() -> str:
            _home = os.path.expanduser("~")
            if sys.platform == "darwin":
                return os.path.join(_home, "Library", "Application Support",
                                    "mcp-memory", "sqlite_vec.db")
            elif sys.platform == "win32":
                return os.path.join(_home, "AppData", "Local",
                                    "mcp-memory", "sqlite_vec.db")
            else:
                return os.path.join(_home, ".local", "share",
                                    "mcp-memory", "sqlite_vec.db")

# ── Constants ────────────────────────────────────────────────────
EMBEDDING_DIM = 384
DEDUP_THRESHOLD = 0.95       # cosine >= this AND same tags → deduplicate
MERGE_THRESHOLD = 0.80       # cosine >= this (within cluster) → merge
NLI_CONTRADICTION_THRESHOLD = 0.90  # NLI contradiction score → flag
MIN_CONTENT_LEN = 30         # Skip very short memories for NLI
STRENGTH_CAP = 5.0

_UID = os.getuid() if hasattr(os, 'getuid') else os.getpid()
DAEMON_SOCK = f"/tmp/b12-embed-{_UID}.sock"
DAEMON_PID = f"/tmp/b12-embed-{_UID}.pid"


# ── Result dataclass ─────────────────────────────────────────────

@dataclass
class ConsolidationResult:
    """Result of a consolidation run."""
    clusters_found: int = 0
    memories_processed: int = 0
    memories_deduplicated: int = 0
    memories_merged: int = 0
    contradictions_flagged: int = 0
    kept_separate: int = 0
    dry_run_report: List[Dict[str, Any]] = field(default_factory=list)


# ── Memory record ────────────────────────────────────────────────

@dataclass
class MemoryRecord:
    """In-memory representation of a memory row with its embedding."""
    id: int
    content_hash: str
    content: str
    tags: str
    memory_type: str
    metadata_str: str
    strength: float
    embedding: np.ndarray  # float32, shape (384,)

    @property
    def tag_set(self) -> set:
        return {t.strip() for t in (self.tags or '').split(',') if t.strip()}

    @property
    def metadata(self) -> dict:
        try:
            return json.loads(self.metadata_str or '{}')
        except (json.JSONDecodeError, TypeError):
            return {}

    def project_tag(self) -> str:
        for t in self.tag_set:
            if t.startswith('proj:'):
                return t[5:]
        return self.metadata.get('project', '')


# ── Daemon communication ─────────────────────────────────────────

def _daemon_alive() -> bool:
    if not os.path.exists(DAEMON_SOCK) or not os.path.exists(DAEMON_PID):
        return False
    try:
        pid = int(open(DAEMON_PID).read().strip())
        os.kill(pid, 0)
        return True
    except (ValueError, ProcessLookupError, PermissionError, FileNotFoundError):
        return False


def _daemon_request(payload: dict, timeout: float = 30) -> Optional[dict]:
    """Send JSON request to embed daemon, return parsed response or None."""
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


def _nli_check_pairs(pairs: List[List[str]]) -> Optional[List[dict]]:
    """Run NLI check via daemon. Returns list of {label, scores} or None."""
    if not pairs or not _daemon_alive():
        return None
    resp = _daemon_request({'op': 'nli_check', 'pairs': pairs}, timeout=60)
    if resp and resp.get('ok'):
        return resp.get('results', [])
    return None


# ── Database helpers ─────────────────────────────────────────────

def _open_db(db_path: str) -> sqlite3.Connection:
    """Open DB with sqlite-vec loaded, WAL mode, busy timeout."""
    import sqlite_vec
    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    return conn


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.strip().lower().encode("utf-8")).hexdigest()


def _load_memories(conn: sqlite3.Connection, project: Optional[str] = None) -> List[MemoryRecord]:
    """Load all active memories with their embeddings."""
    sql = """
        SELECT m.id, m.content_hash, m.content, m.tags, m.memory_type,
               m.metadata, COALESCE(m.strength, 1.0) AS strength,
               e.content_embedding
        FROM memories m
        JOIN memory_embeddings e ON m.id = e.rowid
        WHERE m.deleted_at IS NULL
    """
    params: List[Any] = []

    if project:
        sql += " AND m.tags LIKE ?"
        params.append(f"%proj:{project}%")

    sql += " ORDER BY m.id"
    rows = conn.execute(sql, params).fetchall()

    memories = []
    for row in rows:
        (mid, chash, content, tags, mtype, meta, strength, emb_bytes) = row
        if not emb_bytes or len(emb_bytes) != EMBEDDING_DIM * 4:
            continue
        emb = np.frombuffer(emb_bytes, dtype=np.float32).copy()
        memories.append(MemoryRecord(
            id=mid,
            content_hash=chash,
            content=content or '',
            tags=tags or '',
            memory_type=mtype or 'general',
            metadata_str=meta or '{}',
            strength=float(strength),
            embedding=emb,
        ))

    return memories


# ── Clustering ───────────────────────────────────────────────────

def _cluster_memories(memories: List[MemoryRecord], min_cluster_size: int = 3
                      ) -> Dict[int, List[MemoryRecord]]:
    """Run HDBSCAN on embedding vectors. Returns {cluster_label: [memories]}.
    Noise points (label -1) are excluded."""
    if len(memories) < min_cluster_size:
        return {}

    from sklearn.cluster import HDBSCAN

    # Stack embeddings into matrix
    X = np.vstack([m.embedding for m in memories])

    # Normalize for cosine metric (HDBSCAN with euclidean on L2-normed = cosine)
    norms = np.linalg.norm(X, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    X_normed = X / norms

    clusterer = HDBSCAN(
        min_cluster_size=min_cluster_size,
        metric='euclidean',
        cluster_selection_method='eom',
        n_jobs=1,
    )
    labels = clusterer.fit_predict(X_normed)

    clusters: Dict[int, List[MemoryRecord]] = {}
    for mem, label in zip(memories, labels):
        if label == -1:
            continue  # Noise — not similar enough to any cluster
        clusters.setdefault(label, []).append(mem)

    return clusters


def _cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity between two vectors."""
    dot = float(np.dot(a, b))
    norm_a = float(np.linalg.norm(a))
    norm_b = float(np.linalg.norm(b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


# ── Pairwise analysis within a cluster ───────────────────────────

def _analyze_cluster(
    cluster: List[MemoryRecord],
    use_nli: bool = True,
) -> Tuple[List[Tuple[MemoryRecord, MemoryRecord, str]], List[Dict]]:
    """Analyze a single cluster. Returns (actions, report_entries).

    Each action is (mem_a, mem_b, action_type) where action_type is
    'dedup', 'merge', 'contradiction', or 'keep'.
    """
    actions: List[Tuple[MemoryRecord, MemoryRecord, str]] = []
    report: List[Dict] = []
    nli_pairs: List[Tuple[int, int, List[str]]] = []  # (idx_a, idx_b, [text_a, text_b])

    n = len(cluster)
    for i in range(n):
        for j in range(i + 1, n):
            a = cluster[i]
            b = cluster[j]
            sim = _cosine_similarity(a.embedding, b.embedding)

            if sim >= DEDUP_THRESHOLD and a.tag_set == b.tag_set:
                actions.append((a, b, 'dedup'))
                report.append({
                    'type': 'dedup',
                    'ids': [a.id, b.id],
                    'similarity': round(sim, 4),
                    'snippets': [a.content[:80], b.content[:80]],
                })
            elif sim >= MERGE_THRESHOLD:
                # Potential merge — but check NLI first for contradictions
                if (use_nli and _daemon_alive()
                        and len(a.content) >= MIN_CONTENT_LEN
                        and len(b.content) >= MIN_CONTENT_LEN):
                    nli_pairs.append((i, j, [a.content[:512], b.content[:512]]))
                else:
                    actions.append((a, b, 'merge'))
                    report.append({
                        'type': 'merge',
                        'ids': [a.id, b.id],
                        'similarity': round(sim, 4),
                        'snippets': [a.content[:80], b.content[:80]],
                    })

    # Batch NLI for merge candidates
    if nli_pairs:
        pairs_text = [p[2] for p in nli_pairs]
        nli_results = _nli_check_pairs(pairs_text)

        for k, (idx_i, idx_j, _) in enumerate(nli_pairs):
            a = cluster[idx_i]
            b = cluster[idx_j]
            sim = _cosine_similarity(a.embedding, b.embedding)

            nli_result = nli_results[k] if nli_results and k < len(nli_results) else None
            c_score = 0.0
            if nli_result:
                c_score = nli_result.get('scores', {}).get('contradiction', 0.0)

            if c_score >= NLI_CONTRADICTION_THRESHOLD:
                actions.append((a, b, 'contradiction'))
                report.append({
                    'type': 'contradiction',
                    'ids': [a.id, b.id],
                    'similarity': round(sim, 4),
                    'nli_score': round(c_score, 4),
                    'snippets': [a.content[:80], b.content[:80]],
                })
            else:
                actions.append((a, b, 'merge'))
                report.append({
                    'type': 'merge',
                    'ids': [a.id, b.id],
                    'similarity': round(sim, 4),
                    'snippets': [a.content[:80], b.content[:80]],
                })

    return actions, report


# ── Apply actions to DB ──────────────────────────────────────────

def _apply_dedup(conn: sqlite3.Connection, a: MemoryRecord, b: MemoryRecord) -> int:
    """Deduplicate: keep highest-strength memory, soft-delete the other.
    Returns the kept memory's ID."""
    now = datetime.now(timezone.utc)
    now_ts = now.timestamp()

    if a.strength >= b.strength:
        keep, remove = a, b
    else:
        keep, remove = b, a

    # Increment access_count on kept memory
    meta = keep.metadata
    meta['access_count'] = meta.get('access_count', 0) + 1
    meta_json = json.dumps(meta, ensure_ascii=False)

    conn.execute(
        "UPDATE memories SET metadata = ?, updated_at = ?, updated_at_iso = ? WHERE id = ?",
        (meta_json, now_ts, now.isoformat(), keep.id),
    )

    # Soft-delete the duplicate
    conn.execute(
        "UPDATE memories SET deleted_at = ? WHERE id = ?",
        (now_ts, remove.id),
    )

    # Rewrite graph edges from removed → kept
    _rewrite_graph_edges(conn, remove.content_hash, keep.content_hash)

    return keep.id


def _apply_merge(conn: sqlite3.Connection, memories: List[MemoryRecord]) -> int:
    """Merge a list of memories into one. Returns the new/updated memory's ID.

    Strategy: use the strongest memory as the base, append unique content
    from others, preserve all tags and metadata.
    """
    now = datetime.now(timezone.utc)
    now_ts = now.timestamp()

    # Sort by strength descending — strongest becomes the base
    sorted_mems = sorted(memories, key=lambda m: m.strength, reverse=True)
    base = sorted_mems[0]
    others = sorted_mems[1:]

    # Collect unique content lines from others not already in base
    base_lines = set(base.content.strip().lower().split('\n'))
    extra_parts = []
    for m in others:
        for line in m.content.strip().split('\n'):
            if line.strip() and line.strip().lower() not in base_lines:
                extra_parts.append(line.strip())
                base_lines.add(line.strip().lower())

    if extra_parts:
        merged_content = base.content.rstrip() + '\n' + '\n'.join(
            f'* {part}' for part in extra_parts
        )
    else:
        merged_content = base.content

    new_hash = _sha256_hex(merged_content)
    source_ids = [m.id for m in memories]

    # Build merged metadata
    merged_meta = base.metadata
    merged_meta['consolidated_from'] = source_ids
    merged_meta['consolidated_at'] = now.isoformat()
    merged_meta['access_count'] = sum(
        m.metadata.get('access_count', 0) for m in memories
    )
    merged_meta_json = json.dumps(merged_meta, ensure_ascii=False)

    # Merge all tags
    all_tags = set()
    for m in memories:
        all_tags.update(m.tag_set)
    merged_tags = ','.join(sorted(all_tags))

    # New strength: sum capped at STRENGTH_CAP
    new_strength = min(sum(m.strength for m in memories), STRENGTH_CAP)

    # Check for hash collision with existing non-deleted memory
    existing = conn.execute(
        "SELECT id FROM memories WHERE content_hash = ? AND deleted_at IS NULL AND id != ?",
        (new_hash, base.id),
    ).fetchone()
    if existing:
        # Hash collision — use base hash unchanged
        new_hash = base.content_hash

    # Update base memory with merged content
    conn.execute(
        """UPDATE memories
           SET content = ?, content_hash = ?, tags = ?, metadata = ?,
               strength = ?, updated_at = ?, updated_at_iso = ?
           WHERE id = ?""",
        (merged_content, new_hash, merged_tags, merged_meta_json,
         new_strength, now_ts, now.isoformat(), base.id),
    )

    # Re-embed the merged content via daemon
    if _daemon_alive():
        resp = _daemon_request({'op': 'encode_batch', 'texts': [merged_content]})
        if resp and resp.get('embeddings'):
            import base64
            emb_bytes = base64.b64decode(resp['embeddings'][0])
            # Upsert embedding (vec0 needs UPDATE if exists, INSERT if not)
            row_exists = conn.execute(
                "SELECT 1 FROM memory_embeddings WHERE rowid = ?", (base.id,)
            ).fetchone()
            if row_exists:
                conn.execute(
                    "UPDATE memory_embeddings SET content_embedding = ? WHERE rowid = ?",
                    (emb_bytes, base.id),
                )
            else:
                conn.execute(
                    "INSERT INTO memory_embeddings (rowid, content_embedding) VALUES (?, ?)",
                    (base.id, emb_bytes),
                )

    # Soft-delete others and rewrite their graph edges
    for m in others:
        conn.execute(
            "UPDATE memories SET deleted_at = ? WHERE id = ?",
            (now_ts, m.id),
        )
        _rewrite_graph_edges(conn, m.content_hash, new_hash)

    # Rewrite base's old hash if it changed
    if base.content_hash != new_hash:
        _rewrite_graph_edges(conn, base.content_hash, new_hash)

    return base.id


def _flag_contradiction(conn: sqlite3.Connection, a: MemoryRecord, b: MemoryRecord) -> None:
    """Flag a contradiction pair for user review. Never auto-resolve."""
    now_ts = datetime.now(timezone.utc).timestamp()

    for mem in (a, b):
        meta = mem.metadata
        flags = meta.get('flags', [])
        if 'needs-review' not in flags:
            flags.append('needs-review')
            meta['flags'] = flags
            conn.execute(
                "UPDATE memories SET metadata = ? WHERE id = ?",
                (json.dumps(meta, ensure_ascii=False), mem.id),
            )

    # Write contradicts edge if not already present
    existing = conn.execute(
        "SELECT 1 FROM memory_graph WHERE "
        "((source_hash = ? AND target_hash = ?) OR "
        " (source_hash = ? AND target_hash = ?)) AND "
        "relationship_type = 'contradicts' LIMIT 1",
        (a.content_hash, b.content_hash, b.content_hash, a.content_hash),
    ).fetchone()

    if not existing:
        conn.execute(
            """INSERT OR IGNORE INTO memory_graph
               (source_hash, target_hash, similarity, connection_types,
                metadata, created_at, relationship_type)
               VALUES (?, ?, ?, '["consolidation"]', ?, ?, 'contradicts')""",
            (a.content_hash, b.content_hash, 0.0,
             json.dumps({"source": "consolidation_engine"}), now_ts),
        )


def _rewrite_graph_edges(conn: sqlite3.Connection, old_hash: str, new_hash: str) -> None:
    """Rewrite memory_graph edges from old_hash to new_hash."""
    if old_hash == new_hash:
        return

    # Check if memory_graph table exists
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_graph' LIMIT 1"
    ).fetchone():
        return

    rows = conn.execute(
        """SELECT source_hash, target_hash, similarity, connection_types,
                  metadata, created_at, relationship_type
           FROM memory_graph
           WHERE source_hash = ? OR target_hash = ?""",
        (old_hash, old_hash),
    ).fetchall()

    if not rows:
        return

    for src, tgt, sim, c_types, meta, created_at, rel_type in rows:
        new_src = new_hash if src == old_hash else src
        new_tgt = new_hash if tgt == old_hash else tgt
        if new_src == new_tgt:
            continue  # Skip self-edges
        conn.execute(
            """INSERT OR IGNORE INTO memory_graph
               (source_hash, target_hash, similarity, connection_types,
                metadata, created_at, relationship_type)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (new_src, new_tgt, sim, c_types, meta, created_at, rel_type),
        )

    conn.execute(
        "DELETE FROM memory_graph WHERE source_hash = ? OR target_hash = ?",
        (old_hash, old_hash),
    )


# ── Main consolidation function ──────────────────────────────────

def consolidate(
    db_path: Optional[str] = None,
    project: Optional[str] = None,
    dry_run: bool = False,
    min_cluster_size: int = 3,
) -> ConsolidationResult:
    """Run smart consolidation on the B12 memory database.

    Parameters
    ----------
    db_path : str or None
        Path to sqlite_vec.db. Uses shared_patterns.get_db_path() if None.
    project : str or None
        Only consolidate memories with this proj: tag. None = all memories.
    dry_run : bool
        If True, report clusters without modifying data.
    min_cluster_size : int
        Minimum cluster size for HDBSCAN (default 3).

    Returns
    -------
    ConsolidationResult
        Summary of actions taken (or would be taken in dry-run).
    """
    if db_path is None:
        db_path = get_db_path()

    if not os.path.exists(db_path):
        raise FileNotFoundError(f"Database not found: {db_path}")

    conn = _open_db(db_path)
    result = ConsolidationResult()

    try:
        memories = _load_memories(conn, project=project)
        result.memories_processed = len(memories)

        if len(memories) < min_cluster_size:
            return result

        # Step 1: Cluster
        clusters = _cluster_memories(memories, min_cluster_size=min_cluster_size)
        result.clusters_found = len(clusters)

        # Step 2: Analyze each cluster
        # Track which memory IDs have been consumed (deduped/merged away)
        consumed_ids: set = set()

        for label, cluster_mems in sorted(clusters.items()):
            # Filter out already-consumed memories
            active_mems = [m for m in cluster_mems if m.id not in consumed_ids]
            if len(active_mems) < 2:
                continue

            actions, report = _analyze_cluster(active_mems, use_nli=_daemon_alive())
            result.dry_run_report.extend(report)

            if dry_run:
                for entry in report:
                    if entry['type'] == 'dedup':
                        result.memories_deduplicated += 1
                    elif entry['type'] == 'merge':
                        result.memories_merged += 1
                    elif entry['type'] == 'contradiction':
                        result.contradictions_flagged += 1
                    else:
                        result.kept_separate += 1
                continue

            # Two-pass: dedups first (consume losers), then merges from survivors.
            # This prevents a memory consumed by dedup from lingering in a merge group.

            # Pass 1: Deduplications
            for a, b, action_type in actions:
                if action_type != 'dedup':
                    continue
                if a.id in consumed_ids or b.id in consumed_ids:
                    continue
                kept_id = _apply_dedup(conn, a, b)
                consumed_ids.add(a.id if kept_id != a.id else b.id)
                result.memories_deduplicated += 1

            # Pass 2: Contradictions (before merges — contradicting pairs must not merge)
            for a, b, action_type in actions:
                if action_type != 'contradiction':
                    continue
                if a.id in consumed_ids or b.id in consumed_ids:
                    continue
                _flag_contradiction(conn, a, b)
                result.contradictions_flagged += 1

            # Pass 3: Collect merge groups from unconsumed memories
            merge_group: List[MemoryRecord] = []
            merge_ids: set = set()
            for a, b, action_type in actions:
                if action_type != 'merge':
                    continue
                if a.id in consumed_ids or b.id in consumed_ids:
                    continue
                if a.id not in merge_ids:
                    merge_group.append(a)
                    merge_ids.add(a.id)
                if b.id not in merge_ids:
                    merge_group.append(b)
                    merge_ids.add(b.id)

            # Apply merge group if we collected any
            if len(merge_group) >= 2:
                kept_id = _apply_merge(conn, merge_group)
                for m in merge_group:
                    if m.id != kept_id:
                        consumed_ids.add(m.id)
                result.memories_merged += len(merge_group) - 1

        if not dry_run:
            conn.commit()

    finally:
        conn.close()

    return result


# ── CLI entry point ──────────────────────────────────────────────

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="B12 Smart Consolidation Engine — HDBSCAN clustering + semantic merge"
    )
    parser.add_argument(
        '--db-path', type=str, default=None,
        help='Path to sqlite_vec.db (default: auto-detect)',
    )
    parser.add_argument(
        '--project', type=str, default=None,
        help='Only consolidate memories with this project tag',
    )
    parser.add_argument(
        '--dry-run', action='store_true',
        help='Report clusters without modifying data',
    )
    parser.add_argument(
        '--min-cluster-size', type=int, default=3,
        help='Minimum cluster size for HDBSCAN (default: 3)',
    )
    args = parser.parse_args(argv)

    t0 = time.time()
    result = consolidate(
        db_path=args.db_path,
        project=args.project,
        dry_run=args.dry_run,
        min_cluster_size=args.min_cluster_size,
    )
    elapsed = time.time() - t0

    # Print report
    print("=" * 60)
    print("  B12 Smart Consolidation Report")
    if args.dry_run:
        print("  (DRY RUN — no changes made)")
    print("=" * 60)
    print()
    print(f"  Memories processed:    {result.memories_processed}")
    print(f"  Clusters found:        {result.clusters_found}")
    print(f"  Deduplicated:          {result.memories_deduplicated}")
    print(f"  Merged:                {result.memories_merged}")
    print(f"  Contradictions flagged: {result.contradictions_flagged}")
    print(f"  Elapsed:               {elapsed:.1f}s")
    print()

    if result.dry_run_report:
        print("  Cluster details:")
        for entry in result.dry_run_report:
            action = entry['type'].upper()
            ids = entry['ids']
            sim = entry.get('similarity', 0)
            nli = entry.get('nli_score', '')
            nli_str = f" NLI:{nli}" if nli else ''
            print(f"    [{action}] #{ids[0]} <-> #{ids[1]}  "
                  f"(cosine: {sim:.3f}{nli_str})")
            for snippet in entry.get('snippets', []):
                print(f"      {snippet}")
        print()

    if not args.dry_run and (result.memories_deduplicated or result.memories_merged):
        print("  Changes applied to database.")
    elif args.dry_run and (result.memories_deduplicated or result.memories_merged):
        print("  Run without --dry-run to apply these changes.")

    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
