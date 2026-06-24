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

from shared_patterns import DB_PATH, content_hash, count_active_embeddings, validate_metadata
from b12_pii_scrubber import scrub as scrub_pii

DEFAULT_LIMIT = 10
MAX_LIMIT = 100


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _tag_predicate(column: str = "tags") -> str:
    normalized = f"replace(replace(COALESCE({column}, ''), ', ', ','), ' ,', ',')"
    return f"(',' || {normalized} || ',') LIKE ? ESCAPE '\\'"


def _tag_param(tag: str) -> str:
    return f"%,{_escape_like(tag.strip())},%"


def _coerce_limit(value) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError):
        return DEFAULT_LIMIT
    if limit <= 0:
        return DEFAULT_LIMIT
    return min(limit, MAX_LIMIT)


def _scrub_json_value(value):
    if isinstance(value, str):
        return scrub_pii(value)
    if isinstance(value, list):
        return [_scrub_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _scrub_json_value(item) for key, item in value.items()}
    return value


def _safe_import_metadata(raw_metadata, content_hash_value: str) -> str:
    try:
        parsed = json.loads(validate_metadata(raw_metadata))
    except (json.JSONDecodeError, TypeError, ValueError):
        parsed = {}
    if not isinstance(parsed, dict):
        parsed = {}
    parsed = _scrub_json_value(parsed)
    parsed["content_hash"] = content_hash_value
    return validate_metadata(parsed)


def _safe_import_tags(raw_tags) -> str:
    if isinstance(raw_tags, (list, tuple)):
        tags = [scrub_pii(str(tag).strip()) for tag in raw_tags if str(tag).strip()]
        return ",".join(tags)
    return scrub_pii(str(raw_tags or ""))


def get_db(readonly=False):
    """Get a SQLite connection with standard B12 settings."""
    if not os.path.exists(DB_PATH):
        print(f"Error: B12 database not found at {DB_PATH}", file=sys.stderr)
        print("Run install.sh --full first, then start a Claude Code session.", file=sys.stderr)
        sys.exit(1)

    uri = f"file:{DB_PATH}?mode=ro" if readonly else DB_PATH
    conn = sqlite3.connect(uri if readonly else DB_PATH, timeout=10, uri=readonly)
    if not readonly:
        conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.row_factory = sqlite3.Row
    return conn


def cmd_search(args):
    """Search memories using FTS5 hybrid search."""
    conn = get_db(readonly=True)

    query = args.query
    limit = _coerce_limit(args.limit)

    # Build FTS query (simple tokenization)
    words = query.split()
    fts_query = " OR ".join(f'"{w}"' for w in words if len(w) > 1)

    if not fts_query:
        print("Query too short. Provide at least one keyword.", file=sys.stderr)
        sys.exit(1)

    # Tag filter
    aliased_tag_filter = ""
    plain_tag_filter = ""
    tag_params = []
    if args.project:
        aliased_tag_filter = f"AND {_tag_predicate('m.tags')}"
        plain_tag_filter = f"AND {_tag_predicate('tags')}"
        tag_params.append(_tag_param(f"proj:{args.project}"))

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
              {aliased_tag_filter}
            ORDER BY relevance DESC
            LIMIT ?
        """, (fts_query, *tag_params, limit)).fetchall()
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
              {plain_tag_filter}
            ORDER BY updated_at DESC
            LIMIT ?
        """, (like_pattern, *tag_params, limit)).fetchall()

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

    mem_type = args.type or "note"
    importance = args.importance or 1.0
    project = args.project or os.path.basename(os.getcwd())

    content = args.content
    if not content.startswith("["):
        content = f"[{mem_type.title()}] {content}"
    content = scrub_pii(content)
    ch = content_hash(content)

    # Resolve importance through the shared chokepoint: secret-cap + memory_type
    # floor + the strongest of the supplied --importance and the content score.
    try:
        import b12_importance as _b12imp
        importance = _b12imp.finalize_importance(content, args.importance, mem_type)
    except Exception:
        # Scorer unavailable: keep the supplied/default importance (prior behavior).
        # Content is already PII-scrubbed above, so no raw secret leaks on this path.
        pass

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
        "content_hash": ch,
    })

    try:
        now_epoch = time.time()
        now_iso = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """INSERT INTO memories
               (content, metadata, tags, memory_type, content_hash,
                created_at, updated_at, created_at_iso, updated_at_iso,
                strength, deleted_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1.0, NULL)""",
            (content, meta, tags, mem_type, ch, now_epoch, now_epoch, now_iso, now_iso)
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
    embedded, embedding_warning = count_active_embeddings(conn)

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

    if embedded is None:
        embedding_line = f"unknown ({embedding_warning})"
    else:
        embed_pct = (embedded / total * 100) if total > 0 else 0
        embedding_line = f"{embedded}/{total} ({embed_pct:.0f}%)"

    print(f"""
  B12 Memory Status
  ├─ Memories: {total} active, {archived} archived
  ├─ Projects: {', '.join(project_list[:10]) if project_list else 'none'}
  ├─ DB size: {db_size:.1f} MB
  ├─ Avg strength: {avg_strength:.2f}
  ├─ Last stored: {last_stored}
  ├─ Embeddings: {embedding_line}
  └─ DB path: {DB_PATH}
""")


def cmd_export(args):
    """Export memories as JSON."""
    conn = get_db(readonly=True)

    query = "SELECT * FROM memories WHERE deleted_at IS NULL"
    params = []
    if args.project:
        query += f" AND {_tag_predicate('tags')}"
        params.append(_tag_param(f"proj:{args.project}"))

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
        content = scrub_pii(mem.get("content", ""))
        if not content:
            continue

        ch = content_hash(content)
        safe_metadata = _safe_import_metadata(mem.get("metadata", "{}"), ch)
        safe_tags = _safe_import_tags(mem.get("tags", ""))
        existing = conn.execute(
            "SELECT id FROM memories WHERE content_hash = ?", (ch,)
        ).fetchone()

        if existing:
            skipped += 1
            continue

        now_epoch = time.time()
        now_iso = datetime.now(timezone.utc).isoformat()
        created_at = mem.get("created_at")
        updated_at = mem.get("updated_at")
        created_at_iso = mem.get("created_at_iso")
        updated_at_iso = mem.get("updated_at_iso")
        if not isinstance(created_at, (int, float)):
            created_at = now_epoch
        if not isinstance(updated_at, (int, float)):
            updated_at = now_epoch
        if not isinstance(created_at_iso, str) or not created_at_iso:
            created_at_iso = now_iso
        if not isinstance(updated_at_iso, str) or not updated_at_iso:
            updated_at_iso = now_iso
        strength = mem.get("strength", 1.0)
        if not isinstance(strength, (int, float)):
            strength = 1.0
        conn.execute(
            """INSERT INTO memories
               (content, metadata, tags, memory_type, content_hash,
                created_at, updated_at, created_at_iso, updated_at_iso,
                strength, deleted_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)""",
            (content, safe_metadata, safe_tags,
             mem.get("memory_type", "note"), ch,
             created_at, updated_at, created_at_iso, updated_at_iso, float(strength))
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
