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
6. CLI: python scripts/write_time_merge.py --self-test

Notes
-----
- This file intentionally does not integrate with hooks directly. Import and
  call `merge_or_insert(...)` from your hook code.
- The embedding for the *incoming* memory is expected to be provided as
  float32 bytes (384 dims) from the caller's embed step.
- On merge, we recompute the embedding for the merged content using:
    BAAI/bge-m3  (default, overridable via MCP_EMBEDDING_MODEL)
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


try:
    from shared_patterns import get_db_path as _get_db_path
    DEFAULT_DB_PATH = Path(_get_db_path())
except ImportError:
    import sys as _sys
    _home = Path.home()
    if _sys.platform == "darwin":
        DEFAULT_DB_PATH = _home / "Library" / "Application Support" / "mcp-memory" / "sqlite_vec.db"
    elif _sys.platform == "win32":
        DEFAULT_DB_PATH = _home / "AppData" / "Local" / "mcp-memory" / "sqlite_vec.db"
    else:
        DEFAULT_DB_PATH = _home / ".local" / "share" / "mcp-memory" / "sqlite_vec.db"
DEFAULT_MODEL_NAME = "BAAI/bge-m3"

SIMILARITY_THRESHOLD = 0.85
STRENGTH_BOOST_ON_MERGE = 0.2
STRENGTH_CAP = 5.0
# Bumped from 0.7 → 0.8 in v12.3 to match the contradiction surface filter
# in memory-retrieval.sh (B12_CONTRA_SURFACE_THRESHOLD default 0.85). The
# legacy 0.71-0.79 edges were over-emitting cross-domain false positives.
def _resolve_nli_threshold() -> float:
    """Codex review PR #57 round 3 P2: a malformed B12_NLI_CONTRA_THRESHOLD
    (e.g. "abc", "0,8") would raise ValueError at module-import time,
    breaking every caller that imports write_time_merge. Guard the cast
    and fall back to the documented default on any parse failure."""
    raw = os.environ.get("B12_NLI_CONTRA_THRESHOLD", "0.8")
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return 0.8
    if 0.0 <= v <= 1.0:
        return v
    return 0.8


NLI_CONTRADICTION_THRESHOLD = _resolve_nli_threshold()
# 1024 for BGE-M3 (default since v11.34 / P-FOUNDATION); override via env to
# stay compatible with pre-migration 384-dim DBs.
EMBEDDING_DIM = int(os.environ.get("B12_EMBED_DIM", "1024"))


@dataclass(frozen=True)
class MergeResult:
    action: str  # "inserted" | "merged" | "noop_duplicate"
    memory_id: int
    similarity: Optional[float] = None
    merged_from_id: Optional[int] = None
    reason: Optional[str] = None
    contradictions: Optional[list] = None  # [{id, hash, score, snippet}]


# Lazy global cache: only loaded if we actually need to re-embed on merge.
_MODEL_CACHE: Dict[str, Any] = {}


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.strip().lower().encode("utf-8")).hexdigest()


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
    # Use validate_metadata for guaranteed valid JSON output
    try:
        from shared_patterns import validate_metadata
        return validate_metadata(metadata)
    except ImportError:
        pass
    # Fallback if shared_patterns not available
    if isinstance(metadata, str):
        try:
            json.loads(metadata)
            return metadata
        except (json.JSONDecodeError, ValueError):
            return "{}"
    return json.dumps(metadata, ensure_ascii=False)


