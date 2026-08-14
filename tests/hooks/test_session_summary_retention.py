"""Regression coverage for SessionEnd session-summary retention safety."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _make_db(path: Path) -> list[int]:
    path.parent.mkdir(parents=True)
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE memories (
            id INTEGER PRIMARY KEY,
            content TEXT,
            content_hash TEXT UNIQUE,
            tags TEXT,
            memory_type TEXT,
            metadata TEXT,
            created_at REAL,
            updated_at REAL,
            created_at_iso TEXT,
            updated_at_iso TEXT,
            strength REAL,
            deleted_at REAL,
            valid_until TEXT
        );
        CREATE TABLE memory_embeddings (
            rowid INTEGER PRIMARY KEY,
            content_embedding BLOB
        );
        CREATE TABLE memory_graph (
            source_hash TEXT,
            target_hash TEXT,
            similarity REAL,
            connection_types TEXT,
            metadata TEXT,
            created_at REAL,
            relationship_type TEXT,
            UNIQUE(source_hash, target_hash)
        );
        """
    )
    rows = [
        (1, {"session_identity": "unbound", "producer": "mcp_session_tracker", "platform": "mcp-only", "project": "project"}),
        (2, {"project": "project"}),
        (3, {"session_id": "stable-session-3", "project": "project"}),
        (4, {"source_session": "legacy-prefx", "project": "project"}),
        (5, {"session_identity": "unbound", "producer": "mcp_session_tracker", "platform": "mcp-only", "project": "project"}),
        (6, {"session_identity": "unbound", "producer": "other", "platform": "mcp-only", "project": "project"}),
        (7, {"session_identity": "unbound", "producer": "other", "platform": "mcp-only", "project": "project"}),
    ]
    for row_id, metadata in rows:
        conn.execute(
            """INSERT INTO memories
               (id, content, content_hash, tags, memory_type, metadata,
                created_at, updated_at, strength, deleted_at)
               VALUES (?, ?, ?, 'proj:project,session-summary', 'session_summary', ?, ?, ?, 1.0, NULL)""",
            (row_id, f"summary {row_id}", f"hash-{row_id}", json.dumps(metadata), float(row_id), float(row_id)),
        )
        conn.execute(
            "INSERT INTO memory_embeddings (rowid, content_embedding) VALUES (?, X'00000000')",
            (row_id,),
        )
    conn.commit()
    conn.close()
    return [row_id for row_id, _ in rows]


def _install_embed_stubs(hook_dir: Path) -> None:
    scripts = hook_dir / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "sentence_transformers.py").write_text(
        """import numpy as np
class SentenceTransformer:
    def __init__(self, *args, **kwargs):
        pass
    def encode(self, texts, convert_to_numpy=True):
        return np.zeros((len(texts), 4), dtype=np.float32)
""",
        encoding="utf-8",
    )
    (scripts / "write_time_merge.py").write_text(
        """import hashlib
import json

def upsert_session_summary(conn, *, session_id, content, tags, metadata, embedding_bytes, now):
    payload = json.loads(metadata) if isinstance(metadata, str) else dict(metadata)
    payload["session_id"] = session_id
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    cur = conn.execute(
        "INSERT INTO memories (content, content_hash, tags, memory_type, metadata, "
        "created_at, updated_at, strength, deleted_at) "
        "VALUES (?, ?, ?, 'session_summary', ?, ?, ?, 1.0, NULL)",
        (content, digest, tags, json.dumps(payload), now.timestamp(), now.timestamp()),
    )
    conn.execute(
        "INSERT INTO memory_embeddings (rowid, content_embedding) VALUES (?, ?)",
        (cur.lastrowid, embedding_bytes),
    )
    return cur.lastrowid
""",
        encoding="utf-8",
    )


def test_session_end_does_not_apply_unreviewed_summary_deletion(tmp_path: Path) -> None:
    """SessionEnd may store a summary but retention remains a reviewed, backup-gated operation."""
    home = tmp_path / "home"
    summary_file = tmp_path / "project-latest.md"
    summary_file.write_text(
        "This is a non-trivial session summary used to exercise the detached storage path. "
        "It deliberately contains no extraction section headings, so the test isolates summary retention.\n",
        encoding="utf-8",
    )

    db_path = home / ".local" / "share" / "mcp-memory" / "sqlite_vec.db"
    preexisting_ids = _make_db(db_path)
    hook_dir = tmp_path / "hook-runtime"
    _install_embed_stubs(hook_dir)

    hook_source = (ROOT / "hooks" / "memory-session-end.sh").read_text(encoding="utf-8")
    start_marker = "cat > \"$EMBED_SCRIPT\" << 'MEMPYEOF'\n"
    assert hook_source.count(start_marker) == 1
    embed_source = hook_source.split(start_marker, 1)[1].split("\nMEMPYEOF\n", 1)[0]
    embed_script = tmp_path / "session-end-embed.py"
    embed_script.write_text(embed_source, encoding="utf-8")

    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "B12_HOOK_DIR": str(hook_dir),
        }
    )
    result = subprocess.run(
        [
            sys.executable,
            str(embed_script),
            str(summary_file),
            "project",
            "personal",
            "current-session",
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr

    conn = sqlite3.connect(db_path)
    stored = conn.execute(
        "SELECT COUNT(*) FROM memories WHERE json_extract(metadata, '$.session_id') = 'current-session'"
    ).fetchone()[0]
    conn.close()
    assert stored == 1, "detached SessionEnd storage did not finish"

    conn = sqlite3.connect(db_path)
    deleted = conn.execute(
        f"SELECT id FROM memories WHERE id IN ({','.join('?' for _ in preexisting_ids)}) "
        "AND deleted_at IS NOT NULL ORDER BY id",
        preexisting_ids,
    ).fetchall()
    conn.close()
    assert deleted == []
