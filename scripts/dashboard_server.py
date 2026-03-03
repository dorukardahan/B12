#!/usr/bin/env python3
"""B12 Web Dashboard — Flask backend server.

Serves the B12 memory dashboard UI and provides REST API endpoints for
browsing, searching, editing, and managing memories. Includes SSE for
real-time updates and a Cytoscape.js-compatible graph endpoint.

Usage:
    python3 dashboard_server.py                      # Default port 8742
    python3 dashboard_server.py --port 9000          # Custom port
    python3 dashboard_server.py --db-path /path/db   # Custom DB path

Auth: A random token is generated on startup. All requests require ?token=xxx.
"""

from __future__ import annotations

import argparse
import json
import math
import os
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
from shared_patterns import get_db_path

# ── Constants ─────────────────────────────────────────────────────────
DEFAULT_PORT = 8742
DEFAULT_LIMIT = 50
MAX_LIMIT = 200
MAX_GRAPH_NODES = 500
RATE_LIMIT_WINDOW = 60  # seconds
RATE_LIMIT_MAX = 60     # requests per window per endpoint
SSE_POLL_INTERVAL = 2   # seconds


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
    conn.row_factory = sqlite3.Row
    return conn


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
        token = request.args.get("token")
        if token != auth_token:
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
                "message": f"Expected at {dashboard_html}"
            }), 404
        return send_file(dashboard_html, mimetype="text/html")

    @app.route("/api/memories", methods=["GET"])
    def list_memories():
        """List or search memories with filtering and pagination."""
        q = request.args.get("q", "").strip()
        mem_type = request.args.get("type", "").strip()
        tags_filter = request.args.get("tags", "").strip()
        before_raw = request.args.get("before", "").strip()
        after_raw = request.args.get("after", "").strip()
        try:
            limit = min(int(request.args.get("limit", DEFAULT_LIMIT)), MAX_LIMIT)
        except (ValueError, TypeError):
            limit = DEFAULT_LIMIT
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
                    sql += " AND m.tags LIKE ?"
                    count_sql += " AND m.tags LIKE ?"
                    tag_like = f"%{tags_filter}%"
                    params.append(tag_like)
                    count_params.append(tag_like)
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
                    sql += " AND tags LIKE ?"
                    count_sql += " AND tags LIKE ?"
                    tag_like = f"%{tags_filter}%"
                    params.append(tag_like)
                    count_params.append(tag_like)
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
        if not data:
            return jsonify({"error": "Invalid JSON body"}), 400

        content = data.get("content")
        tags = data.get("tags")
        memory_type = data.get("memory_type")

        if content is None and tags is None and memory_type is None:
            return jsonify({"error": "No fields to update (provide content, tags, or memory_type)"}), 400

        conn = get_rw_connection(db_path)
        try:
            # Verify the memory exists
            row = conn.execute(
                "SELECT id FROM memories WHERE content_hash LIKE ? AND deleted_at IS NULL",
                [f"{memory_id}%"]
            ).fetchone()

            if not row:
                return jsonify({"error": "Memory not found"}), 404

            updates = []
            params = []
            if content is not None:
                updates.append("content = ?")
                params.append(content)
            if tags is not None:
                updates.append("tags = ?")
                params.append(tags)
            if memory_type is not None:
                updates.append("memory_type = ?")
                params.append(memory_type)

            params.append(row["id"])
            sql = f"UPDATE memories SET {', '.join(updates)} WHERE id = ?"
            conn.execute(sql, params)
            conn.commit()

            return jsonify({"status": "updated", "id": memory_id})

        finally:
            conn.close()

    @app.route("/api/memories/<memory_id>", methods=["DELETE"])
    def delete_memory(memory_id: str):
        """Soft-delete a memory by setting deleted_at."""
        conn = get_rw_connection(db_path)
        try:
            row = conn.execute(
                "SELECT id FROM memories WHERE content_hash LIKE ? AND deleted_at IS NULL",
                [f"{memory_id}%"]
            ).fetchone()

            if not row:
                return jsonify({"error": "Memory not found"}), 404

            conn.execute(
                "UPDATE memories SET deleted_at = CAST(strftime('%s', 'now') AS INTEGER) WHERE id = ?",
                [row["id"]]
            )
            conn.commit()

            return jsonify({"status": "deleted", "id": memory_id})

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

            # Get edges between these memories
            edge_rows = conn.execute("""
                SELECT source_hash, target_hash, relationship_type, similarity
                FROM memory_graph
            """).fetchall()

            edges = []
            for row in edge_rows:
                if row["source_hash"] in hash_set and row["target_hash"] in hash_set:
                    edges.append({
                        "data": {
                            "source": row["source_hash"],
                            "target": row["target_hash"],
                            "type": row["relationship_type"] or "related",
                            "weight": row["similarity"] or 0.5,
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
            total_graph_edges = conn.execute(
                "SELECT COUNT(*) FROM memory_graph"
            ).fetchone()[0]

            # Strength distribution (10 bins from 0-5)
            strength_rows = conn.execute("""
                SELECT strength FROM memories WHERE deleted_at IS NULL
            """).fetchall()
            bins = [0] * 10  # bins: 0-0.5, 0.5-1.0, ..., 4.5-5.0
            for row in strength_rows:
                s = row["strength"] or 0
                idx = min(int(s / 0.5), 9)
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
                        # Check for memories created or modified since last check
                        rows = conn.execute("""
                            SELECT content_hash, memory_type,
                                   substr(content, 1, 80) AS preview,
                                   created_at, last_accessed_at, deleted_at
                            FROM memories
                            WHERE deleted_at IS NULL
                              AND (created_at >= ? OR last_accessed_at >= ?)
                            ORDER BY created_at DESC
                            LIMIT 20
                        """, [last_check, last_check]).fetchall()

                        if rows:
                            events = []
                            for row in rows:
                                event_type = "deleted" if row["deleted_at"] else "updated"
                                if row["created_at"] >= last_check:
                                    event_type = "created"
                                events.append({
                                    "content_hash": row["content_hash"],
                                    "memory_type": row["memory_type"],
                                    "preview": (row["preview"] or "").replace("\n", " "),
                                    "event": event_type,
                                    "timestamp": row["created_at"],
                                })
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
        filepath = os.path.normpath(os.path.join(dashboard_dir, filename))
        # Prevent directory traversal
        if not filepath.startswith(os.path.normpath(dashboard_dir)):
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
