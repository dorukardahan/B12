"""
B12 Hook Adapter — platform-agnostic interface for lifecycle hooks.

Provides a common Python API that both Claude Code hooks and future
Codex CLI hooks can call. When Codex ships lifecycle hooks (session-start,
session-end, pre-command, post-command), wire them to these methods.

Current status:
  - Claude Code: hooks call shell scripts that embed Python heredocs
  - Codex CLI: only notify hook exists (agent-turn-complete)
  - This adapter: ready-to-wire Python interface for both platforms

Usage:
    from hook_adapter import HookAdapter
    adapter = HookAdapter(platform="codex", cwd="/path/to/project")

    # Session start — returns context string to inject
    context = adapter.on_session_start(session_id="abc123")

    # User prompt — returns relevant memories
    memories = adapter.on_user_prompt("how did we fix the auth bug?")

    # Session end — extracts and stores session summary
    adapter.on_session_end(transcript_path="/path/to/rollout.jsonl")

Tracking:
  - https://github.com/openai/codex/issues/12190 (governance hooks)
  - https://github.com/openai/codex/pull/9796 (comprehensive hooks — closed)
"""

import json
import os
import socket
import sqlite3
import sys
import time
from datetime import datetime, timezone

_script_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _script_dir)

from shared_patterns import get_db_path, DECISION_RE, ERROR_RE, LEARNING_RE, PREFERENCE_RE
from transcript_adapter import parse, extract_user_messages, extract_assistant_texts, extract_files_modified


def _tag_values(tags: str) -> set[str]:
    return {tag.strip() for tag in (tags or "").split(",") if tag.strip()}


def _tag_predicate(column: str = "tags") -> str:
    normalized = f"replace(replace(COALESCE({column}, ''), ', ', ','), ' ,', ',')"
    return f"(',' || {normalized} || ',') LIKE ? ESCAPE '\\'"


def _tag_param(tag: str) -> str:
    escaped = tag.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%,{escaped},%"


