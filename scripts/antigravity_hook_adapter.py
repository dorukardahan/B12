#!/usr/bin/env python3
"""Antigravity CLI hook adapters for B12.

Antigravity is not Gemini CLI: these adapters implement the documented
Antigravity hook wire format directly. stdout is reserved for JSON; diagnostics
must go to stderr.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


def _stderr(msg: str) -> None:
    print(f"B12 Antigravity hook: {msg}", file=sys.stderr)


def _read_payload() -> dict[str, Any]:
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return {}
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        _stderr(f"invalid JSON input ignored: {exc}")
        return {}


def _emit(obj: dict[str, Any]) -> int:
    sys.stdout.write(json.dumps(obj, separators=(",", ":")) + "\n")
    return 0


def _b12_base() -> Path:
    return Path(os.environ.get("B12_DATA_DIR", str(Path.home() / ".B12")))


def _hook_dir() -> Path:
    return Path(os.environ.get("B12_HOOK_DIR", str(Path.home() / ".B12" / "hooks")))


def _first_workspace(payload: dict[str, Any]) -> str:
    paths = payload.get("workspacePaths")
    if isinstance(paths, list):
        for path in paths:
            if isinstance(path, str) and path:
                return path
    return ""


def _conversation_id(payload: dict[str, Any]) -> str:
    value = payload.get("conversationId")
    if isinstance(value, str) and value:
        return value
    # Stable, non-sensitive fallback derived only from documented metadata.
    seed = json.dumps(
        {"transcriptPath": payload.get("transcriptPath"), "workspacePaths": payload.get("workspacePaths")},
        sort_keys=True,
        default=str,
    )
    return "agy-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def _invocation_num(payload: dict[str, Any]) -> int | None:
    value = payload.get("invocationNum")
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _normalize_tool_call(tool_name: str, arguments: Any) -> tuple[str, dict[str, Any]]:
    """Translate documented Antigravity file edits into B12's shared shape."""
    normalized_input = dict(arguments) if isinstance(arguments, dict) else {}
    edit_tools = {
        "write_to_file": "Write",
        "replace_file_content": "Edit",
        "multi_replace_file_content": "Edit",
    }
    normalized_name = edit_tools.get(tool_name, tool_name)
    if normalized_name in ("Edit", "Write") and "file_path" not in normalized_input:
        for key in ("TargetFile", "target_file", "AbsolutePath", "absolute_path", "path"):
            value = normalized_input.get(key)
            if isinstance(value, str) and value:
                normalized_input["file_path"] = value
                break
    return normalized_name, normalized_input


def _guard_path() -> Path:
    state_dir = _b12_base() / "memory-state"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir / "antigravity-preinvocation-guard.json"


def _already_injected(payload: dict[str, Any]) -> bool:
    invocation = _invocation_num(payload)
    if invocation is not None and invocation > 1:
        return True
    conversation_id = _conversation_id(payload)
    guard_key = hashlib.sha256(conversation_id.encode("utf-8")).hexdigest()
    path = _guard_path()
    try:
        data = json.loads(path.read_text()) if path.exists() else {}
        seen = data.get("seen") if isinstance(data, dict) else []
        if not isinstance(seen, list):
            seen = []
        if guard_key in seen:
            return True
        seen = ([item for item in seen if isinstance(item, str)] + [guard_key])[-200:]
        path.write_text(json.dumps({"seen": seen}, indent=2) + "\n")
        path.chmod(0o600)
    except Exception as exc:
        _stderr(f"duplicate guard unavailable, continuing conservatively: {exc}")
        return invocation is not None and invocation > 0
    return False


def _run_b12_hook(script: str, payload: dict[str, Any], timeout_s: int) -> dict[str, Any]:
    path = _hook_dir() / script
    if not path.exists():
        _stderr(f"{script} not installed; no-op")
        return {}
    try:
        proc = subprocess.run(
            ["bash", str(path)],
            input=json.dumps(payload),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_s,
            check=False,
        )
        if proc.returncode != 0:
            _stderr(f"{script} exited {proc.returncode}; ignoring output")
            return {}
        if not proc.stdout.strip():
            return {}
        parsed = json.loads(proc.stdout)
        return parsed if isinstance(parsed, dict) else {}
    except Exception as exc:
        _stderr(f"{script} failed safely: {exc}")
        return {}


