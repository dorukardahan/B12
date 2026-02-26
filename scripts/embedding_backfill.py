#!/usr/bin/env python3
"""
B12 Embedding Backfill — fills missing embeddings for memories.

Finds active memories without embeddings and generates them via the
embed daemon. Can be run manually or as a scheduled job.

Usage:
    python3 embedding_backfill.py              # Backfill all missing
    python3 embedding_backfill.py --dry-run    # Show what would be backfilled
    python3 embedding_backfill.py --limit 10   # Backfill up to 10
"""

import base64
import json
import os
import socket
import sqlite3
import struct
import sys

_script_dir = os.path.dirname(os.path.abspath(__file__))


def get_db_path():
    if sys.platform == 'darwin':
        return os.path.expanduser('~/Library/Application Support/mcp-memory/sqlite_vec.db')
    elif os.path.isdir(os.path.expanduser('~/AppData')):
        return os.path.expanduser('~/AppData/Local/mcp-memory/sqlite_vec.db')
    else:
        return os.path.expanduser('~/.local/share/mcp-memory/sqlite_vec.db')


def daemon_request(texts):
    """Request embeddings from the embed daemon via Unix socket."""
    _uid = os.getuid() if hasattr(os, 'getuid') else os.getpid()
    sock_path = f"/tmp/b12-embed-{_uid}.sock"
    if not os.path.exists(sock_path):
        return None
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(30)
        sock.connect(sock_path)
        request = json.dumps({"op": "encode_batch", "texts": texts}) + "\n"
        sock.sendall(request.encode())
        response = b""
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            response += chunk
            if b"\n" in response:
                break
        sock.close()
        result = json.loads(response.decode().strip())
        if result.get("ok") and result.get("embeddings"):
            return result["embeddings"]
    except Exception as e:
        print(f"  Daemon error: {e}")
    return None


def find_missing(db_path):
    """Find active memories without embeddings."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        # sqlite-vec virtual table might not be queryable without extension
        # Use the internal rowids table instead
        rows = conn.execute("""
            SELECT m.id, m.content, m.tags
            FROM memories m
            WHERE m.deleted_at IS NULL
              AND m.id NOT IN (
                  SELECT rowid FROM memory_embeddings_rowids
              )
            ORDER BY m.id
        """).fetchall()
        return rows
    except Exception:
        # Fallback: try with a simpler approach
        try:
            all_ids = conn.execute(
                "SELECT id FROM memories WHERE deleted_at IS NULL"
            ).fetchall()
            emb_ids = conn.execute(
                "SELECT rowid FROM memory_embeddings_rowids"
            ).fetchall()
            emb_set = {r[0] for r in emb_ids}
            missing_ids = [r[0] for r in all_ids if r[0] not in emb_set]
            if not missing_ids:
                return []
            placeholders = ",".join("?" * len(missing_ids))
            rows = conn.execute(
                f"SELECT id, content, tags FROM memories WHERE id IN ({placeholders})",
                missing_ids
            ).fetchall()
            return rows
        except Exception as e:
            print(f"Error finding missing embeddings: {e}")
            return []
    finally:
        conn.close()


def store_embedding(db_path, memory_id, embedding_b64):
    """Store a single embedding in the vec0 virtual table."""
    blob = base64.b64decode(embedding_b64)
    conn = sqlite3.connect(db_path)
    try:
        # Load sqlite-vec extension
        import sqlite_vec
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)

        conn.execute(
            "INSERT INTO memory_embeddings (rowid, content_embedding) VALUES (?, ?)",
            (memory_id, blob)
        )
        conn.commit()
        return True
    except Exception as e:
        conn.rollback()
        print(f"  Failed to store embedding for #{memory_id}: {e}")
        return False
    finally:
        conn.close()


def main():
    dry_run = "--dry-run" in sys.argv
    limit = None
    for i, arg in enumerate(sys.argv):
        if arg == "--limit" and i + 1 < len(sys.argv):
            limit = int(sys.argv[i + 1])

    db_path = get_db_path()
    if not os.path.exists(db_path):
        print(f"Database not found: {db_path}")
        sys.exit(1)

    missing = find_missing(db_path)
    if not missing:
        print("All active memories have embeddings.")
        return

    if limit:
        missing = missing[:limit]

    print(f"Found {len(missing)} memories without embeddings.")

    if dry_run:
        for r in missing:
            tags = r[2] if len(r) > 2 else r["tags"]
            content = r[1] if isinstance(r[1], str) else r["content"]
            print(f"  #{r[0]}: {content[:60]}... | {tags}")
        return

    # Check daemon is running
    _uid2 = os.getuid() if hasattr(os, 'getuid') else os.getpid()
    sock_path = f"/tmp/b12-embed-{_uid2}.sock"
    if not os.path.exists(sock_path):
        print("Embed daemon not running. Start a Claude Code session first,")
        print("or run: ~/.local/b12-venv/bin/python3 scripts/embed_daemon.py &")
        sys.exit(1)

    # Process in batches of 10
    batch_size = 10
    success = 0
    failed = 0

    for i in range(0, len(missing), batch_size):
        batch = missing[i:i + batch_size]
        texts = []
        ids = []
        for r in batch:
            content = r[1] if isinstance(r[1], str) else r["content"]
            texts.append(content[:1000])  # Truncate for embedding
            ids.append(r[0])

        embeddings = daemon_request(texts)
        if not embeddings:
            print(f"  Batch {i // batch_size + 1}: daemon returned no embeddings")
            failed += len(batch)
            continue

        for mem_id, emb_b64 in zip(ids, embeddings):
            if store_embedding(db_path, mem_id, emb_b64):
                success += 1
                print(f"  #{mem_id}: embedded")
            else:
                failed += 1

    print(f"\nDone: {success} embedded, {failed} failed")


if __name__ == "__main__":
    main()
