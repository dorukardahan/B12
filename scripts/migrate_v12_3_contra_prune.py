#!/usr/bin/env python3
"""One-shot migration: prune legacy low-confidence 'contradicts' edges.

Before v12.3 the NLI write-time threshold was 0.7 (write_time_merge.py:499)
while the retrieval surface had no threshold filter — so any 0.71-0.79 edge
would surface on every recall, producing noisy cross-domain false positives
(SQLite vs PostgreSQL, REST vs gRPC, etc.).

v12.3 unifies the threshold at 0.8 (write_time_merge.NLI_CONTRADICTION_THRESHOLD)
and the surface filter at 0.85 (B12_CONTRA_SURFACE_THRESHOLD). This script
prunes the legacy below-0.8 edges so a fresh recall does not show pre-v12.3
junk.

Idempotent. Safe to run multiple times. Reports before/after counts.

Usage:
  python3 scripts/migrate_v12_3_contra_prune.py            # uses default DB
  python3 scripts/migrate_v12_3_contra_prune.py /path/to/db.sqlite
  python3 scripts/migrate_v12_3_contra_prune.py --dry-run  # show count only
"""
import os
import sqlite3
import sys

# Allow import-as-module sibling lookup
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from shared_patterns import get_db_path
except ImportError:
    def get_db_path():
        return os.path.expanduser(
            "~/Library/Application Support/mcp-memory/sqlite_vec.db"
        )

PRUNE_THRESHOLD = 0.8


def main(argv):
    dry_run = "--dry-run" in argv
    args = [a for a in argv if a != "--dry-run"]
    db_path = args[1] if len(args) > 1 else get_db_path()

    if not os.path.exists(db_path):
        print(f"DB not found: {db_path}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute("PRAGMA busy_timeout=10000")
    try:
        has_graph = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_graph'"
        ).fetchone()
        if not has_graph:
            print(f"DB: {db_path}")
            print("No memory_graph table found. Nothing to prune.")
            return 0
        before = conn.execute(
            "SELECT COUNT(*) FROM memory_graph "
            "WHERE relationship_type='contradicts'"
        ).fetchone()[0]
        legacy = conn.execute(
            "SELECT COUNT(*) FROM memory_graph "
            "WHERE relationship_type='contradicts' AND similarity < ?",
            (PRUNE_THRESHOLD,),
        ).fetchone()[0]

        print(f"DB: {db_path}")
        print(f"contradicts edges total:    {before}")
        print(f"  legacy (< {PRUNE_THRESHOLD}):           {legacy}")
        print(f"  kept   (>= {PRUNE_THRESHOLD}):          {before - legacy}")

        if dry_run:
            print("[dry-run] no changes written")
            return 0
        if legacy == 0:
            print("Nothing to prune. Already migrated.")
            return 0

        conn.execute(
            "DELETE FROM memory_graph "
            "WHERE relationship_type='contradicts' AND similarity < ?",
            (PRUNE_THRESHOLD,),
        )
        conn.commit()
        after = conn.execute(
            "SELECT COUNT(*) FROM memory_graph "
            "WHERE relationship_type='contradicts'"
        ).fetchone()[0]
        print(f"Pruned {before - after} legacy edges.")
        print(f"contradicts edges remaining: {after}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
