"""Focused subprocess tests for every Antigravity lifecycle adapter event."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
ADAPTER = ROOT / "scripts" / "antigravity_hook_adapter.py"
MANIFEST = ROOT / "plugins" / "antigravity" / "b12" / "hooks.json"
EVENTS = ("PreInvocation", "PostToolUse", "Stop")


def _fixture_env(tmp_path: Path) -> tuple[dict[str, str], Path]:
    home = tmp_path / "home"
    data = tmp_path / "data"
    hooks = tmp_path / "hooks"
    home.mkdir()
    data.mkdir()
    hooks.mkdir()
    (hooks / "memory-session-start.sh").write_text(
        "#!/bin/sh\n"
        "cat > \"$B12_DATA_DIR/pre-invocation.json\"\n"
        "printf '%s\\n' '{\"hookSpecificOutput\":{\"additionalContext\":\"public fixture context\"}}'\n",
        encoding="utf-8",
    )
    (hooks / "memory-session-end.sh").write_text(
        "#!/bin/sh\ncat > \"$B12_DATA_DIR/stop.json\"\nprintf '%s\\n' '{}'\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "B12_DATA_DIR": str(data),
            "B12_HOOK_DIR": str(hooks),
        }
    )
    return env, data


def _run(event: str, raw_payload: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ADAPTER), event],
        input=raw_payload,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        timeout=10,
        check=False,
    )


def test_manifest_routes_every_supported_event_to_the_adapter():
    hooks = json.loads(MANIFEST.read_text(encoding="utf-8"))["b12-memory"]
    assert tuple(hooks) == EVENTS
    for event in EVENTS:
        entries = hooks[event]
        commands = []
        for entry in entries:
            commands.extend(
                hook["command"] for hook in entry.get("hooks", [entry])
            )
        assert commands
        assert all("antigravity_hook_adapter.py" in command for command in commands)
        assert all(command.endswith(f" {event}") for command in commands)


@pytest.mark.parametrize("event", EVENTS)
def test_valid_manifest_event_output_and_dispatch(tmp_path, event):
    env, data = _fixture_env(tmp_path)
    transcript = tmp_path / "trajectory.jsonl"
    transcript.write_text(
        json.dumps({"type": "USER_INPUT", "content": "public fixture request"}) + "\n",
        encoding="utf-8",
    )
    payloads = {
        "PreInvocation": {
            "conversationId": "public-conversation",
            "invocationNum": 1,
            "workspacePaths": [str(tmp_path / "workspace")],
        },
        "PostToolUse": {
            "conversationId": "public-conversation",
            "stepIdx": 3,
            "error": "",
        },
        "Stop": {
            "conversationId": "public-conversation",
            "fullyIdle": True,
            "terminationReason": "complete",
            "workspacePaths": [str(tmp_path / "workspace")],
            "transcriptPath": str(transcript),
        },
    }

    proc = _run(event, json.dumps(payloads[event]), env)

    assert proc.returncode == 0, proc.stderr
    output = json.loads(proc.stdout)
    if event == "PreInvocation":
        assert output == {"injectSteps": [{"ephemeralMessage": "public fixture context"}]}
        delegated = json.loads((data / "pre-invocation.json").read_text())
        assert delegated["source"] == "startup"
        assert delegated["session_id"] == "public-conversation"
    elif event == "PostToolUse":
        assert output == {}
        assert "step=3 error=False; no-op" in proc.stderr
        assert not (data / "pre-invocation.json").exists()
        assert not (data / "stop.json").exists()
    else:
        assert output == {"decision": "stop"}
        delegated = json.loads((data / "stop.json").read_text())
        assert delegated["session_id"] == "public-conversation"
        assert delegated["reason"] == "complete"
        assert delegated["cleanup_transcript"] is True
        assert Path(delegated["transcript_path"]).parent == data / "memory-staging"


@pytest.mark.parametrize("event", EVENTS)
@pytest.mark.parametrize(
    "raw_payload",
    ("", "{not-json", "[]"),
    ids=("empty", "invalid-json", "non-object"),
)
def test_empty_or_malformed_input_fails_safe(tmp_path, event, raw_payload):
    env, data = _fixture_env(tmp_path)

    proc = _run(event, raw_payload, env)

    assert proc.returncode == 0, proc.stderr
    expected = {
        "PreInvocation": {"injectSteps": [{"ephemeralMessage": "public fixture context"}]},
        "PostToolUse": {},
        "Stop": {"decision": "stop"},
    }
    assert json.loads(proc.stdout) == expected[event]
    assert not (data / "stop.json").exists()