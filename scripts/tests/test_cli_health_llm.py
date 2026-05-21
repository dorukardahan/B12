import importlib
import json
import sqlite3
import sys
import types
from argparse import Namespace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import b12_cli
import b12_health
import b12_health_report
import b12_importance
import b12_llm_extractor
import embed_daemon
import hook_adapter
import memory_refine
import migrate_fsrs
import shared_patterns
import transcript_adapter


def init_cli_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT NOT NULL,
            metadata TEXT,
            tags TEXT,
            memory_type TEXT,
            content_hash TEXT UNIQUE NOT NULL,
            created_at REAL,
            updated_at REAL,
            created_at_iso TEXT,
            updated_at_iso TEXT,
            strength REAL,
            deleted_at REAL
        )
        """
    )
    conn.commit()
    conn.close()


def test_cli_export_project_filter_matches_exact_tags(tmp_path, monkeypatch):
    db_path = tmp_path / "memory.sqlite"
    output_path = tmp_path / "export.json"
    init_cli_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO memories (content, metadata, tags, memory_type, content_hash, deleted_at) "
        "VALUES ('alpha', '{}', 'proj:demo', 'fact', 'a', NULL)"
    )
    conn.execute(
        "INSERT INTO memories (content, metadata, tags, memory_type, content_hash, deleted_at) "
        "VALUES ('alpha2', '{}', 'proj:demo2', 'fact', 'b', NULL)"
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(b12_cli, "DB_PATH", str(db_path))

    b12_cli.cmd_export(Namespace(project="demo", output=str(output_path)))

    exported = json.loads(output_path.read_text())
    assert [item["content"] for item in exported] == ["alpha"]


def test_cli_export_project_filter_preserves_spaces_inside_project_tags(tmp_path, monkeypatch):
    db_path = tmp_path / "memory.sqlite"
    output_path = tmp_path / "export.json"
    init_cli_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO memories (content, metadata, tags, memory_type, content_hash, deleted_at) "
        "VALUES ('space project', '{}', 'proj:demo app, type:fact', 'fact', 'a', NULL)"
    )
    conn.execute(
        "INSERT INTO memories (content, metadata, tags, memory_type, content_hash, deleted_at) "
        "VALUES ('collapsed project', '{}', 'proj:demoapp', 'fact', 'b', NULL)"
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(b12_cli, "DB_PATH", str(db_path))

    b12_cli.cmd_export(Namespace(project="demo app", output=str(output_path)))

    exported = json.loads(output_path.read_text())
    assert [item["content"] for item in exported] == ["space project"]


def test_cli_search_fallback_uses_unaliased_exact_project_filter(tmp_path, monkeypatch, capsys):
    db_path = tmp_path / "memory.sqlite"
    init_cli_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO memories (content, metadata, tags, memory_type, content_hash, created_at, updated_at, deleted_at) "
        "VALUES ('needle alpha', '{}', 'proj:demo', 'fact', 'a', 1, 1, NULL)"
    )
    conn.execute(
        "INSERT INTO memories (content, metadata, tags, memory_type, content_hash, created_at, updated_at, deleted_at) "
        "VALUES ('needle sibling', '{}', 'proj:demo2', 'fact', 'b', 1, 1, NULL)"
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(b12_cli, "DB_PATH", str(db_path))

    b12_cli.cmd_search(Namespace(query="needle", project="demo", limit=10))

    out = capsys.readouterr().out
    assert "needle alpha" in out
    assert "needle sibling" not in out


def test_cli_search_negative_limit_falls_back_to_default_cap(tmp_path, monkeypatch, capsys):
    db_path = tmp_path / "memory.sqlite"
    init_cli_db(db_path)
    conn = sqlite3.connect(db_path)
    for idx in range(15):
        conn.execute(
            "INSERT INTO memories (content, metadata, tags, memory_type, content_hash, created_at, updated_at, deleted_at) "
            "VALUES (?, '{}', 'proj:demo', 'fact', ?, ?, ?, NULL)",
            (f"needle {idx}", f"h{idx}", idx, idx),
        )
    conn.commit()
    conn.close()
    monkeypatch.setattr(b12_cli, "DB_PATH", str(db_path))

    b12_cli.cmd_search(Namespace(query="needle", project="demo", limit=-1))

    out = capsys.readouterr().out
    assert "10 memories found" in out
    assert "needle 14" in out
    assert "needle 4" not in out


def test_cli_store_writes_canonical_hash_and_epoch_timestamps(tmp_path, monkeypatch):
    db_path = tmp_path / "memory.sqlite"
    init_cli_db(db_path)
    monkeypatch.setattr(b12_cli, "DB_PATH", str(db_path))

    args = Namespace(
        content="remember the canonical contract",
        type="decision",
        importance=1.5,
        project="demo",
        tags=None,
    )
    b12_cli.cmd_store(args)
    b12_cli.cmd_store(args)

    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT content, content_hash, typeof(created_at), created_at_iso "
            "FROM memories WHERE deleted_at IS NULL"
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 1
    assert rows[0][0].startswith("[Decision]")
    assert rows[0][1] == b12_cli.content_hash(rows[0][0])
    assert rows[0][2] in {"real", "integer"}
    assert "T" in rows[0][3]


def test_cli_import_scrubs_content_and_preserves_timestamp_contract(tmp_path, monkeypatch):
    db_path = tmp_path / "memory.sqlite"
    import_path = tmp_path / "import.json"
    init_cli_db(db_path)
    import_path.write_text(json.dumps([{
        "content": "api_key=plaintext_secret_value_here_long_enough",
        "metadata": "{}",
        "tags": "proj:demo",
        "memory_type": "fact",
        "created_at": 123.0,
        "updated_at": 456.0,
        "created_at_iso": "2026-01-01T00:00:00+00:00",
        "updated_at_iso": "2026-01-02T00:00:00+00:00",
        "strength": 2.25,
    }]))
    monkeypatch.setattr(b12_cli, "DB_PATH", str(db_path))

    b12_cli.cmd_import(Namespace(file=str(import_path)))

    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT content, created_at, updated_at, created_at_iso, updated_at_iso, strength FROM memories"
        ).fetchone()
    finally:
        conn.close()
    assert "plaintext_secret_value_here_long_enough" not in row[0]
    assert row[1:] == (123.0, 456.0, "2026-01-01T00:00:00+00:00", "2026-01-02T00:00:00+00:00", 2.25)


def test_cli_import_scrubs_metadata_tags_and_rebuilds_metadata_hash(tmp_path, monkeypatch):
    db_path = tmp_path / "memory.sqlite"
    import_path = tmp_path / "import.json"
    init_cli_db(db_path)
    import_path.write_text(json.dumps([{
        "content": "api_key=plaintext_secret_value_here_long_enough",
        "metadata": {
            "note": "bearer plaintext_secret_value_here_long_enough",
            "content_hash": "stale",
        },
        "tags": "proj:demo,api_key=plaintext_secret_value_here_long_enough",
        "memory_type": "fact",
    }]))
    monkeypatch.setattr(b12_cli, "DB_PATH", str(db_path))

    b12_cli.cmd_import(Namespace(file=str(import_path)))

    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT content_hash, metadata, tags FROM memories").fetchone()
    finally:
        conn.close()
    metadata = json.loads(row[1])
    assert "plaintext_secret_value_here_long_enough" not in row[1]
    assert "plaintext_secret_value_here_long_enough" not in row[2]
    assert metadata["content_hash"] == row[0]


def test_llm_extraction_scrubs_transcript_and_candidate_content(monkeypatch, tmp_path):
    seen = {}
    secret_text = "api_key=plaintext_secret_value_here_long_enough"

    class Provider:
        name = "stub"
        default_model = "stub-model"

        def extract(self, prompt, transcript_text, **kwargs):
            seen["transcript"] = transcript_text
            return [{
                "type": "fact",
                "content": f"stored {secret_text}",
                "importance": 1.0,
                "reason": f"because {secret_text}",
            }]

    monkeypatch.setitem(
        sys.modules,
        "b12_llm_providers",
        types.SimpleNamespace(get_provider=lambda provider=None: Provider()),
    )
    monkeypatch.setitem(
        sys.modules,
        "b12_llm_prompts",
        types.SimpleNamespace(
            SYSTEM_PROMPT="prompt",
            normalize_transcript=lambda path, cap_chars: f"transcript {secret_text}",
            validate_extraction=lambda line: json.loads(line),
        ),
    )

    captured = {}

    def capture_candidates(candidates, **kwargs):
        captured["candidates"] = candidates
        return len(candidates)

    monkeypatch.setattr(b12_llm_extractor, "_write_candidates", capture_candidates)

    written = b12_llm_extractor.extract_and_store(
        transcript_path=str(tmp_path / "transcript.jsonl"),
        session_id="session-1234567890",
        project_name="demo",
        setup_context="personal",
        source_event="session_end",
    )

    assert written == 1
    assert secret_text not in seen["transcript"]
    assert secret_text not in captured["candidates"][0]["content"]
    assert secret_text not in captured["candidates"][0]["reason"]


def test_llm_write_candidates_returns_zero_when_commit_fails(monkeypatch):
    class FakeConn:
        rolled_back = False

        def commit(self):
            raise sqlite3.OperationalError("disk full")

        def rollback(self):
            self.rolled_back = True

        def close(self):
            pass

    conn = FakeConn()
    monkeypatch.setattr(b12_llm_extractor, "_open_db", lambda: conn)
    monkeypatch.setattr(b12_llm_extractor, "_encode_embedding", lambda content: b"embedding")
    monkeypatch.setattr(b12_llm_extractor, "_same_session_exists", lambda *args: False)
    monkeypatch.setitem(
        sys.modules,
        "shared_patterns",
        types.SimpleNamespace(content_hash=lambda content: "hash", DB_PATH="/tmp/memory.sqlite"),
    )
    monkeypatch.setitem(
        sys.modules,
        "write_time_merge",
        types.SimpleNamespace(
            merge_or_insert=lambda *args, **kwargs: types.SimpleNamespace(action="inserted"),
        ),
    )
    monkeypatch.setitem(sys.modules, "b12_importance", types.SimpleNamespace(score=lambda content: 0.5))

    written = b12_llm_extractor._write_candidates(
        [{"type": "fact", "content": "keep this", "importance": 0.8, "reason": "useful"}],
        session_short="sess",
        project_name="demo",
        setup_context="codex",
        provider_name="stub",
        model_name="stub",
    )

    assert written == 0
    assert conn.rolled_back is True


def test_health_uses_hook_dir_for_hook_code_and_data_dir_for_state(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    hook_dir = tmp_path / "hooks"
    with monkeypatch.context() as env:
        env.setenv("B12_DATA_DIR", str(data_dir))
        env.setenv("B12_HOOK_DIR", str(hook_dir))
        reloaded = importlib.reload(b12_health)
        assert reloaded._B12_DIR == data_dir
        assert reloaded._HOOK_DIR == hook_dir
        assert reloaded._SCRIPT_DIR == hook_dir / "scripts"

    importlib.reload(b12_health)


def test_mcp_host_probe_validates_configured_server_path(tmp_path, monkeypatch):
    home = tmp_path / "home"
    codex_dir = home / ".codex"
    codex_dir.mkdir(parents=True)
    missing = tmp_path / "missing" / "b12_mcp_server.py"
    (codex_dir / "config.toml").write_text(
        "[mcp_servers.B12]\n"
        'command = "python3"\n'
        f'args = ["{missing}"]\n'
    )
    script_dir = tmp_path / "scripts"
    script_dir.mkdir()
    (script_dir / "b12_mcp_server.py").write_text("# current server exists\n")
    monkeypatch.setattr(b12_health, "_HOME", home)
    monkeypatch.setattr(b12_health, "_SCRIPT_DIR", script_dir)

    result = b12_health.check_mcp_hosts()

    assert result.status == b12_health.Status.FAIL
    assert "configured server missing" in result.detail


def test_continue_parser_skips_system_messages(tmp_path):
    session_path = tmp_path / ".continue" / "sessions" / "session.json"
    session_path.parent.mkdir(parents=True)
    session_path.write_text(json.dumps({
        "sessionId": "s1",
        "history": [
            {"role": "system", "message": {"content": "do not ingest"}},
            {"role": "user", "message": {"content": "remember this"}},
        ],
    }))

    info, messages = transcript_adapter.parse(str(session_path))

    assert info.platform == "continue"
    assert [(msg.role, msg.content) for msg in messages] == [("user", "remember this")]


def test_codex_apply_patch_file_extraction_without_prior_assistant_message(tmp_path):
    session_path = tmp_path / "rollout.jsonl"
    patch = "\n".join([
        "*** Begin Patch",
        "*** Update File: scripts/demo.py",
        "*** Move to: scripts/renamed.py",
        "+pass",
        "*** Delete File: scripts/old.py",
        "*** End Patch",
    ])
    session_path.write_text(
        "\n".join([
            json.dumps({"type": "session_meta", "payload": {"id": "s1", "cwd": str(tmp_path)}}),
            json.dumps({
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "call_id": "c1",
                    "name": "apply_patch",
                    "input": patch,
                },
            }),
        ])
    )

    _info, messages = transcript_adapter.parse(str(session_path))

    assert transcript_adapter.extract_files_modified(messages) == {
        "scripts/demo.py",
        "scripts/renamed.py",
        "scripts/old.py",
    }


def test_embedding_count_reports_unknown_when_vec_table_cannot_load(monkeypatch):
    class _Cursor:
        def __init__(self, value):
            self.value = value

        def fetchone(self):
            return self.value

    class _Conn:
        def execute(self, sql):
            if "sqlite_master" in sql:
                return _Cursor((1,))
            raise sqlite3.OperationalError("no such module: vec0")

    monkeypatch.setattr(
        shared_patterns,
        "try_load_sqlite_vec",
        lambda _conn: (False, "sqlite_vec is not importable"),
    )

    embedded, warning = shared_patterns.count_active_embeddings(_Conn())

    assert embedded is None
    assert "unavailable" in warning


def test_cli_status_reports_unknown_embedding_coverage(tmp_path, monkeypatch, capsys):
    db_path = tmp_path / "memory.sqlite"
    init_cli_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO memories (content, metadata, tags, memory_type, content_hash, deleted_at, created_at) "
        "VALUES ('alpha', '{}', 'proj:demo', 'fact', 'a', NULL, 1)"
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(b12_cli, "DB_PATH", str(db_path))
    monkeypatch.setattr(
        b12_cli,
        "count_active_embeddings",
        lambda _conn: (None, "embedding coverage unavailable: sqlite_vec is not importable"),
    )

    b12_cli.cmd_status(Namespace())

    output = capsys.readouterr().out
    assert "Embeddings: unknown" in output
    assert "0%" not in output


def test_memory_refine_scrubs_candidates_before_return(monkeypatch):
    secret = "api_key=plaintext_secret_value_here_long_enough"
    monkeypatch.setattr(memory_refine, "daemon_request", lambda *args, **kwargs: None)

    refined = memory_refine.refine_candidates([
        {
            "content": f"store this {secret}",
            "memory_type": "fact",
            "tags": f"proj:demo,{secret}",
        }
    ])

    rendered = json.dumps(refined)
    assert "plaintext_secret_value_here_long_enough" not in rendered
    assert "[REDACTED:" in rendered


def _init_hook_memory_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE memories (
            id INTEGER PRIMARY KEY,
            content TEXT NOT NULL,
            tags TEXT,
            deleted_at REAL
        );
        CREATE VIRTUAL TABLE memory_content_fts USING fts5(
            content,
            content='memories',
            content_rowid='id',
            tokenize='trigram'
        );
        """
    )
    conn.execute("INSERT INTO memories VALUES (1, 'demo auth memory', 'proj:demo', NULL)")
    conn.execute("INSERT INTO memories VALUES (2, 'other auth memory', 'proj:other', NULL)")
    conn.execute("INSERT INTO memory_content_fts(rowid, content) VALUES (1, 'demo auth memory')")
    conn.execute("INSERT INTO memory_content_fts(rowid, content) VALUES (2, 'other auth memory')")
    conn.commit()
    conn.close()


