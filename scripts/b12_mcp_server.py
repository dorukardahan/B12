#!/usr/bin/env python3
"""
B12 Mini MCP Server — minimal memory CRUD with daemon-delegated ML.

Replaces the 804MB mcp-memory-service with 4 tools, zero ML deps.
All embedding/search ops delegated to embed_daemon via Unix socket.
"""

import asyncio, base64, hashlib, json, os, socket, sqlite3, time
try:
    import sqlite_vec
    _HAS_VEC = True
except ImportError:
    _HAS_VEC = False
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from mcp.server.fastmcp import FastMCP

# Consolidation engine (lazy import path — scripts/ is on sys.path)
try:
    from consolidation_engine import consolidate as _consolidate, ConsolidationResult
except ImportError:
    _consolidate = None
    ConsolidationResult = None

# Refinement module (lazy import path — scripts/ is on sys.path)
try:
    from memory_refine import refine_candidates as _refine_candidates
except ImportError:
    _refine_candidates = None

# ── Paths ────────────────────────────────────────────────────────
import sys as _sys
_home = os.path.expanduser("~")
if _sys.platform == "darwin":
    DB_PATH = os.path.join(_home, "Library", "Application Support",
                           "mcp-memory", "sqlite_vec.db")
elif _sys.platform == "win32":
    DB_PATH = os.path.join(_home, "AppData", "Local",
                           "mcp-memory", "sqlite_vec.db")
else:
    DB_PATH = os.path.join(_home, ".local", "share",
                           "mcp-memory", "sqlite_vec.db")

_UID = os.getuid() if hasattr(os, 'getuid') else os.getpid()
# Hardcode /tmp/ — macOS TMPDIR varies per session, causing socket mismatch
SOCK_PATH = f"/tmp/b12-embed-{_UID}.sock"

# ── SQLite connection (set during lifespan) ──────────────────────
_db: sqlite3.Connection | None = None
_db_lock = asyncio.Lock()


