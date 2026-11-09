"""memory-retrieval.sh does not search on host task notifications.

Claude Code sends <task-notification> as a prompt when a background task ends.
Searching its task ids and output paths injected unrelated memories on 18% of
all retrievals on one install.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

HOOKS = Path(__file__).resolve().parents[2] / "hooks"

NOTIFICATION = ("<task-notification>\n<task-id>b2dmwle8f</task-id>\n<tool-use-id>toolu_01Ct</tool-use-id>\n"
                "<output-file>/tmp/tasks/b2dmwle8f.output</output-file>\n<status>completed</status>\n"
                "<summary>Background command \"Wait for review\" completed (exit code 0)</summary>\n"
                "</task-notification>")


def _retrievals(tmp_path: Path, prompt: str) -> list[dict]:
    home = tmp_path / "home"
    data = home / ".B12"
    (data / "memory-logs").mkdir(parents=True)
    db = (home / "Library" / "Application Support" / "mcp-memory" / "sqlite_vec.db" if sys.platform == "darwin"
          else home / ".local" / "share" / "mcp-memory" / "sqlite_vec.db")
    db.parent.mkdir(parents=True)
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE memories (id INTEGER PRIMARY KEY, content TEXT, content_hash TEXT, tags TEXT,"
        " memory_type TEXT, metadata TEXT, created_at REAL, updated_at REAL, created_at_iso TEXT,"
        " updated_at_iso TEXT, deleted_at REAL, strength REAL, valid_until TEXT, last_accessed_at REAL);"
        " CREATE VIRTUAL TABLE memory_content_fts USING fts5(content, tokenize='trigram');")
    conn.execute("INSERT INTO memories (id, content, content_hash, memory_type, tags, metadata, created_at)"
                 " VALUES (1, 'background task completed, output file written', 'h1', 'learning', '', '{}', 1)")
    conn.execute("INSERT INTO memory_content_fts (rowid, content) VALUES (1, 'background task completed, output file written')")
    conn.commit()
    conn.close()
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home), "B12_DATA_DIR": str(data),
           "B12_HOOK_DIR": str(HOOKS), "B12_EMBED_RUNTIME_DIR": str(tmp_path)}
    payload = json.dumps({"prompt": prompt, "cwd": str(tmp_path), "session_id": "notif-test"})
    r = subprocess.run(["bash", str(HOOKS / "memory-retrieval.sh")], input=payload, env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    log = data / "memory-logs" / "feedback.jsonl"
    lines = log.read_text().splitlines() if log.exists() else []
    return [json.loads(l) for l in lines if '"hook_retrieval"' in l]


def test_task_notification_is_not_searched(tmp_path):
    assert _retrievals(tmp_path, NOTIFICATION) == []


def test_user_prompt_with_the_same_words_is_searched(tmp_path):
    # Guards the test itself: the same words typed by a user do reach retrieval.
    hits = _retrievals(tmp_path, "the background task completed and wrote an output file, what next")
    assert len(hits) == 1
