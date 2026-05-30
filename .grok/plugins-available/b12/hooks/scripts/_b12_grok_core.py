#!/usr/bin/env python3
"""Shared core for the B12 Grok lifecycle hooks.

Resolves the B12 shared scripts directory robustly (env-driven, not by fragile
`__file__` path depth), re-exports the extraction helpers, and provides one
correct write path so the two thin hook adapters (b12-precompact.py,
b12-session-end.py) do not duplicate DB/merge logic.

Why this exists: the previous hooks resolved the core via
`Path(__file__).resolve().parents[5]`, which breaks once the plugin is deployed
to `~/.grok/plugins/b12/` (one fewer path segment → resolves to $HOME). They
also imported extraction helpers that did not exist and called `merge_or_insert`
with the wrong signature. This module fixes all of that in one place.
"""

import base64
import json
import os
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path


def resolve_scripts_dir():
    """Return the B12 shared `scripts/` dir (with shared_patterns + write_time_merge).

    Resolution order:
      1. $B12_HOOK_DIR/scripts          (canonical code location set by install.sh)
      2. ~/.B12/hooks/scripts           (default deployed location)
      3. in-repo: walk up from this file to a dir containing scripts/write_time_merge.py
    """
    candidates = []
    env = os.environ.get("B12_HOOK_DIR")
    if env:
        candidates.append(Path(env) / "scripts")
    candidates.append(Path(os.path.expanduser("~/.B12/hooks")) / "scripts")
    here = Path(__file__).resolve()
    for parent in here.parents:
        cand = parent / "scripts"
        if (cand / "write_time_merge.py").exists():
            candidates.append(cand)
            break
    for c in candidates:
        if (c / "write_time_merge.py").exists():
            return str(c)
    return None


_SCRIPTS_DIR = resolve_scripts_dir()
if _SCRIPTS_DIR and _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

# These imports now resolve against the real shared core. A failure here means
# B12 is not installed; the caller checks CORE_OK and degrades to a no-op.
try:
    from shared_patterns import (  # noqa: E402
        content_hash,
        get_db_path,
        extract_decisions,
        extract_gotchas,
        extract_learnings,
        extract_preferences,
    )
    from write_time_merge import merge_or_insert  # noqa: E402
    CORE_OK = True
    IMPORT_ERROR = ""
except Exception as e:  # pragma: no cover
    CORE_OK = False
    IMPORT_ERROR = str(e)

    # Safe fallbacks so the thin hooks can import these names unconditionally;
    # store_items() additionally guards on CORE_OK before doing any real work.
    def extract_decisions(_text):
        return []

    def extract_gotchas(_text):
        return []

    def extract_learnings(_text):
        return []

    def extract_preferences(_text):
        return []


# PII/secret scrubber, resolved independently of CORE_OK so the FTS-only
# fallback insert also redacts (the merge_or_insert path scrubs internally).
try:
    from b12_pii_scrubber import scrub as _pii_scrub  # noqa: E402
except Exception:
    def _pii_scrub(_text):
        return _text


def _daemon_encode(text):
    """Encode `text` via the embed daemon Unix socket → float32 bytes, or None.

    None means the daemon is unavailable; the caller then stores FTS-only and
    embedding_backfill.py restores the vector later."""
    uid = os.getuid() if hasattr(os, "getuid") else os.getpid()
    sock_path = os.path.join(os.environ.get("B12_EMBED_RUNTIME_DIR", "/tmp"), f"b12-embed-{uid}.sock")
    if not os.path.exists(sock_path):
        return None
    s = None
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(15)
        s.connect(sock_path)
        s.sendall((json.dumps({"op": "encode_batch", "texts": [text]}) + "\n").encode())
        data = b""
        while b"\n" not in data:
            chunk = s.recv(1048576)
            if not chunk:
                break
            data += chunk
        resp = json.loads(data.decode().strip())
        if resp.get("ok") and resp.get("embeddings"):
            return base64.b64decode(resp["embeddings"][0])
    except Exception:
        return None
    finally:
        if s is not None:
            try:
                s.close()
            except Exception:
                pass
    return None


def _load_vec(conn):
    try:
        import sqlite_vec
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        return True
    except Exception:
        return False


