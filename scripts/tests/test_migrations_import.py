import gzip
import importlib
import json
import sqlite3
import struct
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import export_import
import contradiction_resolver
import consolidation_engine
import migrate_embed_to_bge_m3
import migrate_stemmed_fts
import migrate_v10_13
import migrate_v12_3_contra_prune
from b12_ingest_queue import IngestQueue, _write_ack_atomic


@pytest.fixture(autouse=True)
def _clear_fake_b12_mcp_server_import():
    yield
    sys.modules.pop("b12_mcp_server", None)


def _load_b12_mcp_server_with_fake_mcp(monkeypatch):
    class FakeFastMCP:
        def __init__(self, *args, **kwargs):
            pass

        def tool(self, *args, **kwargs):
            return lambda fn: fn

        def resource(self, *args, **kwargs):
            return lambda fn: fn

        def run(self, *args, **kwargs):
            pass

    monkeypatch.setitem(sys.modules, "mcp", types.ModuleType("mcp"))
    monkeypatch.setitem(sys.modules, "mcp.server", types.ModuleType("mcp.server"))
    monkeypatch.setitem(
        sys.modules,
        "mcp.server.fastmcp",
        types.SimpleNamespace(FastMCP=FakeFastMCP),
    )
    sys.modules.pop("b12_mcp_server", None)
    return importlib.import_module("b12_mcp_server")


def indexed_docs(conn, table_name):
    vocab_name = f"{table_name}_vocab"
    conn.execute(f"DROP TABLE IF EXISTS {vocab_name}")
    conn.execute(
        f"CREATE VIRTUAL TABLE {vocab_name} "
        f"USING fts5vocab({table_name}, 'instance')"
    )
    try:
        return {
            row[0]
            for row in conn.execute(
                f"SELECT DISTINCT doc FROM {vocab_name}"
            ).fetchall()
        }
    finally:
        conn.execute(f"DROP TABLE IF EXISTS {vocab_name}")


def test_bge_migration_candidates_include_null_memory_type():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE memories (
            id INTEGER PRIMARY KEY,
            content TEXT,
            tags TEXT,
            memory_type TEXT,
            deleted_at REAL,
            valid_until TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO memories VALUES (1, 'keep me', 'proj:demo', NULL, NULL, NULL)"
    )

    candidates = migrate_embed_to_bge_m3._candidate_memories(conn)

    assert candidates == [(1, "keep me")]


