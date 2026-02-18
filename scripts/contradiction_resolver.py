#!/usr/bin/env python3
"""
B12 Contradiction Resolver — human-in-the-loop review of detected contradictions.

NLI accuracy is ~71%, so contradictions should NEVER be auto-resolved.
This CLI lists detected contradictions and offers interactive resolution.

Usage:
  python3 contradiction_resolver.py             # List all contradictions
  python3 contradiction_resolver.py --resolve   # Interactive resolution

Resolution options per pair:
  A  — Keep memory A, soft-delete memory B
  B  — Keep memory B, soft-delete memory A
  K  — Keep both (mark as reviewed, remove contradicts edge)
  M  — Merge both into one memory
  S  — Skip (no action)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone

DB_PATH = os.path.expanduser("~/Library/Application Support/mcp-memory/sqlite_vec.db")


def get_conn():
    try:
        import sqlite_vec
    except ImportError:
        print("ERROR: sqlite_vec not installed")
        sys.exit(1)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    return conn


def list_contradictions(conn):
    """Fetch all contradiction edges with memory details."""
    rows = conn.execute("""
        SELECT mg.source_hash, mg.target_hash, mg.similarity, mg.metadata,
               m1.id, m1.content, m1.memory_type, m1.tags,
               m2.id, m2.content, m2.memory_type, m2.tags
        FROM memory_graph mg
        LEFT JOIN memories m1 ON m1.content_hash = mg.source_hash AND m1.deleted_at IS NULL
        LEFT JOIN memories m2 ON m2.content_hash = mg.target_hash AND m2.deleted_at IS NULL
        WHERE mg.relationship_type = 'contradicts'
        ORDER BY mg.similarity DESC
    """).fetchall()
    return rows


def print_pair(idx, row):
    src_hash, tgt_hash, sim, meta_json, id1, c1, t1, tags1, id2, c2, t2, tags2 = row
    print(f"\n{'='*70}")
    print(f"Contradiction #{idx+1}  (NLI score: {sim:.2f})")
    print(f"{'='*70}")

    if id1 and c1:
        print(f"\n  [A] Memory #{id1} ({t1})")
        print(f"      Tags: {tags1}")
        for line in c1[:500].split('\n'):
            print(f"      {line}")
    else:
        print(f"\n  [A] MISSING (hash: {src_hash[:16]}...)")

    if id2 and c2:
        print(f"\n  [B] Memory #{id2} ({t2})")
        print(f"      Tags: {tags2}")
        for line in c2[:500].split('\n'):
            print(f"      {line}")
    else:
        print(f"\n  [B] MISSING (hash: {tgt_hash[:16]}...)")

    if meta_json:
        try:
            meta = json.loads(meta_json)
            source = meta.get('detected_by', meta.get('source', ''))
            if source:
                print(f"\n  Detected by: {source}")
        except (json.JSONDecodeError, TypeError):
            pass


def soft_delete(conn, memory_id):
    """Soft-delete a memory."""
    now = datetime.now(timezone.utc)
    conn.execute(
        "UPDATE memories SET deleted_at = ? WHERE id = ?",
        (now.timestamp(), memory_id)
    )


def remove_edge(conn, src_hash, tgt_hash):
    """Remove the contradicts edge."""
    conn.execute(
        "DELETE FROM memory_graph WHERE source_hash = ? AND target_hash = ? AND relationship_type = 'contradicts'",
        (src_hash, tgt_hash)
    )


def mark_reviewed(conn, memory_hash):
    """Add 'reviewed' flag to memory metadata, remove 'needs-review'."""
    row = conn.execute(
        "SELECT metadata FROM memories WHERE content_hash = ? AND deleted_at IS NULL",
        (memory_hash,)
    ).fetchone()
    if not row:
        return
    try:
        meta = json.loads(row[0]) if row[0] else {}
    except (json.JSONDecodeError, TypeError):
        meta = {}
    flags = meta.get('flags', [])
    if 'needs-review' in flags:
        flags.remove('needs-review')
    if 'contradiction-reviewed' not in flags:
        flags.append('contradiction-reviewed')
    meta['flags'] = flags
    conn.execute(
        "UPDATE memories SET metadata = ? WHERE content_hash = ? AND deleted_at IS NULL",
        (json.dumps(meta, ensure_ascii=False), memory_hash)
    )


def merge_memories(conn, id1, content1, hash1, id2, content2, hash2):
    """Merge two memories: combine content into memory A, soft-delete B."""
    merged = f"{content1.rstrip()}\n• [Merged from #{id2}] {content2.strip()}"
    new_hash = hashlib.sha256(merged.encode()).hexdigest()
    now = datetime.now(timezone.utc)

    # Update memory A with merged content
    conn.execute(
        "UPDATE memories SET content = ?, content_hash = ?, updated_at = ?, updated_at_iso = ? WHERE id = ?",
        (merged, new_hash, now.timestamp(), now.isoformat(), id1)
    )

    # Rewrite graph edges from old hashes to new hash (both memories)
    for old_h in (hash1, hash2):
        edges = conn.execute(
            "SELECT source_hash, target_hash, similarity, connection_types, metadata, created_at, relationship_type "
            "FROM memory_graph WHERE source_hash = ? OR target_hash = ?",
            (old_h, old_h)
        ).fetchall()
        for src, tgt, sim, ct, meta, ca, rt in edges:
            new_src = new_hash if src == old_h else src
            new_tgt = new_hash if tgt == old_h else tgt
            if new_src == new_tgt:
                continue  # skip self-edges
            conn.execute(
                "INSERT OR IGNORE INTO memory_graph "
                "(source_hash, target_hash, similarity, connection_types, metadata, created_at, relationship_type) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (new_src, new_tgt, sim, ct, meta, ca, rt)
            )
        conn.execute("DELETE FROM memory_graph WHERE source_hash = ? OR target_hash = ?", (old_h, old_h))

    # Soft-delete memory B
    soft_delete(conn, id2)

    print(f"  Merged into #{id1}, deleted #{id2}")


def resolve_interactive(conn, rows):
    """Interactive resolution loop."""
    actions = {'a': 'Keep A', 'b': 'Keep B', 'k': 'Keep both', 'm': 'Merge', 's': 'Skip'}

    resolved = 0
    skipped = 0

    for idx, row in enumerate(rows):
        src_hash, tgt_hash, sim, meta_json, id1, c1, t1, tags1, id2, c2, t2, tags2 = row

        # Skip if either memory is already deleted
        if not id1 or not c1 or not id2 or not c2:
            print(f"\n  Skipping #{idx+1} — one or both memories already deleted")
            continue

        print_pair(idx, row)

        print(f"\n  Options: [A] Keep A  [B] Keep B  [K] Keep both  [M] Merge  [S] Skip  [Q] Quit")

        while True:
            choice = input("  Choice: ").strip().lower()
            if choice in actions or choice == 'q':
                break
            print("  Invalid choice. Use A, B, K, M, S, or Q.")

        if choice == 'q':
            print(f"\nStopped. Resolved: {resolved}, Skipped: {skipped}")
            break

        if choice == 'a':
            soft_delete(conn, id2)
            remove_edge(conn, src_hash, tgt_hash)
            mark_reviewed(conn, src_hash)
            conn.commit()
            print(f"  Kept #{id1}, deleted #{id2}")
            resolved += 1

        elif choice == 'b':
            soft_delete(conn, id1)
            remove_edge(conn, src_hash, tgt_hash)
            mark_reviewed(conn, tgt_hash)
            conn.commit()
            print(f"  Kept #{id2}, deleted #{id1}")
            resolved += 1

        elif choice == 'k':
            remove_edge(conn, src_hash, tgt_hash)
            mark_reviewed(conn, src_hash)
            mark_reviewed(conn, tgt_hash)
            conn.commit()
            print(f"  Kept both, removed contradicts edge, marked reviewed")
            resolved += 1

        elif choice == 'm':
            merge_memories(conn, id1, c1, src_hash, id2, c2, tgt_hash)
            remove_edge(conn, src_hash, tgt_hash)
            conn.commit()
            resolved += 1

        elif choice == 's':
            skipped += 1

    print(f"\nDone. Resolved: {resolved}, Skipped: {skipped}")


def main():
    parser = argparse.ArgumentParser(description="B12 Contradiction Resolver")
    parser.add_argument('--resolve', action='store_true', help='Interactive resolution mode')
    args = parser.parse_args()

    if not os.path.exists(DB_PATH):
        print("ERROR: Database not found")
        return 1

    conn = get_conn()
    rows = list_contradictions(conn)

    if not rows:
        print("No contradictions found in memory graph.")
        conn.close()
        return 0

    print(f"Found {len(rows)} contradiction(s)")

    if args.resolve:
        resolve_interactive(conn, rows)
    else:
        for idx, row in enumerate(rows):
            print_pair(idx, row)
        print(f"\nRun with --resolve for interactive resolution")

    conn.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