def _text_content(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(filter(None, (_text_content(item) for item in value)))
    if isinstance(value, dict):
        for key in ("text", "content"):
            if key in value:
                return _text_content(value[key])
    return ""


def _convert_antigravity_transcript(transcript: str) -> Path | None:
    """Convert Antigravity trajectory JSONL into B12's Claude-shaped JSONL."""
    source = Path(transcript)
    if not transcript or not source.is_file():
        _stderr("transcript unavailable; session-end will run without transcript evidence")
        return None

    staging = _b12_base() / "memory-staging"
    staging.mkdir(parents=True, exist_ok=True)
    if staging.is_symlink():
        _stderr("private staging directory is a symlink; transcript conversion skipped")
        return None
    staging.chmod(0o700)
    fd, name = tempfile.mkstemp(prefix="antigravity-transcript-", suffix=".jsonl", dir=staging)
    converted = Path(name)
    try:
        with source.open() as src, os.fdopen(fd, "w") as dst:
            for raw_line in src:
                try:
                    step = json.loads(raw_line)
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(step, dict):
                    continue
                step_type = step.get("type")
                text = _text_content(step.get("content"))
                if step_type == "USER_INPUT" and text.strip():
                    record = {"type": "human", "message": {"content": text}}
                elif step_type == "PLANNER_RESPONSE":
                    blocks: list[dict[str, Any]] = []
                    if text.strip():
                        blocks.append({"type": "text", "text": text})
                    tool_calls = step.get("tool_calls")
                    if isinstance(tool_calls, list):
                        for call in tool_calls:
                            if not isinstance(call, dict):
                                continue
                            tool_name = call.get("name") or call.get("tool_name")
                            arguments = call.get("args", call.get("arguments", {}))
                            if isinstance(tool_name, str) and tool_name:
                                tool_name, arguments = _normalize_tool_call(tool_name, arguments)
                                blocks.append(
                                    {
                                        "type": "tool_use",
                                        "name": tool_name,
                                        "input": arguments,
                                    }
                                )
                    if not blocks:
                        continue
                    record = {"type": "assistant", "message": {"content": blocks}}
                else:
                    continue
                dst.write(json.dumps(record, separators=(",", ":")) + "\n")
            dst.flush()
            os.fsync(dst.fileno())
        converted.chmod(0o600)
        return converted
    except Exception as exc:
        try:
            os.close(fd)
        except OSError:
            pass
        converted.unlink(missing_ok=True)
        _stderr(f"transcript conversion failed safely: {type(exc).__name__}")
        return None


def pre_invocation() -> int:
    payload = _read_payload()
    if _already_injected(payload):
        return _emit({"injectSteps": []})

    b12_input = {
        "source": "startup",
        "cwd": _first_workspace(payload),
        "session_id": _conversation_id(payload),
    }
    result = _run_b12_hook("memory-session-start.sh", b12_input, timeout_s=22)
    context = (
        result.get("hookSpecificOutput", {}).get("additionalContext")
        if isinstance(result.get("hookSpecificOutput"), dict)
        else None
    )
    if isinstance(context, str) and context.strip():
        return _emit({"injectSteps": [{"ephemeralMessage": context}]})
    return _emit({"injectSteps": []})


def post_tool_use() -> int:
    payload = _read_payload()
    # The documented payload has step/error metadata but no tool call or result.
    # It cannot safely support B12 retrieval/checkpoint semantics.
    step = payload.get("stepIdx")
    step_receipt = step if isinstance(step, int) and not isinstance(step, bool) else "unknown"
    _stderr(f"PostToolUse observed step={step_receipt} error={bool(payload.get('error'))}; no-op")
    return _emit({})


def stop() -> int:
    payload = _read_payload()
    if payload.get("fullyIdle") is not True:
        _stderr("Stop ignored because fullyIdle is not true")
        return _emit({"decision": "stop"})

    transcript = payload.get("transcriptPath")
    if not isinstance(transcript, str):
        transcript = ""
    converted = _convert_antigravity_transcript(transcript)
    b12_input = {
        "session_id": _conversation_id(payload),
        "reason": str(payload.get("terminationReason") or "stop"),
        "cwd": _first_workspace(payload),
        "transcript_path": str(converted) if converted else "",
        "cleanup_transcript": converted is not None,
    }
    _run_b12_hook("memory-session-end.sh", b12_input, timeout_s=40)
    return _emit({"decision": "stop"})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="B12 Antigravity hook adapter")
    parser.add_argument("event", choices=["PreInvocation", "PostToolUse", "Stop"])
    args = parser.parse_args(argv)
    if args.event == "PreInvocation":
        return pre_invocation()
    if args.event == "PostToolUse":
        return post_tool_use()
    return stop()


if __name__ == "__main__":
    raise SystemExit(main())