def test_bge_migration_does_not_skip_partial_matching_dimension(tmp_path, monkeypatch):
    db_path = tmp_path / "memory.sqlite"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE memories (
            id INTEGER PRIMARY KEY,
            content TEXT,
            tags TEXT,
            memory_type TEXT,
            deleted_at REAL,
            valid_until TEXT
        );
        CREATE TABLE memory_embeddings (
            rowid INTEGER PRIMARY KEY,
            content_embedding BLOB
        );
        """
    )
    conn.execute("INSERT INTO memories VALUES (1, 'one', 'proj:demo', 'fact', NULL, NULL)")
    conn.execute("INSERT INTO memories VALUES (2, 'two', 'proj:demo', 'fact', NULL, NULL)")
    conn.execute(
        "INSERT INTO memory_embeddings VALUES (1, ?)",
        (struct.pack("1024f", *([0.1] * 1024)),),
    )
    conn.commit()
    conn.close()

    class StubModel:
        pass

    monkeypatch.setattr(migrate_embed_to_bge_m3, "_open", lambda path: sqlite3.connect(path))
    monkeypatch.setattr(migrate_embed_to_bge_m3, "_load_model", lambda name, backend: StubModel())
    monkeypatch.setattr(migrate_embed_to_bge_m3, "_encode", lambda model, texts, backend: [[0.2] * 1024 for _ in texts])
    monkeypatch.setattr(migrate_embed_to_bge_m3, "_backup", lambda path: str(tmp_path / "backup.sqlite"))

    result = migrate_embed_to_bge_m3.migrate(str(db_path))

    assert result.get("skipped") != "dim_match"
    conn = sqlite3.connect(db_path)
    try:
        rowids = {row[0] for row in conn.execute("SELECT rowid FROM memory_embeddings")}
    finally:
        conn.close()
    assert rowids == {1, 2}
    assert result["backfilled_missing"] == 1


def test_bge_migration_restores_backup_after_destructive_failure(tmp_path, monkeypatch):
    db_path = tmp_path / "memory.sqlite"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE memories (
            id INTEGER PRIMARY KEY,
            content TEXT,
            tags TEXT,
            memory_type TEXT,
            deleted_at REAL,
            valid_until TEXT
        );
        CREATE TABLE memory_embeddings (
            rowid INTEGER PRIMARY KEY,
            content_embedding BLOB
        );
        """
    )
    conn.execute("INSERT INTO memories VALUES (1, 'one', 'proj:demo', 'fact', NULL, NULL)")
    conn.execute("INSERT INTO memory_embeddings VALUES (1, ?)", (struct.pack("384f", *([0.1] * 384)),))
    conn.commit()
    conn.close()

    class StubModel:
        pass

    monkeypatch.setattr(migrate_embed_to_bge_m3, "_open", lambda path: sqlite3.connect(path))
    monkeypatch.setattr(migrate_embed_to_bge_m3, "_load_model", lambda name, backend: StubModel())

    def failing_encode(model, texts, backend):
        if texts == ["dim_probe"]:
            return [[0.2] * 1024]
        raise RuntimeError("encode failed")

    monkeypatch.setattr(migrate_embed_to_bge_m3, "_encode", failing_encode)
    monkeypatch.setattr(migrate_embed_to_bge_m3, "_backup", lambda path: str(tmp_path / "backup.sqlite"))
    # Create the backup that the rollback path expects.
    import shutil
    shutil.copy2(db_path, tmp_path / "backup.sqlite")

    with pytest.raises(RuntimeError, match="live DB restored"):
        migrate_embed_to_bge_m3.migrate(str(db_path))

    conn = sqlite3.connect(db_path)
    try:
        dim = len(conn.execute("SELECT content_embedding FROM memory_embeddings").fetchone()[0]) // 4
    finally:
        conn.close()
    assert dim == 384


def test_native_fts_migration_reconciles_partial_existing_index(tmp_path):
    db_path = tmp_path / "memory.sqlite"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE memories (
            id INTEGER PRIMARY KEY,
            content TEXT NOT NULL,
            deleted_at REAL
        );
        CREATE VIRTUAL TABLE memory_content_fts USING fts5(
            content,
            content='memories',
            content_rowid='id',
            tokenize='trigram'
        );
        CREATE TRIGGER memories_fts_ai AFTER INSERT ON memories BEGIN
            INSERT INTO memory_content_fts(rowid, content) VALUES (new.id, new.content);
        END;
        CREATE TRIGGER memories_fts_au AFTER UPDATE ON memories BEGIN
            DELETE FROM memory_content_fts WHERE rowid = old.id;
            INSERT INTO memory_content_fts(rowid, content) VALUES (new.id, new.content);
        END;
        CREATE TRIGGER memories_fts_ad AFTER DELETE ON memories BEGIN
            DELETE FROM memory_content_fts WHERE rowid = old.id;
        END;
        """
    )
    conn.execute("INSERT INTO memories VALUES (1, 'one', NULL)")
    conn.execute("INSERT INTO memories VALUES (2, 'two', NULL)")
    conn.execute("INSERT INTO memory_content_fts(memory_content_fts) VALUES('delete-all')")
    conn.execute("INSERT INTO memory_content_fts(rowid, content) VALUES (1, 'one')")
    conn.commit()
    conn.close()

    assert migrate_v10_13.migrate(str(db_path)) is True
    assert migrate_v10_13.migrate(str(db_path), check_only=True) is True

    conn = sqlite3.connect(db_path)
    try:
        rowids = indexed_docs(conn, "memory_content_fts")
    finally:
        conn.close()
    assert rowids == {1, 2}


def test_native_fts_check_only_fails_partial_existing_index(tmp_path):
    db_path = tmp_path / "memory.sqlite"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE memories (id INTEGER PRIMARY KEY, content TEXT NOT NULL, deleted_at REAL);
        CREATE VIRTUAL TABLE memory_content_fts USING fts5(
            content,
            content='memories',
            content_rowid='id',
            tokenize='trigram'
        );
        CREATE TRIGGER memories_fts_ai AFTER INSERT ON memories BEGIN
            INSERT INTO memory_content_fts(rowid, content) VALUES (new.id, new.content);
        END;
        CREATE TRIGGER memories_fts_au AFTER UPDATE ON memories BEGIN
            DELETE FROM memory_content_fts WHERE rowid = old.id;
            INSERT INTO memory_content_fts(rowid, content) VALUES (new.id, new.content);
        END;
        CREATE TRIGGER memories_fts_ad AFTER DELETE ON memories BEGIN
            DELETE FROM memory_content_fts WHERE rowid = old.id;
        END;
        """
    )
    conn.execute("INSERT INTO memories VALUES (1, 'one', NULL)")
    conn.execute("INSERT INTO memories VALUES (2, 'two', NULL)")
    conn.execute("INSERT INTO memory_content_fts(memory_content_fts) VALUES('delete-all')")
    conn.execute("INSERT INTO memory_content_fts(rowid, content) VALUES (1, 'one')")
    conn.commit()
    conn.close()

    assert migrate_v10_13.migrate(str(db_path), check_only=True) is False


