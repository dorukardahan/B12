#!/usr/bin/env python3
"""
SQLite migration for mcp-memory-service:

Adds Ebbinghaus forgetting-curve fields + a bi-temporal validity marker to the
existing `memories` table.

Target DB (default):
  ~/Library/Application Support/mcp-memory/sqlite_vec.db

What gets added (idempotent):
  1) strength         REAL DEFAULT 1.0
  2) last_accessed_at REAL DEFAULT NULL
  3) valid_until      TEXT DEFAULT NULL   (ISO timestamp, set when superseded)

Backfill:
  - For existing rows with NULL last_accessed_at, set it to created_at.

No external dependencies beyond the Python stdlib.
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from pathlib import Path
from typing import Dict, List, Set, Tuple


_home = Path.home()
if sys.platform == "darwin":
    DEFAULT_DB_PATH = _home / "Library" / "Application Support" / "mcp-memory" / "sqlite_vec.db"
elif sys.platform == "win32":
    DEFAULT_DB_PATH = _home / "AppData" / "Local" / "mcp-memory" / "sqlite_vec.db"
else:
    DEFAULT_DB_PATH = _home / ".local" / "share" / "mcp-memory" / "sqlite_vec.db"
DEFAULT_TABLE = "memories"

# Column name -> SQLite column definition used in ALTER TABLE ... ADD COLUMN.
NEW_COLUMNS: Dict[str, str] = {
    "strength": "REAL DEFAULT 1.0",
    "last_accessed_at": "REAL DEFAULT NULL",
    "valid_until": "TEXT DEFAULT NULL",
}

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _assert_safe_identifier(name: str) -> str:
    """
    Guardrail for f-string SQL identifiers.
    We only use this for table/column names that we control.
    """
    if not _IDENT_RE.match(name):
        raise ValueError(f"Unsafe SQL identifier: {name!r}")
    return name


def _table_exists(con: sqlite3.Connection, table: str) -> bool:
    cur = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table,),
    )
    return cur.fetchone() is not None


def _get_columns(con: sqlite3.Connection, table: str) -> Set[str]:
    # PRAGMA does not support parameterized table names.
    table = _assert_safe_identifier(table)
    cur = con.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in cur.fetchall()}  # row[1] = column name


def migrate(db_path: Path, table: str = DEFAULT_TABLE) -> Tuple[List[str], int]:
    """
    Apply the migration.

    Returns:
      (added_columns, backfilled_last_accessed_rows)
    """
    db_path = db_path.expanduser()
    table = _assert_safe_identifier(table)

    if not db_path.exists():
        raise FileNotFoundError(f"SQLite database not found: {db_path}")

    con = sqlite3.connect(str(db_path))
    try:
        con.execute("PRAGMA foreign_keys = ON")

        if not _table_exists(con, table):
            raise RuntimeError(f"Table {table!r} does not exist in {db_path}")

        added: List[str] = []

        with con:  # commit if successful, rollback on exception
            existing = _get_columns(con, table)

            for col_name, col_def in NEW_COLUMNS.items():
                col_name = _assert_safe_identifier(col_name)
                if col_name in existing:
                    continue
                con.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_def}")
                added.append(col_name)

            # Refresh schema view after ALTER TABLE.
            existing = _get_columns(con, table)

            if "last_accessed_at" not in existing:
                raise RuntimeError("Expected column last_accessed_at to exist after migration")
            if "created_at" not in existing:
                raise RuntimeError("Expected column created_at to exist for backfill step")

            before = con.total_changes
            con.execute(
                f"""
                UPDATE {table}
                   SET last_accessed_at = created_at
                 WHERE last_accessed_at IS NULL
                   AND created_at IS NOT NULL
                """
            )
            backfilled = con.total_changes - before

        return added, backfilled
    finally:
        con.close()


def _self_test() -> None:
    """
    Smoke test:
      - creates a temporary DB
      - runs migration twice (idempotency)
      - checks backfill occurred
    """
    import tempfile
    import time

    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "test.db"
        con = sqlite3.connect(str(db_path))
        try:
            con.execute(
                """
                CREATE TABLE memories (
                  id INTEGER PRIMARY KEY,
                  created_at REAL
                )
                """
            )
            created = time.time()
            con.execute("INSERT INTO memories (created_at) VALUES (?)", (created,))
            con.commit()
        finally:
            con.close()

        added1, backfilled1 = migrate(db_path, table="memories")
        assert set(added1) == set(NEW_COLUMNS.keys()), (added1, NEW_COLUMNS)
        assert backfilled1 == 1, backfilled1

        # Second run should be a no-op.
        added2, backfilled2 = migrate(db_path, table="memories")
        assert added2 == [], added2
        assert backfilled2 == 0, backfilled2

        con2 = sqlite3.connect(str(db_path))
        try:
            row = con2.execute(
                "SELECT strength, last_accessed_at, valid_until FROM memories LIMIT 1"
            ).fetchone()
            assert row is not None
            strength, last_accessed, valid_until = row
            assert abs(float(strength) - 1.0) < 1e-9
            assert abs(float(last_accessed) - created) < 1e-6
            assert valid_until is None
        finally:
            con2.close()

    print("Self-test passed.")


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Add Ebbinghaus + bi-temporal columns to the mcp-memory SQLite DB."
    )
    parser.add_argument(
        "--db-path",
        default=str(DEFAULT_DB_PATH),
        help="Path to sqlite_vec.db (default: %(default)s)",
    )
    parser.add_argument(
        "--table",
        default=DEFAULT_TABLE,
        help="Target table name (default: %(default)s)",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Run an isolated self-test on a temporary database.",
    )
    args = parser.parse_args(argv)

    if args.self_test:
        _self_test()
        return 0

    db_path = Path(args.db_path).expanduser()

    try:
        added, backfilled = migrate(db_path, table=args.table)
    except Exception as e:
        print(f"Migration failed: {e}", file=sys.stderr)
        return 1

    print("Migration summary:")
    print(f"  Database: {db_path}")
    print(f"  Table:    {args.table}")
    if added:
        print(f"  Added columns: {', '.join(added)}")
    else:
        print("  Added columns: (none; already present)")
    print(f"  Backfilled last_accessed_at from created_at: {backfilled} row(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