def _ensure_schema(db: sqlite3.Connection) -> None:
    """Create all required tables if they don't exist. Safe on existing DBs."""
    db.execute("""
        CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT NOT NULL,
            content_hash TEXT UNIQUE,
            memory_type TEXT DEFAULT 'general',
            tags TEXT DEFAULT '',
            metadata TEXT DEFAULT '{}',
            created_at REAL,
            updated_at REAL,
            created_at_iso TEXT,
            updated_at_iso TEXT,
            deleted_at REAL DEFAULT NULL,
            strength REAL DEFAULT 1.0,
            last_accessed_at REAL DEFAULT NULL,
            valid_until TEXT DEFAULT NULL
        )
    """)
    # B12 FTS5 table (unicode61 tokenizer, used by hooks)
    # Includes tags column to match existing DB schema from mcp-memory-service
    db.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
            content,
            tags,
            content='memories',
            content_rowid='id',
            tokenize='unicode61'
        )
    """)
    # FTS5 sync triggers for memory_fts
    # NOTE: We do NOT create triggers here if they already exist from mcp-memory-service
    # (fts_insert, fts_update, fts_softdel, fts_hardel). Check before creating to avoid
    # duplicate trigger firing which corrupts the FTS index.
    _existing_triggers = {r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'memory_fts_%'"
    ).fetchall()}
    if not _existing_triggers:
        # Fresh install — create B12 triggers with soft-delete awareness
        db.execute("""
            CREATE TRIGGER IF NOT EXISTS memory_fts_insert AFTER INSERT ON memories
            WHEN new.deleted_at IS NULL BEGIN
                INSERT INTO memory_fts(rowid, content, tags)
                VALUES (new.id, new.content, COALESCE(new.tags, ''));
            END
        """)
        db.execute("""
            CREATE TRIGGER IF NOT EXISTS memory_fts_delete AFTER DELETE ON memories BEGIN
                INSERT INTO memory_fts(memory_fts, rowid, content, tags)
                VALUES('delete', old.id, old.content, COALESCE(old.tags, ''));
            END
        """)
        db.execute("""
            CREATE TRIGGER IF NOT EXISTS memory_fts_update AFTER UPDATE ON memories
            WHEN new.deleted_at IS NULL BEGIN
                INSERT INTO memory_fts(memory_fts, rowid, content, tags)
                VALUES('delete', old.id, old.content, COALESCE(old.tags, ''));
                INSERT INTO memory_fts(rowid, content, tags)
                VALUES (new.id, new.content, COALESCE(new.tags, ''));
            END
        """)
        db.execute("""
            CREATE TRIGGER IF NOT EXISTS memory_fts_softdel AFTER UPDATE ON memories
            WHEN new.deleted_at IS NOT NULL AND old.deleted_at IS NULL BEGIN
                INSERT INTO memory_fts(memory_fts, rowid, content, tags)
                VALUES('delete', old.id, old.content, COALESCE(old.tags, ''));
            END
        """)
    # Native FTS5 table (trigram tokenizer, used by MCP server search)
    db.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS memory_content_fts USING fts5(
            content,
            content='memories',
            content_rowid='id',
            tokenize='trigram'
        )
    """)
    # Triggers for memory_content_fts (with soft-delete guard)
    _existing_content_triggers = {r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND sql LIKE '%memory_content_fts%'"
    ).fetchall()}
    if not _existing_content_triggers:
        db.execute("""
            CREATE TRIGGER IF NOT EXISTS memories_fts_ai AFTER INSERT ON memories
            WHEN new.deleted_at IS NULL BEGIN
                INSERT INTO memory_content_fts(rowid, content) VALUES (new.id, new.content);
            END
        """)
        db.execute("""
            CREATE TRIGGER IF NOT EXISTS memories_fts_au AFTER UPDATE ON memories
            WHEN new.deleted_at IS NULL BEGIN
                DELETE FROM memory_content_fts WHERE rowid = old.id;
                INSERT INTO memory_content_fts(rowid, content) VALUES (new.id, new.content);
            END
        """)
        db.execute("""
            CREATE TRIGGER IF NOT EXISTS memories_fts_softdel AFTER UPDATE ON memories
            WHEN new.deleted_at IS NOT NULL AND old.deleted_at IS NULL BEGIN
                DELETE FROM memory_content_fts WHERE rowid = old.id;
            END
        """)
        db.execute("""
            CREATE TRIGGER IF NOT EXISTS memories_fts_ad AFTER DELETE ON memories BEGIN
                DELETE FROM memory_content_fts WHERE rowid = old.id;
            END
        """)

    # Memory graph (edges between memories)
    # Uses composite PK matching existing DB from mcp-memory-service.
    # One edge per (source, target) pair — INSERT OR REPLACE upgrades type.
    db.execute("""
        CREATE TABLE IF NOT EXISTS memory_graph (
            source_hash TEXT NOT NULL,
            target_hash TEXT NOT NULL,
            similarity REAL NOT NULL,
            connection_types TEXT NOT NULL DEFAULT '[]',
            metadata TEXT,
            created_at REAL NOT NULL,
            relationship_type TEXT DEFAULT 'related',
            PRIMARY KEY (source_hash, target_hash)
        )
    """)
    # Vector embeddings table (requires sqlite-vec)
    if _HAS_VEC:
        try:
            db.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS memory_embeddings USING vec0(
                    content_embedding FLOAT[384] distance_metric=cosine
                )
            """)
        except Exception:
            pass  # vec0 may already exist with different params
    db.commit()


@asynccontextmanager
async def lifespan(server: FastMCP):
    global _db
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    _db = sqlite3.connect(DB_PATH, timeout=30)
    _db.execute("PRAGMA journal_mode=WAL")
    _db.execute("PRAGMA busy_timeout=30000")
    _db.execute("PRAGMA wal_autocheckpoint=100")
    _db.row_factory = sqlite3.Row
    if _HAS_VEC:
        _db.enable_load_extension(True)
        sqlite_vec.load(_db)
    _ensure_schema(_db)
    yield
    if _db:
        _db.close()
        _db = None

server = FastMCP("B12", lifespan=lifespan)


# ── Helpers ──────────────────────────────────────────────────────

def daemon_request(op: str, **kwargs) -> dict | None:
    """Send JSON to embed_daemon via Unix socket. Returns None on failure."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.settimeout(5)
        s.connect(SOCK_PATH)
        s.sendall((json.dumps({"op": op, **kwargs}) + "\n").encode())
        data = b""
        while b"\n" not in data:
            chunk = s.recv(65536)
            if not chunk: break
            data += chunk
        resp = json.loads(data.decode().strip())
        return resp if resp.get("ok") else None
    except Exception:
        return None
    finally:
        s.close()


