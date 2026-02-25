#!/usr/bin/env python3
"""
B12 Mini MCP Server — minimal memory CRUD with daemon-delegated ML.

Replaces the 804MB mcp-memory-service with 4 tools, zero ML deps.
All embedding/search ops delegated to embed_daemon via Unix socket.
"""

import base64, hashlib, json, os, socket, sqlite3, time
try:
    import sqlite_vec
    _HAS_VEC = True
except ImportError:
    _HAS_VEC = False
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from mcp.server.fastmcp import FastMCP

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
        "SELECT name FROM sqlite_master WHERE type='trigger' AND sql LIKE '%memory_fts%'"
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
    _db = sqlite3.connect(DB_PATH, timeout=10)
    _db.execute("PRAGMA journal_mode=WAL")
    _db.execute("PRAGMA busy_timeout=5000")
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


def _fmt_memory(row, score=None) -> str:
    p = [f"[{row['memory_type'] or 'general'}] {row['content'][:500]}"]
    if row["tags"]: p.append(f"  Tags: {row['tags']}")
    p.append(f"  Hash: {row['content_hash'][:16]}...  Created: {row['created_at_iso'] or '?'}")
    if score is not None: p.append(f"  Score: {score:.3f}")
    return "\n".join(p)


# ── Tool: memory_store ───────────────────────────────────────────

@server.tool()
def memory_store(content: str, metadata: dict | None = None) -> str:
    """Store a new memory with optional metadata, tags, and type."""
    db = _require_db()
    metadata = metadata or {}
    tags_raw = metadata.pop("tags", None)
    memory_type = metadata.pop("type", metadata.pop("memory_type", "general"))
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

    db.execute(
        """INSERT OR IGNORE INTO memories
           (content_hash, content, tags, memory_type, metadata,
            strength, created_at, created_at_iso, updated_at, updated_at_iso)
           VALUES (?, ?, ?, ?, ?, 1.0, ?, ?, ?, ?)""",
        (content_hash, content, tags, memory_type, meta_json,
         now_ts, now_iso, now_ts, now_iso),
    )
    db.commit()

    # Get the row id for embedding insertion
    row = db.execute(
        "SELECT id FROM memories WHERE content_hash = ?", (content_hash,)
    ).fetchone()
    if not row:
        return f"Stored (hash: {content_hash[:16]}) but could not retrieve ID"
    mem_id = row["id"]

    # Embed via daemon (graceful degradation)
    resp = daemon_request("encode_batch", texts=[content])
    if resp and resp.get("embeddings"):
        emb_bytes = base64.b64decode(resp["embeddings"][0])
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
def memory_search(
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
    wheres = ["m.deleted_at IS NULL"]
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

    # ── Exact substring search ─────────────────────────────
    if mode == "exact" and query:
        exact_rows = db.execute(
            f"""SELECT * FROM memories m
                WHERE m.content LIKE ? AND {where_sql}
                ORDER BY m.created_at DESC LIMIT ?""",
            [f"%{query}%"] + params + [limit],
        ).fetchall()
        for r in exact_rows:
            results[r["content_hash"]] = (r, 0.9)

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
                    # Phrase match scores higher than OR match
                    bonus = 0.1 if fts_attempt == "phrase" else 0.0
                    score = 1.0 - min(abs(r["rank"]) / 20.0, 0.9) + bonus
                    if h not in results or results[h][1] < score:
                        results[h] = (r, score)
            except Exception:
                continue  # FTS match syntax error, try next

    # ── Semantic search via daemon ───────────────────────────
    if mode in ("semantic", "hybrid") and query:
        resp = daemon_request(
            "semantic_search", query=query, db_path=DB_PATH, limit=limit * 2
        )
        if resp and resp.get("results"):
            for hit in resp["results"]:
                row = db.execute(
                    f"SELECT * FROM memories m WHERE m.id = ? AND {where_sql}",
                    [hit["id"]] + params,
                ).fetchone()
                if row:
                    ch = row["content_hash"]
                    old_score = results.get(ch, (None, 0))[1]
                    new_score = hit["score"]
                    # In hybrid, boost items found by both methods
                    combined = max(old_score, new_score)
                    if ch in results:
                        combined = min(1.0, old_score + new_score * 0.3)
                    results[ch] = (row, combined)

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
def memory_update(
    content_hash: str,
    updates: dict,
) -> str:
    """Update metadata, tags, or type of an existing memory. Content and hash are immutable."""
    db = _require_db()
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
        existing = json.loads(row["metadata"] or "{}")
        existing.update(updates["metadata"])
        sets.append("metadata = ?")
        vals.append(json.dumps(existing, ensure_ascii=False))
    if "strength" in updates:
        sets.append("strength = ?")
        vals.append(float(updates["strength"]))

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
def memory_quality(
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
        row = db.execute(
            "SELECT metadata FROM memories WHERE content_hash = ? AND deleted_at IS NULL",
            (content_hash,),
        ).fetchone()
        if not row:
            return f"Memory not found: {content_hash[:16]}"

        meta = json.loads(row["metadata"] or "{}")
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
        row = db.execute(
            "SELECT content, metadata, tags FROM memories WHERE content_hash = ? AND deleted_at IS NULL",
            (content_hash,),
        ).fetchone()
        if not row:
            return f"Memory not found: {content_hash[:16]}"
        meta = json.loads(row["metadata"] or "{}")
        return (f"Quality: {meta.get('quality_score', 'N/A')}\n"
                f"Provider: {meta.get('quality_provider', 'N/A')}\n"
                f"Content: {row['content'][:200]}\n"
                f"Tags: {row['tags'] or 'none'}")

    elif action == "analyze":
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
        lines = [
            f"Total memories: {stats['total']}",
            f"Quality — avg: {(stats['avg_q'] or 0):.3f}, min: {(stats['min_q'] or 0):.3f}, max: {(stats['max_q'] or 0):.3f}",
            "By type:",
        ]
        for tc in type_counts:
            lines.append(f"  {tc['memory_type'] or 'none'}: {tc['cnt']}")
        return "\n".join(lines)

    return f"Unknown action: {action}. Use rate, get, or analyze."


# ── Entry point ──────────────────────────────────────────────────

if __name__ == "__main__":
    server.run("stdio")
