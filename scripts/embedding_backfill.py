#!/usr/bin/env python3
"""
B12 Embedding Backfill — fills missing embeddings for memories.

Finds active memories without embeddings and generates them via the
embed daemon. Can be run manually or as a scheduled job.

Usage:
    python3 embedding_backfill.py              # Backfill all missing
    python3 embedding_backfill.py --dry-run    # Show what would be backfilled
    python3 embedding_backfill.py --limit 10   # Backfill up to 10
    python3 embedding_backfill.py --if-due 3600 --limit 200 --wait-daemon 90 --quiet
                                               # Safety net run by SessionStart
"""

import base64
import json
import os
import socket
import sqlite3
import struct
import sys
import time

try:
    import fcntl
except ImportError:  # Windows: run unlocked
    fcntl = None

_script_dir = os.path.dirname(os.path.abspath(__file__))


def _sock_path():
    _uid = os.getuid() if hasattr(os, 'getuid') else os.getpid()
    return os.path.join(os.environ.get("B12_EMBED_RUNTIME_DIR", "/tmp"), f"b12-embed-{_uid}.sock")


def get_db_path():
    if sys.platform == 'darwin':
        return os.path.expanduser('~/Library/Application Support/mcp-memory/sqlite_vec.db')
    elif os.path.isdir(os.path.expanduser('~/AppData')):
        return os.path.expanduser('~/AppData/Local/mcp-memory/sqlite_vec.db')
    else:
        return os.path.expanduser('~/.local/share/mcp-memory/sqlite_vec.db')


def daemon_request(texts):
    """Request embeddings from the embed daemon via Unix socket."""
    sock_path = _sock_path()
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