def compute_content_hash(content: str) -> str:
    """Content-only hash — matches upstream mcp-memory-service for backward compat."""
    return hashlib.sha256(content.strip().lower().encode()).hexdigest()


def _normalize_tags(tags) -> str:
    if tags is None: return ""
    if isinstance(tags, list): return ",".join(t.strip() for t in tags if t)
    return str(tags)


def _now():
    ts = time.time()
    return ts, datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _require_db() -> sqlite3.Connection:
    """Raise if DB not initialized (pre-lifespan or post-shutdown)."""
    if _db is None:
        raise RuntimeError("Database not initialized")
    return _db


def _unified_score(row, relevance: float) -> float:
    """Compute unified score: 0.3*decay + 0.3*importance + 0.4*relevance.
    Matches the hook's scoring formula for consistent cross-path results."""
    import math
    now_ts = time.time()
    # Explicit None checks — 0.0 is a valid value for both fields
    accessed = row["last_accessed_at"] if row["last_accessed_at"] is not None else row["created_at"]
    if accessed is None:
        accessed = now_ts
    age_days = max((now_ts - accessed) / 86400.0, 0.001)
    strength = row["strength"] if row["strength"] is not None else 1.0
    # Guard against zero strength (would cause ZeroDivisionError)
    if strength <= 0:
        strength = 0.01
    decay = max(math.exp(-age_days / strength), 0.01)

    meta = {}
    try:
        meta = json.loads(row["metadata"] or "{}")
    except (json.JSONDecodeError, TypeError):
        pass
    importance = min(float(meta.get("importance_score", 1.0)) / 2.0, 1.0)

    return 0.3 * decay + 0.3 * importance + 0.4 * relevance


def _fmt_memory(row, score=None) -> str:
    p = [f"[{row['memory_type'] or 'general'}] {row['content'][:500]}"]
    if row["tags"]: p.append(f"  Tags: {row['tags']}")
    p.append(f"  Hash: {row['content_hash'][:16]}...  Created: {row['created_at_iso'] or '?'}")
    if score is not None: p.append(f"  Score: {score:.3f}")
    return "\n".join(p)


# ── Tool: memory_store ───────────────────────────────────────────

@server.tool()
async def memory_store(content: str, metadata: dict | None = None) -> str:
    """Store a new memory with optional metadata, tags, and type."""
    db = _require_db()
    metadata = metadata or {}
    tags_raw = metadata.pop("tags", None)
    memory_type = metadata.pop("type", metadata.pop("memory_type", "general"))
    valid_until = metadata.pop("valid_until", None)
    tags = _normalize_tags(tags_raw)

    content_hash = compute_content_hash(content)
    now_ts, now_iso = _now()

    # Default metadata fields
    base_meta = {
        "quality_score": 0.5, "quality_provider": "implicit",
        "access_count": 0, "source_type": "user", "credibility": 1.0,
    }
    base_meta.update(metadata)
    meta_json = json.dumps(base_meta, ensure_ascii=False)

    async with _db_lock:
        # Check if a soft-deleted row with same hash exists — undelete it
        existing = db.execute(
            "SELECT id, deleted_at FROM memories WHERE content_hash = ?",
            (content_hash,),
        ).fetchone()
        if existing and existing["deleted_at"] is not None:
            db.execute(
                """UPDATE memories SET deleted_at = NULL, strength = 1.0,
                   tags = ?, memory_type = ?, metadata = ?,
                   updated_at = ?, updated_at_iso = ?, valid_until = ?
                   WHERE content_hash = ?""",
                (tags, memory_type, meta_json, now_ts, now_iso,
                 valid_until, content_hash),
            )
        else:
            db.execute(
                """INSERT OR IGNORE INTO memories
                   (content_hash, content, tags, memory_type, metadata,
                    strength, created_at, created_at_iso, updated_at, updated_at_iso,
                    valid_until)
                   VALUES (?, ?, ?, ?, ?, 1.0, ?, ?, ?, ?, ?)""",
                (content_hash, content, tags, memory_type, meta_json,
                 now_ts, now_iso, now_ts, now_iso, valid_until),
            )
        db.commit()

        # Get the row id for embedding insertion
        row = db.execute(
            "SELECT id FROM memories WHERE content_hash = ?", (content_hash,)
        ).fetchone()
        if not row:
            return f"Stored (hash: {content_hash[:16]}) but could not retrieve ID"
        mem_id = row["id"]

    # Embed via daemon (graceful degradation) — outside lock, daemon call is slow
    resp = daemon_request("encode_batch", texts=[content])
    if resp and resp.get("embeddings"):
        emb_bytes = base64.b64decode(resp["embeddings"][0])
        async with _db_lock:
            try:
                db.execute(
                    "INSERT OR REPLACE INTO memory_embeddings (rowid, content_embedding) VALUES (?, ?)",
                    (mem_id, emb_bytes),
                )
                db.commit()
            except Exception:
                pass  # embedding table may not exist in test DBs

    return f"Stored memory (hash: {content_hash[:16]}, id: {mem_id})"


