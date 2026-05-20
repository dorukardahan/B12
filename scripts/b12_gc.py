#!/usr/bin/env python3
"""B12 garbage collector — hard-delete soft-deleted rows past TTL + VACUUM.

Soft-deleted rows (`deleted_at IS NOT NULL`) accumulate over time. They
stay queryable via the export path but block FTS triggers (the conditional
`WHEN new.deleted_at IS NULL`) and waste disk + embedding rows.

Idempotent. Opt-in only — `install.sh --gc-cron` mirrors `--smoke-cron`.
"""

import argparse
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime


def get_db_path() -> str:
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/mcp-memory/sqlite_vec.db")
    if os.path.isdir(os.path.expanduser("~/AppData")):
        return os.path.expanduser("~/AppData/Local/mcp-memory/sqlite_vec.db")
    return os.path.expanduser("~/.local/share/mcp-memory/sqlite_vec.db")


def get_log_path() -> str:
    base = os.environ.get("B12_DATA_DIR", os.path.expanduser("~/.B12"))
    state = os.path.join(base, "state")
    os.makedirs(state, exist_ok=True)
    return os.path.join(state, f"gc-{datetime.now().strftime('%Y%m%d')}.log")


def setup_logger(log_path: str) -> logging.Logger:
    logger = logging.getLogger("b12_gc")
    logger.setLevel(logging.INFO)
    handler = logging.FileHandler(log_path)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.addHandler(logging.StreamHandler(sys.stdout))
    return logger


def _load_sqlite_vec(conn: sqlite3.Connection, logger: logging.Logger) -> bool:
    """Load sqlite-vec on this connection. Required before any SQL touches
    memory_embeddings (vec0 virtual table). Codex PR #51 P1: without this,
    the GC errored 'no such module: vec0' on the embeddings DELETE."""
    try:
        import sqlite_vec  # type: ignore
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        return True
    except ImportError:
        logger.warning("sqlite_vec not importable — embeddings cleanup skipped")
        return False
    except sqlite3.OperationalError as exc:
        logger.warning("sqlite_vec load failed (%s) — embeddings cleanup skipped", exc)
        return False


def _swap_fts_delete_triggers(conn: sqlite3.Connection):
    """Codex PR #51 P1: the AFTER DELETE FTS5 triggers use the external-
    content `INSERT INTO ... VALUES('delete', ...)` form. For rows that
    were already soft-deleted, fts_softdel removed the FTS row when
    deleted_at was set; hard-deleting then asks FTS5 to remove a rowid
    that no longer exists → 'database disk image is malformed'.

    Capture trigger DDL + DROP the two unsafe ones; caller recreates
    after the batch. Other AFTER DELETE triggers (fts_hardel,
    memories_fts_ad) use straight `DELETE FROM ...` which is idempotent."""
    rows = conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='trigger' "
        "AND tbl_name='memories' AND name IN "
        "('memory_fts_delete', 'memory_fts_stemmed_delete')"
    ).fetchall()
    for name, _ in rows:
        conn.execute(f"DROP TRIGGER IF EXISTS {name}")
    return rows


