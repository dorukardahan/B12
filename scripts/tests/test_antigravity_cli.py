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
        "#!/bin/bash\n"
        "payload=$(cat)\n"
        "printf '%s' \"$payload\" > \"$B12_DATA_DIR/stop-input.json\"\n"
        "transcript=$(printf '%s' \"$payload\" | python3 -c 'import json,sys; print(json.load(sys.stdin).get(\"transcript_path\", \"\"))')\n"
        "[ -z \"$transcript\" ] || cp \"$transcript\" \"$B12_DATA_DIR/converted-transcript.jsonl\"\n"
        "printf '%s\\n' '{}'\n"
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
    guard = state_dir / "memory-state" / "antigravity-preinvocation-guard.json"
    assert "same" not in guard.read_text()
    assert guard.stat().st_mode & 0o777 == 0o600


def test_posttooluse_noop_json_and_stderr_logging(tmp_path: Path):
    hook_dir, state_dir = make_fake_hooks(tmp_path)
    proc = run_adapter(
        "PostToolUse",
        {"conversationId": "c2", "stepIdx": 4, "error": "", "workspacePaths": ["/repo"], "transcriptPath": "/tmp/t.jsonl"},
        {"B12_HOOK_DIR": str(hook_dir), "B12_DATA_DIR": str(state_dir)},
    )
    assert proc.returncode == 0
    assert json.loads(proc.stdout) == {}
    assert "step=4 error=False" in proc.stderr
    assert "c2" not in proc.stderr
    assert "/repo" not in proc.stderr


def test_stop_requires_fully_idle_and_never_forces_continuation(tmp_path: Path):
    hook_dir, state_dir = make_fake_hooks(tmp_path)
    env = {"B12_HOOK_DIR": str(hook_dir), "B12_DATA_DIR": str(state_dir)}
    not_idle = json.loads(run_adapter("Stop", {"conversationId": "c3", "fullyIdle": False}, env).stdout)
    assert not_idle == {"decision": "stop"}
    assert not (state_dir / "stop-input.json").exists()

    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(
        '\n'.join(
            (
                json.dumps({"type": "USER_INPUT", "content": "Remember this preference"}),
                json.dumps(
                    {
                        "type": "PLANNER_RESPONSE",
                        "content": "Decision: use the native plugin because hooks must load.",
                        "tool_calls": [{"name": "view_file", "args": {"AbsolutePath": "/repo/a.py"}}],
                    }
                ),
                json.dumps({"type": "TOOL_RESPONSE", "content": "private tool output must not be copied"}),
            )
        )
        + "\n"
    )
    idle = json.loads(
        run_adapter(
            "Stop",
            {"conversationId": "c3", "fullyIdle": True, "terminationReason": "idle", "workspacePaths": ["/repo"], "transcriptPath": str(transcript)},
            env,
        ).stdout
    )
    assert idle == {"decision": "stop"}
    captured = json.loads((state_dir / "stop-input.json").read_text())
    assert captured["session_id"] == "c3"
    assert captured["cwd"] == "/repo"
    assert captured["transcript_path"] != str(transcript)
    assert not Path(captured["transcript_path"]).exists()
    converted = [json.loads(line) for line in (state_dir / "converted-transcript.jsonl").read_text().splitlines()]
    assert converted[0] == {"type": "human", "message": {"content": "Remember this preference"}}
    assert converted[1]["type"] == "assistant"
    assert converted[1]["message"]["content"][0]["type"] == "text"
    assert converted[1]["message"]["content"][1]["name"] == "view_file"
    assert "private tool output" not in (state_dir / "converted-transcript.jsonl").read_text()


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
    manifest = json.loads((plugin / "plugin.json").read_text())
    assert manifest["$schema"] == "https://antigravity.google/schemas/v1/plugin.json"
    assert set(manifest) == {"$schema", "name", "description"}
    hooks = json.loads((plugin / "hooks.json").read_text())["b12-memory"]
    assert {"PreInvocation", "PostToolUse", "Stop"}.issubset(hooks)
    post = hooks["PostToolUse"][0]
    assert post["matcher"] == "*"
    assert post["hooks"][0]["command"].endswith(" PostToolUse")
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
    staged_hooks = json.loads((dest / "hooks.json").read_text())["b12-memory"]
    assert "PreInvocation" in staged_hooks
    assert "/venv/bin/python3" in staged_hooks["PreInvocation"][0]["command"]
    assert staged_hooks["PostToolUse"][0]["matcher"] == "*"
    assert staged_hooks["PostToolUse"][0]["hooks"][0]["command"].endswith(" PostToolUse")