# ── Tool: memory_search ─────────────────────────────────────────

@server.tool()
async def memory_search(
    query: str = "",
    mode: str = "hybrid",
    tags: list[str] | str | None = None,
    limit: int = 10,
    after: str | None = None,
    before: str | None = None,
    max_response_chars: int = int(os.environ.get("MCP_MAX_RESPONSE_CHARS", "40000")),
) -> str:
    """Search memories by semantic similarity, full-text, or hybrid."""
    db = _require_db()
    results: dict[str, tuple[dict, float]] = {}  # content_hash -> (row, score)

    tag_list = ([t.strip() for t in tags.split(",")] if isinstance(tags, str)
                else tags if tags else [])

    # Build WHERE clause fragments
    wheres = ["m.deleted_at IS NULL",
              "(m.valid_until IS NULL OR m.valid_until > datetime('now'))"]
    params: list = []
    for t in tag_list:
        wheres.append("m.tags LIKE ?")
        params.append(f"%{t}%")
    if after:
        try:
            ts = datetime.fromisoformat(after).timestamp()
            wheres.append("m.created_at >= ?")
            params.append(ts)
        except ValueError:
            pass
    if before:
        try:
            ts = datetime.fromisoformat(before).timestamp()
            wheres.append("m.created_at <= ?")
            params.append(ts)
        except ValueError:
            pass

    where_sql = " AND ".join(wheres)

    async with _db_lock:
        # ── Exact substring search ─────────────────────────────
        if mode == "exact" and query:
            exact_rows = db.execute(
                f"""SELECT * FROM memories m
                    WHERE m.content LIKE ? AND {where_sql}
                    ORDER BY m.created_at DESC LIMIT ?""",
                [f"%{query}%"] + params + [limit],
            ).fetchall()
            for r in exact_rows:
                results[r["content_hash"]] = (r, _unified_score(r, 0.9))

        # ── FTS search (hybrid only) ─────────────────────────
        if mode == "hybrid" and query:
            # Try phrase match first, fall back to OR match for broader results
            for fts_attempt in ("phrase", "or"):
                try:
                    if fts_attempt == "phrase":
                        fts_query = '"' + query.replace('"', '""') + '"'
                    else:
                        # Split into words, join with OR for broader matching
                        words = [w.strip() for w in query.split() if len(w.strip()) > 1]
                        if not words:
                            break
                        fts_query = " OR ".join(
                            '"' + w.replace('"', '""') + '"' for w in words
                        )
                    fts_rows = db.execute(
                        f"""SELECT m.*, rank
                            FROM memory_content_fts fts
                            JOIN memories m ON m.id = fts.rowid
                            WHERE fts.content MATCH ? AND {where_sql}
                            ORDER BY rank LIMIT ?""",
                        [fts_query] + params + [limit],
                    ).fetchall()
                    for r in fts_rows:
                        h = r["content_hash"]
                        # FTS5 rank is negative BM25 (more negative = better match).
                        # Normalize to 0..1 where higher = better relevance.
                        bonus = 0.1 if fts_attempt == "phrase" else 0.0
                        raw_relevance = min(abs(r["rank"]) / 20.0, 1.0) + bonus
                        score = _unified_score(r, raw_relevance)
                        if h not in results or results[h][1] < score:
                            results[h] = (r, score)
                except Exception:
                    continue  # FTS match syntax error, try next

    # ── Semantic search via daemon (outside lock — daemon call is slow) ──
    if mode in ("semantic", "hybrid") and query:
        resp = daemon_request(
            "semantic_search", query=query, db_path=DB_PATH, limit=limit * 2
        )
        if resp and resp.get("results"):
            async with _db_lock:
                for hit in resp["results"]:
                    row = db.execute(
                        f"SELECT * FROM memories m WHERE m.id = ? AND {where_sql}",
                        [hit["id"]] + params,
                    ).fetchone()
                    if row:
                        ch = row["content_hash"]
                        old_score = results.get(ch, (None, 0))[1]
                        new_score = _unified_score(row, hit["score"])
                        # Intentional overlap bonus: memories found by BOTH FTS and semantic
                        # search get a 30% boost from the weaker score. This rewards cross-method
                        # agreement and is distinct from the hook's single-path scoring formula.
                        combined = max(old_score, new_score)
                        if ch in results:
                            combined = min(1.0, max(old_score, new_score) + min(old_score, new_score) * 0.3)
                        results[ch] = (row, combined)

    async with _db_lock:
        # ── Fallback: recent memories if no query ────────────────
        if not query:
            rows = db.execute(
                f"""SELECT * FROM memories m
                    WHERE {where_sql}
                    ORDER BY m.created_at DESC LIMIT ?""",
                params + [limit],
            ).fetchall()
            for r in rows:
                results[r["content_hash"]] = (r, 0.5)

    # ── Sort and format ──────────────────────────────────────
    sorted_results = sorted(results.values(), key=lambda x: x[1], reverse=True)[:limit]

    if not sorted_results:
        return "No memories found."

    # ── Spaced repetition: boost strength + access_count for returned memories ──
    if query and sorted_results:
        async with _db_lock:
            for row, _sc in sorted_results:
                try:
                    db.execute(
                        """UPDATE memories
                           SET strength = min(COALESCE(strength, 1.0) + 0.2, 5.0),
                               last_accessed_at = ?,
                               metadata = json_set(COALESCE(metadata, '{}'),
                                 '$.access_count',
                                 COALESCE(json_extract(metadata, '$.access_count'), 0) + 1)
                           WHERE content_hash = ?""",
                        (int(time.time()), row["content_hash"]),
                    )
                except Exception:
                    pass  # non-critical; don't fail the search
            db.commit()

    output_parts = [f"Found {len(sorted_results)} memories:\n"]
    total_chars = 0
    for row, score in sorted_results:
        entry = _fmt_memory(row, score) + "\n"
        if total_chars + len(entry) > max_response_chars:
            output_parts.append(f"\n... truncated ({len(sorted_results)} total)")
            break
        output_parts.append(entry)
        total_chars += len(entry)

    return "\n".join(output_parts)