def _augment_importance(metadata: Any, content: str,
                        memory_type: Optional[str] = None) -> Any:
    """Pre-populate metadata.importance_score from b12_importance heuristics.

    If the caller already provided `importance_score` in metadata, do nothing
    (caller wins). Otherwise compute the band-based score using
    `b12_importance.score(content)` and stash it under `importance_score` so
    the recall path picks it up identically to a caller-set value.

    `memory_type` is the type merge_or_insert actually stores for the row; it is
    used for the type floor in preference to any type embedded in metadata, so a
    caller passing `memory_type="decision"` with metadata that does not duplicate
    the type still receives the floor.

    Pattern from AytuncYildizli/B12 PR 24 (3534d0d).
    """
    try:
        import b12_importance  # type: ignore[import-not-found]
    except ImportError:
        return metadata

    # Materialize metadata into a dict we can mutate, mirroring _metadata_to_str
    # logic. dict / JSON-string / None all flow through here.
    if isinstance(metadata, str):
        try:
            obj = json.loads(metadata) if metadata.strip() else {}
            if not isinstance(obj, dict):
                return metadata  # leave non-dict JSON alone
        except (json.JSONDecodeError, ValueError):
            return metadata  # leave malformed strings to _metadata_to_str
    elif metadata is None:
        obj = {}
    elif isinstance(metadata, dict):
        obj = dict(metadata)  # shallow copy — never mutate caller's dict
    else:
        return metadata

    # Resolve through the single finalize_importance chokepoint: secret-cap +
    # memory_type floor + the strongest of caller/heuristic. So hook / SessionEnd /
    # extractor writes get a uniform secret cap AND the type floor (a typed memory
    # — decision/error_fix/learning/… — is no longer stuck at baseline when its
    # content lacks a keyword cue), while a stronger caller value is preserved.
    # Prefer the explicit memory_type arg (what merge_or_insert stores for the
    # row) when it is a real type; fall back to a type embedded in metadata.
    mt = (memory_type if (memory_type and memory_type not in ("general", "note", ""))
          else obj.get("type"))
    obj["importance_score"] = b12_importance.finalize_importance(
        content, obj.get("importance_score"), mt)

    return obj


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
            "sqlite-vec is required. Run this using the b12-venv "
            "or install sqlite-vec."
        ) from e

    # Check if sqlite_vec is already loaded (avoids double-load crash)
    try:
        conn.execute("SELECT vec_version()")
        return  # Already loaded, nothing to do
    except sqlite3.OperationalError:
        pass  # Not loaded yet, proceed

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
            "Run this using the b12-venv."
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
               {", m.valid_until" if has_valid_until else ""}
        FROM memories m
        JOIN memory_embeddings me ON m.id = me.rowid
        WHERE {" AND ".join(where)}
        ORDER BY distance ASC
        LIMIT 10
    """

    rows = conn.execute(sql, params).fetchall()
    if not rows:
        return None
    now_dt = datetime.now(timezone.utc)
    row = None
    for candidate in rows:
        if has_valid_until and not _valid_until_active(candidate[5], now_dt):
            continue
        row = candidate
        break
    if row is None:
        return None

    mem_id, old_content, old_hash, strength, distance = row[:5]
    if distance is None:
        return None

    similarity = 1.0 - float(distance)
    return int(mem_id), str(old_content), str(old_hash), float(strength), similarity


def _valid_until_active(value: Any, now_dt: datetime) -> bool:
    if not value:
        return True
    try:
        expires = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        return expires > now_dt
    except (TypeError, ValueError):
        return True


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
    graph_cols = {
        row[1] for row in conn.execute("PRAGMA table_info(memory_graph)").fetchall()
    }
    if not {"source_hash", "target_hash"}.issubset(graph_cols):
        return
    optional_cols = [
        col for col in (
            "similarity",
            "connection_types",
            "metadata",
            "created_at",
            "relationship_type",
        )
        if col in graph_cols
    ]
    select_cols = ["source_hash", "target_hash", *optional_cols]

    # Insert new edges (IGNORE on PK collisions), then delete old ones.
    rows = conn.execute(
        f"""
        SELECT {', '.join(select_cols)}
        FROM memory_graph
        WHERE source_hash = ? OR target_hash = ?
        """,
        (old_hash, old_hash),
    ).fetchall()

    if not rows:
        return

    for row in rows:
        values = dict(zip(select_cols, row))
        values["source_hash"] = new_hash if values["source_hash"] == old_hash else values["source_hash"]
        values["target_hash"] = new_hash if values["target_hash"] == old_hash else values["target_hash"]
        placeholders = ", ".join("?" for _ in select_cols)
        conn.execute(
            f"""
            INSERT OR IGNORE INTO memory_graph
            ({', '.join(select_cols)})
            VALUES ({placeholders})
            """,
            tuple(values[col] for col in select_cols),
        )

    conn.execute("DELETE FROM memory_graph WHERE source_hash = ? OR target_hash = ?", (old_hash, old_hash))


def upsert_session_summary(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    content: str,
    tags: Optional[Union[str, Sequence[str]]],
    metadata: Optional[Union[str, Dict[str, Any]]],
    embedding_bytes: Optional[Union[bytes, bytearray, memoryview]],
    now: Any = None,
) -> int:
    """Atomically insert or replace the newest active summary for a session.

    Existing duplicate rows are intentionally left untouched for a later cleanup
    migration.  ``BEGIN IMMEDIATE`` serializes first writes without requiring a
    UNIQUE migration that would fail on those legacy duplicates.  External-content
    FTS tables follow the UPDATE through their existing triggers; the vec row is
    updated (or removed when no fresh embedding is available) in the same txn.
    """
    sid = (session_id or "").strip()
    legacy_sid = sid[:12]
    content = (content or "").strip()
    if not sid:
        raise ValueError("session_id must be a non-empty string")
    if not content:
        raise ValueError("content must be a non-empty string")

    now_ts, now_iso = _coerce_now(now)
    tags_str = _tags_to_str(tags)
    metadata_str = _metadata_to_str(metadata) or "{}"
    try:
        metadata_obj = json.loads(metadata_str)
    except (json.JSONDecodeError, TypeError):
        metadata_obj = {}
    if not isinstance(metadata_obj, dict):
        metadata_obj = {}
    metadata_obj["session_id"] = sid
    metadata_str = json.dumps(metadata_obj, ensure_ascii=False)
    content_hash = hashlib.sha256(
        f"{content.lower()}|session:{sid}".encode("utf-8")
    ).hexdigest()
    embedding_blob = (
        embedding_bytes.tobytes()
        if isinstance(embedding_bytes, memoryview)
        else bytes(embedding_bytes)
        if embedding_bytes
        else None
    )

    owns_txn = not conn.in_transaction
    if owns_txn:
        conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            """SELECT id, content_hash FROM memories
               WHERE memory_type = 'session_summary'
                 AND deleted_at IS NULL
                 AND (CASE WHEN json_valid(metadata)
                           THEN json_extract(metadata, '$.session_id') END) IN (?, ?)
               ORDER BY
                 ((CASE WHEN json_valid(metadata)
                         THEN json_extract(metadata, '$.session_id') END) = ?) DESC,
                 COALESCE(updated_at, created_at, 0) DESC, id DESC
               LIMIT 1""",
            (sid, legacy_sid, sid),
        ).fetchone()
        if row:
            memory_id, old_hash = int(row[0]), str(row[1])
            conn.execute(
                """UPDATE memories
                   SET content = ?, content_hash = ?, tags = ?, metadata = ?,
                       updated_at = ?, updated_at_iso = ?
                   WHERE id = ?""",
                (content, content_hash, tags_str, metadata_str,
                 now_ts, now_iso, memory_id),
            )
            _rewrite_graph_hashes(conn, old_hash, content_hash)
        else:
            cursor = conn.execute(
                """INSERT INTO memories
                   (content, content_hash, tags, memory_type, metadata,
                    created_at, updated_at, created_at_iso, updated_at_iso)
                   VALUES (?, ?, ?, 'session_summary', ?, ?, ?, ?, ?)""",
                (content, content_hash, tags_str, metadata_str,
                 now_ts, now_ts, now_iso, now_iso),
            )
            if cursor.lastrowid is None:
                raise RuntimeError("session summary insert returned no row id")
            memory_id = int(cursor.lastrowid)

        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'memory_embeddings'"
        ).fetchone():
            if embedding_blob is None:
                conn.execute("DELETE FROM memory_embeddings WHERE rowid = ?", (memory_id,))
            else:
                _upsert_embedding(conn, memory_id, embedding_blob)

        if owns_txn:
            conn.commit()
        return memory_id
    except BaseException:
        if owns_txn:
            conn.rollback()
        raise


def _daemon_request(payload: dict, timeout: float = 10) -> Optional[dict]:
    """Send JSON request to embedding daemon, return parsed response or None."""
    import socket as _sock
    uid = os.getuid() if hasattr(os, 'getuid') else os.getpid()
    # Hardcode /tmp/ — macOS TMPDIR varies per session
    sock_path = f"/tmp/b12-embed-{uid}.sock"
    pid_path = f"/tmp/b12-embed-{uid}.pid"
    if not os.path.exists(sock_path) or not os.path.exists(pid_path):
        return None
    try:
        pid = int(open(pid_path).read().strip())
        os.kill(pid, 0)
    except (ValueError, ProcessLookupError, PermissionError, FileNotFoundError):
        return None
    try:
        s = _sock.socket(_sock.AF_UNIX, _sock.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(sock_path)
        import json as _json
        s.sendall((_json.dumps(payload) + '\n').encode())
        data = b''
        while True:
            chunk = s.recv(1048576)
            if not chunk:
                break
            data += chunk
            if b'\n' in data:
                break
        s.close()
        return _json.loads(data.decode().strip())
    except Exception:
        return None


def check_contradictions(
    content: str,
    db_path: str,
    memory_type: Optional[str],
    embedding_bytes: bytes,
) -> list:
    """
    Check if new content contradicts existing memories via daemon NLI.

    Returns list of dicts: [{id, hash, score, snippet}] for contradictions
    above NLI_CONTRADICTION_THRESHOLD (default 0.8, overridable via
    B12_NLI_CONTRA_THRESHOLD). Returns empty list if daemon unavailable,
    no contradictions found, or the input is a fragment.

    Fragment pre-filter: drops short/incomplete utterances before NLI
    (these otherwise create spurious cross-domain edges).

    IMPORTANT: Must be called BEFORE any writes on the parent connection,
    as this opens a separate DB connection for neighbor lookups.
    """
    if len(content) < 30:
        return []
    try:
        from shared_patterns import is_fragment as _is_fragment
        if _is_fragment(content):
            return []
    except Exception:
        pass

    # Step 1: Find top-5 similar memories via daemon
    # We need a memory_id but we don't have one yet (new content).
    # Instead, use find_neighbors on the closest match, or encode_batch + manual scan.
    # Simpler: ask daemon for semantic_search which already does cosine scan.
    resp = _daemon_request({
        'op': 'semantic_search',
        'query': content,
        'db_path': db_path,
        'limit': 5,
    })
    if not resp or not resp.get('ok'):
        return []

    neighbors = resp.get('results', [])
    if not neighbors:
        return []

    # Step 2: Get neighbor content for NLI
    import sqlite3 as _sqlite3
    try:
        conn = _sqlite3.connect(db_path, timeout=5)
        conn.execute("PRAGMA busy_timeout=10000")
    except Exception:
        return []

    pairs = []
    neighbor_info = []
    for n in neighbors:
        n_id = n.get('id')
        if not n_id:
            continue
        row = conn.execute(
            "SELECT content_hash, content FROM memories WHERE id = ? AND deleted_at IS NULL",
            (n_id,)
        ).fetchone()
        if not row or len(row[1]) < 30:
            continue
        pairs.append([content, row[1]])
        neighbor_info.append({'id': n_id, 'hash': row[0], 'content': row[1]})
    conn.close()

    if not pairs:
        return []

    # Step 3: NLI check via daemon
    nli_resp = _daemon_request({'op': 'nli_check', 'pairs': pairs}, timeout=15)
    if not nli_resp or not nli_resp.get('ok'):
        return []

    contradictions = []
    for i, result in enumerate(nli_resp.get('results', [])):
        scores = result.get('scores', {})
        if scores.get('contradiction', 0) > NLI_CONTRADICTION_THRESHOLD:
            info = neighbor_info[i]
            contradictions.append({
                'id': info['id'],
                'hash': info['hash'],
                'score': round(scores['contradiction'], 4),
                'snippet': info['content'][:100],
            })

    return contradictions


def merge_or_insert(
    conn: sqlite3.Connection,
    content: str,
    content_hash: Optional[str],
    tags: Optional[Union[str, Sequence[str]]],
    memory_type: Optional[str],
    metadata: Optional[Union[str, Dict[str, Any]]],
    embedding_bytes: Union[bytes, bytearray, memoryview],
    now: Any,
    db_path: Optional[str] = None,
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
    db_path:
        Optional path to sqlite_vec.db. If provided, enables contradiction
        detection via the embedding daemon's NLI service.
    """
    if not isinstance(conn, sqlite3.Connection):
        raise TypeError("conn must be a sqlite3.Connection")

    content = (content or "").strip()
    if not content:
        raise ValueError("content must be a non-empty string")

    # Fragment gate (write-time twin of the MCP memory_store filter). Reject
    # short/incomplete utterances before the embedding + DB work. Bypassable
    # via B12_DISABLE_FRAGMENT_FILTER=1 for callers that intentionally store
    # short tagged notes.
    # Codex review PR #69 round 3 P1: respect caller-supplied memory_type.
    # When a caller explicitly classifies the memory (anything other than
    # "general" / "note" / empty), they've already declared intent.
    _typed_explicitly = bool(memory_type) and memory_type not in ("general", "note", "")
    if not _typed_explicitly and \
       os.environ.get("B12_DISABLE_FRAGMENT_FILTER", "").lower() not in ("1", "true", "yes"):
        try:
            from shared_patterns import is_fragment as _is_fragment
            if _is_fragment(content):
                return MergeResult(
                    action="noop_duplicate",
                    memory_id=0,
                    reason="rejected_fragment",
                )
        except ImportError:
            pass

    # PII / secret scrubber (PR #67). Runs after fragment gate so we don't
    # waste an embedding call on content that would be rejected anyway.
    try:
        from b12_pii_scrubber import scrub as _pii_scrub
        _scrubbed = _pii_scrub(content)
        if _scrubbed != content:
            content = _scrubbed
            content_hash = None
            try:
                _model_name = os.environ.get("MCP_EMBEDDING_MODEL", DEFAULT_MODEL_NAME)
                embedding_bytes = _encode_embedding_bytes(content, _model_name)
            except Exception:
                embedding_bytes = b""
    except ImportError:
        pass

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
    # Auto-populate metadata.importance_score from b12_importance if the
    # caller did not pre-set it. Pattern ported from AytuncYildizli/B12
    # PR 24 (3534d0d feat(scoring): ingest-time importance heuristics).
    # Memorable / decision / fact bands map to importance_score values
    # the recall scorer already understands (>= 0.7 ranks above
    # baseline). Caller-provided metadata.importance_score wins.
    metadata = _augment_importance(metadata, content, memory_type)
    metadata_str = _metadata_to_str(metadata)

    # Secret cap on the NORMALIZED metadata: _augment_importance bails on the
    # legacy "key:val, ..." string format (it only handles dict/JSON), but
    # _metadata_to_str above converts it to JSON — so re-apply the cap here to
    # cover every supported metadata format on the insert/hash-dup paths.
    if metadata_str:
        try:
            import b12_importance as _imp_cap
            if _imp_cap.is_secret(content):
                _mc = json.loads(metadata_str)
                if isinstance(_mc, dict) and _mc.get("importance_score") != _imp_cap.IMPORTANCE_BASELINE:
                    _mc["importance_score"] = _imp_cap.IMPORTANCE_BASELINE
                    metadata_str = _metadata_to_str(_mc)
        except Exception:
            pass

    if not content_hash:
        content_hash = _sha256_hex(content)

    _ensure_sqlite_vec_loaded(conn)

    # Exact duplicate guard: avoid UNIQUE(content_hash) errors and redundant
    # inserts. If the same hash only exists as an expired row, revive that row
    # instead of letting stale valid_until state suppress a fresh write.
    cols = _get_table_columns(conn, "memories")
    has_valid_until = "valid_until" in cols
    dup_sql = (
        "SELECT id, valid_until FROM memories WHERE content_hash = ? AND deleted_at IS NULL LIMIT 1"
        if has_valid_until
        else "SELECT id, NULL AS valid_until FROM memories WHERE content_hash = ? AND deleted_at IS NULL LIMIT 1"
    )
    dup = conn.execute(dup_sql, (content_hash,)).fetchone()
    if dup:
        expired = False
        if has_valid_until and dup[1]:
            try:
                expires = datetime.fromisoformat(str(dup[1]).replace("Z", "+00:00"))
                if expires.tzinfo is None:
                    expires = expires.replace(tzinfo=timezone.utc)
                expired = expires <= datetime.fromtimestamp(now_ts, tz=timezone.utc)
            except (TypeError, ValueError):
                expired = False
        if not expired:
            return MergeResult(action="noop_duplicate", memory_id=int(dup[0]), reason="exact_hash")
        conn.execute(
            """
            UPDATE memories
               SET content = ?,
                   tags = ?,
                   memory_type = ?,
                   metadata = ?,
                   valid_until = NULL,
                   updated_at = ?,
                   updated_at_iso = ?
             WHERE id = ?
            """,
            (content, tags_str, memory_type, metadata_str, now_ts, now_iso, int(dup[0])),
        )
        _upsert_embedding(conn, int(dup[0]), embedding_blob)
        return MergeResult(
            action="merged",
            memory_id=int(dup[0]),
            reason="revived_expired_duplicate",
        )

    best = _best_match(conn, memory_type=memory_type, embedding_bytes=embedding_blob)

    if best is not None:
        best_id, best_content, best_hash, best_strength, best_similarity = best
    else:
        best_id, best_content, best_hash, best_strength, best_similarity = None, None, None, None, None

    # Contradiction check via daemon NLI (only if db_path provided)
    detected_contradictions = None
    if db_path:
        try:
            detected_contradictions = check_contradictions(
                content, db_path, memory_type, embedding_blob
            ) or None
        except Exception:
            pass  # Never block store on contradiction check failure

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

        # Reconcile the merged row's importance (the content-only UPDATEs above
        # don't touch metadata) through the SAME finalize_importance chokepoint as
        # the insert path: a credential-bearing merge caps at baseline, the
        # memory_type floor applies, and the row's existing score is compared
        # against the merged content's heuristic on the RET-3-normalized scale —
        # so a high-signal write ("save this ...") merged into a baseline row lifts
        # it, a higher explicit/level value is preserved at its raw scale, and a
        # level-multiplier existing value is no longer max'd against a fractional
        # new signal on mismatched scales.
        try:
            import b12_importance as _imp_merge
            _row = conn.execute(
                "SELECT metadata FROM memories WHERE id = ?", (best_id,)
            ).fetchone()
            _md: Any = {}
            if _row and _row[0]:
                try:
                    _md = json.loads(_row[0])
                except Exception:
                    _md = {}
            if not isinstance(_md, dict):
                _md = {}
            _md["importance_score"] = _imp_merge.finalize_importance(
                merged_content, _md.get("importance_score"), memory_type)
            conn.execute(
                "UPDATE memories SET metadata = ? WHERE id = ?",
                (_metadata_to_str(_md), best_id),
            )
        except Exception:
            pass

        _upsert_embedding(conn, best_id, new_embedding)

        # Keep graph edges stable if we changed the hash.
        if set_hash and best_hash != new_hash:
            _rewrite_graph_hashes(conn, old_hash=best_hash, new_hash=new_hash)

        return MergeResult(
            action="merged",
            memory_id=int(best_id),
            similarity=float(best_similarity),
            merged_from_id=int(best_id),
            contradictions=detected_contradictions,
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
        contradictions=detected_contradictions,
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
        raise RuntimeError("Self-test requires sqlite-vec. Run using the b12-venv.") from e

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
        # Codex review PR #59 round 2 P2: bypass the new fragment gate
        # for the documented self-test fixtures.
        # Round 3 P2 follow-up: restore the prior env value in
        # try/finally so an in-process caller hitting main() (e.g. a
        # test harness importing this module) doesn't get a leak that
        # silently disables fragment rejection for the rest of the run.
        _prior = os.environ.get("B12_DISABLE_FRAGMENT_FILTER")
        os.environ["B12_DISABLE_FRAGMENT_FILTER"] = "1"
        try:
            _self_test()
        finally:
            if _prior is None:
                os.environ.pop("B12_DISABLE_FRAGMENT_FILTER", None)
            else:
                os.environ["B12_DISABLE_FRAGMENT_FILTER"] = _prior
        return 0

    parser.print_help()
    return 2


if __name__ == "__main__":
    import sys
    raise SystemExit(main(sys.argv[1:]))
