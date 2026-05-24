"""Hook smoke suite — input shape + graceful degradation.

For each critical hook, feed a minimal valid JSON payload on stdin and
assert:
- exits 0 within the timeout
- if stdout is non-empty, it parses as JSON (hook output protocol)
- stderr does not contain Python tracebacks or `set -e` failure signatures

These tests run against the source-of-truth `hooks/` directory with a
fresh `$HOME` and an empty `~/.B12/` skeleton — they catch the common
class of bugs where a hook crashes the moment it tries to read a missing
DB / socket / config rather than degrading silently.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest


def run_hook(
    hook_path: Path,
    payload: str,
    env: dict[str, str],
    timeout: float = 15.0,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(hook_path)],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
    )


# (hook_filename, stdin_payload) — payload shape mirrors the JSON Claude
# Code / Codex emit at each lifecycle event. Each hook should accept its
# event's canonical payload AND any extra fields without crashing.
#
# Starter scope: lightweight hooks only. The heavy hooks (session-start,
# precompact, session-end) do real DB/embedding work and timing varies
# significantly with env; they are tested separately under a generous
# timeout in `test_heavy_hooks_complete_under_timeout`.
CRITICAL_HOOKS: list[tuple[str, dict]] = [
    (
        "memory-retrieval.sh",
        {
            "prompt": "how does authentication work",
            "cwd": "/tmp",
            "session_id": "smoke-test-session",
        },
    ),
    (
        "memory-instructions-loaded.sh",
        {
            "cwd": "/tmp",
            "session_id": "smoke-test-session",
        },
    ),
    (
        "memory-file-changed.sh",
        {
            "file_path": "/tmp/CLAUDE.md",
            "cwd": "/tmp",
            "session_id": "smoke-test-session",
        },
    ),
    (
        "memory-turn-end.sh",
        {
            "cwd": "/tmp",
            "session_id": "smoke-test-session",
        },
    ),
    (
        "memory-tool-failure.sh",
        {
            "tool_name": "Read",
            "tool_input": {"file_path": "/nonexistent"},
            "error": "ENOENT",
            "cwd": "/tmp",
            "session_id": "smoke-test-session",
        },
    ),
]


# Heavy hooks — generous timeout, exit-0 + no-crash only (no JSON-shape
# assertions because output varies with DB state).
HEAVY_HOOKS: list[tuple[str, dict, float]] = [
    (
        "memory-session-start.sh",
        {"source": "startup", "cwd": "/tmp", "session_id": "heavy-smoke-1"},
        90.0,
    ),
    (
        "memory-precompact.sh",
        {"trigger": "manual", "cwd": "/tmp", "session_id": "heavy-smoke-2"},
        90.0,
    ),
    (
        "memory-session-end.sh",
        {"reason": "clear", "cwd": "/tmp", "session_id": "heavy-smoke-3"},
        120.0,
    ),
]


PYTHON_TRACEBACK_SIGNATURES = (
    "Traceback (most recent call last):",
    "SyntaxError:",
    "IndentationError:",
)

BASH_CRITICAL_SIGNATURES = (
    "unbound variable",
    "command not found",
    "syntax error near unexpected token",
)


def _make_id(hook: str, payload: dict) -> str:
    # Pytest test id like: memory-session-start.sh[startup]
    discriminator = payload.get("source") or payload.get("trigger") or payload.get("reason") or "default"
    return f"{hook}[{discriminator}]"


@pytest.mark.parametrize(
    "hook_name,payload",
    CRITICAL_HOOKS,
    ids=[_make_id(h, p) for h, p in CRITICAL_HOOKS],
)
def test_hook_accepts_minimal_payload(hook_name, payload, hooks_dir, hook_env):
    hook_path = hooks_dir / hook_name
    if not hook_path.exists():
        pytest.skip(f"{hook_name} not present in hooks/ directory")

    result = run_hook(hook_path, json.dumps(payload), hook_env)

    # Exit code: 0 is required. Hooks must degrade silently on missing DB,
    # absent embed daemon, absent MCP server — never crash the host.
    assert result.returncode == 0, (
        f"{hook_name} exited {result.returncode} on minimal payload. "
        f"stderr={result.stderr[:500]!r}"
    )

    # If stdout is non-empty, it must parse as JSON (Claude Code hook output
    # protocol — only JSON additionalContext / decision blocks are valid).
    stdout = result.stdout.strip()
    if stdout:
        try:
            json.loads(stdout)
        except json.JSONDecodeError as exc:
            pytest.fail(
                f"{hook_name} stdout is not valid JSON: {exc}. "
                f"stdout={stdout[:500]!r}"
            )

    # Stderr should not contain Python tracebacks or bash critical errors.
    # Diagnostic / info messages on stderr are fine — only structural
    # failures fail the test.
    for sig in PYTHON_TRACEBACK_SIGNATURES + BASH_CRITICAL_SIGNATURES:
        assert sig not in result.stderr, (
            f"{hook_name} stderr contains {sig!r}: stderr={result.stderr[:500]!r}"
        )


@pytest.mark.parametrize(
    "hook_name,payload,timeout",
    HEAVY_HOOKS,
    ids=[h for h, _, _ in HEAVY_HOOKS],
)
def test_heavy_hooks_complete_under_timeout(
    hook_name, payload, timeout, hooks_dir, hook_env
):
    """Heavy lifecycle hooks (session-start / precompact / session-end) do
    real DB / embedding / extraction work. They should still complete within
    a generous timeout on an empty install (most paths skip when DB is
    missing); a hard timeout here indicates a regression where the hook
    blocks instead of degrading.
    """
    hook_path = hooks_dir / hook_name
    if not hook_path.exists():
        pytest.skip(f"{hook_name} not present")

    result = run_hook(hook_path, json.dumps(payload), hook_env, timeout=timeout)
    assert result.returncode == 0, (
        f"{hook_name} exited {result.returncode}. stderr={result.stderr[:500]!r}"
    )


def test_retrieval_handles_empty_db(hooks_dir, hook_env):
    """memory-retrieval should not crash when the DB doesn't exist yet."""
    hook_path = hooks_dir / "memory-retrieval.sh"
    if not hook_path.exists():
        pytest.skip("memory-retrieval.sh not present")

    payload = {"prompt": "anything", "cwd": "/tmp", "session_id": "smoke-empty-db"}
    result = run_hook(hook_path, json.dumps(payload), hook_env)

    assert result.returncode == 0, (
        f"retrieval crashed on empty DB. stderr={result.stderr[:500]!r}"
    )
