"""
B12 Proactive Surfacing Engine — context-aware memory injection.

Surfaces relevant memories when the user interacts with files or encounters errors.
Designed to be called from hooks/memory-proactive-surface.sh.
"""
import json, os, socket, sqlite3, time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


@dataclass
class SurfacingResult:
    """Result from a surfacing attempt."""
    memories: list[dict] = field(default_factory=list)  # [{id, content, memory_type, score}]
    surfaced: bool = False  # True if memories were surfaced
    reason: str = ""  # Why surfacing did/didn't happen


# Constants
SIMILARITY_THRESHOLD = 0.80
STRENGTH_THRESHOLD = 0.5
AGE_THRESHOLD_SECONDS = 86400  # 24 hours
MAX_SURFACED_PER_INJECTION = 3
RATE_LIMIT_TOOL_CALLS = 5  # surface at most once per N tool calls
RATE_LIMIT_COOLDOWN = 60  # seconds between surfacings
MAX_CONTEXT_CHARS = 2000  # max characters for additionalContext output


def surface(trigger_type: str, context: str, db_path: str = "",
            state_path: str = "") -> SurfacingResult:
    """
    Main entry point for proactive surfacing.

    Args:
        trigger_type: "file" (file path), "error" (error message), or "topic" (keywords)
        context: The trigger context (file path, error text, or topic text)
        db_path: Path to SQLite database (auto-detected if empty)
        state_path: Path to surfacing state file (auto-detected if empty)

    Returns:
        SurfacingResult with relevant memories (if any) and reason
    """
    # 1. Auto-detect paths if not provided
    if not db_path:
        db_path = _get_db_path()
    if not state_path:
        b12_base = os.environ.get("B12_DATA_DIR", os.path.expanduser("~/.B12"))
        state_path = os.path.join(b12_base, "surfacing-state.json")

    # 2. Check rate limit
    allowed, reason = check_rate_limit(state_path)
    if not allowed:
        return SurfacingResult(surfaced=False, reason=reason)

    # 3. Build query based on trigger type
    query = _build_query(trigger_type, context)
    if not query:
        return SurfacingResult(surfaced=False, reason="No searchable query from trigger")

    # 4. Query embed daemon for similar memories
    daemon_results = _daemon_search(query, db_path, limit=20)
    if not daemon_results:
        return SurfacingResult(surfaced=False, reason="Daemon unavailable or no results")

    # 5. Filter results (single DB connection for all lookups)
    state = _load_state(state_path)
    now = time.time()
    now_iso = datetime.fromtimestamp(now, tz=timezone.utc).isoformat()
    surfaced_set = set(state.get("surfaced_ids", []))
    filtered = []

    # Pre-filter by similarity and already-surfaced before hitting DB
    candidate_ids = []
    candidates_by_id = {}
    for mem in daemon_results:
        if mem.get("score", 0) < SIMILARITY_THRESHOLD:
            continue
        if mem["id"] in surfaced_set:
            continue
        candidate_ids.append(mem["id"])
        candidates_by_id[mem["id"]] = mem

    if not candidate_ids:
        _increment_tool_calls(state_path, state)
        return SurfacingResult(surfaced=False, reason="No memories passed similarity/surfaced filters")

    # Batch fetch from DB with single connection
    mem_infos = _get_memory_infos_batch(db_path, candidate_ids)

    for mid in candidate_ids:
        mem_info = mem_infos.get(mid)
        if not mem_info:
            continue

        # Strength filter
        if (mem_info.get("strength") or 1.0) < STRENGTH_THRESHOLD:
            continue

        # Age filter (must be older than 24h)
        created_at = mem_info.get("created_at", now)
        if created_at is None:
            created_at = now
        if now - created_at < AGE_THRESHOLD_SECONDS:
            continue

        # Skip soft-deleted
        if mem_info.get("deleted_at"):
            continue

        # Skip expired (valid_until is ISO text, compared with datetime('now'))
        valid_until = mem_info.get("valid_until")
        if valid_until and valid_until != "" and valid_until < now_iso:
            continue

        mem = candidates_by_id[mid]
        filtered.append({
            "id": mid,
            "content": mem_info.get("content", mem.get("content", "")),
            "memory_type": mem_info.get("memory_type", "general"),
            "score": mem["score"],
            "tags": mem_info.get("tags", ""),
        })

        if len(filtered) >= MAX_SURFACED_PER_INJECTION:
            break

    if not filtered:
        # Update tool call counter even when not surfacing
        _increment_tool_calls(state_path, state)
        return SurfacingResult(surfaced=False, reason="No memories passed filters")

    # 6. Update state and return
    surfaced_ids = [m["id"] for m in filtered]
    update_surfacing_state(state_path, surfaced_ids, state)

    return SurfacingResult(
        memories=filtered,
        surfaced=True,
        reason=f"Surfaced {len(filtered)} memories for {trigger_type} trigger"
    )


def format_for_context(result: SurfacingResult) -> str:
    """Format surfacing result as additionalContext string (max 2000 chars)."""
    if not result.surfaced or not result.memories:
        return ""

    lines = ["B12 proactive context (from past sessions):"]
    chars = len(lines[0])

    for mem in result.memories:
        mem_line = f"  [{mem['memory_type']}] {mem['content']}"
        if chars + len(mem_line) + 1 > MAX_CONTEXT_CHARS:
            # Truncate this memory to fit
            remaining = MAX_CONTEXT_CHARS - chars - 10
            if remaining > 50:
                lines.append(f"  [{mem['memory_type']}] {mem['content'][:remaining]}...")
            break
        lines.append(mem_line)
        chars += len(mem_line) + 1

    return "\n".join(lines)


