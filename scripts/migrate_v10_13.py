#!/usr/bin/env python3
"""
B12 Migration: mcp-memory-service v10.13.0

One-time migration for existing databases after upgrading to v10.13.0.

v10.13.0 introduced native FTS5 via `memory_content_fts` table (trigram tokenizer),
but its init code skips table creation on existing databases (early return after
migration checks in sqlite_vec.py line 580-605). This script creates the table
manually so native hybrid search and BM25 features work.

B12's own `memory_fts` table (unicode61 tokenizer) is NOT affected — both tables
coexist. B12 hooks use `memory_fts`; the MCP server uses `memory_content_fts`.

Usage:
    python3 migrate_v10_13.py              # Run migration
    python3 migrate_v10_13.py --check      # Check status only
    python3 migrate_v10_13.py --db PATH    # Use custom DB path
"""

import os
import sys
import sqlite3
from pathlib import Path


def get_db_path():
    """Find the mcp-memory database."""
    # macOS
    mac_path = Path.home() / "Library" / "Application Support" / "mcp-memory" / "sqlite_vec.db"
    if mac_path.exists():
        return str(mac_path)

    # Windows
    win_path = Path.home() / "AppData" / "Local" / "mcp-memory" / "sqlite_vec.db"
    if win_path.exists():
        return str(win_path)

    # Linux / WSL
    linux_path = Path.home() / ".local" / "share" / "mcp-memory" / "sqlite_vec.db"
    if linux_path.exists():
        return str(linux_path)

    return None


def check_table_exists(conn, table_name):
    """Check if a table exists in the database."""
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?",
        (table_name,)
    )
    return cursor.fetchone() is not None


def check_trigger_exists(conn, trigger_name):
    """Check if a trigger exists in the database."""
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'trigger' AND name = ?",
        (trigger_name,)
    )
    return cursor.fetchone() is not None