# ── Tool: memory_update ─────────────────────────────────────────

@server.tool()
async def memory_update(
    content_hash: str,
    updates: dict,
) -> str:
    """Update metadata, tags, or type of an existing memory. Content and hash are immutable."""
    db = _require_db()

    async with _db_lock:
        row = db.execute(
            "SELECT * FROM memories WHERE content_hash = ? AND deleted_at IS NULL",
            (content_hash,),
        ).fetchone()
        if not row:
            return f"Memory not found: {content_hash[:16]}"

        # Check protected fields first
        protected = {"content", "content_hash", "embedding", "id"}
        bad = set(updates.keys()) & protected
        if bad:
            return f"Cannot update protected fields: {bad}"

        sets: list[str] = []
        vals: list = []

        if "tags" in updates:
            sets.append("tags = ?")
            vals.append(_normalize_tags(updates["tags"]))
        if "memory_type" in updates or "type" in updates:
            sets.append("memory_type = ?")
            vals.append(updates.get("memory_type", updates.get("type")))
        if "metadata" in updates:
            try:
                existing = json.loads(row["metadata"] or "{}")
            except (json.JSONDecodeError, TypeError):
                existing = {}
            existing.update(updates["metadata"])
            sets.append("metadata = ?")
            vals.append(json.dumps(existing, ensure_ascii=False))
        if "strength" in updates:
            # Clamp to [0.3, 5.0] to prevent poisoning the Ebbinghaus decay formula
            strength = max(0.3, min(5.0, float(updates["strength"])))
            sets.append("strength = ?")
            vals.append(strength)
        if "valid_until" in updates:
            # None clears dormancy/TTL, string sets it (ISO datetime or epoch)
            sets.append("valid_until = ?")
            vals.append(updates["valid_until"])
        if "deleted_at" in updates:
            # Soft-delete: set to epoch timestamp; None to un-delete
            sets.append("deleted_at = ?")
            vals.append(updates["deleted_at"])

        if not sets:
            return "No valid fields to update."

        now_ts, now_iso = _now()
        sets.append("updated_at = ?")
        vals.append(now_ts)
        sets.append("updated_at_iso = ?")
        vals.append(now_iso)

        vals.append(content_hash)
        db.execute(
            f"UPDATE memories SET {', '.join(sets)} WHERE content_hash = ?", vals
        )
        db.commit()
    return f"Updated memory {content_hash[:16]}"


