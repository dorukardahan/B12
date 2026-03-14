#!/usr/bin/env python3
"""
B12 Codex Session End Processor

Processes Codex CLI rollout files to extract session summaries and
micro-memories, then stores them in the shared B12 SQLite database.

Can be triggered by:
  - Codex notify hook (b12-codex-notify.sh)
  - Manual: python3 codex_session_end.py <rollout-path>
  - Cron/launchd: python3 codex_session_end.py --scan-recent

Uses transcript_adapter.py for format-agnostic parsing and
shared_patterns.py for content extraction (same patterns as Claude Code).
"""

import json
import os
import re
import sqlite3
import sys
import time
import hashlib
import glob
from datetime import datetime, timezone

# Add scripts directory to path
_script_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _script_dir)

from transcript_adapter import parse, extract_user_messages, extract_assistant_texts, extract_files_modified
from shared_patterns import DECISION_RE, ERROR_RE, LEARNING_RE, PREFERENCE_RE


def get_db_path():
    """Get the B12 SQLite database path (platform-aware)."""
    if sys.platform == 'darwin':
        return os.path.expanduser('~/Library/Application Support/mcp-memory/sqlite_vec.db')
    elif os.path.isdir(os.path.expanduser('~/AppData')):
        return os.path.expanduser('~/AppData/Local/mcp-memory/sqlite_vec.db')
    else:
        return os.path.expanduser('~/.local/share/mcp-memory/sqlite_vec.db')


def get_data_dir():
    """Get the B12 data directory."""
    return os.environ.get('B12_DATA_DIR', os.path.expanduser('~/.B12'))


def get_state_file():
    """Path to state file tracking processed sessions."""
    data_dir = get_data_dir()
    state_dir = os.path.join(data_dir, 'memory-logs')
    os.makedirs(state_dir, exist_ok=True)
    return os.path.join(state_dir, 'codex-processed-sessions.json')


def load_processed_sessions():
    """Load set of already-processed session IDs."""
    state_file = get_state_file()
    try:
        with open(state_file, 'r') as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def save_processed_session(session_id):
    """Mark a session as processed."""
    processed = load_processed_sessions()
    processed.add(session_id)
    # Keep only last 500 entries to prevent unbounded growth
    if len(processed) > 500:
        processed = set(sorted(processed)[-500:])
    with open(get_state_file(), 'w') as f:
        json.dump(sorted(processed), f)


def get_embedding(text):
    """Get embedding via daemon first, fallback to direct model load."""
    # Try daemon first (fast, no model load)
    emb = _get_embedding_daemon(text)
    if emb:
        return emb
    # Fallback: load model directly (slow first time, ~2s cached)
    return _get_embedding_direct(text)


def _get_embedding_daemon(text):
    """Get embedding via embed daemon Unix socket."""
    import socket
    _uid = os.getuid() if hasattr(os, 'getuid') else os.getpid()
    sock_path = f"/tmp/b12-embed-{_uid}.sock"
    if not os.path.exists(sock_path):
        return None
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(10)
        sock.connect(sock_path)
        request = json.dumps({"op": "encode_batch", "texts": [text]}) + "\n"
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
            return result["embeddings"][0]
    except Exception:
        pass
    return None


# Module-level model cache (loaded once per process)
_model = None


def _get_embedding_direct(text):
    """Get embedding by loading SentenceTransformer directly."""
    global _model
    try:
        if _model is None:
            from sentence_transformers import SentenceTransformer
            model_name = os.environ.get(
                "MCP_EMBEDDING_MODEL", "paraphrase-multilingual-MiniLM-L12-v2"
            )
            _model = SentenceTransformer(model_name)
        import base64, numpy as np
        emb = _model.encode([text], normalize_embeddings=True, convert_to_numpy=True)
        emb_bytes = emb[0].astype(np.float32).tobytes()
        return base64.b64encode(emb_bytes).decode('ascii')
    except ImportError:
        return None
    except Exception:
        return None


