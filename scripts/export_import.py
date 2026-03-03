"""
B12 Memory Export/Import — portable .b12 archive format.

Format: gzip-compressed JSONL
  Line 1: manifest (version, schema, count, model)
  Lines 2..N+1: memory records
  Lines N+2..M: graph edges

Excludes embeddings by default (regenerated on import).
Import uses content_hash dedup — importing same file twice is safe.
"""
import gzip
import hashlib
import json
import os
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

B12_VERSION = "11.4.0"
SCHEMA_VERSION = 1
EMBEDDING_MODEL = "paraphrase-multilingual-MiniLM-L12-v2"


@dataclass
class ExportResult:
    memories_exported: int = 0
    edges_exported: int = 0
    output_path: str = ""
    file_size_bytes: int = 0
    duration_seconds: float = 0.0


@dataclass
class ImportResult:
    memories_imported: int = 0
    memories_skipped: int = 0
    edges_imported: int = 0
    edges_skipped: int = 0
    errors: list[str] = field(default_factory=list)
    duration_seconds: float = 0.0


def get_db_path() -> str:
    home = os.path.expanduser("~")
    if sys.platform == "darwin":
        return os.path.join(home, "Library", "Application Support",
                            "mcp-memory", "sqlite_vec.db")
    elif sys.platform == "win32":
        return os.path.join(home, "AppData", "Local",
                            "mcp-memory", "sqlite_vec.db")
    else:
        return os.path.join(home, ".local", "share",
                            "mcp-memory", "sqlite_vec.db")


# ── Export ────────────────────────────────────────────────────────