def collect(db_path: str, age_days: int, dry_run: bool, vacuum: bool,
            logger: logging.Logger) -> dict:
    """Returns dict: {found, deleted_memories, deleted_embeddings, vacuumed}."""
    if not os.path.exists(db_path):
        logger.warning("DB missing at %s — nothing to GC", db_path)
        return {"found": 0, "deleted_memories": 0, "deleted_embeddings": 0, "vacuumed": False}

    cutoff_ts = time.time() - (age_days * 86400)
    conn = sqlite3.connect(db_path)
    # Codex PR #51 round 2 P3: WAL mode change persists -wal/-shm side
    # files even in dry-run mode. Defer to the mutating branch only.
    conn.execute("PRAGMA busy_timeout=30000")
    vec_loaded = _load_sqlite_vec(conn, logger)
    try:
        rows = conn.execute(
            "SELECT id FROM memories WHERE deleted_at IS NOT NULL AND deleted_at < ?",
            (cutoff_ts,),
        ).fetchall()
        ids = [r[0] for r in rows]
        logger.info("Found %d soft-deleted rows older than %d days", len(ids), age_days)
        if dry_run or not ids:
            return {"found": len(ids), "deleted_memories": 0, "deleted_embeddings": 0, "vacuumed": False}

        # Codex PR #51 round 2 P2: if vec0 is unloadable but memory_embeddings
        # is a vec0 virtual table with rows for these ids, hard-deleting
        # memories would orphan them — a later GC run can never find them
        # via the SELECT above because the source `memories` rows are gone.
        # Refuse to proceed unless either (a) vec0 loaded successfully or
        # (b) memory_embeddings doesn't actually exist on this DB.
        if not vec_loaded:
            has_vec_table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='memory_embeddings'"
            ).fetchone() is not None
            if has_vec_table:
                logger.error(
                    "memory_embeddings present but sqlite_vec missing — "
                    "refusing to hard-delete memories rows that would "
                    "orphan vec0 entries. Install sqlite-vec (via the "
                    "B12 venv) and re-run."
                )
                return {"found": len(ids), "deleted_memories": 0,
                        "deleted_embeddings": 0, "vacuumed": False}

        # Only switch to WAL once we're actually going to mutate the DB.
        conn.execute("PRAGMA journal_mode=WAL")

        # Codex PR #51 round 3 P2: chunk DELETEs to stay under SQLite's
        # SQLITE_LIMIT_VARIABLE_NUMBER (999 on older builds, 32766 on
        # newer). 500 is a conservative ceiling that works on every
        # SQLite shipped with macOS / Linux distros + Python stdlib.
        # Without chunking, a 1000+-row backlog raised
        # `too many SQL variables` and aborted the GC.
        _CHUNK = 500
        emb_deleted = 0
        saved_triggers = _swap_fts_delete_triggers(conn)
        try:
            if vec_loaded:
                for i in range(0, len(ids), _CHUNK):
                    batch = ids[i:i + _CHUNK]
                    placeholders = ",".join("?" * len(batch))
                    cur = conn.execute(
                        f"DELETE FROM memory_embeddings WHERE rowid IN ({placeholders})",
                        batch,
                    )
                    emb_deleted += cur.rowcount or 0
            mem_deleted = 0
            for i in range(0, len(ids), _CHUNK):
                batch = ids[i:i + _CHUNK]
                placeholders = ",".join("?" * len(batch))
                cur = conn.execute(
                    f"DELETE FROM memories WHERE id IN ({placeholders})", batch
                )
                mem_deleted += cur.rowcount or 0
        finally:
            for _, sql in saved_triggers:
                if sql:
                    conn.execute(sql.replace(
                        "CREATE TRIGGER", "CREATE TRIGGER IF NOT EXISTS", 1
                    ))
        conn.commit()
        logger.info("Hard-deleted %d memory rows + %d embedding rows", mem_deleted, emb_deleted)

        vacuumed = False
        if vacuum:
            try:
                conn.execute("VACUUM")
                vacuumed = True
                logger.info("VACUUM complete — disk reclaimed")
            except sqlite3.OperationalError as exc:
                logger.warning("VACUUM skipped: %s", exc)

        return {"found": len(ids), "deleted_memories": mem_deleted,
                "deleted_embeddings": emb_deleted, "vacuumed": vacuumed}
    finally:
        conn.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="B12 GC: hard-delete soft-deleted rows + VACUUM")
    parser.add_argument("--age-days", type=int, default=90,
                        help="Age threshold for soft-deleted rows (default 90)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what would be deleted; do not modify the DB")
    parser.add_argument("--no-vacuum", action="store_true",
                        help="Skip the VACUUM pass (faster, no disk reclaim)")
    parser.add_argument("--db-path", default=None, help="Override DB path")
    args = parser.parse_args(argv)

    log_path = get_log_path()
    logger = setup_logger(log_path)
    db_path = args.db_path or get_db_path()
    logger.info("Starting GC: db=%s age_days=%d dry_run=%s vacuum=%s",
                db_path, args.age_days, args.dry_run, not args.no_vacuum)
    try:
        result = collect(db_path, args.age_days, args.dry_run, not args.no_vacuum, logger)
        logger.info("Done: %s", result)
        return 0
    except Exception as exc:  # noqa: BLE001 — top-level safety net for cron
        logger.exception("GC failed: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