def test_hook_adapter_filters_semantic_hits_to_current_project(tmp_path, monkeypatch):
    db_path = tmp_path / "memory.sqlite"
    _init_hook_memory_db(db_path)
    adapter = hook_adapter.HookAdapter(platform="codex", cwd=str(tmp_path / "demo"))
    adapter.db_path = str(db_path)
    monkeypatch.setattr(
        adapter,
        "_daemon_request",
        lambda *args, **kwargs: {
            "ok": True,
            "results": [
                {"id": 2, "display": "other auth memory"},
                {"id": 1, "display": "demo auth memory"},
            ],
        },
    )

    output = adapter.on_user_prompt("auth")

    assert "demo auth memory" in output
    assert "other auth memory" not in output


def test_hook_adapter_fts_fallback_filters_to_current_project(tmp_path, monkeypatch):
    db_path = tmp_path / "memory.sqlite"
    _init_hook_memory_db(db_path)
    adapter = hook_adapter.HookAdapter(platform="codex", cwd=str(tmp_path / "demo"))
    adapter.db_path = str(db_path)
    monkeypatch.setattr(adapter, "_daemon_request", lambda *args, **kwargs: None)

    output = adapter.on_user_prompt("auth")

    assert "demo auth memory" in output
    assert "other auth memory" not in output


