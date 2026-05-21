import os
import sqlite3
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import codex_session_end
from codex_session_end import store_memory
from hook_adapter import HookAdapter


def _init_memory_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT,
            metadata TEXT,
            tags TEXT,
            content_hash TEXT,
            memory_type TEXT,
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


def test_codex_store_memory_scrubs_content_before_insert(tmp_path):
    db_path = tmp_path / "memory.sqlite"
    _init_memory_db(db_path)

    memory_id = store_memory(
        str(db_path),
        "deployment note api_key=plaintext_secret_value_here_long_enough",
        "{}",
        "proj:demo,user:codex",
        memory_type="fact",
    )

    conn = sqlite3.connect(db_path)
    try:
        content = conn.execute(
            "SELECT content FROM memories WHERE id = ?",
            (memory_id,),
        ).fetchone()[0]
    finally:
        conn.close()
    assert "plaintext_secret_value_here_long_enough" not in content
    assert "[REDACTED:" in content


def test_hook_adapter_preserves_list_tags_as_complete_tags():
    adapter = HookAdapter(platform="codex", cwd=os.path.join(os.sep, "work", "demo"))
    result = adapter.on_pre_tool_use(
        "memory_store",
        {
            "content": "remember this",
            "metadata": {"tags": ["user:pref"]},
        },
    )

    tags = result["metadata"]["tags"]
    assert tags == ["user:pref", "proj:demo", "platform:codex"]
    assert "," not in tags
    assert "p" not in tags


def test_hook_adapter_string_tags_use_exact_project_membership():
    adapter = HookAdapter(platform="codex", cwd=os.path.join(os.sep, "work", "demo"))
    result = adapter.on_pre_tool_use(
        "memory_store",
        {
            "content": "remember this",
            "metadata": {"tags": "proj:demo2"},
        },
    )

    tags = result["metadata"]["tags"]
    tag_values = {tag.strip() for tag in tags.split(",")}
    assert "proj:demo2" in tag_values
    assert "proj:demo" in tag_values


def test_codex_rollout_missing_database_is_retryable(monkeypatch, tmp_path):
    session_id = "retry-session-123456"
    info = types.SimpleNamespace(
        session_id=session_id,
        cwd=str(tmp_path / "project"),
        timestamp="2026-05-21T00:00:00Z",
        cli_version="test",
        git_branch="main",
        git_repo_url="",
    )
    monkeypatch.setenv("B12_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(codex_session_end, "parse", lambda path: (info, ["a", "b", "c"]))
    monkeypatch.setattr(codex_session_end, "_is_imported_from_claude", lambda info: False)
    monkeypatch.setattr(codex_session_end, "extract_user_messages", lambda messages: ["do work"])
    monkeypatch.setattr(codex_session_end, "extract_assistant_texts", lambda messages: ["decided to keep tests"])
    monkeypatch.setattr(codex_session_end, "extract_files_modified", lambda messages: [])
    monkeypatch.setattr(codex_session_end, "get_db_path", lambda: str(tmp_path / "missing.sqlite"))

    result = codex_session_end.process_rollout(str(tmp_path / "rollout.jsonl"))

    assert result == {"status": "error", "reason": "database not found"}
    assert session_id not in codex_session_end.load_processed_sessions()


def test_codex_rollout_writes_scrubbed_summary_file_and_embedding_input(monkeypatch, tmp_path):
    db_path = tmp_path / "memory.sqlite"
    _init_memory_db(db_path)
    session_id = "scrub-session-123456"
    info = types.SimpleNamespace(
        session_id=session_id,
        cwd=str(tmp_path / "demo"),
        timestamp="2026-05-21T00:00:00Z",
        cli_version="test",
        git_branch="main",
        git_repo_url="",
    )
    secret_text = "api_key=plaintext_secret_value_here_long_enough"
    embedding_inputs = []
    monkeypatch.setenv("B12_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(codex_session_end, "parse", lambda path: (info, ["a", "b", "c"]))
    monkeypatch.setattr(codex_session_end, "_is_imported_from_claude", lambda info: False)
    monkeypatch.setattr(codex_session_end, "extract_user_messages", lambda messages: [f"please fix {secret_text}"])
    monkeypatch.setattr(
        codex_session_end,
        "extract_assistant_texts",
        lambda messages: [f"decided to keep the safer storage because {secret_text}"],
    )
    monkeypatch.setattr(codex_session_end, "extract_files_modified", lambda messages: [])
    monkeypatch.setattr(codex_session_end, "get_db_path", lambda: str(db_path))

    def fake_embedding(text):
        embedding_inputs.append(text)
        return None

    monkeypatch.setattr(codex_session_end, "get_embedding", fake_embedding)

    result = codex_session_end.process_rollout(str(tmp_path / "rollout.jsonl"))

    summary_path = tmp_path / "data" / "memory-summaries" / "demo-codex-latest.md"
    summary = summary_path.read_text()
    conn = sqlite3.connect(db_path)
    try:
        contents = "\n".join(row[0] for row in conn.execute("SELECT content FROM memories"))
    finally:
        conn.close()
    assert result["status"] == "ok"
    assert "plaintext_secret_value_here_long_enough" not in summary
    assert "plaintext_secret_value_here_long_enough" not in contents
    assert all("plaintext_secret_value_here_long_enough" not in item for item in embedding_inputs)