def test_native_fts_check_accepts_short_trigram_content(tmp_path):
    db_path = tmp_path / "memory.sqlite"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE memories (id INTEGER PRIMARY KEY, content TEXT NOT NULL, deleted_at REAL);
        CREATE VIRTUAL TABLE memory_content_fts USING fts5(
            content,
            content='memories',
            content_rowid='id',
            tokenize='trigram'
        );
        CREATE TRIGGER memories_fts_ai AFTER INSERT ON memories BEGIN
            INSERT INTO memory_content_fts(rowid, content) VALUES (new.id, new.content);
        END;
        CREATE TRIGGER memories_fts_au AFTER UPDATE ON memories BEGIN
            INSERT INTO memory_content_fts(memory_content_fts, rowid, content)
            SELECT 'delete', old.id, old.content WHERE old.deleted_at IS NULL;
            INSERT INTO memory_content_fts(rowid, content) VALUES (new.id, new.content);
        END;
        CREATE TRIGGER memories_fts_ad AFTER DELETE ON memories BEGIN
            INSERT INTO memory_content_fts(memory_content_fts, rowid, content)
            SELECT 'delete', old.id, old.content WHERE old.deleted_at IS NULL;
        END;
        """
    )
    conn.execute("INSERT INTO memories VALUES (1, 'hi', NULL)")
    conn.commit()
    conn.close()

    assert migrate_v10_13.migrate(str(db_path), check_only=True) is True


def test_native_fts_triggers_guard_deleted_rows(tmp_path):
    db_path = tmp_path / "memory.sqlite"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE memories (id INTEGER PRIMARY KEY, content TEXT NOT NULL, deleted_at REAL)")
    conn.execute("INSERT INTO memories VALUES (1, 'deleted token', 1)")
    conn.commit()
    conn.close()

    assert migrate_v10_13.migrate(str(db_path)) is True

    conn = sqlite3.connect(db_path)
    try:
        trigger_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = 'memories_fts_au'"
        ).fetchone()[0]
        conn.execute("UPDATE memories SET content = 'still deleted token' WHERE id = 1")
        conn.commit()
    finally:
        conn.close()
    assert "WHERE old.deleted_at IS NULL" in trigger_sql


def test_bge_candidate_filter_excludes_same_day_expired_valid_until(tmp_path):
    db_path = tmp_path / "memory.sqlite"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE memories (
            id INTEGER PRIMARY KEY,
            content TEXT,
            deleted_at REAL,
            valid_until TEXT,
            memory_type TEXT,
            tags TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO memories VALUES (1, 'expired', NULL, '2000-01-01T23:59:59+00:00', 'fact', 'proj:demo')"
    )
    conn.execute(
        "INSERT INTO memories VALUES (2, 'active', NULL, '2999-01-01T00:00:00+00:00', 'fact', 'proj:demo')"
    )
    conn.commit()

    candidates = migrate_embed_to_bge_m3._candidate_memories(conn)

    conn.close()
    assert candidates == [(2, "active")]


def test_bge_backup_uses_consistent_sqlite_snapshot(tmp_path):
    db_path = tmp_path / "memory.sqlite"
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE memories (id INTEGER PRIMARY KEY, content TEXT)")
    conn.execute("INSERT INTO memories VALUES (1, 'snapshot')")
    conn.commit()
    conn.close()

    backup_path = migrate_embed_to_bge_m3._backup(str(db_path))

    backup = sqlite3.connect(backup_path)
    try:
        assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert backup.execute("SELECT content FROM memories WHERE id = 1").fetchone()[0] == "snapshot"
    finally:
        backup.close()


def test_native_fts_migration_recreates_stale_external_content_triggers(tmp_path):
    db_path = tmp_path / "memory.sqlite"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE memories (id INTEGER PRIMARY KEY, content TEXT NOT NULL, deleted_at REAL);
        CREATE VIRTUAL TABLE memory_content_fts USING fts5(
            content,
            content='memories',
            content_rowid='id',
            tokenize='trigram'
        );
        CREATE TRIGGER memories_fts_ai AFTER INSERT ON memories BEGIN
            INSERT INTO memory_content_fts(rowid, content) VALUES (new.id, new.content);
        END;
        CREATE TRIGGER memories_fts_au AFTER UPDATE ON memories BEGIN
            DELETE FROM memory_content_fts WHERE rowid = old.id;
            INSERT INTO memory_content_fts(rowid, content) VALUES (new.id, new.content);
        END;
        CREATE TRIGGER memories_fts_ad AFTER DELETE ON memories BEGIN
            DELETE FROM memory_content_fts WHERE rowid = old.id;
        END;
        """
    )
    conn.execute("INSERT INTO memories VALUES (1, 'redwood token', NULL)")
    conn.commit()
    conn.close()

    assert migrate_v10_13.migrate(str(db_path)) is True

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("UPDATE memories SET content = 'cedar token' WHERE id = 1")
        conn.commit()
        stale = conn.execute(
            "SELECT rowid FROM memory_content_fts WHERE memory_content_fts MATCH 'redwood'"
        ).fetchall()
        fresh = conn.execute(
            "SELECT rowid FROM memory_content_fts WHERE memory_content_fts MATCH 'cedar'"
        ).fetchall()
        conn.execute("DELETE FROM memories WHERE id = 1")
        conn.commit()
        deleted = conn.execute(
            "SELECT rowid FROM memory_content_fts WHERE memory_content_fts MATCH 'cedar'"
        ).fetchall()
    finally:
        conn.close()
    assert stale == []
    assert fresh == [(1,)]
    assert deleted == []


