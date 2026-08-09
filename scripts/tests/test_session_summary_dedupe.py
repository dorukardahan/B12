from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

import b12_dedupe_session_summaries as tool  # noqa: E402
import b12_mcp_server as server  # noqa: E402
from write_time_merge import upsert_session_summary  # noqa: E402

SCRIPT = SCRIPTS / "b12_dedupe_session_summaries.py"

def _db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    server._ensure_schema(conn)
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='memory_embeddings'").fetchone():
        conn.execute("CREATE TABLE memory_embeddings(rowid INTEGER PRIMARY KEY, content_embedding BLOB)")
    conn.commit()
    return conn

def _add(conn, sid: str | None, content, created, updated,
         platform: str | None = "codex", content_hash=None):
    metadata = {} if sid is None else {"session_id": sid}
    if platform is not None:
        metadata["platform"] = platform
    return conn.execute(
        "INSERT INTO memories(content,content_hash,tags,memory_type,metadata,created_at,updated_at) "
        "VALUES (?,?,?,'session_summary',?,?,?)",
        (content, content_hash or f"hash-{content}", "session-summary", json.dumps(metadata), created, updated),
    ).lastrowid

def _run(path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(SCRIPT), "--db-path", str(path), *args],
                          text=True, capture_output=True, timeout=10)

def test_dry_run_reports_exact_plan_and_never_writes(tmp_path):
    path = tmp_path / "dry.db"
    conn = _db(path)
    _add(conn, "123456789012", "a-old", 1, 1)
    _add(conn, "123456789012-full", "a-new", 2, 20)
    _add(conn, "sid-b", "b-old", 3, 3, None)
    _add(conn, "sid-b", "b-new", 4, 40, None)
    _add(conn, "sid-unique", "unique", 5, 5)
    for n, sid in enumerate((None, "unknown", "gemini-unknown", "gemini-unkno", " sid-space ", " sid-space ")):
        _add(conn, sid, f"out-{n}", 6 + n, 6 + n, None)
    conn.commit()
    conn.close()
    before = path.read_bytes()

    result = _run(path)

    assert result.returncode == 0, result.stderr
    assert result.stdout == """B12 session-summary dedupe
Mode: DRY-RUN (no changes)
Session plans:
  sid=123456789012-full platforms=codex keep=2 remove=1
  sid=sid-b platforms=(none) keep=4 remove=3
Platform totals:
  (none): rows=2 sessions=1 duplicate_sessions=1 keep=1 remove=1
  codex: rows=3 sessions=2 duplicate_sessions=1 keep=2 remove=1
No-session-id live rows: 6 (untouched)
Would soft-delete 2 rows across 2 sessions.
Re-run with --execute to apply.
"""
    assert path.read_bytes() == before

def test_execute_preserves_rows_indexes_is_idempotent_and_upserts(tmp_path):
    path = tmp_path / "execute.db"
    conn = _db(path)
    sid = "sid-returning"
    old_content = "returning exact summary"
    old_hash = __import__("hashlib").sha256(f"{old_content}|session:{sid}".encode()).hexdigest()
    old = _add(conn, sid, old_content, 1, 1, content_hash=old_hash)
    other = _add(conn, sid, "other old summary", 2, 2)
    keep = _add(conn, sid, "canonical updated summary", 3, 30)
    out_of_scope = [_add(conn, value, f"out-{n}", 4 + n, 4 + n, None) for n, value in enumerate((None, "unknown", "gemini-unknown", "gemini-unkno", " sid-space ", " sid-space "))]
    for row_id in (old, other, keep):
        conn.execute("INSERT INTO memory_embeddings VALUES (?, ?)", (row_id, f"vec-{row_id}".encode()))
    conn.execute(
        "INSERT INTO memory_graph(source_hash,target_hash,similarity,connection_types,created_at) "
        "VALUES (?, 'target', .5, '[]', 1)", (old_hash,),
    )
    conn.commit()
    before = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM memories")}
    conn.close()

    first = _run(path, "--execute")
    assert first.returncode == 0, first.stderr
    assert "Soft-deleted 2 rows across 1 sessions." in first.stdout
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    assert [r[0] for r in conn.execute(
        "SELECT id FROM memories WHERE memory_type='session_summary' "
        "AND deleted_at IS NULL ORDER BY id"
    )] == [keep, *out_of_scope]
    for row_id in (old, other):
        after = dict(conn.execute("SELECT * FROM memories WHERE id=?", (row_id,)).fetchone())
        assert after.pop("deleted_at") is not None
        expected = before[row_id].copy()
        expected.pop("deleted_at")
        assert after == expected
    for table in ("memory_fts", "memory_fts_stemmed", "memory_content_fts"):
        assert conn.execute(f"SELECT rowid FROM {table} WHERE {table} MATCH 'returning'").fetchall() == []
    assert conn.execute("SELECT COUNT(*) FROM memory_embeddings").fetchone()[0] == 3
    assert conn.execute("SELECT COUNT(*) FROM memory_graph WHERE source_hash=?", (old_hash,)).fetchone()[0] == 1
    assert all(conn.execute("SELECT deleted_at FROM memories WHERE id=?", (row_id,)).fetchone()[0] is None for row_id in out_of_scope)
    deleted_state = [tuple(r) for r in conn.execute("SELECT id,deleted_at FROM memories ORDER BY id")]
    conn.close()

    second = _run(path, "--execute")
    assert second.returncode == 0, second.stderr
    assert "No duplicate live session summaries found; database unchanged." in second.stdout
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT id,deleted_at FROM memories ORDER BY id").fetchall() == deleted_state
    revived = upsert_session_summary(
        conn, session_id=sid, content=old_content, tags="session-summary",
        metadata={"session_id": sid, "platform": "codex"}, embedding_bytes=None, now=100,
    )
    assert revived == old
    assert conn.execute(
        "SELECT COUNT(*) FROM memories WHERE memory_type='session_summary' "
        "AND json_extract(metadata,'$.session_id')=? AND deleted_at IS NULL", (sid,),
    ).fetchone()[0] == 1
    conn.close()

def test_monster_session_uses_one_transaction(tmp_path):
    conn = _db(tmp_path / "monster.db")
    for n in range(64):
        _add(conn, "sid-monster", f"monster-{n}", n, n)
    conn.commit()
    trace = []
    conn.set_trace_callback(trace.append)
    started = time.monotonic()
    report = tool.deduplicate(conn, execute=True, now=500)
    assert time.monotonic() - started < 2
    assert report["remove_count"] == 63
    assert sum(s == "BEGIN IMMEDIATE" for s in trace) == 1
    assert sum(s == "COMMIT" for s in trace) == 1
    assert conn.execute("SELECT COUNT(*) FROM memories WHERE deleted_at IS NULL").fetchone()[0] == 1
    conn.close()
