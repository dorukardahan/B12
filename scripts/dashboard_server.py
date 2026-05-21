#!/usr/bin/env python3
"""B12 Web Dashboard — Flask backend server.

Serves the B12 memory dashboard UI and provides REST API endpoints for
browsing, searching, editing, and managing memories. Includes SSE for
real-time updates and a Cytoscape.js-compatible graph endpoint.

Usage:
    python3 dashboard_server.py                      # Default port 8742
    python3 dashboard_server.py --port 9000          # Custom port
    python3 dashboard_server.py --db-path /path/db   # Custom DB path

Auth: a random token is generated on startup. API requests require
Authorization: Bearer <token> or the dashboard's same-site token cookie.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import signal
import sqlite3
import secrets
import sys
import threading
import time
from datetime import datetime, timezone

from flask import Flask, request, jsonify, Response, send_file, abort

# ── Path setup ────────────────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
from shared_patterns import content_hash, get_db_path, try_load_sqlite_vec
from b12_pii_scrubber import scrub as scrub_pii

# ── Constants ─────────────────────────────────────────────────────────
DEFAULT_PORT = 8742
DEFAULT_LIMIT = 50
MAX_LIMIT = 200
MAX_GRAPH_NODES = 500
RATE_LIMIT_WINDOW = 60  # seconds
RATE_LIMIT_MAX = 60     # requests per window per endpoint
SSE_POLL_INTERVAL = 2   # seconds
HEX_HASH_RE = re.compile(r"^[0-9a-fA-F]{1,64}$")


# ── Rate limiter ──────────────────────────────────────────────────────

class RateLimiter:
    """Simple in-memory per-endpoint rate limiter."""

    def __init__(self, max_requests: int = RATE_LIMIT_MAX,
                 window: int = RATE_LIMIT_WINDOW):
        self.max_requests = max_requests
        self.window = window
        self._counts: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def check(self, endpoint: str) -> bool:
        """Return True if request is allowed, False if rate-limited."""
        now = time.time()
        cutoff = now - self.window
        with self._lock:
            timestamps = self._counts.get(endpoint, [])
            timestamps = [t for t in timestamps if t > cutoff]
            if len(timestamps) >= self.max_requests:
                self._counts[endpoint] = timestamps
                return False
            timestamps.append(now)
            self._counts[endpoint] = timestamps
            return True


# ── Database helpers ──────────────────────────────────────────────────

def get_ro_connection(db_path: str) -> sqlite3.Connection:
    """Open a read-only SQLite connection with WAL and busy timeout."""
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=10)
    conn.execute("PRAGMA busy_timeout=10000")
    conn.row_factory = sqlite3.Row
    return conn


def get_rw_connection(db_path: str) -> sqlite3.Connection:
    """Open a read-write SQLite connection with WAL and busy timeout."""
    conn = sqlite3.connect(db_path, timeout=10)
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA journal_mode=WAL")
    try_load_sqlite_vec(conn)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def _table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()}
    except sqlite3.Error:
        return set()


def _coerce_limit(raw_value) -> int:
    try:
        limit = int(raw_value)
    except (ValueError, TypeError):
        return DEFAULT_LIMIT
    if limit <= 0:
        return DEFAULT_LIMIT
    return min(limit, MAX_LIMIT)


def _request_auth_token() -> str | None:
    """Read auth from header/cookie; only the index route may bootstrap via query."""
    header = request.headers.get("Authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()

    cookie_token = request.cookies.get("b12_dashboard_token")
    if cookie_token:
        return cookie_token

    if request.endpoint == "index":
        return request.args.get("token")
    return None


def _lookup_memory_id(conn: sqlite3.Connection, memory_id: str):
    """Resolve an exact hash or unique hex prefix to one active memory row."""
    candidate = (memory_id or "").strip().lower()
    if not HEX_HASH_RE.fullmatch(candidate):
        return None, ("Invalid memory id", 400)

    if len(candidate) == 64:
        row = conn.execute(
            "SELECT id, content_hash, content, metadata FROM memories WHERE content_hash = ? AND deleted_at IS NULL",
            (candidate,),
        ).fetchone()
        if not row:
            return None, ("Memory not found", 404)
        return row, None

    rows = conn.execute(
        """
        SELECT id, content_hash, content, metadata FROM memories
        WHERE content_hash >= ?
          AND content_hash < ?
          AND deleted_at IS NULL
        ORDER BY content_hash
        LIMIT 2
        """,
        (candidate, candidate + "\uffff"),
    ).fetchall()
    if not rows:
        return None, ("Memory not found", 404)
    if len(rows) > 1:
        return None, ("Ambiguous memory id prefix", 409)
    return rows[0], None


def _normalize_tags_value(tags):
    if tags is None:
        return None
    if isinstance(tags, str):
        return tags
    if isinstance(tags, (list, tuple)):
        return ",".join(str(tag).strip() for tag in tags if str(tag).strip())
    return str(tags)


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _tag_predicate(column: str) -> str:
    normalized = f"replace(replace(COALESCE({column}, ''), ', ', ','), ' ,', ',')"
    return f"(',' || {normalized} || ',') LIKE ? ESCAPE '\\'"


def _tag_param(tag: str) -> str:
    return f"%,{_escape_like(tag.strip())},%"


def _rewrite_memory_graph_hash(conn: sqlite3.Connection, old_hash: str, new_hash: str) -> None:
    if old_hash == new_hash or not _table_exists(conn, "memory_graph"):
        return
    columns = _table_columns(conn, "memory_graph")
    if not {"source_hash", "target_hash"}.issubset(columns):
        return
    rows = conn.execute(
        "SELECT * FROM memory_graph WHERE source_hash = ? OR target_hash = ?",
        (old_hash, old_hash),
    ).fetchall()
    for row in rows:
        values = dict(row)
        values["source_hash"] = new_hash if values.get("source_hash") == old_hash else values.get("source_hash")
        values["target_hash"] = new_hash if values.get("target_hash") == old_hash else values.get("target_hash")
        names = list(values.keys())
        placeholders = ",".join("?" for _ in names)
        conn.execute(
            f"INSERT OR IGNORE INTO memory_graph ({','.join(names)}) VALUES ({placeholders})",
            [values[name] for name in names],
        )
    conn.execute(
        "DELETE FROM memory_graph WHERE source_hash = ? OR target_hash = ?",
        (old_hash, old_hash),
    )


def _collect_changed_memory_events(
    conn: sqlite3.Connection,
    last_check: int,
    limit: int = 20,
) -> list[dict]:
    rows = conn.execute(
        """
        SELECT content_hash, memory_type,
               substr(content, 1, 80) AS preview,
               created_at, updated_at, last_accessed_at, deleted_at
        FROM memories
        WHERE COALESCE(created_at, 0) >= ?
           OR COALESCE(updated_at, 0) >= ?
           OR COALESCE(last_accessed_at, 0) >= ?
           OR COALESCE(deleted_at, 0) >= ?
        ORDER BY MAX(
            COALESCE(created_at, 0),
            COALESCE(updated_at, 0),
            COALESCE(last_accessed_at, 0),
            COALESCE(deleted_at, 0)
        ) DESC
        LIMIT ?
        """,
        [last_check, last_check, last_check, last_check, limit],
    ).fetchall()

    events = []
    for row in rows:
        created_at = row["created_at"] or 0
        updated_at = row["updated_at"] or 0
        deleted_at = row["deleted_at"] or 0
        if deleted_at >= last_check:
            event_type = "deleted"
            timestamp = deleted_at
        elif created_at >= last_check and created_at >= updated_at:
            event_type = "created"
            timestamp = created_at
        else:
            event_type = "updated"
            timestamp = max(updated_at, row["last_accessed_at"] or 0, created_at)
        events.append({
            "content_hash": row["content_hash"],
            "memory_type": row["memory_type"],
            "preview": (row["preview"] or "").replace("\n", " "),
            "event": event_type,
            "timestamp": timestamp,
        })
    return events


# ── Flask app factory ─────────────────────────────────────────────────

def create_app(db_path: str | None = None, port: int = DEFAULT_PORT,
               token: str | None = None) -> tuple[Flask, str]:
    """Create and configure the Flask application.

    Returns (app, auth_token).
    """
    if db_path is None:
        db_path = get_db_path()

    if not os.path.isfile(db_path):
        print(f"Error: database not found at {db_path}", file=sys.stderr)
        sys.exit(1)

    auth_token = token if token else secrets.token_urlsafe(32)
    rate_limiter = RateLimiter()

    app = Flask(__name__)

    # Resolve dashboard HTML location: ../dashboard/ relative to this script
    dashboard_dir = os.path.join(SCRIPT_DIR, os.pardir, "dashboard")
    dashboard_html = os.path.normpath(
        os.path.join(dashboard_dir, "dashboard.html")
    )

    # ── Auth middleware ────────────────────────────────────────────

    @app.before_request
    def check_auth():
        token = _request_auth_token()
        if token != auth_token:
            endpoint = f"auth:{request.endpoint or request.path}"
            if not rate_limiter.check(endpoint):
                return jsonify({
                    "error": "Rate limited",
                    "message": f"Max {RATE_LIMIT_MAX} requests per {RATE_LIMIT_WINDOW}s"
                }), 429
            return jsonify({"error": "Unauthorized", "message": "Missing or invalid token"}), 401

    # ── Rate limit middleware ─────────────────────────────────────

    @app.before_request
    def check_rate_limit():
        endpoint = request.endpoint or request.path
        if not rate_limiter.check(endpoint):
            return jsonify({
                "error": "Rate limited",
                "message": f"Max {RATE_LIMIT_MAX} requests per {RATE_LIMIT_WINDOW}s"
            }), 429

    # ── Routes ────────────────────────────────────────────────────

    @app.route("/")
    def index():
        """Serve the dashboard HTML."""
        if not os.path.isfile(dashboard_html):
            return jsonify({
                "error": "Dashboard not found",
            }), 404
        response = send_file(dashboard_html, mimetype="text/html")
        initial_token = request.args.get("token")
        if initial_token == auth_token:
            response.set_cookie(
                "b12_dashboard_token",
                auth_token,
                httponly=False,
                samesite="Strict",
            )
        return response

    @app.route("/api/memories", methods=["GET"])
    def list_memories():
        """List or search memories with filtering and pagination."""
        q = request.args.get("q", "").strip()
        mem_type = request.args.get("type", "").strip()
        tags_filter = request.args.get("tags", "").strip()
        before_raw = request.args.get("before", "").strip()
        after_raw = request.args.get("after", "").strip()
        limit = _coerce_limit(request.args.get("limit", DEFAULT_LIMIT))
        try:
            offset = max(int(request.args.get("offset", 0)), 0)
        except (ValueError, TypeError):
            offset = 0
        # Validate before/after as integers (Unix epoch)
        before = ""
        after = ""
        try:
            if before_raw:
                before = int(before_raw)
        except (ValueError, TypeError):
            pass
        try:
            if after_raw:
                after = int(after_raw)
        except (ValueError, TypeError):
            pass

        conn = get_ro_connection(db_path)
        try:
            if q:
                # FTS5 search — sanitize query
                safe_q = _sanitize_fts_query(q)
                if not safe_q:
                    return jsonify({"memories": [], "total": 0, "limit": limit, "offset": offset})

                sql = """
                    SELECT m.content_hash, m.content, m.memory_type, m.tags,
                           m.metadata, m.strength, m.created_at, m.last_accessed_at,
                           m.valid_until, f.rank AS fts_rank
                    FROM memory_fts f
                    JOIN memories m ON m.id = f.rowid
                    WHERE memory_fts MATCH ?
                      AND m.deleted_at IS NULL
                """
                params: list = [safe_q]
                count_sql = """
                    SELECT COUNT(*) FROM memory_fts f
                    JOIN memories m ON m.id = f.rowid
                    WHERE memory_fts MATCH ?
                      AND m.deleted_at IS NULL
                """
                count_params: list = [safe_q]

                if mem_type:
                    sql += " AND m.memory_type = ?"
                    count_sql += " AND m.memory_type = ?"
                    params.append(mem_type)
                    count_params.append(mem_type)
                if tags_filter:
                    sql += f" AND {_tag_predicate('m.tags')}"
                    count_sql += f" AND {_tag_predicate('m.tags')}"
                    tag_param = _tag_param(tags_filter)
                    params.append(tag_param)
                    count_params.append(tag_param)
                if before:
                    sql += " AND m.created_at < ?"
                    count_sql += " AND m.created_at < ?"
                    params.append(before)
                    count_params.append(before)
                if after:
                    sql += " AND m.created_at > ?"
                    count_sql += " AND m.created_at > ?"
                    params.append(after)
                    count_params.append(after)

                sql += " ORDER BY f.rank LIMIT ? OFFSET ?"
                params.extend([limit, offset])

            else:
                # Regular listing
                sql = """
                    SELECT content_hash, content, memory_type, tags,
                           metadata, strength, created_at, last_accessed_at,
                           valid_until, NULL AS fts_rank
                    FROM memories
                    WHERE deleted_at IS NULL
                """
                params = []
                count_sql = "SELECT COUNT(*) FROM memories WHERE deleted_at IS NULL"
                count_params = []

                if mem_type:
                    sql += " AND memory_type = ?"
                    count_sql += " AND memory_type = ?"
                    params.append(mem_type)
                    count_params.append(mem_type)
                if tags_filter:
                    sql += f" AND {_tag_predicate('tags')}"
                    count_sql += f" AND {_tag_predicate('tags')}"
                    tag_param = _tag_param(tags_filter)
                    params.append(tag_param)
                    count_params.append(tag_param)
                if before:
                    sql += " AND created_at < ?"
                    count_sql += " AND created_at < ?"
                    params.append(before)
                    count_params.append(before)
                if after:
                    sql += " AND created_at > ?"
                    count_sql += " AND created_at > ?"
                    params.append(after)
                    count_params.append(after)

                sql += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
                params.extend([limit, offset])

            rows = conn.execute(sql, params).fetchall()
            total = conn.execute(count_sql, count_params).fetchone()[0]

            memories = []
            for row in rows:
                memories.append({
                    "content_hash": row["content_hash"],
                    "content": row["content"],
                    "memory_type": row["memory_type"],
                    "tags": row["tags"],
                    "metadata": _parse_json_safe(row["metadata"]),
                    "strength": row["strength"],
                    "created_at": row["created_at"],
                    "last_accessed_at": row["last_accessed_at"],
                    "valid_until": row["valid_until"],
                    "fts_rank": row["fts_rank"],
                })

            return jsonify({
                "memories": memories,
                "total": total,
                "limit": limit,
                "offset": offset,
            })

        finally:
            conn.close()

    @app.route("/api/memories/<memory_id>", methods=["POST"])
    def edit_memory(memory_id: str):
        """Edit a memory's content, tags, or type."""
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or not data:
            return jsonify({"error": "Invalid JSON body"}), 400

        content = data.get("content")
        tags = data.get("tags")
        memory_type = data.get("memory_type")

        if content is None and tags is None and memory_type is None:
            return jsonify({"error": "No fields to update (provide content, tags, or memory_type)"}), 400

        conn = get_rw_connection(db_path)
        try:
            row, lookup_error = _lookup_memory_id(conn, memory_id)
            if lookup_error:
                message, status = lookup_error
                return jsonify({"error": message}), status

            updates = []
            params = []
            canonical_hash = row["content_hash"]
            hash_changed = False
            content_changed = False
            if content is not None:
                if not isinstance(content, str):
                    return jsonify({"error": "content must be a string"}), 400
                scrubbed_content = scrub_pii(content)
                new_hash = content_hash(scrubbed_content)
                content_changed = scrubbed_content != row["content"]
                if content_changed:
                    collision = conn.execute(
                        "SELECT id FROM memories WHERE content_hash = ? AND id != ? LIMIT 1",
                        (new_hash, row["id"]),
                    ).fetchone()
                    if collision:
                        return jsonify({"error": "Content hash already exists"}), 409
                    updates.append("content = ?")
                    params.append(scrubbed_content)
                    updates.append("content_hash = ?")
                    params.append(new_hash)
                    try:
                        metadata = json.loads(row["metadata"] or "{}")
                    except (json.JSONDecodeError, TypeError):
                        metadata = None
                    if isinstance(metadata, dict):
                        metadata["content_hash"] = new_hash
                        updates.append("metadata = ?")
                        params.append(json.dumps(metadata, ensure_ascii=False))
                    hash_changed = new_hash != row["content_hash"]
                    canonical_hash = new_hash
            if tags is not None:
                if not isinstance(tags, (str, list, tuple)):
                    return jsonify({"error": "tags must be a string or list"}), 400
                updates.append("tags = ?")
                scrubbed_tags = (
                    [scrub_pii(str(tag)) for tag in tags]
                    if isinstance(tags, (list, tuple))
                    else scrub_pii(tags)
                )
                params.append(_normalize_tags_value(scrubbed_tags))
            if memory_type is not None:
                if not isinstance(memory_type, str):
                    return jsonify({"error": "memory_type must be a string"}), 400
                updates.append("memory_type = ?")
                params.append(scrub_pii(memory_type))

            now_epoch = int(time.time())
            now_iso = datetime.fromtimestamp(now_epoch, tz=timezone.utc).isoformat()
            updates.extend(["updated_at = ?", "updated_at_iso = ?"])
            params.extend([now_epoch, now_iso])
            params.extend([row["id"], row["content_hash"]])
            sql = (
                f"UPDATE memories SET {', '.join(updates)} "
                "WHERE id = ? AND content_hash = ? AND deleted_at IS NULL"
            )
            try:
                cur = conn.execute(sql, params)
            except sqlite3.IntegrityError:
                conn.rollback()
                return jsonify({"error": "Memory changed during edit"}), 409
            if cur.rowcount == 0:
                conn.rollback()
                return jsonify({"error": "Memory changed during edit"}), 409
            if content_changed and _table_exists(conn, "memory_embeddings"):
                try:
                    conn.execute("DELETE FROM memory_embeddings WHERE rowid = ?", (row["id"],))
                except sqlite3.Error:
                    conn.rollback()
                    return jsonify({"error": "Could not clear stale embedding state"}), 500
            if hash_changed:
                _rewrite_memory_graph_hash(conn, row["content_hash"], canonical_hash)
            try:
                conn.commit()
            except sqlite3.IntegrityError:
                conn.rollback()
                return jsonify({"error": "Memory changed during edit"}), 409

            return jsonify({"status": "updated", "id": canonical_hash})

        finally:
            conn.close()

    @app.route("/api/memories/<memory_id>", methods=["DELETE"])
    def delete_memory(memory_id: str):
        """Soft-delete a memory by setting deleted_at."""
        conn = get_rw_connection(db_path)
        try:
            row, lookup_error = _lookup_memory_id(conn, memory_id)
            if lookup_error:
                message, status = lookup_error
                return jsonify({"error": message}), status

            now_epoch = int(time.time())
            now_iso = datetime.fromtimestamp(now_epoch, tz=timezone.utc).isoformat()
            cur = conn.execute(
                """
                UPDATE memories
                SET deleted_at = ?, updated_at = ?, updated_at_iso = ?
                WHERE id = ? AND content_hash = ? AND deleted_at IS NULL
                """,
                [now_epoch, now_epoch, now_iso, row["id"], row["content_hash"]]
            )
            if cur.rowcount == 0:
                conn.rollback()
                return jsonify({"error": "Memory changed during delete"}), 409
            conn.commit()

            return jsonify({"status": "deleted", "id": row["content_hash"]})

        finally:
            conn.close()

    @app.route("/api/graph", methods=["GET"])
    def get_graph():
        """Return Cytoscape.js compatible graph JSON."""
        conn = get_ro_connection(db_path)
        try:
            # Get the most recent non-deleted memories as nodes (limit MAX_GRAPH_NODES)
            node_rows = conn.execute("""
                SELECT content_hash, substr(content, 1, 60) AS label,
                       memory_type, strength, created_at, tags
                FROM memories
                WHERE deleted_at IS NULL
                ORDER BY created_at DESC
                LIMIT ?
            """, [MAX_GRAPH_NODES]).fetchall()

            if not node_rows:
                return jsonify({"nodes": [], "edges": []})

            # Build hash set for filtering edges
            hash_set = {row["content_hash"] for row in node_rows}

            nodes = []
            for row in node_rows:
                label = row["label"] or ""
                label = label.replace("\n", " ").replace("\r", "")
                nodes.append({
                    "data": {
                        "id": row["content_hash"],
                        "label": label,
                        "type": row["memory_type"],
                        "strength": row["strength"],
                        "created": row["created_at"],
                        "tags": row["tags"],
                    }
                })

            if not _table_exists(conn, "memory_graph"):
                return jsonify({"nodes": nodes, "edges": []})

            placeholders = ",".join("?" for _ in hash_set)
            # Get edges between the capped memory node set only.
            edge_rows = conn.execute(f"""
                SELECT source_hash, target_hash, relationship_type, similarity
                FROM memory_graph
                WHERE source_hash IN ({placeholders})
                  AND target_hash IN ({placeholders})
            """, list(hash_set) + list(hash_set)).fetchall()

            edges = []
            for row in edge_rows:
                if row["source_hash"] in hash_set and row["target_hash"] in hash_set:
                    edges.append({
                        "data": {
                            "source": row["source_hash"],
                            "target": row["target_hash"],
                            "type": row["relationship_type"] or "related",
                            "weight": row["similarity"] if row["similarity"] is not None else 0.5,
                        }
                    })

            return jsonify({"nodes": nodes, "edges": edges})

        finally:
            conn.close()

    @app.route("/api/stats", methods=["GET"])
    def get_stats():
        """Return comprehensive memory statistics."""
        conn = get_ro_connection(db_path)
        try:
            # Counts by type
            type_rows = conn.execute("""
                SELECT COALESCE(memory_type, 'unknown') AS mtype, COUNT(*) AS cnt
                FROM memories WHERE deleted_at IS NULL
                GROUP BY mtype ORDER BY cnt DESC
            """).fetchall()
            types = {row["mtype"]: row["cnt"] for row in type_rows}

            # Total counts
            total_active = conn.execute(
                "SELECT COUNT(*) FROM memories WHERE deleted_at IS NULL"
            ).fetchone()[0]
            total_deleted = conn.execute(
                "SELECT COUNT(*) FROM memories WHERE deleted_at IS NOT NULL"
            ).fetchone()[0]
            if _table_exists(conn, "memory_graph"):
                if {"source_hash", "target_hash"}.issubset(_table_columns(conn, "memory_graph")):
                    total_graph_edges = conn.execute(
                        """
                        SELECT COUNT(*)
                        FROM memory_graph g
                        JOIN memories src ON src.content_hash = g.source_hash AND src.deleted_at IS NULL
                        JOIN memories dst ON dst.content_hash = g.target_hash AND dst.deleted_at IS NULL
                        """
                    ).fetchone()[0]
                else:
                    total_graph_edges = 0
            else:
                total_graph_edges = 0

            # Strength distribution (10 bins from 0-5)
            strength_rows = conn.execute("""
                SELECT strength FROM memories WHERE deleted_at IS NULL
            """).fetchall()
            bins = [0] * 10  # bins: 0-0.5, 0.5-1.0, ..., 4.5-5.0
            for row in strength_rows:
                try:
                    s = 1.0 if row["strength"] is None else float(row["strength"])
                except (TypeError, ValueError):
                    s = 1.0
                idx = min(max(int(s / 0.5), 0), 9)
                bins[idx] += 1
            strength_histogram = []
            for i in range(10):
                lo = i * 0.5
                hi = lo + 0.5
                strength_histogram.append({
                    "range": f"{lo:.1f}-{hi:.1f}",
                    "count": bins[i],
                })

            # Tag cloud
            tag_rows = conn.execute("""
                SELECT tags FROM memories
                WHERE deleted_at IS NULL AND tags IS NOT NULL AND tags != ''
            """).fetchall()
            tag_counts: dict[str, int] = {}
            for row in tag_rows:
                for tag in row["tags"].split(","):
                    tag = tag.strip()
                    if tag:
                        tag_counts[tag] = tag_counts.get(tag, 0) + 1
            tag_cloud = sorted(
                [{"tag": t, "count": c} for t, c in tag_counts.items()],
                key=lambda x: x["count"], reverse=True
            )

            # Recent activity (last 7 days by day)
            now_epoch = int(time.time())
            seven_days_ago = now_epoch - 7 * 86400
            activity_rows = conn.execute("""
                SELECT date(created_at, 'unixepoch', 'localtime') AS day,
                       COUNT(*) AS cnt
                FROM memories
                WHERE deleted_at IS NULL AND created_at >= ?
                GROUP BY day ORDER BY day ASC
            """, [seven_days_ago]).fetchall()
            recent_activity = [
                {"date": row["day"], "count": row["cnt"]}
                for row in activity_rows
            ]

            return jsonify({
                "types": types,
                "total_active": total_active,
                "total_deleted": total_deleted,
                "total_graph_edges": total_graph_edges,
                "strength_histogram": strength_histogram,
                "tag_cloud": tag_cloud,
                "recent_activity": recent_activity,
            })

        finally:
            conn.close()

    @app.route("/api/consolidation", methods=["POST"])
    def trigger_consolidation():
        """Trigger smart memory consolidation."""
        try:
            from consolidation_engine import main as consolidation_main
            # Run in dry-run mode first — actual consolidation
            # should be explicit
            result = {"status": "triggered", "message": "Consolidation engine imported successfully. Run manually with --dry-run for safety."}
            return jsonify(result)
        except ImportError:
            return jsonify({
                "status": "unavailable",
                "message": "consolidation_engine module not found. Ensure it exists in the scripts/ directory."
            }), 501
        except Exception as e:
            return jsonify({
                "status": "error",
                "message": str(e)
            }), 500

    @app.route("/events", methods=["GET"])
    def sse_stream():
        """SSE endpoint: poll DB every 2 seconds for new/modified memories."""
        def generate():
            last_check = int(time.time())
            while True:
                time.sleep(SSE_POLL_INTERVAL)
                try:
                    conn = get_ro_connection(db_path)
                    try:
                        events = _collect_changed_memory_events(conn, last_check, 20)
                        if events:
                            data = json.dumps({"events": events})
                            yield f"data: {data}\n\n"

                        last_check = int(time.time())
                    finally:
                        conn.close()
                except Exception:
                    # DB might be locked or unavailable, skip this tick
                    pass

        return Response(
            generate(),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    # ── Static assets from dashboard directory ────────────────────

    @app.route("/dashboard/<path:filename>")
    def serve_dashboard_asset(filename: str):
        """Serve static assets from the dashboard directory."""
        dashboard_root = os.path.realpath(dashboard_dir)
        filepath = os.path.realpath(os.path.join(dashboard_root, filename))
        # Prevent directory traversal
        if os.path.commonpath([dashboard_root, filepath]) != dashboard_root:
            abort(403)
        if not os.path.isfile(filepath):
            abort(404)
        return send_file(filepath)

    return app, auth_token


# ── Helpers ───────────────────────────────────────────────────────────

def _sanitize_fts_query(query: str) -> str:
    """Sanitize user input for FTS5 MATCH, building OR-joined quoted terms."""
    # Remove SQL-dangerous chars and FTS5 operators
    safe = query
    for ch in "'\";\\/(){}*^:":
        safe = safe.replace(ch, "")
    safe = safe.replace("--", "")

    # Remove FTS5 boolean operators
    import re
    safe = re.sub(r'\b(AND|OR|NOT|NEAR)\b', '', safe, flags=re.IGNORECASE)

    # Split into words, filter short ones, quote and OR-join
    words = [w.strip() for w in safe.split() if len(w.strip()) > 1]
    if not words:
        return ""
    return " OR ".join(f'"{w}"' for w in words)


def _parse_json_safe(value) -> dict | list | str | None:
    """Parse a JSON string, returning the raw string on failure."""
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return value


# ── Graceful shutdown ─────────────────────────────────────────────────

_shutdown_event = threading.Event()


def _signal_handler(signum, frame):
    """Handle SIGINT/SIGTERM for graceful shutdown."""
    print(f"\nReceived signal {signum}, shutting down...")
    _shutdown_event.set()
    # Flask's dev server will exit on KeyboardInterrupt
    raise SystemExit(0)


# ── Main ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="B12 Web Dashboard Server"
    )
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT,
        help=f"Port to bind to (default: {DEFAULT_PORT})"
    )
    parser.add_argument(
        "--db-path", type=str, default=None,
        help="Path to SQLite database (default: auto-detect)"
    )
    parser.add_argument(
        "--token", type=str, default=None,
        help="Auth token (auto-generated if not provided)"
    )
    args = parser.parse_args()

    app, auth_token = create_app(db_path=args.db_path, port=args.port,
                                  token=args.token)

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    url = f"http://127.0.0.1:{args.port}?token={auth_token}"
    print(f"Dashboard running at {url}")
    print(f"DB: {args.db_path or get_db_path()}")
    print("Press Ctrl+C to stop.\n")

    app.run(
        host="127.0.0.1",
        port=args.port,
        debug=False,
        threaded=True,
    )


if __name__ == "__main__":
    main()
