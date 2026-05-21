#!/usr/bin/env python3
"""
B12 FSRS Migration — adds difficulty, due_date columns and migrates strength → FSRS.

Safe to run multiple times (idempotent).

Usage:
    python3 scripts/migrate_fsrs.py
    # or via venv:
    ~/.local/b12-venv/bin/python3 scripts/migrate_fsrs.py
"""

import os
import sys
import sqlite3
import json
from datetime import datetime, timezone, timedelta

# Add scripts dir to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from shared_patterns import DB_PATH


def migrate(db_path: str = DB_PATH) -> bool:
    if not os.path.exists(db_path):
        print(f"Database not found at {db_path}")
        return False

    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")

    # Step 1: Add new columns if they don't exist
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(memories)")}

    added = []
    if "difficulty" not in existing_cols:
        conn.execute("ALTER TABLE memories ADD COLUMN difficulty REAL DEFAULT 5.0")
        added.append("difficulty")

    if "due_date" not in existing_cols:
        conn.execute("ALTER TABLE memories ADD COLUMN due_date TEXT")
        added.append("due_date")

    if added:
        print(f"Added columns: {', '.join(added)}")
    else:
        print("Columns already exist (idempotent)")

    # Step 2: Migrate existing memories
    # Get memories that need migration (no due_date set yet)
    rows = conn.execute("""
        SELECT id, strength, metadata, created_at
        FROM memories
        WHERE deleted_at IS NULL AND due_date IS NULL
    """).fetchall()

    if not rows:
        print("No memories need migration")
        conn.close()
        return True

    print(f"Migrating {len(rows)} memories...")

    now = datetime.now(timezone.utc)
    migrated = 0

    for mem_id, strength, metadata_str, created_at in rows:
        strength = 1.0 if strength is None else float(strength)

        # Extract access_count from metadata
        access_count = 0
        try:
            if metadata_str:
                meta = json.loads(metadata_str)
                access_count = meta.get("access_count", 0)
        except (json.JSONDecodeError, TypeError):
            pass

        # Infer difficulty from access_count
        if access_count == 0:
            difficulty = 5.0
        elif access_count <= 2:
            difficulty = 7.0
        elif access_count <= 5:
            difficulty = 5.0
        else:
            difficulty = 3.0

        # Due date: now + stability days
        due = now + timedelta(days=max(strength, 0.5))

        conn.execute(
            "UPDATE memories SET difficulty = ?, due_date = ? WHERE id = ?",
            (difficulty, due.isoformat(), mem_id)
        )
        migrated += 1

    conn.commit()
    conn.close()
    print(f"Migrated {migrated} memories to FSRS format")
    return True


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    db_path = args[0] if args else DB_PATH
    return 0 if migrate(db_path) else 1


if __name__ == "__main__":
    sys.exit(main())
