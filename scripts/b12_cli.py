#!/usr/bin/env python3
"""
B12 CLI — direct terminal access to B12 memory system.

No MCP overhead, no LLM needed. Direct SQLite queries.

Usage:
    b12 search "authentication decisions"
    b12 search "redis" --project myapp --limit 10
    b12 store "Redis chosen for caching because of pub/sub"
    b12 store "Bug fix: connection pool leak" --type error --importance 1.5
    b12 status
    b12 export --project myapp
    b12 import backup.b12
"""

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone

# Add scripts dir to path
_script_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _script_dir)

from shared_patterns import DB_PATH, content_hash, validate_metadata


def get_db(readonly=False):
    """Get a SQLite connection with standard B12 settings."""
    if not os.path.exists(DB_PATH):
        print(f"Error: B12 database not found at {DB_PATH}", file=sys.stderr)
        print("Run install.sh --full first, then start a Claude Code session.", file=sys.stderr)
        sys.exit(1)

    uri = f"file:{DB_PATH}?mode=ro" if readonly else DB_PATH
    conn = sqlite3.connect(uri if readonly else DB_PATH, timeout=10, uri=readonly)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.row_factory = sqlite3.Row
    return conn


def cmd_search(args):
    """Search memories using FTS5 hybrid search."""
    conn = get_db(readonly=True)

    query = args.query
    limit = args.limit or 10

    # Build FTS query (simple tokenization)
    words = query.split()
    fts_query = " OR ".join(f'"{w}"' for w in words if len(w) > 1)

    if not fts_query:
        print("Query too short. Provide at least one keyword.", file=sys.stderr)
        sys.exit(1)

    # Tag filter
    tag_filter = ""
    if args.project:
        tag_filter = f"AND m.tags LIKE '%proj:{args.project}%'"

    try:
        rows = conn.execute(f"""
            SELECT m.id, m.content, m.memory_type, m.tags, m.strength,
                   m.created_at, m.updated_at,
                   CASE WHEN json_valid(m.metadata) THEN json_extract(m.metadata, '$.importance_score') ELSE NULL END as importance,
                   (1.0 / (1.0 + abs(f.rank))) as relevance
            FROM memories m
            JOIN memory_fts f ON m.id = f.rowid
            WHERE f.memory_fts MATCH ?
              AND m.deleted_at IS NULL
              {tag_filter}
            ORDER BY relevance DESC
            LIMIT ?
        """, (fts_query, limit)).fetchall()
    except sqlite3.OperationalError as e:
        # Fallback to LIKE search if FTS fails
        like_pattern = f"%{query}%"
        rows = conn.execute(f"""
            SELECT id, content, memory_type, tags, strength,
                   created_at, updated_at,
                   CASE WHEN json_valid(metadata) THEN json_extract(metadata, '$.importance_score') ELSE NULL END as importance,
                   1.0 as relevance
            FROM memories
            WHERE content LIKE ?
              AND deleted_at IS NULL
              {tag_filter}
            ORDER BY updated_at DESC
            LIMIT ?
        """, (like_pattern, limit)).fetchall()

    conn.close()

    if not rows:
        print(f"No memories found for: {query}")
        return

    print(f"\n  {len(rows)} memories found for: {query}\n")
    for row in rows:
        content = row["content"][:200].replace("\n", " ")
        mem_type = row["memory_type"] or "note"
        created = str(row["created_at"])[:10] if row["created_at"] else "?"
        importance = row["importance"] or 1.0
        tags = row["tags"] or ""
        strength = row["strength"] or 1.0

        print(f"  [{mem_type}] {content}")
        print(f"    id:{row['id']}  created:{created}  strength:{strength:.1f}  importance:{importance}")
        if tags:
            print(f"    tags: {tags}")
        print()


def cmd_store(args):
    """Store a new memory."""
    conn = get_db()

    content = args.content
    mem_type = args.type or "note"
    importance = args.importance or 1.0
    project = args.project or os.path.basename(os.getcwd())

    # Build tags
    tags = f"proj:{project},type:{mem_type},source:cli"
    if args.tags:
        tags += f",{args.tags}"

    # Build metadata
    meta = validate_metadata({
        "type": mem_type,
        "importance_score": importance,
        "project": project,
        "source": "cli",
        "content_hash": content_hash(content),
    })

    # Prefix content with type label
    if not content.startswith("["):
        content = f"[{mem_type.title()}] {content}"

    try:
        conn.execute(
            """INSERT INTO memories (content, metadata, tags, memory_type, created_at, updated_at, strength)
               VALUES (?, ?, ?, ?, datetime('now'), datetime('now'), 1.0)""",
            (content, meta, tags, mem_type)
        )
        conn.commit()
        mem_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        print(f"Stored memory #{mem_id}: {content[:80]}")
    except sqlite3.IntegrityError:
        print("Memory already exists (duplicate content hash).", file=sys.stderr)

    conn.close()


