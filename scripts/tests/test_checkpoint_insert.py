"""Regression guard for the checkpoint-hook P0 (2026-06-27 audit #1/#21/#17/M2).

The mid-session checkpoint hook (`hooks/memory-checkpoint.sh`) stored NOTHING for
40+ days: its INSERT omitted `content_hash`, but the live DB schema is
`content_hash TEXT UNIQUE NOT NULL` (from upstream mcp-memory-service), so every
insert raised IntegrityError and was swallowed as a dedup drop. It also wrote
`datetime('now')` (TEXT) into REAL created_at/updated_at columns (NULLs the
`datetime(created_at,'unixepoch')` recency score) and derived the project tag
from `$PWD` instead of the session `.cwd`.

The fix also avoids `INSERT OR IGNORE` (which suppresses NOT NULL/CHECK/trigger
failures too, re-creating the hide-error-as-dedup bug) in favour of the targeted
`ON CONFLICT(content_hash) DO NOTHING` (3-model review blocker). These tests
extract the hook's actual INSERT and run it, so they track the source, not a
hand-copied SQL string.
"""
from __future__ import annotations

import datetime as _dt
import re
import sqlite3
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOK = REPO_ROOT / "hooks" / "memory-checkpoint.sh"


def _hook_src() -> str:
    return HOOK.read_text()


def _hook_insert_sql() -> str:
    """Extract the real INSERT statement (through ON CONFLICT) from the hook."""
    m = re.search(r"INSERT INTO memories.*?DO NOTHING", _hook_src(), re.DOTALL)
    assert m, "checkpoint INSERT (INSERT INTO ... ON CONFLICT) not found in hook"
    return m.group(0)


def _schema(check_memory_type: bool = False) -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    mt = (
        "memory_type TEXT CHECK(memory_type != 'BAD')"
        if check_memory_type
        else "memory_type TEXT DEFAULT 'general'"
    )
    db.execute(
        f"""CREATE TABLE memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT NOT NULL,
            content_hash TEXT UNIQUE NOT NULL,
            {mt},
            tags TEXT DEFAULT '', metadata TEXT DEFAULT '{{}}',
            created_at REAL, updated_at REAL,
            created_at_iso TEXT, updated_at_iso TEXT,
            deleted_at REAL DEFAULT NULL, strength REAL DEFAULT 1.0)"""
    )
    return db


def _params(content_hash: str = "h1", memory_type: str = "decision") -> tuple:
    now = int(time.time())
    iso = _dt.datetime.fromtimestamp(now, _dt.timezone.utc).isoformat()
    # Mirrors the hook's bind order: hash, content, type, metadata, tags,
    # (strength literal 1.0), created_at, created_at_iso, updated_at, updated_at_iso.
    return (content_hash, "[decision] use WAL", memory_type, "{}", "proj:B12,checkpoint",
            now, iso, now, iso)


# ── Source guards ────────────────────────────────────────────────

def test_insert_supplies_content_hash_and_type():
    cols = re.search(r"INSERT INTO memories\s*\((.*?)\)\s*VALUES", _hook_src(), re.DOTALL)
    assert cols, "checkpoint INSERT column list not found"
    for required in ("content_hash", "content", "memory_type", "created_at", "created_at_iso"):
        assert required in cols.group(1), f"checkpoint INSERT missing column: {required}"


def test_uses_targeted_on_conflict_not_or_ignore():
    src = _hook_src()
    # The blocker: OR IGNORE swallows NOT NULL/CHECK too. Must use targeted ON CONFLICT.
    assert "ON CONFLICT(content_hash) DO NOTHING" in src, "checkpoint INSERT must use targeted ON CONFLICT"
    assert "INSERT OR IGNORE INTO memories" not in src, "OR IGNORE reintroduced (swallows real constraint errors as dedup)"


def test_insert_uses_numeric_timestamps_not_datetime_now():
    insert = _hook_insert_sql()
    assert "datetime('now')" not in insert, "INSERT reintroduced datetime('now') TEXT into REAL columns (#21)"


def test_project_name_from_stdin_cwd():
    assert re.search(r"CWD=\$\(echo \"\$INPUT\" \| jq -r '\.cwd", _hook_src()), "project tag not parsed from stdin .cwd (#17)"


def test_constraint_error_surfaced_and_buffer_retained():
    src = _hook_src()
    assert "dropped_constraint" in src, "constraint errors still mislabeled (no dropped_constraint counter)"
    assert "_q5_flush_ok" in src, "buffer not retained on DB error (no _q5_flush_ok gate, M2)"
    assert "flush_giveup" in src, "no circuit-breaker for permanent DB errors (audit review #3/#7)"
    # The constraint message must actually reach the telemetry log, not just the count.
    assert re.search(r"error=_q5_constraint_err", src), "constraint message captured but not logged (review #1)"


# ── Behavioral guards (run the hook's real INSERT) ───────────────

def test_corrected_insert_satisfies_not_null_schema():
    """The P0: the hook's INSERT now stores against the NOT NULL content_hash schema."""
    db = _schema()
    sql = _hook_insert_sql()
    assert db.execute(sql, _params()).rowcount == 1, "insert failed against NOT NULL content_hash schema (the P0)"
    assert db.execute(sql, _params()).rowcount == 0, "duplicate content_hash must be a no-op (dedup)"
    assert db.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1
    rec = db.execute("SELECT datetime(created_at, 'unixepoch') FROM memories").fetchone()[0]
    assert rec is not None, "created_at non-numeric -> unixepoch NULL -> dead-last recency (#21)"


def test_on_conflict_surfaces_real_constraint_violations():
    """The review blocker: a non-dedup constraint failure must RAISE, not be
    silently swallowed as a rowcount-0 dedup (which INSERT OR IGNORE would do)."""
    db = _schema(check_memory_type=True)
    sql = _hook_insert_sql()
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(sql, _params(memory_type="BAD"))
    # And a clean row still inserts on the same schema.
    assert db.execute(sql, _params(memory_type="decision")).rowcount == 1