def test_native_fts_migration_rebuilds_same_rowid_stale_terms(tmp_path):
    db_path = tmp_path / "memory.sqlite"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE memories (id INTEGER PRIMARY KEY, content TEXT NOT NULL, deleted_at REAL);
        CREATE VIRTUAL TABLE memory_content_fts USING fts5(
            content,
            content='memories',
            content_rowid='id',
            tokenize='trigram'
        );
        CREATE TRIGGER memories_fts_ai AFTER INSERT ON memories BEGIN
            INSERT INTO memory_content_fts(rowid, content) VALUES (new.id, new.content);
        END;
        CREATE TRIGGER memories_fts_au AFTER UPDATE ON memories BEGIN
            INSERT INTO memory_content_fts(memory_content_fts, rowid, content)
            VALUES('delete', old.id, old.content);
            INSERT INTO memory_content_fts(rowid, content) VALUES (new.id, new.content);
        END;
        CREATE TRIGGER memories_fts_ad AFTER DELETE ON memories BEGIN
            INSERT INTO memory_content_fts(memory_content_fts, rowid, content)
            VALUES('delete', old.id, old.content);
        END;
        """
    )
    conn.execute("INSERT INTO memories VALUES (1, 'cedar token', NULL)")
    conn.execute("INSERT INTO memory_content_fts(memory_content_fts) VALUES('delete-all')")
    conn.execute("INSERT INTO memory_content_fts(rowid, content) VALUES (1, 'redwood token')")
    conn.commit()
    conn.close()

    assert migrate_v10_13.migrate(str(db_path)) is True

    conn = sqlite3.connect(db_path)
    try:
        stale = conn.execute(
            "SELECT rowid FROM memory_content_fts WHERE memory_content_fts MATCH 'redwood'"
        ).fetchall()
        fresh = conn.execute(
            "SELECT rowid FROM memory_content_fts WHERE memory_content_fts MATCH 'cedar'"
        ).fetchall()
    finally:
        conn.close()
    assert stale == []
    assert fresh == [(1,)]


def test_contradiction_prune_noops_when_graph_table_missing(tmp_path, capsys):
    db_path = tmp_path / "memory.sqlite"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE memories (id INTEGER PRIMARY KEY, content TEXT)")
    conn.commit()
    conn.close()

    assert migrate_v12_3_contra_prune.main(["migrate_v12_3_contra_prune.py", str(db_path)]) == 0
    assert "No memory_graph table" in capsys.readouterr().out


def test_mcp_schema_recreates_stale_native_fts_triggers(monkeypatch):
    b12_mcp_server = _load_b12_mcp_server_with_fake_mcp(monkeypatch)
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE memories (
            id INTEGER PRIMARY KEY,
            content TEXT NOT NULL,
            content_hash TEXT,
            tags TEXT,
            metadata TEXT,
            memory_type TEXT,
            created_at REAL,
            updated_at REAL,
            deleted_at REAL
        );
        CREATE VIRTUAL TABLE memory_content_fts USING fts5(
            content,
            content='memories',
            content_rowid='id',
            tokenize='trigram'
        );
        CREATE TRIGGER memories_fts_ai AFTER INSERT ON memories BEGIN
            INSERT INTO memory_content_fts(rowid, content) VALUES (new.id, new.content);
        END;
        CREATE TRIGGER memories_fts_au AFTER UPDATE ON memories BEGIN
            DELETE FROM memory_content_fts WHERE rowid = old.id;
            INSERT INTO memory_content_fts(rowid, content) VALUES (new.id, new.content);
        END;
        CREATE TRIGGER memories_fts_ad AFTER DELETE ON memories BEGIN
            DELETE FROM memory_content_fts WHERE rowid = old.id;
        END;
        """
    )

    b12_mcp_server._ensure_schema(conn)

    conn.execute("INSERT INTO memories (id, content, deleted_at) VALUES (1, 'redwood token', NULL)")
    conn.execute("UPDATE memories SET content = 'cedar token' WHERE id = 1")
    stale = conn.execute(
        "SELECT rowid FROM memory_content_fts WHERE memory_content_fts MATCH 'redwood'"
    ).fetchall()
    fresh = conn.execute(
        "SELECT rowid FROM memory_content_fts WHERE memory_content_fts MATCH 'cedar'"
    ).fetchall()
    assert stale == []
    assert fresh == [(1,)]