def store_memory(db_path, content, metadata_str, tags, embedding=None, memory_type='general'):
    """Store a memory in the B12 database."""
    # Validate metadata is valid JSON before INSERT
    try:
        from shared_patterns import validate_metadata
        metadata_str = validate_metadata(metadata_str)
    except ImportError:
        if metadata_str and isinstance(metadata_str, str):
            try:
                json.loads(metadata_str)
            except (json.JSONDecodeError, ValueError):
                metadata_str = "{}"
    content_hash = hashlib.sha256(content.strip().lower().encode()).hexdigest()
    now = datetime.now(timezone.utc).isoformat()

    now_epoch = time.time()

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    try:
        # Check for duplicate (deleted_at IS NULL = not soft-deleted)
        existing = conn.execute(
            "SELECT id FROM memories WHERE content_hash = ? AND deleted_at IS NULL",
            (content_hash,)
        ).fetchone()
        if existing:
            return existing[0]  # Already stored

        cursor = conn.execute(
            """INSERT INTO memories (content, metadata, tags, content_hash, memory_type,
               created_at, updated_at, created_at_iso, updated_at_iso,
               strength, deleted_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0.5, NULL)""",
            (content, metadata_str, tags, content_hash, memory_type,
             now_epoch, now_epoch, now, now)
        )
        memory_id = cursor.lastrowid

        # Store embedding if available
        if embedding and memory_id:
            import base64, struct
            blob = base64.b64decode(embedding) if isinstance(embedding, str) else embedding
            try:
                conn.execute(
                    "INSERT INTO memory_embeddings (rowid, content_embedding) VALUES (?, ?)",
                    (memory_id, blob)
                )
            except Exception:
                pass  # Vector table might not exist

        conn.commit()
        return memory_id
    except Exception as e:
        conn.rollback()
        log_error(f"store_memory failed: {e}")
        return None
    finally:
        conn.close()


def log_error(msg):
    """Log error to B12 error log."""
    log_dir = os.path.join(get_data_dir(), 'memory-logs')
    os.makedirs(log_dir, exist_ok=True)
    with open(os.path.join(log_dir, 'memory-errors.log'), 'a') as f:
        f.write(f"[{datetime.now().isoformat()}] codex_session_end: {msg}\n")