def _hash_col(conn, prefix=""):
    """content_hash as a 4th column when the schema has it (minimal DBs may not)."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(memories)")}
    return f", {prefix}content_hash" if "content_hash" in cols else ""


def find_missing(db_path):
    """Find active memories without embeddings."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.row_factory = sqlite3.Row
    try:
        # sqlite-vec virtual table might not be queryable without extension
        # Use the internal rowids table instead
        rows = conn.execute(f"""
            SELECT m.id, m.content, m.tags{_hash_col(conn, "m.")}
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
                f"SELECT id, content, tags{_hash_col(conn)} FROM memories WHERE id IN ({placeholders})",
                missing_ids
            ).fetchall()
            return rows
        except Exception as e:
            print(f"Error finding missing embeddings: {e}")
            return []
    finally:
        conn.close()


def _find_missing_for_hashes(db_path, content_hashes):
    """Like find_missing, but restricted to the given content_hashes — active rows
    among them that still lack an embedding. Returns (id, content, tags) rows.
    Chunked to stay under SQLite's bound-variable limit."""
    hashes = list(content_hashes or [])
    if not hashes:
        return []
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.row_factory = sqlite3.Row
    try:
        out = []
        for i in range(0, len(hashes), 400):
            chunk = hashes[i:i + 400]
            ph = ",".join("?" * len(chunk))
            rows = conn.execute(
                f"""SELECT m.id, m.content, m.tags, m.content_hash
                    FROM memories m
                    WHERE m.content_hash IN ({ph})
                      AND m.deleted_at IS NULL
                      AND m.id NOT IN (SELECT rowid FROM memory_embeddings_rowids)""",
                chunk,
            ).fetchall()
            out.extend(rows)
        return out
    except Exception:
        return []
    finally:
        conn.close()


def store_embedding(db_path, memory_id, embedding_b64, expected_hash=None):
    """Store a single embedding in the vec0 virtual table.

    With `expected_hash`, store only if the row still has that content: a row
    edited (or deleted) while its text was being embedded must not get a stale
    vector; it stays missing and the next backfill embeds the new text."""
    blob = base64.b64decode(embedding_b64)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    try:
        if expected_hash is not None:
            row = conn.execute(
                "SELECT content_hash FROM memories WHERE id = ? AND deleted_at IS NULL",
                (memory_id,),
            ).fetchone()
            if not row or row[0] != expected_hash:
                return False
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


def backfill(db_path, limit=None, missing=None, content_hashes=None, deadline=None):
    """Embed memories in db_path that lack a vector, via the embed daemon's
    encode_batch op. Returns (embedded, failed). No-op-safe: if the daemon is
    down, daemon_request returns None and that batch counts as failed (nothing is
    written). Quiet (no prints) so it's reusable — used by main() (CLI) and by
    export_import after an import so freshly-imported memories become
    vector-searchable instead of staying FTS-only (audit M1).

    Scope:
      - `content_hashes` set → embed ONLY those rows (import path: bounds the work
        to this import's rows so it can't monopolize the single-connection daemon
        and starve concurrent stores).
      - else → every active memory missing an embedding (CLI / full backfill).
    Pass a pre-fetched `missing` to skip the lookup (the CLI does)."""
    if missing is None:
        if content_hashes:
            missing = _find_missing_for_hashes(db_path, content_hashes)
        else:
            missing = find_missing(db_path)
    if limit:
        missing = missing[:limit]
    if not missing:
        return (0, 0)

    batch_size = 10
    success = 0
    failed = 0
    for i in range(0, len(missing), batch_size):
        if deadline is not None and time.time() >= deadline:
            break  # bounded run: the rest waits for the next one
        batch = missing[i:i + batch_size]
        texts = []
        ids = []
        hashes = []
        for r in batch:
            content = r[1] if isinstance(r[1], str) else r["content"]
            texts.append(content[:1000])  # Truncate for embedding
            ids.append(r[0])
            hashes.append(r[3] if len(r) > 3 else None)

        embeddings = daemon_request(texts)
        if not embeddings:
            failed += len(batch)
            continue

        for mem_id, emb_b64, expected in zip(ids, embeddings, hashes):
            kwargs = {"expected_hash": expected} if expected is not None else {}
            if store_embedding(db_path, mem_id, emb_b64, **kwargs):
                success += 1
            else:
                failed += 1

    return (success, failed)


def _daemon_ready():
    """The daemon answers a health request. A socket file alone is not enough:
    a daemon killed without cleanup leaves it behind, refusing connections."""
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(2)
        sock.connect(_sock_path())
        sock.sendall(b'{"op": "health"}\n')
        reply = sock.recv(4096)
        sock.close()
        return json.loads(reply.split(b"\n")[0]).get("ok") is True
    except (OSError, ValueError, AttributeError):
        return False


def scheduled_backfill(db_path, limit=200, min_interval=3600, wait_daemon=0, state_dir=None,
                       max_seconds=60):
    """Bounded safety-net run, meant to be started detached from a hook.

    Any writer that stores a memory while the embed daemon is down, or a path
    that never embeds, leaves the row reachable by FTS only. This run fills up
    to `limit` such rows, at most once per `min_interval` seconds, and never
    twice at once (non-blocking lock). It waits up to `wait_daemon` seconds for
    the daemon socket; if the daemon never comes up, the slot is not consumed.
    Returns (embedded, failed), or None when skipped."""
    state_dir = state_dir or os.environ.get("B12_DATA_DIR") or os.path.expanduser("~/.B12")
    os.makedirs(state_dir, exist_ok=True)
    stamp = os.path.join(state_dir, ".embedding-backfill.last")
    try:
        if time.time() - os.path.getmtime(stamp) < min_interval:
            return None
    except OSError:
        pass
    lock_fh = open(stamp + ".lock", "a+")
    if fcntl is not None:
        try:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return None  # another run holds the lock
    deadline = time.time() + max(0, wait_daemon)
    while not _daemon_ready() and time.time() < deadline:
        time.sleep(2)
    if not _daemon_ready() or not os.path.exists(db_path):
        return None
    with open(stamp, "a"):
        pass
    os.utime(stamp, None)
    return backfill(db_path, limit=limit, deadline=time.time() + max_seconds)


def _arg_int(name, default=None):
    if name in sys.argv:
        i = sys.argv.index(name)
        if i + 1 < len(sys.argv):
            return int(sys.argv[i + 1])
    return default


def main():
    dry_run = "--dry-run" in sys.argv
    limit = _arg_int("--limit")

    if "--hashes" in sys.argv:
        # Scoped run for a writer that cannot embed in-process (the checkpoint
        # hook runs under a python3 without sqlite-vec): embed exactly these rows.
        i = sys.argv.index("--hashes")
        hashes = [h for h in (sys.argv[i + 1] if i + 1 < len(sys.argv) else "").split(",") if h]
        result = backfill(get_db_path(), content_hashes=hashes, deadline=time.time() + 60)
        if "--quiet" not in sys.argv:
            print(f"Done: {result[0]} embedded, {result[1]} failed")
        return

    if "--if-due" in sys.argv:
        # Own session: a host that kills the hook's process group must not take
        # this background run down with it.
        if hasattr(os, "setsid"):
            try:
                os.setsid()
            except OSError:
                pass
        result = scheduled_backfill(
            get_db_path(),
            limit=limit or 200,
            min_interval=_arg_int("--if-due", 3600),
            wait_daemon=_arg_int("--wait-daemon", 0),
        )
        if "--quiet" not in sys.argv:
            print("skipped" if result is None else f"Done: {result[0]} embedded, {result[1]} failed")
        return

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
    if not os.path.exists(_sock_path()):
        print("Embed daemon not running. Start a Claude Code session first,")
        print("or run: ~/.local/b12-venv/bin/python3 scripts/embed_daemon.py &")
        sys.exit(1)

    success, failed = backfill(db_path, missing=missing)   # reuse the fetch above
    print(f"\nDone: {success} embedded, {failed} failed")


if __name__ == "__main__":
    main()