class HookAdapter:
    """Platform-agnostic hook handler for B12 memory system."""

    def __init__(self, platform: str = "auto", cwd: str = ""):
        """
        Args:
            platform: "claude", "codex", or "auto" (detect from environment)
            cwd: Current working directory (for project name extraction)
        """
        if platform == "auto":
            platform = self._detect_platform()
        self.platform = platform
        self.cwd = cwd or os.getcwd()
        self.project_name = os.path.basename(self.cwd)
        self.db_path = get_db_path()

    def _project_tag(self) -> str:
        return f"proj:{self.project_name}"

    def _filter_semantic_hits_for_project(self, hits: list[dict], limit: int = 3) -> list[str]:
        hit_ids = []
        for hit in hits:
            try:
                hit_ids.append(int(hit.get("id")))
            except (TypeError, ValueError):
                continue
        if not hit_ids:
            return []

        placeholders = ",".join("?" for _ in hit_ids)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                f"""
                SELECT id, content
                FROM memories
                WHERE id IN ({placeholders})
                  AND deleted_at IS NULL
                  AND {_tag_predicate("tags")}
                """,
                (*hit_ids, _tag_param(self._project_tag())),
            ).fetchall()
        finally:
            conn.close()

        by_id = {int(row["id"]): row["content"] for row in rows}
        return [by_id[mem_id] for mem_id in hit_ids if mem_id in by_id][:limit]

    @staticmethod
    def _detect_platform() -> str:
        """Detect which platform we're running under."""
        if os.environ.get("CODEX_HOME") or os.path.isdir(
            os.path.expanduser("~/.codex")
        ):
            # Check if we're in a Codex process
            if "codex" in os.environ.get("_", "").lower():
                return "codex"
        return "claude"

    def on_session_start(self, session_id: str = "") -> str:
        """
        Called at session start. Returns context string to inject.

        Searches for recent memories about the current project and
        returns a formatted context block.

        On Claude Code: called by memory-session-start.sh
        On Codex: would be called by future on-session-start hook
        """
        if not os.path.exists(self.db_path):
            return ""

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            # Recent memories for this project
            rows = conn.execute(
                """SELECT content, tags, created_at_iso
                   FROM memories
                   WHERE """ + _tag_predicate("tags") + """ AND deleted_at IS NULL
                   ORDER BY created_at DESC LIMIT 5""",
                (_tag_param(self._project_tag()),)
            ).fetchall()

            if not rows:
                return ""

            lines = [f"# B12 Memory Context ({self.project_name})"]
            for r in rows:
                content = r["content"][:200]
                lines.append(f"- {content}")

            return "\n".join(lines)
        except Exception:
            return ""
        finally:
            conn.close()

    def on_user_prompt(self, prompt: str) -> str:
        """
        Called on each user prompt. Returns relevant memories.

        Uses the embed daemon for semantic search if available,
        falls back to FTS.

        On Claude Code: called by memory-retrieval.sh
        On Codex: would be called by future on-user-prompt hook
        """
        if not prompt.strip() or not os.path.exists(self.db_path):
            return ""

        # Try daemon semantic search first
        resp = self._daemon_request(
            "semantic_search",
            query=prompt[:500],
            db_path=self.db_path,
            limit=10
        )
        if resp and resp.get("results"):
            lines = [
                f"- {content[:200]}"
                for content in self._filter_semantic_hits_for_project(resp["results"])
            ]
            if lines:
                return "\n".join(lines)

        # Fallback: FTS search
        conn = sqlite3.connect(self.db_path)
        try:
            words = [w for w in prompt.split() if len(w) > 2][:5]
            if not words:
                return ""
            fts_query = " OR ".join(f'"{w}"' for w in words)
            rows = conn.execute(
                """SELECT m.content FROM memory_content_fts fts
                   JOIN memories m ON m.id = fts.rowid
                   WHERE fts.content MATCH ? AND m.deleted_at IS NULL
                     AND """ + _tag_predicate("m.tags") + """
                   ORDER BY rank LIMIT 3""",
                (fts_query, _tag_param(self._project_tag()))
            ).fetchall()
            return "\n".join(f"- {r[0][:200]}" for r in rows) if rows else ""
        except Exception:
            return ""
        finally:
            conn.close()

    def on_session_end(self, transcript_path: str) -> dict:
        """
        Called at session end. Extracts and stores session summary.

        Parses the transcript, extracts patterns, and stores
        summary + micro-memories to the database.

        On Claude Code: called by memory-session-end.sh
        On Codex: called by codex_session_end.py (via notify hook)

        Returns dict with processing results.
        """
        from codex_session_end import process_rollout
        return process_rollout(transcript_path, force=True)

    def on_pre_tool_use(self, tool_name: str, tool_input: dict) -> dict:
        """
        Called before a tool is executed. Returns modified tool input.

        Use cases:
        - Auto-inject scope tags on memory_store calls
        - Validate memory content before storage

        On Claude Code: called by memory-tag-enforce.sh
        On Codex: would be called by future pre-command hook
        """
        # Auto-inject tags for memory store operations
        if tool_name in ("memory_store", "mcp__B12__memory_store"):
            content = tool_input.get("content", "")
            metadata = tool_input.get("metadata", {})
            if isinstance(metadata, dict):
                tags = metadata.get("tags", "")
                if isinstance(tags, (list, tuple, set)):
                    tag_values = [str(tag).strip() for tag in tags if str(tag).strip()]
                    if f"proj:{self.project_name}" not in tag_values:
                        tag_values.append(f"proj:{self.project_name}")
                    platform_tag = f"platform:{self.platform}"
                    if platform_tag not in tag_values:
                        tag_values.append(platform_tag)
                    metadata["tags"] = tag_values
                    tool_input["metadata"] = metadata
                    return tool_input

                # Ensure project tag exists
                tag_set = _tag_values(tags)
                if f"proj:{self.project_name}" not in tag_set:
                    if tags:
                        tags += f", proj:{self.project_name}"
                    else:
                        tags = f"proj:{self.project_name}"
                    tag_set.add(f"proj:{self.project_name}")
                # Ensure platform tag exists
                platform_tag = f"platform:{self.platform}"
                if platform_tag not in tag_set:
                    tags += f", {platform_tag}"
                metadata["tags"] = tags
                tool_input["metadata"] = metadata
        return tool_input

    def on_post_tool_use(self, tool_name: str, tool_input: dict, tool_output: str):
        """
        Called after a tool is executed. Side effects only.

        Use cases:
        - Track memory usage (search/store counts) for feedback
        - Update working context (active files list)

        On Claude Code: called by memory-feedback.sh / memory-working-context.sh
        On Codex: would be called by future post-command hook
        """
        # Track memory tool usage for feedback digest
        if tool_name in ("memory_search", "memory_store",
                         "mcp__B12__memory_search", "mcp__B12__memory_store"):
            self._log_tool_usage(tool_name, tool_input)

    def _log_tool_usage(self, tool_name: str, tool_input: dict):
        """Log memory tool usage for feedback analysis."""
        log_dir = os.path.join(
            os.environ.get("B12_DATA_DIR", os.path.expanduser("~/.B12")),
            "memory-logs"
        )
        os.makedirs(log_dir, exist_ok=True)
        log_file = os.path.join(log_dir, "tool-usage.jsonl")
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "tool": tool_name,
            "platform": self.platform,
            "project": self.project_name,
        }
        if "query" in tool_input:
            entry["query"] = tool_input["query"][:100]
        try:
            with open(log_file, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass

    @staticmethod
    def _daemon_request(op: str, **kwargs) -> dict | None:
        """Send a request to the embed daemon."""
        _uid = os.getuid() if hasattr(os, 'getuid') else os.getpid()
        sock_path = f"/tmp/b12-embed-{_uid}.sock"
        if not os.path.exists(sock_path):
            return None
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(10)
            sock.connect(sock_path)
            request = json.dumps({"op": op, **kwargs}) + "\n"
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
            return json.loads(response.decode().strip())
        except Exception:
            return None


# ── CLI usage ─────────────────────────────────

if __name__ == "__main__":
    adapter = HookAdapter()
    print(f"Platform: {adapter.platform}")
    print(f"Project: {adapter.project_name}")
    print(f"DB: {adapter.db_path}")
    print()

    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd == "context":
            ctx = adapter.on_session_start()
            print(ctx or "(no context)")
        elif cmd == "search" and len(sys.argv) > 2:
            result = adapter.on_user_prompt(" ".join(sys.argv[2:]))
            print(result or "(no results)")
        elif cmd == "process" and len(sys.argv) > 2:
            result = adapter.on_session_end(sys.argv[2])
            print(json.dumps(result, indent=2))
        else:
            print("Commands: context, search <query>, process <transcript>")
    else:
        print("Commands: context, search <query>, process <transcript>")