def test_ingest_queue_stale_ack_reset_allows_next_ack_to_advance(tmp_path):
    ingest_path = tmp_path / "ingest.jsonl"
    queue = IngestQueue(ingest_path=ingest_path)
    queue.enqueue({"content": "first", "content_hash": "one"})
    queue.enqueue({"content": "second", "content_hash": "two"})
    _write_ack_atomic(queue.ack_path, 999999)

    records = list(queue.drain())
    queue.ack(records[-1])

    assert [record.payload["content"] for record in records] == ["first", "second"]
    assert queue.read_ack_offset() == records[-1].end_offset


def test_stemmed_fts_migration_clears_stale_rows_when_no_active_memories(tmp_path):
    db_path = tmp_path / "memory.sqlite"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE memories (
            id INTEGER PRIMARY KEY,
            content TEXT,
            tags TEXT,
            deleted_at REAL
        );
        CREATE VIRTUAL TABLE memory_fts_stemmed USING fts5(
            content,
            tags,
            content='memories',
            content_rowid='id',
            tokenize='porter unicode61'
        );
        """
    )
    conn.execute("INSERT INTO memories VALUES (1, 'deleted', '', 1)")
    conn.execute(
        "INSERT INTO memory_fts_stemmed(rowid, content, tags) VALUES (1, 'deleted', '')"
    )
    conn.commit()
    conn.close()

    migrate_stemmed_fts.migrate(str(db_path))

    conn = sqlite3.connect(db_path)
    try:
        rowids = indexed_docs(conn, "memory_fts_stemmed")
    finally:
        conn.close()
    assert rowids == set()


def test_mcp_schema_supersedes_legacy_fts_triggers(monkeypatch):
    """Audit #20: B12's memory_fts_* set must SUPERSEDE the legacy upstream fts_*
    triggers, not coexist with them. The former behavior preserved the legacy set
    (and skipped creating B12's), but when a DB carried both they double-indexed
    memory_fts. _ensure_schema now creates B12's set and drops the legacy one."""
    b12_mcp_server = _load_b12_mcp_server_with_fake_mcp(monkeypatch)
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE memories (
            id INTEGER PRIMARY KEY,
            content TEXT NOT NULL,
            content_hash TEXT,
            tags TEXT,
            deleted_at REAL
        );
        CREATE VIRTUAL TABLE memory_fts USING fts5(
            content,
            tags,
            content='memories',
            content_rowid='id',
            tokenize='unicode61'
        );
        CREATE TRIGGER fts_insert AFTER INSERT ON memories BEGIN
            INSERT INTO memory_fts(rowid, content, tags) VALUES (new.id, new.content, COALESCE(new.tags, ''));
        END;
        """
    )

    b12_mcp_server._ensure_schema(conn)

    trigger_names = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall()
    }
    # Legacy upstream set is dropped — B12's set now owns memory_fts.
    assert "fts_insert" not in trigger_names
    assert not {"fts_update", "fts_softdel", "fts_hardel"} & trigger_names
    # B12's soft-delete-aware set is present.
    assert {
        "memory_fts_insert",
        "memory_fts_delete",
        "memory_fts_update",
        "memory_fts_softdel",
    } <= trigger_names


def test_mcp_session_tracker_resets_after_flush(monkeypatch):
    b12_mcp_server = _load_b12_mcp_server_with_fake_mcp(monkeypatch)
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE memories (
            id INTEGER PRIMARY KEY,
            content TEXT,
            content_hash TEXT UNIQUE,
            metadata TEXT,
            tags TEXT,
            memory_type TEXT,
            created_at REAL,
            updated_at REAL,
            strength REAL
        )
        """
    )
    b12_mcp_server._session_tracker.update({
        "search_queries": ["alpha"],
        "stored_count": 1,
        "tool_calls": 3,
        "start_time": 1,
        "project": "alpha",
    })

    b12_mcp_server._flush_session_tracker(conn)

    assert b12_mcp_server._session_tracker["search_queries"] == []
    assert b12_mcp_server._session_tracker["stored_count"] == 0
    assert b12_mcp_server._session_tracker["tool_calls"] == 0
    assert b12_mcp_server._session_tracker["project"] is None