def export_memories(
    db_path: str = "",
    output_path: str = "",
    project: str = "",
    tags: str = "",
    after: str = "",
    before: str = "",
    include_embeddings: bool = False,
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> ExportResult:
    """Export memories to a .b12 archive file.

    Args:
        db_path: SQLite database path (auto-detected if empty)
        output_path: Output .b12 file path (auto-generated if empty)
        project: Filter by project name (tag match)
        tags: Filter by tags (comma-separated)
        after: Only memories created after this ISO date
        before: Only memories created before this ISO date
        include_embeddings: Include embedding vectors (increases file size)
        progress_callback: Optional callback(exported, total) for progress
    """
    start = time.time()
    if not db_path:
        db_path = get_db_path()
    if not output_path:
        b12_base = os.environ.get("B12_DATA_DIR", os.path.expanduser("~/.B12"))
        export_dir = os.path.join(b12_base, "exports")
        os.makedirs(export_dir, exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        output_path = os.path.join(export_dir, f"backup-{ts}.b12")

    if not output_path.endswith(".b12"):
        output_path += ".b12"

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    # Build WHERE clause
    wheres = ["deleted_at IS NULL"]
    params: list = []
    if project:
        wheres.append("tags LIKE ?")
        params.append(f"%proj:{project}%")
    if tags:
        for t in tags.split(","):
            t = t.strip()
            if t:
                wheres.append("tags LIKE ?")
                params.append(f"%{t}%")
    if after:
        try:
            ts_val = datetime.fromisoformat(after).timestamp()
            wheres.append("created_at >= ?")
            params.append(ts_val)
        except ValueError:
            pass
    if before:
        try:
            ts_val = datetime.fromisoformat(before).timestamp()
            wheres.append("created_at <= ?")
            params.append(ts_val)
        except ValueError:
            pass

    where_sql = " AND ".join(wheres)

    # Count for progress
    total = conn.execute(
        f"SELECT COUNT(*) FROM memories WHERE {where_sql}", params
    ).fetchone()[0]

    # Collect exported hashes for edge filtering
    exported_hashes: set[str] = set()
    memories_exported = 0

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    with gzip.open(output_path, "wt", encoding="utf-8") as f:
        # Write manifest
        manifest = {
            "_type": "manifest",
            "b12_version": B12_VERSION,
            "schema": SCHEMA_VERSION,
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "count": total,
            "model": EMBEDDING_MODEL,
            "filters": {
                "project": project,
                "tags": tags,
                "after": after,
                "before": before,
            },
        }
        f.write(json.dumps(manifest, ensure_ascii=False) + "\n")

        # Write memories
        rows = conn.execute(
            f"""SELECT id, content, content_hash, memory_type, tags, metadata,
                       created_at, created_at_iso, updated_at, updated_at_iso,
                       strength, last_accessed_at, valid_until
                FROM memories WHERE {where_sql}
                ORDER BY created_at ASC""",
            params,
        ).fetchall()

        for row in rows:
            record = {
                "_type": "memory",
                "content": row["content"],
                "content_hash": row["content_hash"],
                "memory_type": row["memory_type"],
                "tags": row["tags"],
                "metadata": json.loads(row["metadata"]) if row["metadata"] else {},
                "created_at": row["created_at"],
                "created_at_iso": row["created_at_iso"],
                "updated_at": row["updated_at"],
                "updated_at_iso": row["updated_at_iso"],
                "strength": row["strength"],
                "last_accessed_at": row["last_accessed_at"],
                "valid_until": row["valid_until"],
            }
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            exported_hashes.add(row["content_hash"])
            memories_exported += 1

            if progress_callback and memories_exported % 100 == 0:
                progress_callback(memories_exported, total)

        # Write graph edges (only edges between exported memories)
        edges_exported = 0
        if exported_hashes:
            placeholders = ",".join("?" * len(exported_hashes))
            hash_list = list(exported_hashes)
            edge_rows = conn.execute(
                f"""SELECT source_hash, target_hash, similarity,
                           connection_types, relationship_type
                    FROM memory_graph
                    WHERE source_hash IN ({placeholders})
                      AND target_hash IN ({placeholders})""",
                hash_list + hash_list,
            ).fetchall()

            for edge in edge_rows:
                edge_record = {
                    "_type": "edge",
                    "source_hash": edge["source_hash"],
                    "target_hash": edge["target_hash"],
                    "similarity": edge["similarity"],
                    "connection_types": edge["connection_types"],
                    "relationship_type": edge["relationship_type"],
                }
                f.write(json.dumps(edge_record, ensure_ascii=False) + "\n")
                edges_exported += 1

    conn.close()

    file_size = os.path.getsize(output_path)
    if file_size > 100 * 1024 * 1024:
        sys.stderr.write(f"WARNING: Export file is {file_size / 1024 / 1024:.1f}MB (>100MB)\n")

    return ExportResult(
        memories_exported=memories_exported,
        edges_exported=edges_exported,
        output_path=output_path,
        file_size_bytes=file_size,
        duration_seconds=round(time.time() - start, 2),
    )


# ── Import ────────────────────────────────────────────────────────

def import_memories(
    db_path: str = "",
    input_path: str = "",
    mode: str = "merge",
    source_name: str = "",
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> ImportResult:
    """Import memories from a .b12 archive file.

    Args:
        db_path: SQLite database path (auto-detected if empty)
        input_path: Input .b12 file path
        mode: "merge" (skip duplicates) or "replace" (clear + import)
        source_name: Name of the source machine/setup for provenance tracking
        progress_callback: Optional callback(imported, total) for progress
    """
    start = time.time()
    if not db_path:
        db_path = get_db_path()

    if not input_path or not os.path.exists(input_path):
        return ImportResult(errors=[f"File not found: {input_path}"])

    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.row_factory = sqlite3.Row

    result = ImportResult()
    manifest = None
    total = 0

    try:
        with gzip.open(input_path, "rt", encoding="utf-8") as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as e:
                    result.errors.append(f"Line {line_num}: JSON parse error: {e}")
                    continue

                record_type = record.get("_type", "")

                if record_type == "manifest":
                    manifest = record
                    total = manifest.get("count", 0)
                    schema = manifest.get("schema", 1)
                    if schema > SCHEMA_VERSION:
                        sys.stderr.write(
                            f"WARNING: Archive schema {schema} > supported {SCHEMA_VERSION}. "
                            "Unknown fields will be preserved in metadata.\n"
                        )
                    if mode == "replace":
                        conn.execute(
                            "UPDATE memories SET deleted_at = ? WHERE deleted_at IS NULL",
                            (time.time(),)
                        )
                        conn.commit()

                elif record_type == "memory":
                    imported = _import_memory(conn, record, mode, source_name)
                    if imported:
                        result.memories_imported += 1
                    else:
                        result.memories_skipped += 1

                    if progress_callback:
                        done = result.memories_imported + result.memories_skipped
                        if done % 50 == 0:
                            progress_callback(done, total)

                elif record_type == "edge":
                    imported = _import_edge(conn, record)
                    if imported:
                        result.edges_imported += 1
                    else:
                        result.edges_skipped += 1

                # Unknown types: skip silently (forward compatibility)

        conn.commit()

    except Exception as e:
        result.errors.append(f"Import error: {e}")
    finally:
        conn.close()

    # Request embedding backfill via daemon (batch)
    if result.memories_imported > 0:
        _request_embedding_backfill(db_path, result.memories_imported)

    result.duration_seconds = round(time.time() - start, 2)
    return result


def _import_memory(
    conn: sqlite3.Connection, record: dict, mode: str, source_name: str
) -> bool:
    """Import a single memory record. Returns True if imported, False if skipped."""
    content = record.get("content", "")
    if not content:
        return False

    content_hash = record.get("content_hash", "")
    if not content_hash:
        content_hash = hashlib.sha256(content.encode()).hexdigest()

    # Check if already exists
    existing = conn.execute(
        "SELECT id, deleted_at FROM memories WHERE content_hash = ?",
        (content_hash,)
    ).fetchone()

    if existing:
        if mode == "merge":
            if existing["deleted_at"] is not None:
                # Undelete if soft-deleted
                conn.execute(
                    "UPDATE memories SET deleted_at = NULL, strength = 1.0 WHERE content_hash = ?",
                    (content_hash,)
                )
                conn.commit()
                return True
            return False  # Already exists, skip
        # Replace mode: already cleared above, but entry might exist from this import
        if existing["deleted_at"] is None:
            return False

    # Prepare metadata with provenance
    metadata = record.get("metadata", {})
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except json.JSONDecodeError:
            metadata = {}

    if source_name:
        metadata["imported_from"] = source_name
    metadata["import_date"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if record.get("_type") == "memory":
        # Preserve original created_at as original_created_at
        if "original_created_at" not in metadata and record.get("created_at"):
            metadata["original_created_at"] = record["created_at"]

    now_ts = time.time()
    now_iso = datetime.now(timezone.utc).isoformat()

    conn.execute(
        """INSERT OR IGNORE INTO memories
           (content_hash, content, tags, memory_type, metadata,
            strength, created_at, created_at_iso, updated_at, updated_at_iso,
            valid_until, last_accessed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            content_hash,
            content,
            record.get("tags", ""),
            record.get("memory_type", "general"),
            json.dumps(metadata, ensure_ascii=False),
            record.get("strength", 1.0),
            record.get("created_at", now_ts),
            record.get("created_at_iso", now_iso),
            now_ts,
            now_iso,
            record.get("valid_until"),
            record.get("last_accessed_at"),
        ),
    )
    conn.commit()
    return True


def _import_edge(conn: sqlite3.Connection, record: dict) -> bool:
    """Import a single graph edge. Returns True if imported."""
    source = record.get("source_hash", "")
    target = record.get("target_hash", "")
    if not source or not target:
        return False

    try:
        conn.execute(
            """INSERT OR REPLACE INTO memory_graph
               (source_hash, target_hash, similarity, connection_types, relationship_type)
               VALUES (?, ?, ?, ?, ?)""",
            (
                source,
                target,
                record.get("similarity", 0.0),
                record.get("connection_types", ""),
                record.get("relationship_type", "related"),
            ),
        )
        return True
    except Exception:
        return False


def _request_embedding_backfill(db_path: str, count: int):
    """Request the embed daemon to backfill embeddings for imported memories."""
    import socket
    uid = os.getuid() if hasattr(os, 'getuid') else os.getpid()
    sock_path = f"/tmp/b12-embed-{uid}.sock"

    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(5)
        s.connect(sock_path)
        request = json.dumps({
            "op": "backfill",
            "db_path": db_path,
            "limit": min(count, 500),
        })
        s.sendall((request + "\n").encode())
        data = b""
        while b"\n" not in data:
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
        s.close()
    except Exception:
        # Daemon not available — embeddings will be backfilled on next session
        pass


# ── CLI ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="B12 Memory Export/Import")
    sub = parser.add_subparsers(dest="command")

    exp = sub.add_parser("export", help="Export memories to .b12 file")
    exp.add_argument("--output", "-o", default="", help="Output file path")
    exp.add_argument("--project", "-p", default="", help="Filter by project")
    exp.add_argument("--tags", "-t", default="", help="Filter by tags (comma-separated)")
    exp.add_argument("--after", default="", help="After date (ISO)")
    exp.add_argument("--before", default="", help="Before date (ISO)")
    exp.add_argument("--db", default="", help="Database path")

    imp = sub.add_parser("import", help="Import memories from .b12 file")
    imp.add_argument("input", help="Input .b12 file path")
    imp.add_argument("--mode", choices=["merge", "replace"], default="merge")
    imp.add_argument("--source", default="", help="Source machine name")
    imp.add_argument("--db", default="", help="Database path")

    args = parser.parse_args()

    if args.command == "export":
        def progress(done, total):
            print(f"  Exported {done}/{total}...", end="\r")

        result = export_memories(
            db_path=args.db,
            output_path=args.output,
            project=args.project,
            tags=args.tags,
            after=args.after,
            before=args.before,
            progress_callback=progress,
        )
        print(f"\nExport complete:")
        print(f"  Memories: {result.memories_exported}")
        print(f"  Edges:    {result.edges_exported}")
        print(f"  File:     {result.output_path}")
        print(f"  Size:     {result.file_size_bytes / 1024:.1f} KB")
        print(f"  Time:     {result.duration_seconds}s")

    elif args.command == "import":
        def progress(done, total):
            print(f"  Imported {done}/{total}...", end="\r")

        result = import_memories(
            db_path=args.db,
            input_path=args.input,
            mode=args.mode,
            source_name=args.source,
            progress_callback=progress,
        )
        print(f"\nImport complete:")
        print(f"  Imported: {result.memories_imported}")
        print(f"  Skipped:  {result.memories_skipped}")
        print(f"  Edges:    {result.edges_imported}")
        print(f"  Time:     {result.duration_seconds}s")
        if result.errors:
            print(f"  Errors:")
            for e in result.errors[:10]:
                print(f"    - {e}")
    else:
        parser.print_help()
