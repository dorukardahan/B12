#!/usr/bin/env python3
"""Migration: Fix malformed metadata (f-string format → valid JSON).

Old Codex sessions stored metadata as f-strings like:
  "type:progress, importance:0.6, session_id:abc123"
instead of valid JSON:
  {"type": "progress", "importance_score": 0.6, "session_id": "abc123"}

This migration converts all invalid metadata to proper JSON.

Usage:
  python3 migrate_fix_metadata.py [DB_PATH]
  python3 migrate_fix_metadata.py --dry-run [DB_PATH]
"""

import json
import re
import sqlite3
import sys
import os


def parse_legacy_metadata(raw: str) -> dict:
    """Parse f-string metadata like 'type:progress, importance:0.6' into a dict."""
    result = {}
    # Split by comma, then parse key:value pairs
    for part in raw.split(","):
        part = part.strip()
        if ":" not in part:
            continue
        key, _, value = part.partition(":")
        key = key.strip()
        value = value.strip()

        # Normalize key names
        if key == "importance":
            key = "importance_score"
        elif key == "type" and value in ("progress", "session_summary"):
            key = "type"

        # Try numeric conversion
        try:
            value = float(value)
            if value == int(value):
                value = int(value)
        except (ValueError, TypeError):
            pass

        result[key] = value

    return result


def main():
    dry_run = "--dry-run" in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith("--")]

    if args:
        db_path = args[0]
    elif sys.platform == "darwin":
        db_path = os.path.expanduser("~/Library/Application Support/mcp-memory/sqlite_vec.db")
    elif sys.platform == "win32":
        db_path = os.path.expanduser("~/AppData/Local/mcp-memory/sqlite_vec.db")
    else:
        db_path = os.path.expanduser("~/.local/share/mcp-memory/sqlite_vec.db")

    if not os.path.exists(db_path):
        print(f"Database not found: {db_path}")
        sys.exit(1)

    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.row_factory = sqlite3.Row

    # Find all rows with invalid metadata
    rows = conn.execute("""
        SELECT id, metadata FROM memories
        WHERE metadata IS NOT NULL AND metadata != '' AND json_valid(metadata) = 0
    """).fetchall()

    print(f"Database: {db_path}")
    print(f"Found {len(rows)} rows with invalid metadata")

    if not rows:
        print("Nothing to fix.")
        conn.close()
        return

    fixed = 0
    skipped = 0
    for row in rows:
        mem_id = row["id"]
        raw = row["metadata"]

        parsed = parse_legacy_metadata(raw)
        if not parsed:
            if not dry_run:
                # Set to empty JSON object
                conn.execute("UPDATE memories SET metadata = '{}' WHERE id = ?", (mem_id,))
            skipped += 1
            continue

        new_metadata = json.dumps(parsed, ensure_ascii=False)

        if dry_run:
            print(f"  [{mem_id}] {raw[:60]} → {new_metadata[:60]}")
        else:
            conn.execute("UPDATE memories SET metadata = ? WHERE id = ?", (new_metadata, mem_id))
        fixed += 1

    if not dry_run:
        conn.commit()
        print(f"Fixed: {fixed}, Cleared: {skipped}")
    else:
        print(f"Would fix: {fixed}, Would clear: {skipped}")
        print("(Run without --dry-run to apply)")

    conn.close()


if __name__ == "__main__":
    main()