def test_mcp_session_tracker_contexts_are_isolated(monkeypatch):
    b12_mcp_server = _load_b12_mcp_server_with_fake_mcp(monkeypatch)
    first = b12_mcp_server._new_session_tracker()
    second = b12_mcp_server._new_session_tracker()
    token1 = b12_mcp_server._session_tracker_var.set(first)
    try:
        b12_mcp_server._current_session_tracker()["search_queries"].append("alpha")
    finally:
        b12_mcp_server._session_tracker_var.reset(token1)
    token2 = b12_mcp_server._session_tracker_var.set(second)
    try:
        b12_mcp_server._current_session_tracker()["search_queries"].append("beta")
    finally:
        b12_mcp_server._session_tracker_var.reset(token2)

    assert first["search_queries"] == ["alpha"]
    assert second["search_queries"] == ["beta"]


def test_replace_import_validates_archive_before_soft_deleting_existing_rows(tmp_path, monkeypatch):
    db_path = tmp_path / "memory.sqlite"
    archive_path = tmp_path / "broken.b12"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE memories (
            id INTEGER PRIMARY KEY,
            content TEXT,
            content_hash TEXT,
            deleted_at REAL
        )
        """
    )
    conn.execute("INSERT INTO memories VALUES (1, 'keep', 'hash', NULL)")
    conn.commit()
    conn.close()
    with gzip.open(archive_path, "wt", encoding="utf-8") as handle:
        handle.write(json.dumps({"_type": "manifest", "schema": 1, "count": 1}) + "\n")
        handle.write("{broken json\n")
    monkeypatch.setattr(export_import, "_request_embedding_backfill", lambda *args, **kwargs: None)

    result = export_import.import_memories(
        db_path=str(db_path),
        input_path=str(archive_path),
        mode="replace",
    )

    conn = sqlite3.connect(db_path)
    try:
        deleted_at = conn.execute("SELECT deleted_at FROM memories WHERE id = 1").fetchone()[0]
    finally:
        conn.close()
    assert result.errors
    assert deleted_at is None


def test_archive_import_scrubs_content_and_remaps_graph_edges(tmp_path, monkeypatch):
    db_path = tmp_path / "memory.sqlite"
    archive_path = tmp_path / "archive.b12"
    raw_content = "api_key=plaintext_secret_value_here_long_enough"
    old_hash = export_import._content_hash(raw_content)
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE memories (
            id INTEGER PRIMARY KEY,
            content_hash TEXT UNIQUE,
            content TEXT,
            tags TEXT,
            memory_type TEXT,
            metadata TEXT,
            strength REAL,
            created_at REAL,
            created_at_iso TEXT,
            updated_at REAL,
            updated_at_iso TEXT,
            valid_until TEXT,
            last_accessed_at REAL,
            deleted_at REAL
        );
        CREATE TABLE memory_graph (
            source_hash TEXT,
            target_hash TEXT,
            similarity REAL,
            connection_types TEXT,
            relationship_type TEXT,
            created_at REAL,
            metadata TEXT,
            PRIMARY KEY (source_hash, target_hash)
        );
        """
    )
    conn.commit()
    conn.close()
    with gzip.open(archive_path, "wt", encoding="utf-8") as handle:
        handle.write(json.dumps({"_type": "manifest", "schema": 1, "count": 1}) + "\n")
        handle.write(json.dumps({
            "_type": "memory",
            "content": raw_content,
            "content_hash": old_hash,
            "tags": "proj:demo,api_key=plaintext_secret_value_here_long_enough",
            "memory_type": "fact",
            "metadata": {
                "content_hash": old_hash,
                "nested": {"token": "api_key=plaintext_secret_value_here_long_enough"},
            },
            "strength": 1.5,
        }) + "\n")
        handle.write(json.dumps({
            "_type": "edge",
            "source_hash": old_hash,
            "target_hash": old_hash,
            "similarity": 0.7,
            "connection_types": "related",
        }) + "\n")
    monkeypatch.setattr(export_import, "_request_embedding_backfill", lambda *args, **kwargs: None)

    result = export_import.import_memories(
        db_path=str(db_path),
        input_path=str(archive_path),
        mode="merge",
    )

    conn = sqlite3.connect(db_path)
    try:
        memory = conn.execute("SELECT content, content_hash, strength, tags, metadata FROM memories").fetchone()
        edge = conn.execute("SELECT source_hash, target_hash FROM memory_graph").fetchone()
    finally:
        conn.close()
    assert result.errors == []
    assert "plaintext_secret_value_here_long_enough" not in memory[0]
    assert "plaintext_secret_value_here_long_enough" not in memory[3]
    assert "plaintext_secret_value_here_long_enough" not in memory[4]
    assert memory[1] != old_hash
    assert json.loads(memory[4])["content_hash"] == memory[1]
    assert memory[2] == 1.5
    assert edge == (memory[1], memory[1])