# ── Tool: memory_quality ─────────────────────────────────────────

@server.tool()
async def memory_quality(
    action: str,
    content_hash: str | None = None,
    rating: str | None = None,
    feedback: str | None = None,
) -> str:
    """Rate, get, or analyze memory quality scores."""
    db = _require_db()

    if action == "rate":
        if not content_hash or rating is None:
            return "Need content_hash and rating (-1, 0, or 1)"
        async with _db_lock:
            row = db.execute(
                "SELECT metadata FROM memories WHERE content_hash = ? AND deleted_at IS NULL",
                (content_hash,),
            ).fetchone()
            if not row:
                return f"Memory not found: {content_hash[:16]}"

            try:
                meta = json.loads(row["metadata"] or "{}")
            except (json.JSONDecodeError, TypeError):
                meta = {}
            user_score = {"1": 1.0, "0": 0.5, "-1": 0.0}.get(str(rating), 0.5)
            existing = float(meta.get("quality_score", 0.5))
            new_score = round(0.6 * user_score + 0.4 * existing, 4)

            meta["quality_score"] = new_score
            meta["quality_provider"] = "user"
            if feedback:
                meta["quality_feedback"] = feedback

            now_ts, now_iso = _now()
            db.execute(
                "UPDATE memories SET metadata = ?, updated_at = ?, updated_at_iso = ? WHERE content_hash = ?",
                (json.dumps(meta, ensure_ascii=False), now_ts, now_iso, content_hash),
            )
            db.commit()
        return f"Quality updated to {new_score} for {content_hash[:16]}"

    elif action == "get":
        if not content_hash:
            return "Need content_hash"
        async with _db_lock:
            row = db.execute(
                "SELECT content, metadata, tags FROM memories WHERE content_hash = ? AND deleted_at IS NULL",
                (content_hash,),
            ).fetchone()
            if not row:
                return f"Memory not found: {content_hash[:16]}"
            try:
                meta = json.loads(row["metadata"] or "{}")
            except (json.JSONDecodeError, TypeError):
                meta = {}
        return (f"Quality: {meta.get('quality_score', 'N/A')}\n"
                f"Provider: {meta.get('quality_provider', 'N/A')}\n"
                f"Content: {row['content'][:200]}\n"
                f"Tags: {row['tags'] or 'none'}")

    elif action == "analyze":
        async with _db_lock:
            stats = db.execute("""
                SELECT COUNT(*) as total,
                       AVG(json_extract(metadata, '$.quality_score')) as avg_q,
                       MIN(json_extract(metadata, '$.quality_score')) as min_q,
                       MAX(json_extract(metadata, '$.quality_score')) as max_q
                FROM memories WHERE deleted_at IS NULL
            """).fetchone()
            type_counts = db.execute(
                "SELECT memory_type, COUNT(*) as cnt FROM memories WHERE deleted_at IS NULL GROUP BY memory_type"
            ).fetchall()
        total = stats["total"] or 0
        if total == 0:
            return "No memories in database."
        avg_q = float(stats["avg_q"]) if stats["avg_q"] is not None else 0.0
        min_q = float(stats["min_q"]) if stats["min_q"] is not None else 0.0
        max_q = float(stats["max_q"]) if stats["max_q"] is not None else 0.0
        lines = [
            f"Total memories: {total}",
            f"Quality — avg: {avg_q:.3f}, min: {min_q:.3f}, max: {max_q:.3f}",
            "By type:",
        ]
        for tc in type_counts:
            lines.append(f"  {tc['memory_type'] or 'none'}: {tc['cnt']}")
        return "\n".join(lines)

    return f"Unknown action: {action}. Use rate, get, or analyze."