def trigger_uses_external_content_delete(conn, trigger_name):
    """Check whether an FTS5 external-content trigger uses delete commands."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = ?",
        (trigger_name,),
    ).fetchone()
    sql = (row[0] if row else "") or ""
    return (
        "memory_content_fts, rowid, content" in sql
        and "'delete'" in sql
        and "WHERE old.deleted_at IS NULL" in sql
    )


def native_fts_matches_active_memories(conn) -> bool:
    tokenized_active_ids = {
        row[0] for row in conn.execute(
            """
            SELECT id FROM memories
            WHERE deleted_at IS NULL
              AND content IS NOT NULL
              AND length(content) >= 3
            """
        ).fetchall()
    }
    return tokenized_active_ids == indexed_native_fts_docs(conn)


def indexed_native_fts_docs(conn) -> set[int]:
    vocab_table = "_b12_v10_13_memory_content_fts_vocab"
    conn.execute(f"DROP TABLE IF EXISTS {vocab_table}")
    conn.execute(
        f"CREATE VIRTUAL TABLE {vocab_table} "
        "USING fts5vocab(memory_content_fts, 'instance')"
    )
    try:
        return {
            row[0] for row in conn.execute(
                f"SELECT DISTINCT doc FROM {vocab_table}"
            ).fetchall()
        }
    finally:
        conn.execute(f"DROP TABLE IF EXISTS {vocab_table}")


def rebuild_native_fts(conn) -> int:
    conn.execute("INSERT INTO memory_content_fts(memory_content_fts) VALUES('delete-all')")
    conn.execute('''
        INSERT INTO memory_content_fts(rowid, content)
        SELECT id, content FROM memories WHERE deleted_at IS NULL
    ''')
    return conn.execute(
        "SELECT COUNT(*) FROM memories WHERE deleted_at IS NULL"
    ).fetchone()[0]


def migrate(db_path, check_only=False):
    """Run the v10.13.0 migration."""
    print(f"Database: {db_path}")

    conn = sqlite3.connect(db_path, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")

    # ── Status check ──────────────────────────────────────────
    has_memories = check_table_exists(conn, "memories")
    has_native_fts = check_table_exists(conn, "memory_content_fts")
    has_b12_fts = check_table_exists(conn, "memory_fts")

    mem_count = 0
    active_count = 0
    if has_memories:
        mem_count = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        active_count = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE deleted_at IS NULL"
        ).fetchone()[0]

    native_fts_count = 0
    if has_native_fts:
        native_fts_count = conn.execute("SELECT COUNT(*) FROM memory_content_fts").fetchone()[0]

    b12_fts_count = 0
    if has_b12_fts:
        b12_fts_count = conn.execute("SELECT COUNT(*) FROM memory_fts").fetchone()[0]

    # Check triggers
    has_ai = check_trigger_exists(conn, "memories_fts_ai")
    has_au = check_trigger_exists(conn, "memories_fts_au")
    has_ad = check_trigger_exists(conn, "memories_fts_ad")

    print(f"\nStatus:")
    print(f"  memories table:        {'YES' if has_memories else 'NO'} ({mem_count} total, {active_count} active)")
    print(f"  memory_content_fts:    {'YES' if has_native_fts else 'NO'} ({native_fts_count} indexed)")
    print(f"  memory_fts (B12):      {'YES' if has_b12_fts else 'NO'} ({b12_fts_count} indexed)")
    print(f"  Trigger INSERT (ai):   {'YES' if has_ai else 'NO'}")
    print(f"  Trigger UPDATE (au):   {'YES' if has_au else 'NO'}")
    print(f"  Trigger DELETE (ad):   {'YES' if has_ad else 'NO'}")

    if not has_memories:
        print("\nNo memories table found. Nothing to migrate.")
        conn.close()
        return True

    if check_only:
        needs = []
        if not has_native_fts:
            needs.append("Create memory_content_fts table")
        elif not native_fts_matches_active_memories(conn):
            needs.append(f"Reconcile {active_count} active memories into FTS index")
        if not has_ai:
            needs.append("Create INSERT trigger")
        if not has_au:
            needs.append("Create UPDATE trigger")
        elif not trigger_uses_external_content_delete(conn, "memories_fts_au"):
            needs.append("Recreate UPDATE trigger with FTS5 external-content delete")
        if not has_ad:
            needs.append("Create DELETE trigger")
        elif not trigger_uses_external_content_delete(conn, "memories_fts_ad"):
            needs.append("Recreate DELETE trigger with FTS5 external-content delete")
        if needs:
            print("\nNeeded:")
            for n in needs:
                print(f"  - {n}")
        conn.close()
        return len(needs) == 0

    # ── Migration ─────────────────────────────────────────────
    print("\nMigrating...")

    # 1. Create FTS5 table (matches v10.13.0 init code exactly)
    if not has_native_fts:
        conn.execute('''
            CREATE VIRTUAL TABLE IF NOT EXISTS memory_content_fts USING fts5(
                content,
                content='memories',
                content_rowid='id',
                tokenize='trigram'
            )
        ''')
        print("  [OK] Created memory_content_fts table (trigram tokenizer)")
        has_native_fts = True

    # 2. Create sync triggers. FTS5 external-content tables require the
    # special 'delete' command; plain DELETE leaves stale terms searchable.
    if has_au and not trigger_uses_external_content_delete(conn, "memories_fts_au"):
        conn.execute("DROP TRIGGER IF EXISTS memories_fts_au")
        has_au = False
    if has_ad and not trigger_uses_external_content_delete(conn, "memories_fts_ad"):
        conn.execute("DROP TRIGGER IF EXISTS memories_fts_ad")
        has_ad = False

    if not has_ai:
        conn.execute('''
            CREATE TRIGGER IF NOT EXISTS memories_fts_ai AFTER INSERT ON memories
            BEGIN
                INSERT INTO memory_content_fts(rowid, content)
                SELECT new.id, new.content WHERE new.deleted_at IS NULL;
            END;
        ''')
        print("  [OK] Created INSERT trigger (memories_fts_ai)")

    if not has_au:
        conn.execute('''
            CREATE TRIGGER IF NOT EXISTS memories_fts_au AFTER UPDATE ON memories
            BEGIN
                INSERT INTO memory_content_fts(memory_content_fts, rowid, content)
                SELECT 'delete', old.id, old.content WHERE old.deleted_at IS NULL;
                INSERT INTO memory_content_fts(rowid, content)
                SELECT new.id, new.content WHERE new.deleted_at IS NULL;
            END;
        ''')
        print("  [OK] Created UPDATE trigger (memories_fts_au)")

    if not has_ad:
        conn.execute('''
            CREATE TRIGGER IF NOT EXISTS memories_fts_ad AFTER DELETE ON memories
            BEGIN
                INSERT INTO memory_content_fts(memory_content_fts, rowid, content)
                SELECT 'delete', old.id, old.content WHERE old.deleted_at IS NULL;
            END;
        ''')
        print("  [OK] Created DELETE trigger (memories_fts_ad)")

    # 3. Backfill existing memories. Rowid parity is not enough to prove
    # token freshness, so rebuild even when the same rowids are present.
    if has_native_fts:
        backfilled = rebuild_native_fts(conn)
        print(f"  [OK] Rebuilt {backfilled} active memories into FTS index")

    conn.commit()

    # 4. Verify B12's memory_fts is still intact
    if has_b12_fts:
        b12_count_after = conn.execute("SELECT COUNT(*) FROM memory_fts").fetchone()[0]
        if b12_count_after == b12_fts_count:
            print(f"  [OK] B12 memory_fts intact ({b12_count_after} entries)")
        else:
            print(f"  [WARN] B12 memory_fts count changed: {b12_fts_count} -> {b12_count_after}")

    # Final verification
    final_fts = conn.execute("SELECT COUNT(*) FROM memory_content_fts").fetchone()[0]
    print(f"\nMigration complete. memory_content_fts: {final_fts} entries.")

    conn.close()
    return True


def main():
    check_only = "--check" in sys.argv

    # Custom DB path
    db_path = None
    for i, arg in enumerate(sys.argv[1:], 1):
        if arg == "--db" and i + 1 < len(sys.argv):
            db_path = sys.argv[i + 1]
            break

    if not db_path:
        db_path = get_db_path()

    if not db_path:
        print("Database not found. Specify with --db PATH or ensure mcp-memory-service has been run.")
        sys.exit(1)

    if not os.path.exists(db_path):
        print(f"Database file not found: {db_path}")
        sys.exit(1)

    print("=== B12 Migration: v10.13.0 (memory_content_fts) ===")
    print(f"Mode: {'CHECK' if check_only else 'MIGRATE'}")

    success = migrate(db_path, check_only=check_only)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