def test_antigravity_config_merge_refuses_invalid_or_wrong_shape(tmp_path: Path):
    sys.path.insert(0, str(SCRIPTS))
    from antigravity_install import merge_global_mcp_config

    cases = (("invalid.json", "{broken"), ("wrong.json", json.dumps({"mcpServers": []})))
    for name, original in cases:
        cfg = tmp_path / name
        cfg.write_text(original)
        with pytest.raises(ValueError):
            merge_global_mcp_config(cfg, "/venv/bin/python3", "/repo/server.py")
        assert cfg.read_text() == original


def test_staged_hook_commands_quote_paths(tmp_path: Path):
    sys.path.insert(0, str(SCRIPTS))
    from antigravity_install import stage_plugin

    dest = tmp_path / "plugin"
    stage_plugin(ROOT, dest, "/venv with space/python3", "/repo/server.py", "/repo with space/adapter.py")
    hooks = json.loads((dest / "hooks.json").read_text())["b12-memory"]
    assert hooks["PreInvocation"][0]["command"] == "'/venv with space/python3' '/repo with space/adapter.py' PreInvocation"


def test_installer_exposes_antigravity_without_repointing_gemini():
    install = (ROOT / "install.sh").read_text()
    assert "INSTALL_ANTIGRAVITY=false" in install
    assert "--antigravity) INSTALL_ANTIGRAVITY=true" in install
    assert "--gemini)    INSTALL_GEMINI=true" in install
    assert "inject_gemini_mcp_config" in install
    assert "install_antigravity" in install
    assert 'agy plugin install "$STAGE_DIR"' in install


def test_installer_antigravity_flag_installs_plugin_in_isolated_home(tmp_path: Path):
    home = tmp_path / "home"
    deployed = home / ".B12" / "hooks" / "scripts"
    venv_bin = home / ".local" / "b12-venv" / "bin"
    fake_bin = tmp_path / "bin"
    deployed.mkdir(parents=True)
    venv_bin.mkdir(parents=True)
    fake_bin.mkdir()
    (venv_bin / "python3").symlink_to(sys.executable)
    for name in ("b12_mcp_server.py", "antigravity_hook_adapter.py", "antigravity_install.py"):
        (deployed / name).write_bytes((SCRIPTS / name).read_bytes())
    agy = fake_bin / "agy"
    agy.write_text(
        "#!/bin/sh\n"
        "case \"$1:$2\" in\n"
        "  plugin:install|plugin:validate) exit 0 ;;\n"
        "  plugin:list) printf '%s\\n' '{\"imports\":[{\"name\":\"b12\"}]}' ;;\n"
        "  *) exit 2 ;;\n"
        "esac\n"
    )
    agy.chmod(0o755)

    proc = subprocess.run(
        ["bash", str(ROOT / "install.sh"), "--antigravity", "--no-gc-cron"],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, "HOME": str(home), "PATH": f"{fake_bin}:{os.environ['PATH']}"},
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    config = json.loads((home / ".gemini" / "config" / "mcp_config.json").read_text())
    assert "B12" in config["mcpServers"]
    hooks = json.loads((home / ".B12" / "antigravity-plugin" / "b12" / "hooks.json").read_text())
    assert hooks["b12-memory"]["PostToolUse"][0]["matcher"] == "*"
