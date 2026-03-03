#!/usr/bin/env python3
"""
Backfill memory_fts_stemmed from existing memories table.

Idempotent: safe to run multiple times. Clears and rebuilds the FTS index
from all active (non-deleted) memories.

Usage:
    python3 scripts/migrate_stemmed_fts.py --db-path /path/to/sqlite_vec.db
"""

import argparse
import os
import sqlite3
import sys


def get_default_db_path() -> str:
    """Return default DB path matching b12_mcp_server.py logic."""
    home = os.path.expanduser("~")
    if sys.platform == "darwin":
        return os.path.join(home, "Library", "Application Support",
                            "mcp-memory", "sqlite_vec.db")
    elif sys.platform == "win32":
        return os.path.join(home, "AppData", "Local",
                            "mcp-memory", "sqlite_vec.db")
    else:
        return os.path.join(home, ".local", "share",
                            "mcp-memory", "sqlite_vec.db")


def migrate(db_path: str) -> None:
    """Create memory_fts_stemmed if needed and backfill from memories."""
    if not os.path.exists(db_path):
        print(f"ERROR: Database not found: {db_path}", file=sys.stderr)
        sys.exit(1)

    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=30000")

    # Ensure the stemmed FTS table exists
    db.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts_stemmed USING fts5(
            content,
            tags,
            content='memories',
            content_rowid='id',
            tokenize='porter unicode61'
        )
    """)

    # Count active memories
    total = db.execute(
        "SELECT COUNT(*) FROM memories WHERE deleted_at IS NULL"
    ).fetchone()[0]
    print(f"Found {total} active memories to index.")

    if total == 0:
        print("Nothing to backfill.")
        db.close()
        return

    # Clear existing FTS content (idempotent rebuild)
    # FTS5 'delete-all' command removes all entries from the index
    db.execute("INSERT INTO memory_fts_stemmed(memory_fts_stemmed) VALUES('delete-all')")

    # Backfill in batches
    batch_size = 500
    indexed = 0
    cursor = db.execute(
        "SELECT id, content, tags FROM memories WHERE deleted_at IS NULL ORDER BY id"
    )

    while True:
        rows = cursor.fetchmany(batch_size)
        if not rows:
            break
        for row in rows:
            db.execute(
                "INSERT INTO memory_fts_stemmed(rowid, content, tags) VALUES (?, ?, ?)",
                (row["id"], row["content"], row["tags"] or ""),
            )
        indexed += len(rows)
        print(f"  Indexed {indexed}/{total} memories...")

    db.commit()
    db.close()
    print(f"Done. Backfilled {indexed} memories into memory_fts_stemmed.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill memory_fts_stemmed (porter-stemmed FTS5) from memories table."
    )
    parser.add_argument(
        "--db-path",
        default=get_default_db_path(),
        help="Path to SQLite database (default: platform-specific mcp-memory path)",
    )
    args = parser.parse_args()
    migrate(args.db_path)


if __name__ == "__main__":
    main()