def test_turkish_remember_token_does_not_overmatch_english_negative():
    ordinary = b12_importance.score_with_breakdown("not allowed in production")
    memorable = b12_importance.score_with_breakdown("lütfen not al: production flag stays off")

    assert ordinary.remember_hit is False
    assert memorable.remember_hit is True


def test_fsrs_migration_missing_database_returns_failure(tmp_path):
    missing = tmp_path / "missing.sqlite"

    assert migrate_fsrs.migrate(str(missing)) is False
    assert migrate_fsrs.main([str(missing)]) == 1


def test_fsrs_migration_clamps_zero_strength_instead_of_defaulting(tmp_path, monkeypatch):
    db_path = tmp_path / "memory.sqlite"
    init_cli_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("ALTER TABLE memories ADD COLUMN valid_until TEXT")
    conn.execute(
        """
        INSERT INTO memories
        (content, metadata, tags, memory_type, content_hash, created_at, updated_at,
         created_at_iso, updated_at_iso, strength, deleted_at)
        VALUES ('weak', '{}', '', 'fact', 'weak', 1, 1, '', '', 0.0, NULL)
        """
    )
    conn.commit()
    conn.close()

    assert migrate_fsrs.migrate(str(db_path)) is True

    conn = sqlite3.connect(db_path)
    try:
        due = conn.execute("SELECT due_date FROM memories WHERE content_hash = 'weak'").fetchone()[0]
    finally:
        conn.close()
    due_dt = b12_health_report._parse_ts(due)
    assert due_dt is not None
    delta_days = (due_dt - b12_health_report.datetime.now(b12_health_report.timezone.utc)).total_seconds() / 86400
    assert 0.45 <= delta_days <= 0.55