def process_rollout(rollout_path: str, force: bool = False) -> dict:
    """
    Process a single Codex rollout file.

    Returns dict with processing results.
    """
    info, messages = parse(rollout_path)

    if not info.session_id:
        return {"status": "skip", "reason": "no session_id"}

    # Check if already processed
    if not force and info.session_id in load_processed_sessions():
        return {"status": "skip", "reason": "already processed"}

    if len(messages) < 3:
        save_processed_session(info.session_id)
        return {"status": "skip", "reason": "too few messages"}

    # Extract content
    user_msgs = extract_user_messages(messages)
    assistant_texts = extract_assistant_texts(messages)
    files_modified = extract_files_modified(messages)
    project_name = os.path.basename(info.cwd) if info.cwd else "unknown"

    # Pattern matching on assistant texts
    decisions = []
    errors = []
    learnings = []
    preferences = []

    for text in assistant_texts:
        snippet = text[:500]
        if DECISION_RE.search(snippet):
            decisions.append(snippet[:200])
        if ERROR_RE.search(snippet):
            errors.append(snippet[:200])
        if LEARNING_RE.search(snippet):
            learnings.append(snippet[:200])
        if PREFERENCE_RE.search(snippet):
            preferences.append(snippet[:200])

    # Build session summary
    summary_lines = []
    summary_lines.append(f"# Session Summary: {project_name} (Codex CLI)")
    summary_lines.append(f"- **Date**: {info.timestamp or 'unknown'}")
    summary_lines.append(f"- **Directory**: {info.cwd}")
    summary_lines.append(f"- **Platform**: Codex CLI ({info.cli_version})")
    summary_lines.append(f"- **Session**: {info.session_id[:12]}")
    summary_lines.append(f"- **User messages**: {len(user_msgs)}")
    summary_lines.append(f"- **Files modified**: {len(files_modified)}")
    summary_lines.append("")

    if user_msgs:
        summary_lines.append("## User Requests")
        for msg in user_msgs[-10:]:
            summary_lines.append(f"- {msg[:150]}")
        summary_lines.append("")

    if decisions:
        summary_lines.append("## Decisions")
        for d in decisions[:5]:
            summary_lines.append(f"- {d}")
        summary_lines.append("")

    if errors:
        summary_lines.append("## Errors & Fixes")
        for e in errors[:5]:
            summary_lines.append(f"- {e}")
        summary_lines.append("")

    if learnings:
        summary_lines.append("## Learnings")
        for l in learnings[:5]:
            summary_lines.append(f"- {l}")
        summary_lines.append("")

    if files_modified:
        summary_lines.append("## Files Modified")
        for f in sorted(files_modified)[:20]:
            summary_lines.append(f"- {f}")

    summary = '\n'.join(summary_lines)

    # Store to database
    db_path = get_db_path()
    if not os.path.exists(db_path):
        log_error(f"Database not found at {db_path}")
        save_processed_session(info.session_id)
        return {"status": "error", "reason": "database not found"}

    # Store session summary
    tags = f"proj:{project_name},user:codex,session-summary,platform:codex"
    metadata = json.dumps({
        "type": "session_summary",
        "importance_score": 0.6,
        "platform": "codex",
        "extraction_method": "codex_v2",
        "session_id": info.session_id[:12]
    })
    embedding = get_embedding(summary[:1000])
    summary_id = store_memory(db_path, summary, metadata, tags, embedding, memory_type='session_summary')

    # Store micro-memories (decisions, learnings, errors)
    micro_count = 0
    for category, items, importance in [
        ("decision", decisions, 0.8),
        ("gotcha", errors, 0.8),
        ("learning", learnings, 0.7),
        ("preference", preferences, 0.7),
    ]:
        for item in items[:3]:  # Max 3 per category
            micro_tags = f"proj:{project_name},user:codex,{category},platform:codex"
            micro_meta = json.dumps({
                "type": category,
                "importance_score": importance,
                "platform": "codex",
                "extraction_method": "codex_v2"
            })
            micro_emb = get_embedding(item)
            mid = store_memory(db_path, item, micro_meta, micro_tags, micro_emb, memory_type=category)
            if mid:
                micro_count += 1

    # Write summary file (same location as Claude Code summaries)
    summary_dir = os.path.join(get_data_dir(), 'memory-summaries')
    os.makedirs(summary_dir, exist_ok=True)
    summary_file = os.path.join(summary_dir, f"{project_name}-codex-latest.md")
    with open(summary_file, 'w') as f:
        f.write(summary)

    save_processed_session(info.session_id)

    return {
        "status": "ok",
        "session_id": info.session_id,
        "project": project_name,
        "user_messages": len(user_msgs),
        "files_modified": len(files_modified),
        "decisions": len(decisions),
        "errors": len(errors),
        "learnings": len(learnings),
        "summary_id": summary_id,
        "micro_memories": micro_count,
    }


def scan_recent(hours: int = 24):
    """Scan recent Codex sessions and process unprocessed ones."""
    codex_dir = os.environ.get('CODEX_HOME', os.path.expanduser('~/.codex'))
    sessions_dir = os.path.join(codex_dir, 'sessions')

    if not os.path.isdir(sessions_dir):
        print(f"No sessions directory at {sessions_dir}")
        return

    # Find rollout files modified in the last N hours
    cutoff = time.time() - (hours * 3600)
    rollout_files = glob.glob(os.path.join(sessions_dir, '**', 'rollout-*.jsonl'), recursive=True)

    processed = 0
    skipped = 0
    for path in sorted(rollout_files):
        if os.path.getmtime(path) < cutoff:
            continue
        result = process_rollout(path)
        if result['status'] == 'ok':
            processed += 1
            print(f"  Processed: {result['project']} ({result['user_messages']} msgs, "
                  f"{result['micro_memories']} micro-memories)")
        else:
            skipped += 1

    print(f"Done: {processed} processed, {skipped} skipped")


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage:")
        print("  codex_session_end.py <rollout.jsonl>    # Process one file")
        print("  codex_session_end.py --scan-recent      # Scan last 24h")
        print("  codex_session_end.py --scan-recent 48   # Scan last 48h")
        sys.exit(1)

    if sys.argv[1] == '--scan-recent':
        hours = int(sys.argv[2]) if len(sys.argv) > 2 else 24
        print(f"Scanning Codex sessions from last {hours} hours...")
        scan_recent(hours)
    else:
        path = sys.argv[1]
        if not os.path.exists(path):
            print(f"File not found: {path}")
            sys.exit(1)
        result = process_rollout(path, force=True)
        print(json.dumps(result, indent=2))