# ── Tool: memory_session_context ─────────────────────────────────

@server.tool()
async def memory_session_context(
    project_name: str = "",
    cwd: str = "",
) -> str:
    """Get session start context: pre-fetched project memories, last session summary,
    and behavioral instructions. Call this FIRST in every new session."""
    db = _require_db()
    now_ts = time.time()
    sections: list[str] = []

    # Detect project from cwd if not provided
    if not project_name and cwd:
        project_name = os.path.basename(cwd)

    async with _db_lock:
        # 1. Pre-fetched project memories (top 3 by importance x strength)
        if project_name:
            proj_memories = db.execute("""
                SELECT id, content, memory_type, tags, metadata, strength
                FROM memories
                WHERE deleted_at IS NULL
                  AND (valid_until IS NULL OR valid_until > datetime('now'))
                  AND tags LIKE ?
                  AND memory_type NOT IN ('session_summary', 'progress')
                ORDER BY COALESCE(json_extract(metadata, '$.importance_score'), 1.0)
                         * COALESCE(strength, 1.0) DESC
                LIMIT 3
            """, (f"%proj:{project_name}%",)).fetchall()
            if proj_memories:
                sections.append(f"## Project Memories ({project_name})")
                boost_ids = []
                for m in proj_memories:
                    sections.append(f"- [{m['memory_type']}] {m['content'][:300]}")
                    boost_ids.append(m['id'])
                # Spaced repetition: boost retrieved memories
                if boost_ids:
                    placeholders = ",".join("?" * len(boost_ids))
                    db.execute(f"""
                        UPDATE memories
                        SET strength = min(COALESCE(strength, 1.0) + 0.2, 5.0),
                            last_accessed_at = ?,
                            metadata = json_set(COALESCE(metadata, '{{}}'),
                              '$.access_count',
                              COALESCE(json_extract(metadata, '$.access_count'), 0) + 1)
                        WHERE id IN ({placeholders})
                    """, [now_ts] + boost_ids)

        # 2. Universal memories (top 2, not project-specific)
        universal = db.execute("""
            SELECT content, memory_type FROM memories
            WHERE deleted_at IS NULL
              AND (valid_until IS NULL OR valid_until > datetime('now'))
              AND (tags NOT LIKE '%proj:%' OR tags IS NULL OR tags = '')
              AND memory_type NOT IN ('session_summary', 'progress')
            ORDER BY COALESCE(json_extract(metadata, '$.importance_score'), 1.0)
                     * COALESCE(strength, 1.0) DESC
            LIMIT 2
        """).fetchall()
        if universal:
            sections.append("## Universal Memories")
            for m in universal:
                sections.append(f"- [{m['memory_type']}] {m['content'][:300]}")

        # 3. Last session summary for this project
        if project_name:
            last_summary = db.execute("""
                SELECT content FROM memories
                WHERE memory_type = 'session_summary'
                  AND deleted_at IS NULL
                  AND tags LIKE ?
                ORDER BY created_at DESC LIMIT 1
            """, (f"%proj:{project_name}%",)).fetchone()
            if last_summary:
                sections.append("## Last Session Summary")
                sections.append(last_summary['content'][:800])

        db.commit()

    # 4. User profile (from templates directory) — no DB, outside lock
    profile_path = os.path.join(
        os.environ.get("B12_DATA_DIR", os.path.expanduser("~/.B12")),
        "user-profile.md"
    )
    if os.path.exists(profile_path):
        try:
            with open(profile_path) as f:
                profile = f.read().strip()
            if profile:
                sections.append("## User Profile")
                sections.append(profile[:500])
        except Exception:
            pass

    # 5. Behavioral instructions
    sections.append("## Instructions")
    sections.append(
        "- Search memory before answering questions about past work\n"
        "- Store decisions, errors/fixes, and learnings as memories\n"
        "- Include tags (proj:<name>) and metadata (scope, importance_score) when storing\n"
        "- Update existing memories instead of creating duplicates"
    )

    if not sections:
        return "No context available. Start storing memories to build your knowledge base."

    return "\n\n".join(sections)


# ── Tool: memory_consolidate ─────────────────────────────────────

