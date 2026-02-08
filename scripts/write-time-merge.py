#!/usr/bin/env python3
"""
B12 Memory System - Write-time Semantic Merge

This module is intended to be called from the SessionEnd "embed" step before a
new memory is stored. It performs a semantic nearest-neighbor lookup using
sqlite-vec. If the closest existing memory (same memory_type, not deleted)
exceeds a cosine similarity threshold, we merge the content into the existing
row instead of inserting a new row.

Why vec_distance_cosine (not vec_cosine_similarity)?
  sqlite-vec exposes `vec_distance_cosine(a, b)` which returns cosine distance.
  Cosine similarity is computed as:
    similarity = 1.0 - distance

Requirements implemented
------------------------
1. merge_or_insert(conn, content, content_hash, tags, memory_type, metadata,
   embedding_bytes, now)
2. Nearest neighbor lookup using sqlite-vec cosine distance (same memory_type,
   deleted_at IS NULL)
3. If similarity > 0.85:
   - Update content: "{old}\\n• {new}"
   - Recompute content_hash (sha256 hex)
   - Recompute embedding for merged content using sentence-transformers
   - Update updated_at + updated_at_iso
   - Boost strength by +0.2 (cap 5.0) if column exists
4. Else: normal INSERT into memories + memory_embeddings
5. Self-test on a temporary DB: merge and insert scenarios
6. CLI: python scripts/write-time-merge.py --self-test

Notes
-----
- This file intentionally does not integrate with hooks directly. Import and
  call `merge_or_insert(...)` from your hook code.
- The embedding for the *incoming* memory is expected to be provided as
  float32 bytes (384 dims) from the caller's embed step.
- On merge, we recompute the embedding for the merged content using:
    paraphrase-multilingual-MiniLM-L12-v2  (default, overridable via MCP_EMBEDDING_MODEL)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import time
import warnings
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union


# Keep hook logs clean and prevent noisy tokenizer warnings.
warnings.filterwarnings("ignore")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
# sentence-transformers sometimes pulls in accelerate -> wandb; disable it defensively.
os.environ.setdefault("WANDB_DISABLED", "true")
os.environ.setdefault("WANDB_MODE", "disabled")


DEFAULT_DB_PATH = Path("~/Library/Application Support/mcp-memory/sqlite_vec.db").expanduser()
DEFAULT_MODEL_NAME = "paraphrase-multilingual-MiniLM-L12-v2"

SIMILARITY_THRESHOLD = 0.85
STRENGTH_BOOST_ON_MERGE = 0.2
STRENGTH_CAP = 5.0
EMBEDDING_DIM = 384


@dataclass(frozen=True)
class MergeResult:
    action: str  # "inserted" | "merged" | "noop_duplicate"
    memory_id: int
    similarity: Optional[float] = None
    merged_from_id: Optional[int] = None
    reason: Optional[str] = None


# Lazy global cache: only loaded if we actually need to re-embed on merge.
_MODEL_CACHE: Dict[str, Any] = {}


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _coerce_now(now: Any) -> Tuple[float, str]:
    """
    Accepts:
      - None (uses current UTC time)
      - datetime (converted to UTC)
      - int/float timestamp (seconds)
    Returns (timestamp_seconds, iso_utc).
    """
    if now is None:
        dt = datetime.now(timezone.utc)
        return dt.timestamp(), dt.isoformat()

    if isinstance(now, datetime):
        dt = now.astimezone(timezone.utc)
        return dt.timestamp(), dt.isoformat()

    try:
        ts = float(now)
    except Exception as e:  # noqa: BLE001 (we want a tight error message)
        raise TypeError("now must be None, datetime, int, or float (unix seconds)") from e

    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return ts, dt.isoformat()


def _tags_to_str(tags: Any) -> Optional[str]:
    if tags is None:
        return None
    if isinstance(tags, str):
        return tags
    if isinstance(tags, (list, tuple, set)):
        return ",".join(str(t) for t in tags)
    return str(tags)


def _metadata_to_str(metadata: Any) -> Optional[str]:
    if metadata is None:
        return None
    if isinstance(metadata, str):
        return metadata
    # Keep defaults stable for ASCII-only logs; allow non-ascii if metadata has it.
    return json.dumps(metadata, ensure_ascii=False)


def _get_table_columns(conn: sqlite3.Connection, table: str) -> set:
    cur = conn.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in cur.fetchall()}


def _ensure_sqlite_vec_loaded(conn: sqlite3.Connection) -> None:
    """
    Load sqlite-vec extension into this connection (idempotent-ish).
    """
    try:
        import sqlite_vec
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(
            "sqlite-vec is required. Run this using the mcp-memory-service venv "
            "or install sqlite-vec."
        ) from e

    # Many callers already loaded the extension; calling again is harmless.
    try:
        conn.enable_load_extension(True)
    except Exception:
        # Some environments disallow extension loading; if already loaded, we can proceed.
        pass
    try:
        sqlite_vec.load(conn)
    finally:
        try:
            conn.enable_load_extension(False)
        except Exception:
            pass


def _get_sentence_transformer(model_name: str):
    """
    Lazy-load SentenceTransformer only when merge happens.
    """
    cached = _MODEL_CACHE.get(model_name)
    if cached is not None:
        return cached

    try:
        from sentence_transformers import SentenceTransformer
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(
            "sentence-transformers is required for merge re-embedding. "
            "Run this using the mcp-memory-service venv."
        ) from e

    model = SentenceTransformer(model_name, device="cpu")
    _MODEL_CACHE[model_name] = model
    return model


def _encode_embedding_bytes(text: str, model_name: str) -> bytes:
    """
    Compute float32 embedding bytes for `text` using sentence-transformers.
    """
    model = _get_sentence_transformer(model_name)

    # sentence-transformers returns numpy arrays for convert_to_numpy=True.
    import numpy as np

    emb = model.encode([text], convert_to_numpy=True)[0]
    emb = np.asarray(emb, dtype=np.float32)
    if emb.ndim != 1 or emb.shape[0] != EMBEDDING_DIM:
        raise ValueError(f"Unexpected embedding shape: {emb.shape}, expected ({EMBEDDING_DIM},)")
    return emb.tobytes()


def _best_match(
    conn: sqlite3.Connection,
    memory_type: Optional[str],
    embedding_bytes: bytes,
) -> Optional[Tuple[int, str, str, float, float]]:
    """
    Returns the nearest memory as:
      (id, content, content_hash, strength, similarity)
    or None if no candidate exists.
    """
    cols = _get_table_columns(conn, "memories")
    has_strength = "strength" in cols
    has_valid_until = "valid_until" in cols

    where: List[str] = ["m.deleted_at IS NULL"]
    params: List[Any] = [embedding_bytes]

    if has_valid_until:
        where.append("m.valid_until IS NULL")

    if memory_type is None:
        where.append("m.memory_type IS NULL")
    else:
        where.append("m.memory_type = ?")
        params.append(memory_type)

    strength_expr = "COALESCE(m.strength, 1.0)" if has_strength else "1.0"

    # sqlite-vec: smaller distance => higher similarity
    # similarity = 1 - distance
    sql = f"""
        SELECT m.id,
               m.content,
               m.content_hash,
               {strength_expr} AS strength,
               vec_distance_cosine(me.content_embedding, ?) AS distance
        FROM memories m
        JOIN memory_embeddings me ON m.id = me.rowid
        WHERE {" AND ".join(where)}
        ORDER BY distance ASC
        LIMIT 1
    """

    row = conn.execute(sql, params).fetchone()
    if not row:
        return None

    mem_id, old_content, old_hash, strength, distance = row
    if distance is None:
        return None

    similarity = 1.0 - float(distance)
    return int(mem_id), str(old_content), str(old_hash), float(strength), similarity


def _upsert_embedding(conn: sqlite3.Connection, memory_id: int, embedding_bytes: bytes) -> None:
    """
    Insert or update an embedding row for a given memory_id.

    Note: sqlite-vec vec0 virtual tables support UPDATE, but do *not* support
    INSERT OR REPLACE reliably (it raises UNIQUE constraint errors). So we
    implement upsert as: UPDATE if exists else INSERT.
    """
    exists = conn.execute(
        "SELECT 1 FROM memory_embeddings WHERE rowid = ? LIMIT 1",
        (memory_id,),
    ).fetchone()
    if exists:
        conn.execute(
            "UPDATE memory_embeddings SET content_embedding = ? WHERE rowid = ?",
            (embedding_bytes, memory_id),
        )
    else:
        conn.execute(
            "INSERT INTO memory_embeddings (rowid, content_embedding) VALUES (?, ?)",
            (memory_id, embedding_bytes),
        )


def _rewrite_graph_hashes(conn: sqlite3.Connection, old_hash: str, new_hash: str) -> None:
    """
    Best-effort: keep memory_graph consistent if we change content_hash.

    memory_graph uses hashes as identifiers (not foreign keys), so changing
    content_hash without rewriting graph rows breaks existing edges.
    """
    if old_hash == new_hash:
        return

    # If the graph table doesn't exist, do nothing.
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_graph' LIMIT 1"
    ).fetchone():
        return

    # Insert new edges (IGNORE on PK collisions), then delete old ones.
    rows = conn.execute(
        """
        SELECT source_hash, target_hash, similarity, connection_types, metadata, created_at, relationship_type
        FROM memory_graph
        WHERE source_hash = ? OR target_hash = ?
        """,
        (old_hash, old_hash),
    ).fetchall()

    if not rows:
        return

    for src, tgt, sim, c_types, meta, created_at, rel_type in rows:
        src2 = new_hash if src == old_hash else src
        tgt2 = new_hash if tgt == old_hash else tgt
        conn.execute(
            """
            INSERT OR IGNORE INTO memory_graph
            (source_hash, target_hash, similarity, connection_types, metadata, created_at, relationship_type)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (src2, tgt2, sim, c_types, meta, created_at, rel_type),
        )

    conn.execute("DELETE FROM memory_graph WHERE source_hash = ? OR target_hash = ?", (old_hash, old_hash))