def store_items(items):
    """Persist `items` = list of (memory_type, content, tags, metadata).

    Uses the canonical write path: daemon-encode → merge_or_insert (semantic
    dedup). Falls back to a direct INSERT (FTS-only, backfill restores the
    vector) when the daemon or sqlite-vec is unavailable. Returns count stored.
    Never raises — the hook must exit 0."""
    if not CORE_OK or not items:
        return 0
    import sqlite3

    db_path = get_db_path()
    if not db_path or not os.path.exists(db_path):
        return 0

    try:
        conn = sqlite3.connect(db_path, timeout=10)
    except sqlite3.Error:
        return 0
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    vec_ok = _load_vec(conn)
    now = datetime.now(timezone.utc)
    stored = 0

    # Older DBs (pre-Ebbinghaus migration) may lack the valid_until column; only
    # use the expiry-aware predicates when it exists, else fall back to
    # deleted_at-only so the queries don't raise "no such column: valid_until".
    try:
        _cols = {r[1] for r in conn.execute("PRAGMA table_info(memories)").fetchall()}
    except sqlite3.Error:
        _cols = set()
    _has_vu = "valid_until" in _cols
    # Skip only LIVE (+ non-expired, when valid_until exists) duplicates; an
    # expired row must fall through so it gets revived rather than left dormant.
    _live_sql = (
        "SELECT 1 FROM memories WHERE content_hash = ? AND deleted_at IS NULL"
        + (" AND (valid_until IS NULL OR valid_until > datetime('now'))" if _has_vu else "")
    )
    # Revive a SOFT-DELETED (or EXPIRED, when valid_until exists) row before the
    # daemon/merge split: merge_or_insert filters deleted_at IS NULL and would hit
    # the UNIQUE index on a soft-deleted dup, and an expired row would be skipped
    # above without this. Clearing deleted_at (+ valid_until) makes it visible
    # again (v11 ghost-memory); embedding_backfill restores the vector.
    _revive_sql = (
        "SELECT 1 FROM memories WHERE content_hash = ? AND ("
        + ("deleted_at IS NOT NULL OR (valid_until IS NOT NULL AND valid_until <= datetime('now'))"
           if _has_vu else "deleted_at IS NOT NULL")
        + ") LIMIT 1"
    )
    _revive_update = (
        "UPDATE memories SET deleted_at = NULL, "
        + ("valid_until = NULL, " if _has_vu else "")
        + "content = ?, tags = ?, memory_type = ?, metadata = ?, "
        + "updated_at = ?, updated_at_iso = ? WHERE content_hash = ?"
    )
    try:
        for memory_type, content, tags, metadata in items:
            content = (content or "").strip()
            if not content:
                continue
            # Scrub BEFORE hashing so BOTH the merge_or_insert path and the
            # FTS-only fallback store redacted content (merge_or_insert scrubs
            # internally; the fallback INSERT below previously did not), and so
            # dedup keys on the scrubbed form — matching merge_or_insert's order.
            content = _pii_scrub(content)
            ch = content_hash(content)
            tags_str = ",".join(tags) if isinstance(tags, (list, tuple)) else (tags or "")
            meta_str = json.dumps(metadata, ensure_ascii=False) if isinstance(metadata, dict) else (metadata or "{}")
            if conn.execute(_live_sql, (ch,)).fetchone():
                continue
            if conn.execute(_revive_sql, (ch,)).fetchone():
                conn.execute(
                    _revive_update,
                    (content, tags_str, memory_type, meta_str,
                     now.timestamp(), now.isoformat(), ch),
                )
                stored += 1
                continue
            emb = _daemon_encode(content)
            try:
                if emb is not None and vec_ok:
                    result = merge_or_insert(conn, content, ch, tags, memory_type, metadata, emb, now, db_path=db_path)
                    # Count only real writes; noop_duplicate would over-report.
                    if getattr(result, "action", "") != "noop_duplicate":
                        stored += 1
                else:
                    cur = conn.execute(
                        "INSERT OR IGNORE INTO memories "
                        "(content, content_hash, tags, memory_type, metadata, "
                        " created_at, updated_at, created_at_iso, updated_at_iso) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (content, ch, tags_str, memory_type, meta_str,
                         now.timestamp(), now.timestamp(), now.isoformat(), now.isoformat()),
                    )
                    if cur.rowcount and cur.rowcount > 0:
                        stored += 1
            except Exception as e:
                print(f"[b12-grok] store failed ({memory_type}): {e}", file=sys.stderr)
        conn.commit()
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return stored