@server.tool()
async def memory_consolidate(
    project: str = "",
    dry_run: bool = True,
    min_cluster_size: int = 3,
) -> str:
    """Consolidate similar memories: deduplicate near-identical entries,
    merge related memories, and flag contradictions for review.
    Uses HDBSCAN clustering on embeddings for semantic grouping.
    Defaults to dry_run=True (report only, no changes)."""
    if _consolidate is None:
        return "Error: consolidation_engine not available. Check scripts/ directory."

    try:
        result = _consolidate(
            db_path=DB_PATH,
            project=project or None,
            dry_run=dry_run,
            min_cluster_size=min_cluster_size,
        )
    except FileNotFoundError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Consolidation error: {e}"

    # Build human-readable summary
    lines = []
    mode = "DRY RUN" if dry_run else "APPLIED"
    lines.append(f"Consolidation ({mode}):")
    lines.append(f"  Memories processed:    {result.memories_processed}")
    lines.append(f"  Clusters found:        {result.clusters_found}")
    lines.append(f"  Deduplicated:          {result.memories_deduplicated}")
    lines.append(f"  Merged:                {result.memories_merged}")
    lines.append(f"  Contradictions flagged: {result.contradictions_flagged}")

    if dry_run and result.dry_run_report:
        lines.append("")
        lines.append("Cluster details:")
        for entry in result.dry_run_report:
            action = entry['type'].upper()
            ids = entry['ids']
            sim = entry.get('similarity', 0)
            nli = entry.get('nli_score', '')
            nli_str = f" NLI:{nli}" if nli else ''
            lines.append(f"  [{action}] #{ids[0]} <-> #{ids[1]}  "
                         f"(cosine: {sim:.3f}{nli_str})")
            for snippet in entry.get('snippets', []):
                lines.append(f"    {snippet}")

    if dry_run and (result.memories_deduplicated or result.memories_merged):
        lines.append("")
        lines.append("Run with dry_run=False to apply these changes.")
    elif not dry_run and not result.memories_deduplicated and not result.memories_merged:
        lines.append("")
        lines.append("No consolidation needed — database looks clean.")

    return "\n".join(lines)


# ── Tool: memory_refine ─────────────────────────────────────────

@server.tool()
async def memory_refine(
    candidates: str = "[]",
    project: str = "",
    similarity_threshold: float = 0.85,
) -> str:
    """Refine and deduplicate raw memory candidates.

    Accepts a JSON array of candidate memories, groups near-duplicates by
    semantic similarity, picks the best representative from each group,
    and scores quality. Returns refined candidates ready for storage.

    Args:
        candidates: JSON array of objects with {content, memory_type, tags}
        project: Project name for tag generation
        similarity_threshold: Cosine similarity threshold for grouping (0.0-1.0)
    """
    if _refine_candidates is None:
        return "Error: memory_refine module not available. Check scripts/ directory."

    try:
        candidate_list = json.loads(candidates)
    except (json.JSONDecodeError, TypeError):
        return "Error: candidates must be a valid JSON array"

    if not isinstance(candidate_list, list):
        return "Error: candidates must be a JSON array"

    if not candidate_list:
        return "No candidates provided"

    # Validate each candidate has at least 'content'
    valid = []
    for c in candidate_list:
        if isinstance(c, dict) and c.get("content"):
            valid.append({
                "content": str(c["content"]),
                "memory_type": str(c.get("memory_type", "general")),
                "tags": str(c.get("tags", "")),
            })

    if not valid:
        return "Error: no valid candidates (each must have 'content' field)"

    refined = _refine_candidates(valid, similarity_threshold)

    # Format output
    lines = [f"Refined {len(valid)} candidates \u2192 {len(refined)} unique memories:\n"]
    for r in refined:
        quality_bar = "\u2588" * int(r["quality_score"] * 10) + "\u2591" * (10 - int(r["quality_score"] * 10))
        lines.append(f"[{quality_bar}] {r['quality_score']:.2f} | {r['memory_type']} | {r['content'][:120]}")
        if r["group_size"] > 1:
            lines.append(f"  \u21b3 merged {r['group_size']} near-duplicates")

    lines.append(f"\nTo store refined memories, call memory_store for each candidate above.")
    lines.append(f"\nJSON output:\n{json.dumps(refined, ensure_ascii=False, indent=2)}")

    return "\n".join(lines)


# ── Entry point ──────────────────────────────────────────────────

if __name__ == "__main__":
    server.run("stdio")
