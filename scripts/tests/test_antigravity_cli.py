from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
ADAPTER = SCRIPTS / "antigravity_hook_adapter.py"


def run_adapter(event: str, payload: dict, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ADAPTER), event],
        input=json.dumps(payload),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, **env},
        timeout=10,
        check=False,
    )


def make_fake_hooks(tmp_path: Path) -> tuple[Path, Path]:
    hook_dir = tmp_path / "hooks"
    state_dir = tmp_path / "data"
    hook_dir.mkdir()
    state_dir.mkdir()
    (hook_dir / "memory-session-start.sh").write_text(
        "#!/bin/bash\ncat >/dev/null\nprintf '%s\\n' '{\"hookSpecificOutput\":{\"additionalContext\":\"B12 ctx\"}}'\n"
    )
    (hook_dir / "memory-session-end.sh").write_text(
        "#!/bin/bash\ncat > \"$B12_DATA_DIR/stop-input.json\"\nprintf '%s\\n' '{}'\n"
    )
    for p in hook_dir.glob("*.sh"):
        p.chmod(0o755)
    return hook_dir, state_dir


def test_preinvocation_injects_ephemeral_message_and_stdout_json(tmp_path: Path):
    hook_dir, state_dir = make_fake_hooks(tmp_path)
    proc = run_adapter(
        "PreInvocation",
        {"conversationId": "c1", "invocationNum": 0, "workspacePaths": ["/repo"], "transcriptPath": "/tmp/t.jsonl"},
        {"B12_HOOK_DIR": str(hook_dir), "B12_DATA_DIR": str(state_dir)},
    )
    assert proc.returncode == 0
    assert json.loads(proc.stdout) == {"injectSteps": [{"ephemeralMessage": "B12 ctx"}]}
    assert "B12 ctx" not in proc.stderr


def test_preinvocation_duplicate_guard_skips_second_injection(tmp_path: Path):
    hook_dir, state_dir = make_fake_hooks(tmp_path)
    env = {"B12_HOOK_DIR": str(hook_dir), "B12_DATA_DIR": str(state_dir)}
    payload = {"conversationId": "same", "invocationNum": 0, "workspacePaths": ["/repo"]}
    first = json.loads(run_adapter("PreInvocation", payload, env).stdout)
    second = json.loads(run_adapter("PreInvocation", payload, env).stdout)
    later = json.loads(run_adapter("PreInvocation", {**payload, "conversationId": "new", "invocationNum": 2}, env).stdout)
    assert first["injectSteps"]
    assert second == {"injectSteps": []}
    assert later == {"injectSteps": []}


def test_posttooluse_noop_json_and_stderr_logging(tmp_path: Path):
    hook_dir, state_dir = make_fake_hooks(tmp_path)
    proc = run_adapter(
        "PostToolUse",
        {"conversationId": "c2", "toolName": "Read", "workspacePaths": ["/repo"], "transcriptPath": "/tmp/t.jsonl"},
        {"B12_HOOK_DIR": str(hook_dir), "B12_DATA_DIR": str(state_dir)},
    )
    assert proc.returncode == 0
    assert json.loads(proc.stdout) == {}
    assert "PostToolUse observed" in proc.stderr


def test_stop_requires_fully_idle_and_never_forces_continuation(tmp_path: Path):
    hook_dir, state_dir = make_fake_hooks(tmp_path)
    env = {"B12_HOOK_DIR": str(hook_dir), "B12_DATA_DIR": str(state_dir)}
    not_idle = json.loads(run_adapter("Stop", {"conversationId": "c3", "fullyIdle": False}, env).stdout)
    assert not_idle == {"decision": "stop"}
    assert not (state_dir / "stop-input.json").exists()

    idle = json.loads(
        run_adapter(
            "Stop",
            {"conversationId": "c3", "fullyIdle": True, "terminationReason": "idle", "workspacePaths": ["/repo"], "transcriptPath": "/tmp/t.jsonl"},
            env,
        ).stdout
    )
    assert idle == {"decision": "stop"}
    captured = json.loads((state_dir / "stop-input.json").read_text())
    assert captured["session_id"] == "c3"
    assert captured["cwd"] == "/repo"
    assert captured["transcript_path"] == "/tmp/t.jsonl"


def test_invalid_input_and_missing_hooks_are_safe_json(tmp_path: Path):
    proc = subprocess.run(
        [sys.executable, str(ADAPTER), "PreInvocation"],
        input="not-json",
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, "B12_HOOK_DIR": str(tmp_path / "missing"), "B12_DATA_DIR": str(tmp_path / "data")},
        timeout=10,
        check=False,
    )
    assert proc.returncode == 0
    assert json.loads(proc.stdout) == {"injectSteps": []}
    assert "invalid JSON" in proc.stderr


def test_antigravity_plugin_template_structure_and_privacy_safe_paths():
    plugin = ROOT / "plugins" / "antigravity" / "b12"
    assert (plugin / "plugin.json").exists()
    assert (plugin / "mcp_config.json").exists()
    assert (plugin / "hooks.json").exists()
    mcp = json.loads((plugin / "mcp_config.json").read_text())
    assert mcp["mcpServers"]["B12"]["command"] == "python3"
    assert "mcpServers" in mcp
    hooks = json.loads((plugin / "hooks.json").read_text())["hooks"]
    assert set(["PreInvocation", "PostToolUse", "Stop"]).issubset(hooks)
    combined = "\n".join(p.read_text() for p in plugin.rglob("*.*"))
    assert "/home/" not in combined
    assert ("/" + "Users/") not in combined


def test_antigravity_config_merge_preserves_existing_servers(tmp_path: Path):
    sys.path.insert(0, str(SCRIPTS))
    from antigravity_install import merge_global_mcp_config, stage_plugin

    cfg = tmp_path / ".gemini" / "config" / "mcp_config.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({"mcpServers": {"Other": {"command": "other"}}, "x": 1}))
    merge_global_mcp_config(cfg, "/venv/bin/python3", "/repo/scripts/b12_mcp_server.py")
    data = json.loads(cfg.read_text())
    assert data["x"] == 1
    assert data["mcpServers"]["Other"]["command"] == "other"
    assert data["mcpServers"]["B12"]["args"] == ["/repo/scripts/b12_mcp_server.py"]

    dest = tmp_path / "plugin"
    stage_plugin(ROOT, dest, "/venv/bin/python3", "/repo/scripts/b12_mcp_server.py", "/repo/scripts/antigravity_hook_adapter.py")
    staged_hooks = json.loads((dest / "hooks.json").read_text())["hooks"]
    assert "PreInvocation" in staged_hooks
    assert "/venv/bin/python3" in staged_hooks["PreInvocation"][0]["command"]


def test_installer_exposes_antigravity_without_repointing_gemini():
    install = (ROOT / "install.sh").read_text()
    assert "INSTALL_ANTIGRAVITY=false" in install
    assert "--antigravity) INSTALL_ANTIGRAVITY=true" in install
    assert "--gemini)    INSTALL_GEMINI=true" in install
    assert "inject_gemini_mcp_config" in install
    assert "install_antigravity" in install
