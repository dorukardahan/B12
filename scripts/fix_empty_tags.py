#!/usr/bin/env python3
"""One-time fix: repair memories with empty 'user:' tag.

37 memories have tags like "proj:B12,user:,session-summary,2026-02".
This script replaces "user:," with "user:personal," in all affected rows.

Usage:
    python3 scripts/fix_empty_tags.py [--dry-run]
"""
import os
import sys
import sqlite3

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

def main():
    dry_run = '--dry-run' in sys.argv

    if not os.path.exists(DB_PATH):
        print(f"ERROR: DB not found at {DB_PATH}")
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA busy_timeout=5000")

    # Count affected rows
    count = conn.execute(
        "SELECT COUNT(*) FROM memories WHERE tags LIKE '%user:,%' AND tags NOT LIKE '%user:personal%' AND deleted_at IS NULL"
    ).fetchone()[0]

    print(f"Found {count} memories with empty user: tag")

    if count == 0:
        print("Nothing to fix.")
        conn.close()
        return

    if dry_run:
        # Show affected rows
        rows = conn.execute(
            "SELECT id, tags FROM memories WHERE tags LIKE '%user:,%' AND tags NOT LIKE '%user:personal%' AND deleted_at IS NULL"
        ).fetchall()
        for row_id, tags in rows:
            print(f"  id={row_id} tags={tags}")
        print(f"\nDry run: {count} rows would be fixed. Run without --dry-run to apply.")
    else:
        conn.execute("""
            UPDATE memories
            SET tags = REPLACE(tags, 'user:,', 'user:personal,')
            WHERE tags LIKE '%user:,%'
              AND tags NOT LIKE '%user:personal%'
              AND deleted_at IS NULL
        """)
        conn.commit()
        # Verify
        remaining = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE tags LIKE '%user:,%' AND tags NOT LIKE '%user:personal%' AND deleted_at IS NULL"
        ).fetchone()[0]
        print(f"Fixed {count} rows. Remaining empty tags: {remaining}")

    conn.close()


if __name__ == '__main__':
    main()
