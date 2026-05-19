"""
B12 Transcript Adapter — Unified parser for Claude Code and Codex CLI transcripts.

Normalizes both formats into a common structure so session-end processing
can work identically across platforms.

Claude Code format: JSONL with {type: "human"|"assistant", message: {content: [...]}}
Codex CLI format:   JSONL with {type: "response_item"|"event_msg", payload: {...}}
"""

import json
import os
import re
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ToolUse:
    name: str
    input_data: dict = field(default_factory=dict)
    output: str = ""


@dataclass
class Message:
    role: str           # "user" | "assistant" | "system"
    content: str = ""   # Text content (combined from all text blocks)
    tool_uses: list = field(default_factory=list)  # List of ToolUse
    files_modified: list = field(default_factory=list)
    timestamp: str = ""


@dataclass
class SessionInfo:
    session_id: str = ""
    cwd: str = ""
    platform: str = ""      # "claude" | "codex"
    model: str = ""
    cli_version: str = ""
    timestamp: str = ""
    # Git provenance from Codex rollout's session_meta (SessionMetaLine.git)
    git_branch: str = ""
    git_repo_url: str = ""
    git_commit: str = ""
    # SessionMeta.source value — "cli"/"vscode"/"exec"/"mcp"/"custom"/etc.
    # Plus the related SessionMeta.originator (rust constant for the
    # external-agent-migration writer is "claude").
    source: str = ""
    originator: str = ""


def detect_format(path: str) -> str:
    """Detect transcript format from first line. Returns 'claude' / 'codex' / 'continue'."""
    # Continue.dev writes a single JSON object per session (not JSONL); the
    # whole file is one valid JSON object with a `history` array. Easy to
    # detect by trying a whole-file parse first if the path looks Continue-
    # shaped (~/.continue/sessions/*.json).
    if "/.continue/sessions/" in path and path.endswith(".json"):
        try:
            with open(path, "r") as f:
                obj = json.load(f)
            if isinstance(obj, dict) and ("history" in obj or "sessionId" in obj):
                return "continue"
        except (json.JSONDecodeError, OSError):
            pass
    try:
        with open(path, 'r') as f:
            first_line = f.readline().strip()
            if not first_line:
                return "unknown"
            obj = json.loads(first_line)
            if obj.get('type') == 'session_meta':
                return "codex"
            if obj.get('type') in ('human', 'user', 'assistant', 'summary', 'progress'):
                return "claude"
            # Fallback: check for payload key (Codex) vs message key (Claude)
            if 'payload' in obj:
                return "codex"
            if 'message' in obj:
                return "claude"
    except (json.JSONDecodeError, IOError, KeyError):
        pass
    return "unknown"


def parse(path: str, tail_lines: int = 0) -> tuple:
    """
    Parse a transcript file into (SessionInfo, list[Message]).

    Args:
        path: Path to the JSONL transcript file
        tail_lines: If > 0, only read last N lines (optimization for large files)

    Returns:
        (SessionInfo, [Message]) tuple
    """
    fmt = detect_format(path)
    if fmt == "claude":
        return _parse_claude(path, tail_lines)
    elif fmt == "codex":
        return _parse_codex(path, tail_lines)
    elif fmt == "continue":
        return _parse_continue(path)
    else:
        return SessionInfo(platform="unknown"), []


def _read_lines(path: str, tail_lines: int = 0) -> list:
    """Read lines from file, optionally only last N lines."""
    if tail_lines > 0:
        import subprocess
        result = subprocess.run(
            ['tail', '-n', str(tail_lines), path],
            capture_output=True, text=True, timeout=10
        )
        return result.stdout.splitlines() if result.returncode == 0 else []
    else:
        with open(path, 'r') as f:
            return f.readlines()


# ─── Claude Code Parser ───────────────────────────────