def merge_or_insert(
    conn: sqlite3.Connection,
    content: str,
    content_hash: Optional[str],
    tags: Optional[Union[str, Sequence[str]]],
    memory_type: Optional[str],
    metadata: Optional[Union[str, Dict[str, Any]]],
    embedding_bytes: Union[bytes, bytearray, memoryview],
    now: Any,
) -> MergeResult:
    """
    Merge new content into nearest existing memory if similarity > threshold.

    Parameters
    ----------
    conn:
        sqlite3 connection (should be connected to sqlite_vec.db)
    content:
        New memory content
    content_hash:
        sha256 hex of content (caller may provide; computed if missing)
    tags:
        Tags string or list of tags (used on INSERT)
    memory_type:
        Memory type (used for matching + insert)
    metadata:
        JSON string or dict (used on INSERT)
    embedding_bytes:
        float32 embedding bytes for `content` (used for matching + insert)
    now:
        datetime | unix seconds | None
    """
    if not isinstance(conn, sqlite3.Connection):
        raise TypeError("conn must be a sqlite3.Connection")

    content = (content or "").strip()
    if not content:
        raise ValueError("content must be a non-empty string")

    if isinstance(embedding_bytes, memoryview):
        embedding_blob = embedding_bytes.tobytes()
    else:
        embedding_blob = bytes(embedding_bytes)

    # sqlite-vec expects float32 arrays; 384 dims -> 1536 bytes.
    # We don't hard-fail here because callers may use serialize_float32 which
    # could differ in representation between sqlite-vec versions.
    if len(embedding_blob) == EMBEDDING_DIM * 4:
        pass

    now_ts, now_iso = _coerce_now(now)

    tags_str = _tags_to_str(tags)
    metadata_str = _metadata_to_str(metadata)

    if not content_hash:
        content_hash = _sha256_hex(content)

    _ensure_sqlite_vec_loaded(conn)

    # Exact duplicate guard: avoid UNIQUE(content_hash) errors and redundant inserts.
    dup = conn.execute(
        "SELECT id FROM memories WHERE content_hash = ? AND deleted_at IS NULL LIMIT 1",
        (content_hash,),
    ).fetchone()
    if dup:
        return MergeResult(action="noop_duplicate", memory_id=int(dup[0]), reason="exact_hash")

    best = _best_match(conn, memory_type=memory_type, embedding_bytes=embedding_blob)

    if best is not None:
        best_id, best_content, best_hash, best_strength, best_similarity = best
    else:
        best_id, best_content, best_hash, best_strength, best_similarity = None, None, None, None, None

    if best is not None and best_similarity is not None and best_similarity > SIMILARITY_THRESHOLD:
        merged_content = f"{best_content.rstrip()}\n• {content.strip()}"
        new_hash = _sha256_hex(merged_content)

        model_name = os.environ.get("MCP_EMBEDDING_MODEL", DEFAULT_MODEL_NAME)
        new_embedding = _encode_embedding_bytes(merged_content, model_name=model_name)

        cols = _get_table_columns(conn, "memories")
        has_strength = "strength" in cols

        # Best-effort: avoid failing the hook if the recomputed hash collides.
        set_hash = True
        if new_hash != best_hash:
            conflict = conn.execute(
                "SELECT id FROM memories WHERE content_hash = ? AND id != ? LIMIT 1",
                (new_hash, best_id),
            ).fetchone()
            if conflict:
                set_hash = False

        if has_strength:
            new_strength = min(float(best_strength or 1.0) + STRENGTH_BOOST_ON_MERGE, STRENGTH_CAP)
        else:
            new_strength = None

        if set_hash and has_strength:
            conn.execute(
                """
                UPDATE memories
                   SET content = ?,
                       content_hash = ?,
                       updated_at = ?,
                       updated_at_iso = ?,
                       strength = ?
                 WHERE id = ?
                """,
                (merged_content, new_hash, now_ts, now_iso, new_strength, best_id),
            )
        elif set_hash and not has_strength:
            conn.execute(
                """
                UPDATE memories
                   SET content = ?,
                       content_hash = ?,
                       updated_at = ?,
                       updated_at_iso = ?
                 WHERE id = ?
                """,
                (merged_content, new_hash, now_ts, now_iso, best_id),
            )
        elif not set_hash and has_strength:
            conn.execute(
                """
                UPDATE memories
                   SET content = ?,
                       updated_at = ?,
                       updated_at_iso = ?,
                       strength = ?
                 WHERE id = ?
                """,
                (merged_content, now_ts, now_iso, new_strength, best_id),
            )
            new_hash = best_hash  # report the effective hash (unchanged)
        else:
            conn.execute(
                """
                UPDATE memories
                   SET content = ?,
                       updated_at = ?,
                       updated_at_iso = ?
                 WHERE id = ?
                """,
                (merged_content, now_ts, now_iso, best_id),
            )
            new_hash = best_hash

        _upsert_embedding(conn, best_id, new_embedding)

        # Keep graph edges stable if we changed the hash.
        if set_hash and best_hash != new_hash:
            _rewrite_graph_hashes(conn, old_hash=best_hash, new_hash=new_hash)

        return MergeResult(
            action="merged",
            memory_id=int(best_id),
            similarity=float(best_similarity),
            merged_from_id=int(best_id),
        )

    # Normal INSERT (existing behavior)
    now_dt = datetime.fromtimestamp(now_ts, tz=timezone.utc)
    cur = conn.execute(
        """
        INSERT INTO memories (content, content_hash, tags, memory_type, metadata,
                              created_at, updated_at, created_at_iso, updated_at_iso)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            content,
            content_hash,
            tags_str,
            memory_type,
            metadata_str,
            now_ts,
            now_ts,
            now_dt.isoformat(),
            now_dt.isoformat(),
        ),
    )
    new_id = int(cur.lastrowid)
    _upsert_embedding(conn, new_id, embedding_blob)

    return MergeResult(
        action="inserted",
        memory_id=new_id,
        similarity=float(best_similarity) if best_similarity is not None else None,
        reason="no_match" if best is None else "below_threshold",
    )


def _pack_f32(values: Sequence[float]) -> bytes:
    """
    Helper for tests: pack float32 values without numpy.
    """
    import struct

    return struct.pack("<%sf" % len(values), *values)


def _self_test() -> None:
    """
    Self-test:
      - Creates a temp sqlite DB with vec0 embeddings
      - Inserts a memory
      - Inserts a low-similarity memory (should INSERT)
      - Inserts a high-similarity memory (should MERGE)
    """
    import tempfile

    try:
        import sqlite_vec  # noqa: F401
    except Exception as e:  # noqa: BLE001
        raise RuntimeError("Self-test requires sqlite-vec. Run using the mcp-memory-service venv.") from e

    # Trigger model load early so failures show up as test failures.
    _ = _get_sentence_transformer(os.environ.get("MCP_EMBEDDING_MODEL", DEFAULT_MODEL_NAME))

    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "test.db"
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=10000")

            _ensure_sqlite_vec_loaded(conn)

            conn.execute(
                """
                CREATE TABLE memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    content_hash TEXT UNIQUE NOT NULL,
                    content TEXT NOT NULL,
                    tags TEXT,
                    memory_type TEXT,
                    metadata TEXT,
                    created_at REAL,
                    updated_at REAL,
                    created_at_iso TEXT,
                    updated_at_iso TEXT,
                    deleted_at REAL DEFAULT NULL,
                    strength REAL DEFAULT 1.0
                )
                """
            )
            conn.execute(
                f"""
                CREATE VIRTUAL TABLE memory_embeddings USING vec0(
                    content_embedding FLOAT[{EMBEDDING_DIM}] distance_metric=cosine
                )
                """
            )

            # 1) Insert first memory with a deterministic, non-zero embedding vector.
            emb_ones = _pack_f32([1.0] * EMBEDDING_DIM)
            t1 = time.time()
            r1 = merge_or_insert(
                conn,
                content="Alpha memory",
                content_hash=None,
                tags="t1",
                memory_type="test",
                metadata={"importance_score": 1.0},
                embedding_bytes=emb_ones,
                now=t1,
            )
            assert r1.action == "inserted"
            assert r1.memory_id == 1

            # 2) Insert a low-similarity memory (different embedding).
            # Similarity between ones vector and a unit vector is ~1/sqrt(384) ~= 0.051 < 0.85.
            emb_unit = _pack_f32([1.0] + [0.0] * (EMBEDDING_DIM - 1))
            t2 = t1 + 10
            r2 = merge_or_insert(
                conn,
                content="Totally different thing",
                content_hash=None,
                tags="t2",
                memory_type="test",
                metadata={"importance_score": 1.0},
                embedding_bytes=emb_unit,
                now=t2,
            )
            assert r2.action == "inserted"
            assert r2.memory_id == 2

            # 3) Merge a high-similarity memory (same embedding as mem #1).
            t3 = t2 + 10
            r3 = merge_or_insert(
                conn,
                content="Alpha memory extra detail",
                content_hash=None,
                tags="t3",
                memory_type="test",
                metadata={"importance_score": 1.0},
                embedding_bytes=emb_ones,
                now=t3,
            )
            assert r3.action == "merged"
            assert r3.memory_id == 1
            assert r3.similarity is not None and r3.similarity > SIMILARITY_THRESHOLD

            # Validate merged content format + strength boost.
            row = conn.execute(
                "SELECT content, strength FROM memories WHERE id = 1"
            ).fetchone()
            assert row is not None
            merged_content, strength = row
            assert merged_content == "Alpha memory\n• Alpha memory extra detail"
            assert abs(float(strength) - 1.2) < 1e-6

            # Ensure embeddings for id=1 got rewritten to a new blob (likely != emb_ones).
            emb_after = conn.execute(
                "SELECT content_embedding FROM memory_embeddings WHERE rowid = 1"
            ).fetchone()[0]
            assert emb_after is not None
            assert len(emb_after) == EMBEDDING_DIM * 4
            assert emb_after != emb_ones

            # Ensure we still have exactly 2 rows total (merge should not add a row).
            count = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            assert int(count) == 2

            conn.commit()
        finally:
            conn.close()

    print("Self-test passed.")


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description="B12 write-time semantic merge helper.")
    parser.add_argument("--self-test", action="store_true", help="Run self-test on a temporary database.")
    args = parser.parse_args(argv)

    if args.self_test:
        _self_test()
        return 0

    parser.print_help()
    return 2


if __name__ == "__main__":
    import sys
    raise SystemExit(main(sys.argv[1:]))