_MERGE_SCHEMA = """
    CREATE TABLE memories (
        id INTEGER PRIMARY KEY,
        content TEXT,
        content_hash TEXT,
        updated_at REAL,
        updated_at_iso TEXT,
        deleted_at REAL,
        metadata TEXT
    );
    CREATE TABLE memory_embeddings (rowid INTEGER PRIMARY KEY, content_embedding BLOB);
    CREATE TABLE memory_graph (
        source_hash TEXT,
        target_hash TEXT,
        similarity REAL,
        connection_types TEXT,
        metadata TEXT,
        created_at REAL,
        relationship_type TEXT
    );
"""


def _seed_merge_pair(conn):
    conn.execute("INSERT INTO memories VALUES (1, 'one', 'h1', 0, '', NULL, '{}')")
    conn.execute("INSERT INTO memories VALUES (2, 'two', 'h2', 0, '', NULL, '{}')")
    conn.execute("INSERT INTO memory_embeddings VALUES (1, X'0001')")


def test_contradiction_merge_reembeds_merged_content(monkeypatch):
    # RET-1: merge_memories now RE-EMBEDS the merged text (it used to delete the
    # embedding without re-inserting, making the survivor invisible to semantic
    # search). With the daemon available it must replace the stale vector.
    monkeypatch.setattr(contradiction_resolver, "_encode_via_daemon", lambda text: b"\x09\x09\x09")
    conn = sqlite3.connect(":memory:")
    conn.executescript(_MERGE_SCHEMA)
    _seed_merge_pair(conn)

    contradiction_resolver.merge_memories(conn, 1, "one", "h1", 2, "two", "h2")

    row = conn.execute("SELECT content_embedding FROM memory_embeddings WHERE rowid = 1").fetchone()
    assert row is not None, "survivor lost its embedding (RET-1 regression)"
    assert row[0] == b"\x09\x09\x09", "embedding was not re-encoded from the merged text"