def _parse_claude(path: str, tail_lines: int = 0) -> tuple:
    """Parse Claude Code transcript JSONL."""
    info = SessionInfo(platform="claude")
    messages = []
    lines = _read_lines(path, tail_lines)

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue

        msg_type = obj.get('type', '')

        # Extract session metadata from any entry that has it
        if 'sessionId' in obj and not info.session_id:
            info.session_id = obj['sessionId']
        if 'cwd' in obj and not info.cwd:
            info.cwd = obj['cwd']

        if msg_type in ('summary', 'system', 'progress', 'file-history-snapshot'):
            continue

        if msg_type in ('human', 'user'):
            msg = Message(role="user", timestamp=obj.get('timestamp', ''))
            content = obj.get('message', {}).get('content', '')
            if isinstance(content, str):
                msg.content = content
            elif isinstance(content, list):
                texts = []
                for block in content:
                    if isinstance(block, dict) and block.get('type') == 'text':
                        texts.append(block['text'])
                msg.content = '\n'.join(texts)
            if msg.content.strip():
                messages.append(msg)

        elif msg_type == 'assistant':
            msg = Message(role="assistant", timestamp=obj.get('timestamp', ''))
            content = obj.get('message', {}).get('content', [])
            texts = []
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get('type') in ('text', 'output_text'):
                        texts.append(block.get('text', ''))
                    elif block.get('type') == 'tool_use':
                        tool = ToolUse(
                            name=block.get('name', ''),
                            input_data=block.get('input', {})
                        )
                        msg.tool_uses.append(tool)
                        # Track file modifications
                        if block.get('name') in ('Edit', 'Write') and 'file_path' in block.get('input', {}):
                            msg.files_modified.append(block['input']['file_path'])
                    elif block.get('type') == 'tool_result':
                        # Match to previous tool_use by index
                        if msg.tool_uses:
                            result_content = block.get('content', '')
                            if isinstance(result_content, list):
                                result_content = '\n'.join(
                                    b.get('text', '') for b in result_content
                                    if isinstance(b, dict) and b.get('type') == 'text'
                                )
                            msg.tool_uses[-1].output = str(result_content)[:500]
            msg.content = '\n'.join(texts)
            messages.append(msg)

    return info, messages


# ─── Codex CLI Parser ─────────────────────────────────

def _parse_codex(path: str, tail_lines: int = 0) -> tuple:
    """Parse Codex CLI rollout JSONL."""
    info = SessionInfo(platform="codex")
    messages = []
    # Track function calls by call_id to match with outputs
    pending_calls = {}
    lines = _read_lines(path, tail_lines)

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue

        entry_type = obj.get('type', '')
        timestamp = obj.get('timestamp', '')
        payload = obj.get('payload', {})

        if entry_type == 'session_meta':
            info.session_id = payload.get('id', '')
            info.cwd = payload.get('cwd', '')
            info.cli_version = payload.get('cli_version', '')
            info.timestamp = payload.get('timestamp', '')
            info.model = payload.get('model_provider', '')
            # SessionMetaLine flattens SessionMeta + Option<GitInfo>.
            # Source / originator are how external-agent-sessions
            # marks Claude-Code-imported rollouts.
            info.source = str(payload.get('source', '') or '')
            info.originator = str(payload.get('originator', '') or '')
            git = obj.get('git') or payload.get('git') or {}
            if isinstance(git, dict):
                info.git_branch = str(git.get('branch') or '')
                info.git_repo_url = str(git.get('repository_url') or '')
                info.git_commit = str(git.get('commit_hash') or '')
            continue

        if entry_type == 'turn_context':
            # Extract model info from turn context
            if payload.get('model'):
                info.model = payload['model']
            continue

        if entry_type != 'response_item':
            continue

        item_type = payload.get('type', '')
        role = payload.get('role', '')

        # User messages
        if item_type == 'message' and role == 'user':
            msg = Message(role="user", timestamp=timestamp)
            content = payload.get('content', [])
            if isinstance(content, list):
                texts = []
                for block in content:
                    if isinstance(block, dict) and block.get('type') == 'input_text':
                        text = block.get('text', '')
                        # Skip system injections (AGENTS.md, permissions, etc.)
                        if text.startswith('<permissions') or text.startswith('<app-context'):
                            continue
                        if text.startswith('# AGENTS.md instructions'):
                            continue
                        if text.startswith('<environment_context'):
                            continue
                        texts.append(text)
                msg.content = '\n'.join(texts)
            # Only add if there's actual user content
            if msg.content.strip():
                messages.append(msg)

        # Assistant messages
        elif item_type == 'message' and role == 'assistant':
            msg = Message(role="assistant", timestamp=timestamp)
            content = payload.get('content', [])
            if isinstance(content, list):
                texts = []
                for block in content:
                    if isinstance(block, dict) and block.get('type') == 'output_text':
                        texts.append(block.get('text', ''))
                msg.content = '\n'.join(texts)
            if msg.content.strip():
                messages.append(msg)

        # Shell commands (exec_command)
        elif item_type == 'function_call':
            call_id = payload.get('call_id', '')
            name = payload.get('name', '')
            args_raw = payload.get('arguments', '{}')
            try:
                args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
            except json.JSONDecodeError:
                args = {'raw': args_raw}

            tool = ToolUse(name=name, input_data=args)
            pending_calls[call_id] = tool

        # Shell command outputs
        elif item_type == 'function_call_output':
            call_id = payload.get('call_id', '')
            output = payload.get('output', '')
            if call_id in pending_calls:
                pending_calls[call_id].output = str(output)[:500]

        # MCP / custom tool calls (apply_patch, MCP tools)
        elif item_type == 'custom_tool_call':
            call_id = payload.get('call_id', '')
            name = payload.get('name', '')
            input_data = payload.get('input', '')
            tool = ToolUse(name=name, input_data={'raw': str(input_data)[:500]})
            pending_calls[call_id] = tool

            # Track file modifications from apply_patch
            if name == 'apply_patch' and isinstance(input_data, str):
                # Extract file paths from patch headers
                for match in re.finditer(r'\*\*\* (?:Update|Add) File: (.+)', input_data):
                    filepath = match.group(1).strip()
                    # Attach to a message
                    if messages and messages[-1].role == 'assistant':
                        messages[-1].files_modified.append(filepath)

        elif item_type == 'custom_tool_call_output':
            call_id = payload.get('call_id', '')
            output = payload.get('output', '')
            if call_id in pending_calls:
                pending_calls[call_id].output = str(output)[:500]

    # Attach pending tool calls to their nearest assistant messages
    # Group by approximate position (simplified: attach all to assistant messages)
    tool_list = list(pending_calls.values())
    for msg in messages:
        if msg.role == 'assistant' and tool_list:
            # Simple heuristic: distribute tools to assistant messages
            pass  # Tools are tracked separately for extraction purposes

    # Store all tools on session info for extraction
    info._all_tools = tool_list

    return info, messages