def check_rate_limit(state_path: str) -> tuple[bool, str]:
    """Check if surfacing is allowed based on rate limits.

    Returns (allowed, reason).
    """
    state = _load_state(state_path)
    now = time.time()

    # Check cooldown
    last_surfaced = state.get("last_surfaced_at", 0)
    if now - last_surfaced < RATE_LIMIT_COOLDOWN:
        remaining = int(RATE_LIMIT_COOLDOWN - (now - last_surfaced))
        return False, f"Cooldown: {remaining}s remaining"

    # Check tool call count
    tool_calls_since = state.get("tool_calls_since", 0)
    if tool_calls_since < RATE_LIMIT_TOOL_CALLS:
        return False, f"Rate limit: {tool_calls_since}/{RATE_LIMIT_TOOL_CALLS} tool calls"

    return True, "Rate limit passed"


def update_surfacing_state(state_path: str, surfaced_ids: list[int],
                           state: dict | None = None):
    """Update state file after successful surfacing."""
    if state is None:
        state = _load_state(state_path)

    state["last_surfaced_at"] = time.time()
    state["tool_calls_since"] = 0
    existing_ids = state.get("surfaced_ids", [])
    combined = existing_ids + surfaced_ids
    # Cap to prevent unbounded growth across long sessions
    state["surfaced_ids"] = combined[-200:] if len(combined) > 200 else combined

    _save_state(state_path, state)


# -- Internal helpers ----------------------------------------------------------

def _get_db_path() -> str:
    """Auto-detect the B12 database path."""
    import sys
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


def _build_query(trigger_type: str, context: str) -> str:
    """Build a search query from the trigger context."""
    if trigger_type == "file":
        # Extract meaningful parts from file path
        parts = context.replace("\\", "/").split("/")
        # Skip common prefixes that add no semantic value
        noise = {"Users", "home", "Desktop", "Documents", "src", "var",
                 "tmp", "opt", "usr", "lib", "etc", ""}
        meaningful = [p for p in parts if p not in noise]
        if meaningful:
            return " ".join(meaningful[-3:])
        return ""
    elif trigger_type == "error":
        # Use first 200 chars of error message as query
        return context[:200].strip()
    elif trigger_type == "topic":
        return context[:200].strip()
    return ""


def _daemon_search(query: str, db_path: str, limit: int = 20) -> list[dict]:
    """Query embed daemon for semantic search results.

    Protocol: newline-delimited JSON over Unix socket at /tmp/b12-embed-{UID}.sock
    Matches the pattern in b12_mcp_server.py daemon_request().
    """
    uid = os.getuid() if hasattr(os, 'getuid') else os.getpid()
    # Hardcode /tmp/ — macOS TMPDIR varies per session, causing socket mismatch
    sock_path = f"/tmp/b12-embed-{uid}.sock"

    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(2.0)  # Strict timeout for surfacing (latency-sensitive)
        s.connect(sock_path)

        request = json.dumps({
            "op": "semantic_search",
            "query": query,
            "db_path": db_path,
            "limit": limit,
        })
        s.sendall((request + "\n").encode())

        data = b""
        while b"\n" not in data:
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk

        resp = json.loads(data.decode().strip())
        if resp.get("ok") and resp.get("results"):
            return resp["results"]
        return []
    except Exception:
        return []
    finally:
        s.close()


def _get_memory_info(db_path: str, memory_id: int) -> dict | None:
    """Get memory details from the database (single lookup)."""
    try:
        conn = sqlite3.connect(db_path, timeout=2)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT content, memory_type, tags, strength, created_at, "
            "deleted_at, valid_until, metadata FROM memories WHERE id = ?",
            (memory_id,)
        ).fetchone()
        conn.close()
        if row:
            return dict(row)
        return None
    except Exception:
        return None


def _get_memory_infos_batch(db_path: str, memory_ids: list[int]) -> dict[int, dict]:
    """Batch-fetch memory details with a single DB connection."""
    if not memory_ids:
        return {}
    try:
        conn = sqlite3.connect(db_path, timeout=2)
        conn.row_factory = sqlite3.Row
        placeholders = ",".join("?" for _ in memory_ids)
        rows = conn.execute(
            f"SELECT id, content, memory_type, tags, strength, created_at, "
            f"deleted_at, valid_until, metadata FROM memories WHERE id IN ({placeholders})",
            memory_ids,
        ).fetchall()
        conn.close()
        return {row["id"]: dict(row) for row in rows}
    except Exception:
        return {}


def _load_state(state_path: str) -> dict:
    """Load surfacing state from file."""
    try:
        with open(state_path, 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {
            "last_surfaced_at": 0,
            "tool_calls_since": 0,
            "surfaced_ids": [],
            "session_start": time.time(),
        }


def _save_state(state_path: str, state: dict):
    """Save surfacing state atomically (write-to-temp + rename)."""
    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    tmp_path = state_path + ".tmp"
    try:
        with open(tmp_path, 'w') as f:
            json.dump(state, f)
        os.replace(tmp_path, state_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def _increment_tool_calls(state_path: str, state: dict | None = None):
    """Increment the tool call counter without surfacing."""
    if state is None:
        state = _load_state(state_path)
    state["tool_calls_since"] = state.get("tool_calls_since", 0) + 1
    _save_state(state_path, state)
