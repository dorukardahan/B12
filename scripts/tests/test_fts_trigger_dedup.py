"""Guard for audit #20 — the duplicate FTS5 trigger sets.

A DB upgraded across versions could carry BOTH the legacy upstream
mcp-memory-service triggers (fts_insert/update/softdel/hardel) AND B12's own
memory_fts_* triggers, all writing the SAME memory_fts table. Every write then
double-indexed (BM25 term-frequency inflation + redundant writes).

_ensure_schema must (a) keep B12's soft-delete-aware set and (b) drop the legacy
set so an existing dual-trigger DB self-heals on the next start — while a fresh
install still ends up with exactly the B12 set and no legacy triggers.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import b12_mcp_server as srv  # noqa: E402

LEGACY = {
    "fts_insert": """
        CREATE TRIGGER fts_insert AFTER INSERT ON memories BEGIN
            INSERT INTO memory_fts(rowid, content, tags)
            VALUES (new.id, new.content, COALESCE(new.tags, ''));
        END""",
    "fts_update": """
        CREATE TRIGGER fts_update AFTER UPDATE ON memories BEGIN
            INSERT INTO memory_fts(memory_fts, rowid, content, tags)
            VALUES('delete', old.id, old.content, COALESCE(old.tags, ''));
            INSERT INTO memory_fts(rowid, content, tags)
            VALUES (new.id, new.content, COALESCE(new.tags, ''));
        END""",
    "fts_softdel": """
        CREATE TRIGGER fts_softdel AFTER UPDATE ON memories
        WHEN new.deleted_at IS NOT NULL BEGIN
            INSERT INTO memory_fts(memory_fts, rowid, content, tags)
            VALUES('delete', old.id, old.content, COALESCE(old.tags, ''));
        END""",
    "fts_hardel": """
        CREATE TRIGGER fts_hardel AFTER DELETE ON memories BEGIN
            INSERT INTO memory_fts(memory_fts, rowid, content, tags)
            VALUES('delete', old.id, old.content, COALESCE(old.tags, ''));
        END""",
}
LEGACY_NAMES = set(LEGACY)
B12_NAMES = {
    "memory_fts_insert", "memory_fts_delete", "memory_fts_update",
    "memory_fts_softdel", "memory_fts_restore",
}


def _triggers(db):
    return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}


def _add_legacy(db):
    for sql in LEGACY.values():
        db.execute(sql)
    db.commit()


def test_fresh_install_has_b12_set_and_no_legacy(tmp_path):
    db = sqlite3.connect(str(tmp_path / "fresh.db"))
    srv._ensure_schema(db)
    trigs = _triggers(db)
    assert B12_NAMES <= trigs, f"B12 trigger set incomplete on fresh install: {trigs}"
    assert not (LEGACY_NAMES & trigs), f"fresh install must not create legacy triggers: {trigs}"


def test_legacy_dropped_b12_kept_on_reensure(tmp_path):
    db = sqlite3.connect(str(tmp_path / "dual.db"))
    srv._ensure_schema(db)          # fresh: B12 set, no legacy
    _add_legacy(db)                 # simulate an upgraded DB that acquired the legacy set
    assert LEGACY_NAMES <= _triggers(db), "fixture failed to install legacy triggers"
    srv._ensure_schema(db)          # re-ensure should clean it up
    trigs = _triggers(db)
    assert not (LEGACY_NAMES & trigs), f"legacy triggers survived re-ensure: {trigs}"
    assert B12_NAMES <= trigs, f"B12 trigger set lost during dedup: {trigs}"


def test_partial_b12_set_is_healed(tmp_path):
    """A DB with only a PARTIAL B12 set (e.g. memory_fts_insert but no update/softdel)
    plus the legacy set must end up with the FULL B12 set and no legacy triggers —
    the former `if memory_fts_insert not in ...` sentinel would have skipped creation
    and left the set incomplete after dropping legacy (GPT-5.5 review of audit #20)."""
    db = sqlite3.connect(str(tmp_path / "partial.db"))
    srv._ensure_schema(db)
    # Tear the B12 set down to just the insert trigger, then re-add legacy.
    for t in ("memory_fts_delete", "memory_fts_update", "memory_fts_softdel"):
        db.execute(f"DROP TRIGGER IF EXISTS {t}")
    _add_legacy(db)
    db.commit()
    srv._ensure_schema(db)
    trigs = _triggers(db)
    assert B12_NAMES <= trigs, f"partial B12 set was not healed: {trigs}"
    assert not (LEGACY_NAMES & trigs), f"legacy survived alongside healed B12 set: {trigs}"


def test_search_works_after_dedup(tmp_path):
    """Functional smoke: after dedup, an active row is searchable and a row that is
    soft-deleted AT INSERT stays out of the index. (Double-indexing itself is NOT
    observable in the FTS index — FTS5 collapses duplicate-rowid inserts — so the
    real guard against #20 is the structural legacy-dropped assertion above; this
    just confirms the surviving B12 triggers behave correctly.)"""
    db = sqlite3.connect(str(tmp_path / "idx.db"))
    srv._ensure_schema(db)
    _add_legacy(db)
    srv._ensure_schema(db)          # legacy gone; only B12's soft-delete-aware set fires
    db.execute("INSERT INTO memories(content, content_hash) VALUES ('alpha beta gamma', 'h1')")
    db.execute("INSERT INTO memories(content, content_hash, deleted_at) VALUES ('alpha hidden', 'h2', 123.0)")
    db.commit()
    rows = {r[0] for r in db.execute(
        "SELECT m.content_hash FROM memory_fts f JOIN memories m ON m.id=f.rowid "
        "WHERE memory_fts MATCH 'alpha'")}
    assert "h1" in rows, "active row not searchable after dedup"
    assert "h2" not in rows, "soft-deleted-at-insert row leaked into the index (legacy fts_insert not dropped?)"


def test_restore_soft_deleted_does_not_corrupt_index(tmp_path):
    """Restoring a soft-deleted row (deleted_at NOT NULL → NULL) must NOT issue an
    FTS5 'delete' for the already-absent row — that corrupts external-content FTS5
    ('database disk image is malformed'). Reachable via B12's undelete path.
    (Codex GitHub App review of audit #20.)"""
    db = sqlite3.connect(str(tmp_path / "restore.db"))
    srv._ensure_schema(db)
    db.execute("INSERT INTO memories(content, content_hash) VALUES ('alpha beta gamma', 'h1')")
    db.commit()
    rid = db.execute("SELECT id FROM memories WHERE content_hash='h1'").fetchone()[0]
    db.execute("UPDATE memories SET deleted_at=123.0 WHERE id=?", (rid,))   # soft-delete
    db.commit()
    db.execute("UPDATE memories SET deleted_at=NULL WHERE id=?", (rid,))     # restore
    db.commit()
    # 'integrity-check' raises sqlite3.DatabaseError if the index is corrupt. ALL THREE
    # external-content FTS indexes shared the same restore bug, so verify each.
    for t in ("memory_fts", "memory_fts_stemmed", "memory_content_fts"):
        db.execute(f"INSERT INTO {t}({t}) VALUES('integrity-check')")
    rows = {r[0] for r in db.execute(
        "SELECT m.content_hash FROM memory_fts f JOIN memories m ON m.id=f.rowid "
        "WHERE memory_fts MATCH 'alpha'")}
    assert rows == {"h1"}, f"restored row not searchable exactly once: {rows}"


def test_stale_update_trigger_is_migrated(tmp_path):
    """A DB carrying the OLD memory_fts_update (WHEN tests only new.deleted_at, no
    restore trigger) must be migrated to the guarded trio so restores stop corrupting
    the index."""
    db = sqlite3.connect(str(tmp_path / "stale.db"))
    srv._ensure_schema(db)
    # Downgrade to the buggy update trigger + remove the restore trigger.
    db.execute("DROP TRIGGER memory_fts_update")
    db.execute("DROP TRIGGER IF EXISTS memory_fts_restore")
    db.execute("""
        CREATE TRIGGER memory_fts_update AFTER UPDATE ON memories
        WHEN new.deleted_at IS NULL BEGIN
            INSERT INTO memory_fts(memory_fts, rowid, content, tags)
            VALUES('delete', old.id, old.content, COALESCE(old.tags, ''));
            INSERT INTO memory_fts(rowid, content, tags)
            VALUES (new.id, new.content, COALESCE(new.tags, ''));
        END
    """)
    db.commit()
    assert "memory_fts_restore" not in _triggers(db)
    srv._ensure_schema(db)               # should detect the stale guard + migrate
    trigs = _triggers(db)
    assert "memory_fts_restore" in trigs, "stale DB was not migrated to add the restore trigger"
    upd = db.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' AND name='memory_fts_update'"
    ).fetchone()[0]
    assert "old.deleted_at IS NULL" in upd, "memory_fts_update was not migrated to the guarded form"