# ─── Continue.dev Parser ──────────────────────────────

def _parse_continue(path: str) -> tuple:
    """Parse a Continue.dev session JSON (~/.continue/sessions/<id>.json).
    Continue stores one whole-file JSON object per session (NOT JSONL).
    Shape: {sessionId, title, history: [{role, message, ...}], ...}
    where each history entry's `message.content` is a string or a list
    of content blocks. Confirmed against upstream
    `core/util/paths.ts:getSessionFilePath`.
    """
    info = SessionInfo(platform="continue")
    messages: list = []
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            obj = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return info, messages

    info.session_id = str(obj.get("sessionId") or "")
    info.cwd = str(obj.get("workspaceDirectory") or obj.get("cwd") or "")
    info.timestamp = str(obj.get("createdAt") or obj.get("title") or "")

    history = obj.get("history") or []
    if not isinstance(history, list):
        return info, messages

    for entry in history:
        if not isinstance(entry, dict):
            continue
        role = entry.get("role") or entry.get("message", {}).get("role") or ""
        if role not in ("user", "assistant", "system"):
            continue
        raw_msg = entry.get("message") or entry
        content = ""
        if isinstance(raw_msg, dict):
            inner = raw_msg.get("content")
            if isinstance(inner, str):
                content = inner
            elif isinstance(inner, list):
                texts = []
                for block in inner:
                    if isinstance(block, dict):
                        if block.get("type") in ("text", "output_text"):
                            texts.append(block.get("text", ""))
                        elif "content" in block and isinstance(block["content"], str):
                            texts.append(block["content"])
                content = "\n".join(t for t in texts if t)
        if not content.strip():
            continue
        msg = Message(
            role=role if role != "system" else "user",
            timestamp=str(entry.get("timestamp") or ""),
            content=content,
        )
        # Tool uses, when Continue surfaces them per Claude Code's
        # message-content-block shape.
        if isinstance(raw_msg, dict) and isinstance(raw_msg.get("content"), list):
            for block in raw_msg["content"]:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tool = ToolUse(
                        name=block.get("name", ""),
                        input_data=block.get("input", {}) or {},
                    )
                    msg.tool_uses.append(tool)
                    if tool.name in ("Edit", "Write") and isinstance(tool.input_data, dict):
                        fp = tool.input_data.get("file_path")
                        if fp:
                            msg.files_modified.append(str(fp))
        messages.append(msg)

    return info, messages


