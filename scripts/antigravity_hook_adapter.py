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
        for p in paths:
            if isinstance(p, str) and p:
                return p
    for key in ("cwd", "workspacePath"):
        val = payload.get(key)
        if isinstance(val, str) and val:
            return val
    return ""


def _conversation_id(payload: dict[str, Any]) -> str:
    for key in ("conversationId", "sessionId", "session_id"):
        val = payload.get(key)
        if isinstance(val, str) and val:
            return val
    # Stable, non-sensitive fallback: hash transcript + workspace metadata.
    seed = json.dumps(
        {"transcriptPath": payload.get("transcriptPath"), "workspacePaths": payload.get("workspacePaths")},
        sort_keys=True,
        default=str,
    )
    return "agy-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def _invocation_num(payload: dict[str, Any]) -> int | None:
    for key in ("invocationNum", "invocationNumber"):
        val = payload.get(key)
        if isinstance(val, int):
            return val
        if isinstance(val, str) and val.isdigit():
            return int(val)
    return None


def _guard_path() -> Path:
    state_dir = _b12_base() / "memory-state"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir / "antigravity-preinvocation-guard.json"


def _already_injected(payload: dict[str, Any]) -> bool:
    inv = _invocation_num(payload)
    if inv is not None and inv > 1:
        return True
    cid = _conversation_id(payload)
    path = _guard_path()
    try:
        data = json.loads(path.read_text()) if path.exists() else {}
        seen = data.get("seen") if isinstance(data, dict) else []
        if not isinstance(seen, list):
            seen = []
        if cid in seen:
            return True
        seen = ([x for x in seen if isinstance(x, str)] + [cid])[-200:]
        path.write_text(json.dumps({"seen": seen}, indent=2) + "\n")
    except Exception as exc:
        _stderr(f"duplicate guard unavailable, continuing conservatively: {exc}")
        return inv is not None and inv > 0
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
        if proc.stderr.strip():
            _stderr(proc.stderr.strip()[-4000:])
        if not proc.stdout.strip():
            return {}
        parsed = json.loads(proc.stdout)
        return parsed if isinstance(parsed, dict) else {}
    except Exception as exc:
        _stderr(f"{script} failed safely: {exc}")
        return {}


def pre_invocation() -> int:
    payload = _read_payload()
    if _already_injected(payload):
        return _emit({"injectSteps": []})

    source = "startup"
    inv = _invocation_num(payload)
    if inv is not None and inv > 0:
        source = "resume"
    b12_input = {"source": source, "cwd": _first_workspace(payload)}
    result = _run_b12_hook("memory-session-start.sh", b12_input, timeout_s=22)
    ctx = (
        result.get("hookSpecificOutput", {}).get("additionalContext")
        if isinstance(result.get("hookSpecificOutput"), dict)
        else None
    )
    if isinstance(ctx, str) and ctx.strip():
        return _emit({"injectSteps": [{"ephemeralMessage": ctx}]})
    return _emit({"injectSteps": []})


def post_tool_use() -> int:
    payload = _read_payload()
    # Antigravity PostToolUse does not guarantee enough tool result text for B12
    # retrieval/checkpoint semantics. Consume and validate documented metadata,
    # log a compact receipt only, and return the required no-op JSON.
    tool = payload.get("toolName") or payload.get("tool_name") or "unknown"
    cid = _conversation_id(payload)
    _stderr(f"PostToolUse observed tool={tool!s} conversation={cid[:24]}; no context injection supported for this event")
    return _emit({})


def stop() -> int:
    payload = _read_payload()
    if payload.get("fullyIdle") is not True:
        _stderr("Stop ignored because fullyIdle is not true")
        return _emit({"decision": "stop"})

    transcript = payload.get("transcriptPath")
    if not isinstance(transcript, str):
        transcript = ""
    b12_input = {
        "session_id": _conversation_id(payload),
        "reason": str(payload.get("terminationReason") or "stop"),
        "cwd": _first_workspace(payload),
        "transcript_path": transcript,
    }
    # Run synchronously but bounded; the underlying hook is also timeout guarded.
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