def cmd_status(args):
    """Show B12 system health status."""
    conn = get_db(readonly=True)

    total = conn.execute("SELECT COUNT(*) FROM memories WHERE deleted_at IS NULL").fetchone()[0]
    archived = conn.execute("SELECT COUNT(*) FROM memories WHERE deleted_at IS NOT NULL").fetchone()[0]

    # Projects
    projects = conn.execute("""
        SELECT DISTINCT
            CASE WHEN json_valid(metadata) THEN json_extract(metadata, '$.project') ELSE NULL END as proj
        FROM memories WHERE deleted_at IS NULL AND proj IS NOT NULL
        ORDER BY proj
    """).fetchall()
    project_list = [r[0] for r in projects if r[0]]

    # DB size
    db_size = os.path.getsize(DB_PATH) / (1024 * 1024)

    # Embeddings (stored in separate vec table)
    try:
        embedded = conn.execute(
            "SELECT COUNT(*) FROM memory_embeddings"
        ).fetchone()[0]
    except sqlite3.OperationalError:
        embedded = 0

    # Last stored
    last = conn.execute(
        "SELECT created_at FROM memories WHERE deleted_at IS NULL ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    last_stored = str(last[0])[:19] if last else "never"

    # Strength stats
    avg_strength = conn.execute(
        "SELECT AVG(COALESCE(strength, 1.0)) FROM memories WHERE deleted_at IS NULL"
    ).fetchone()[0] or 1.0

    conn.close()

    embed_pct = (embedded / total * 100) if total > 0 else 0

    print(f"""
  B12 Memory Status
  ├─ Memories: {total} active, {archived} archived
  ├─ Projects: {', '.join(project_list[:10]) if project_list else 'none'}
  ├─ DB size: {db_size:.1f} MB
  ├─ Avg strength: {avg_strength:.2f}
  ├─ Last stored: {last_stored}
  ├─ Embeddings: {embedded}/{total} ({embed_pct:.0f}%)
  └─ DB path: {DB_PATH}
""")


def cmd_export(args):
    """Export memories as JSON."""
    conn = get_db(readonly=True)

    query = "SELECT * FROM memories WHERE deleted_at IS NULL"
    params = []
    if args.project:
        query += " AND tags LIKE ?"
        params.append(f"%proj:{args.project}%")

    rows = conn.execute(query, params).fetchall()
    conn.close()

    memories = []
    for row in rows:
        mem = dict(row)
        # Remove binary embedding from export
        mem.pop("embedding", None)
        memories.append(mem)

    output = args.output or f"b12-export-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    with open(output, "w") as f:
        json.dump(memories, f, indent=2, default=str)

    print(f"Exported {len(memories)} memories to {output}")


def cmd_import(args):
    """Import memories from JSON."""
    if not os.path.exists(args.file):
        print(f"File not found: {args.file}", file=sys.stderr)
        sys.exit(1)

    with open(args.file) as f:
        memories = json.load(f)

    conn = get_db()
    imported = 0
    skipped = 0

    for mem in memories:
        content = mem.get("content", "")
        if not content:
            continue

        ch = content_hash(content)
        existing = conn.execute(
            "SELECT id FROM memories WHERE content_hash = ?", (ch,)
        ).fetchone()

        if existing:
            skipped += 1
            continue

        conn.execute(
            """INSERT INTO memories (content, metadata, tags, memory_type, content_hash,
               created_at, updated_at, strength)
               VALUES (?, ?, ?, ?, ?, ?, datetime('now'), 1.0)""",
            (content, mem.get("metadata", "{}"), mem.get("tags", ""),
             mem.get("memory_type", "note"), ch,
             mem.get("created_at", datetime.now(timezone.utc).isoformat()))
        )
        imported += 1

    conn.commit()
    conn.close()
    print(f"Imported {imported} memories, skipped {skipped} duplicates")


def main():
    parser = argparse.ArgumentParser(
        prog="b12",
        description="B12 — persistent memory for AI coding assistants"
    )
    sub = parser.add_subparsers(dest="command", help="Available commands")

    # search
    p_search = sub.add_parser("search", help="Search memories")
    p_search.add_argument("query", help="Search query")
    p_search.add_argument("--project", "-p", help="Filter by project")
    p_search.add_argument("--limit", "-l", type=int, default=10, help="Max results")

    # store
    p_store = sub.add_parser("store", help="Store a memory")
    p_store.add_argument("content", help="Memory content")
    p_store.add_argument("--type", "-t", default="note",
                         choices=["decision", "error", "learning", "preference", "note", "progress"])
    p_store.add_argument("--importance", "-i", type=float, default=1.0, help="Importance score (0.7-2.0)")
    p_store.add_argument("--project", "-p", help="Project name (default: current dir)")
    p_store.add_argument("--tags", help="Additional tags (comma-separated)")

    # status
    sub.add_parser("status", help="Show system health")

    # export
    p_export = sub.add_parser("export", help="Export memories as JSON")
    p_export.add_argument("--project", "-p", help="Filter by project")
    p_export.add_argument("--output", "-o", help="Output file path")

    # import
    p_import = sub.add_parser("import", help="Import memories from JSON")
    p_import.add_argument("file", help="JSON file to import")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    commands = {
        "search": cmd_search,
        "store": cmd_store,
        "status": cmd_status,
        "export": cmd_export,
        "import": cmd_import,
    }

    commands[args.command](args)


if __name__ == "__main__":
    main()