def test_contradiction_merge_drops_embedding_when_daemon_unavailable(monkeypatch):
    # When the daemon is down, re-embed yields nothing → the stale vector is
    # dropped (FTS-only; embedding_backfill.py restores it later). Deterministic
    # regardless of whether a real embed daemon happens to be running locally.
    monkeypatch.setattr(contradiction_resolver, "_encode_via_daemon", lambda text: None)
    conn = sqlite3.connect(":memory:")
    conn.executescript(_MERGE_SCHEMA)
    _seed_merge_pair(conn)

    contradiction_resolver.merge_memories(conn, 1, "one", "h1", 2, "two", "h2")

    remaining = conn.execute("SELECT COUNT(*) FROM memory_embeddings WHERE rowid = 1").fetchone()[0]
    assert remaining == 0


def test_consolidation_merge_clears_stale_embedding_when_daemon_unavailable(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE memories (
            id INTEGER PRIMARY KEY,
            content TEXT,
            content_hash TEXT,
            tags TEXT,
            metadata TEXT,
            strength REAL,
            updated_at REAL,
            updated_at_iso TEXT,
            deleted_at REAL
        );
        CREATE TABLE memory_embeddings (rowid INTEGER PRIMARY KEY, content_embedding BLOB);
        CREATE TABLE memory_graph (
            source_hash TEXT,
            target_hash TEXT,
            similarity REAL,
            connection_types TEXT,
            metadata TEXT,
            created_at REAL,
            relationship_type TEXT
        );
        """
    )
    conn.execute("INSERT INTO memories VALUES (1, 'one', 'h1', 'proj:demo', '{}', 1.0, 0, '', NULL)")
    conn.execute("INSERT INTO memories VALUES (2, 'two', 'h2', 'proj:demo', '{}', 1.0, 0, '', NULL)")
    conn.execute("INSERT INTO memory_embeddings VALUES (1, X'0001')")
    monkeypatch.setattr(consolidation_engine, "_daemon_alive", lambda: False)
    mem1 = consolidation_engine.MemoryRecord(1, "h1", "one", "proj:demo", "fact", "{}", 2.0, None)
    mem2 = consolidation_engine.MemoryRecord(2, "h2", "two", "proj:demo", "fact", "{}", 1.0, None)

    consolidation_engine._apply_merge(conn, [mem1, mem2])

    remaining = conn.execute("SELECT COUNT(*) FROM memory_embeddings WHERE rowid = 1").fetchone()[0]
    assert remaining == 0