def extract_files_modified(messages: list) -> set:
    """Extract all modified file paths from messages."""
    files = set()
    for msg in messages:
        files.update(msg.files_modified)
    return files


def extract_user_messages(messages: list, max_count: int = 20) -> list:
    """Extract user message texts, limited to max_count."""
    user_msgs = []
    for msg in messages:
        if msg.role == 'user' and msg.content.strip():
            user_msgs.append(msg.content[:300])
    return user_msgs[-max_count:]


def extract_assistant_texts(messages: list) -> list:
    """Extract assistant message texts."""
    texts = []
    for msg in messages:
        if msg.role == 'assistant' and msg.content.strip():
            texts.append(msg.content)
    return texts


# ─── OpenCode `[M#]` macro verbs ──────────────────────
# Format: `[M#<type>] <content>` or `[M#<type>:<importance>] <content>`
# `type` ∈ {decision, learning, gotcha, preference, architecture, pattern};
# `importance` ∈ {1,2,3} maps to 1.0/1.5/2.0. Default 1.0.
# Codex PR #50 round 3 P2: anchor to line start (with optional leading
# whitespace + optional `>` quote marker) so quoted examples and
# documentation snippets like the AGENTS template's
# `[M#decision] Chose PostgreSQL` are NOT ingested as real macros when
# the model echoes them mid-paragraph. Without anchoring, the AGENTS
# template would itself pollute memory the first time a user asked
# Claude to read it.
_MACRO_VERB_RE = re.compile(
    r"(?m)^[ \t>]*"
    r"\[M#(?P<type>[a-zA-Z_][a-zA-Z0-9_-]{0,31})"
    r"(?::(?P<importance>[123]))?\]\s+(?P<content>[^\n]{4,400})",
)
_MACRO_TYPE_ALIASES = {
    "dec": "decision", "err": "gotcha", "err_fix": "gotcha", "fix": "gotcha",
    "learn": "learning", "pref": "preference", "arch": "architecture",
}
# Codex PR #50 round 2 P2: drop matches with unknown types so the
# memory_type column stays canonical. Typos like `[M#preferrence]` and
# freeform `[M#todo]` would otherwise pollute downstream filtering.
_MACRO_TYPE_ALLOWLIST = {
    "decision", "learning", "gotcha", "preference", "architecture", "pattern",
}
_MACRO_IMPORTANCE = {"1": 1.0, "2": 1.5, "3": 2.0}


def extract_macro_verbs(messages: list, max_count: int = 20) -> list:
    """Scan both sides of the transcript for `[M#<type>] <content>` lines.

    Returns list of {type, importance, content, source}. Dedupes within a
    session by (type, content[:120]).
    """
    seen = set()
    out = []
    for msg in messages:
        text = (msg.content or "")
        if not text or "[M#" not in text:
            continue
        for m in _MACRO_VERB_RE.finditer(text):
            t = (m.group("type") or "").lower()
            t = _MACRO_TYPE_ALIASES.get(t, t)
            if t not in _MACRO_TYPE_ALLOWLIST:
                continue
            content = m.group("content").strip()
            key = (t, content[:120])
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "type": t,
                "importance": _MACRO_IMPORTANCE.get(m.group("importance") or "1", 1.0),
                "content": content,
                "source": msg.role,
            })
            if len(out) >= max_count:
                return out
    return out


# ─── Quick test ───────────────────────────────────────

if __name__ == '__main__':
    import sys
    if len(sys.argv) < 2:
        print("Usage: transcript_adapter.py <path-to-jsonl>")
        sys.exit(1)

    path = sys.argv[1]
    fmt = detect_format(path)
    print(f"Format: {fmt}")

    info, messages = parse(path)
    print(f"Platform: {info.platform}")
    print(f"Session ID: {info.session_id}")
    print(f"CWD: {info.cwd}")
    print(f"Messages: {len(messages)}")

    user_msgs = extract_user_messages(messages)
    print(f"User messages: {len(user_msgs)}")
    for m in user_msgs[:5]:
        print(f"  - {m[:80]}...")

    files = extract_files_modified(messages)
    print(f"Files modified: {len(files)}")
    for f in sorted(files)[:10]:
        print(f"  - {f}")

    asst = extract_assistant_texts(messages)
    print(f"Assistant messages: {len(asst)}")