def test_health_report_counts_same_day_iso_valid_until_as_dormant(tmp_path):
    db_path = tmp_path / "memory.sqlite"
    init_cli_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("ALTER TABLE memories ADD COLUMN valid_until TEXT")
    conn.execute("ALTER TABLE memories ADD COLUMN last_accessed_at REAL")
    conn.execute(
        """
        INSERT INTO memories
        (content, metadata, tags, memory_type, content_hash, created_at, updated_at,
         created_at_iso, updated_at_iso, strength, deleted_at, valid_until)
        VALUES
          ('expired', '{}', '', 'fact', 'expired', 1, 1, '', '', 1.0, NULL, '2026-05-21T00:00:00+00:00'),
          ('active', '{}', '', 'fact', 'active', 1, 1, '', '', 1.0, NULL, '2026-05-21T23:59:59+00:00')
        """
    )
    conn.commit()

    lifecycle = b12_health_report._section_lifecycle(
        conn,
        now=b12_health_report.datetime(2026, 5, 21, 10, 0, tzinfo=b12_health_report.timezone.utc),
    )

    conn.close()
    assert lifecycle["dormant"] == 1


def test_pyproject_exposes_b12_console_script():
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover
        pytest.skip("tomllib unavailable")

    data = tomllib.loads((Path(__file__).resolve().parents[2] / "pyproject.toml").read_text())
    assert data["project"]["scripts"]["b12"] == "b12_cli:main"
    assert "b12_cli" in data["tool"]["setuptools"]["py-modules"]
