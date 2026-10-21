"""Memories without a memory_type must still render a display line.

The display string was built as '[' || m.memory_type || '] ' || content. In
SQLite concatenating NULL yields NULL, so a memory stored without a type (for
example some session summaries written by other hosts) surfaced as a bare
"null" with its content lost, in proactive surfacing, recall and the session
context blocks.
"""
from __future__ import annotations

import json
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("sqlite_vec")

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import embed_daemon  # noqa: E402


class _FakeModel:
    def encode(self, texts, normalize_embeddings=True):
        return np.array([[1.0, 0.0, 0.0, 0.0]] * len(texts), dtype=np.float32)


def _make_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE memories (id INTEGER PRIMARY KEY, content TEXT, content_hash TEXT, memory_type TEXT,"
        " metadata TEXT, tags TEXT, deleted_at REAL, valid_until TEXT)"
    )
    conn.execute("CREATE TABLE memory_embeddings (rowid INTEGER PRIMARY KEY, content_embedding BLOB)")
    vector = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32).tobytes()
    for mem_id, mtype, content in ((1, None, "untyped memory text"), (2, "decision", "typed memory text")):
        conn.execute(
            "INSERT INTO memories (id, content, content_hash, memory_type, metadata, tags) VALUES (?, ?, ?, ?, '{}', '')",
            (mem_id, content, f"h{mem_id}", mtype),
        )
        conn.execute("INSERT INTO memory_embeddings (rowid, content_embedding) VALUES (?, ?)", (mem_id, vector))
    conn.commit()
    conn.close()


@pytest.mark.parametrize("op", [embed_daemon._recall, embed_daemon._semantic_search])
def test_untyped_memory_gets_a_general_display(tmp_path, op):
    db = tmp_path / "mem.db"
    _make_db(db)
    response = op(_FakeModel(), {"query": "anything", "db_path": str(db), "threshold": 0.1, "limit": 5})
    displays = {r["id"]: r["display"] for r in response["results"]}
    assert displays[1] == "[general] untyped memory text"
    assert displays[2] == "[decision] typed memory text"


def _offending_lines(pattern: re.Pattern, allow: re.Pattern | None = None) -> list[str]:
    return [
        f"{path.relative_to(REPO_ROOT)}:{n}"
        for path in [*REPO_ROOT.glob("hooks/*.sh"), *REPO_ROOT.glob("scripts/*.py")]
        for n, line in enumerate(path.read_text().splitlines(), 1)
        if pattern.search(line) and not (allow and allow.search(line))
    ]


def test_no_unguarded_memory_type_concatenation_remains():
    # Qualified (m.memory_type) and unqualified (memory_type) column references.
    assert not _offending_lines(re.compile(r"'\[' \|\| (?:\w+\.)?memory_type \|\|"))


def test_type_exclusion_predicates_keep_untyped_rows():
    # In SQL `NULL NOT IN (...)` and `NULL != 'x'` are NULL, which drops untyped
    # memories; every SQL exclusion on memory_type needs an IS NULL branch.
    sql_exclusion = re.compile(r"(?:\w+\.)?memory_type\s*(?:NOT IN\s*\(|!=\s*'|<>\s*')")
    guarded = re.compile(r"(?:\w+\.)?memory_type IS NULL OR")
    assert not _offending_lines(sql_exclusion, guarded)


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq not installed")
def test_proactive_surface_never_prints_null_displays():
    hook = (REPO_ROOT / "hooks" / "memory-proactive-surface.sh").read_text()
    expr = re.search(r"_DISPLAY=\$\(echo \"\$_RESP\" \| jq -r '([^']+)'\)", hook).group(1)
    payload = json.dumps({"results": [{"id": 1, "display": None}, {"id": 2, "display": "[decision] kept"}]})
    out = subprocess.run(["jq", "-r", expr], input=payload, capture_output=True, text=True, check=True).stdout
    assert out.splitlines() == ["[decision] kept"]
