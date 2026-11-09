"""Guards for two b12_health checks that failed on a healthy install.

- check_claude_setups flagged every hook outside ~/.B12/hooks/, including other
  tools' hooks that install.sh deliberately keeps in the same settings.json.
- The Antigravity probe parsed `agy plugin list` as JSON, but agy 1.5 prints
  text unless given --json, and its JSON is a list.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import b12_health as H  # noqa: E402


@pytest.fixture
def home(tmp_path, monkeypatch):
    hooks = tmp_path / ".B12" / "hooks"
    hooks.mkdir(parents=True)
    (hooks / "memory-retrieval.sh").write_text("")
    monkeypatch.setattr(H, "_HOME", tmp_path)
    monkeypatch.setattr(H, "_HOOK_DIR", hooks)
    return tmp_path


def _setup(home, *commands):
    d = home / ".claude"
    d.mkdir(exist_ok=True)
    blocks = [{"hooks": [{"type": "command", "command": c}]} for c in commands]
    (d / "settings.json").write_text(json.dumps({"hooks": {"UserPromptSubmit": blocks}}))


def test_other_tools_hooks_do_not_fail_the_setup_check(home):
    _setup(home, f"{home}/.B12/hooks/memory-retrieval.sh", "/usr/local/bin/other-tool-approve")
    assert H.check_claude_setups().status == H.Status.OK


def test_b12_hook_outside_the_hook_dir_still_fails(home):
    _setup(home, "/old/checkout/hooks/memory-retrieval.sh", "/usr/local/bin/other-tool-approve")
    r = H.check_claude_setups()
    assert r.status != H.Status.OK and "1 hooks reference wrong path" in r.detail


def _agy(monkeypatch, home, outputs):
    stage = home / ".B12" / "antigravity-plugin" / "b12"
    stage.mkdir(parents=True)
    for name in ("plugin.json", "mcp_config.json", "hooks.json"):
        (stage / name).write_text("{}")
    monkeypatch.setattr(H.shutil, "which", lambda name: "/bin/agy")
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        rc, out = outputs[" ".join(cmd[1:])]
        return subprocess.CompletedProcess(cmd, rc, out, "")

    monkeypatch.setattr(H.subprocess, "run", run)
    return calls


B12 = {"name": "b12", "enabled": True, "mcpServers": ["B12"], "hooks": ["b12-memory"]}


def test_agy_json_list_is_understood(home, monkeypatch):
    _agy(monkeypatch, home, {"plugin list --json": (0, json.dumps([B12]))})
    ok, detail = H._antigravity_plugin_loadable()
    assert ok, detail


def test_a_second_enabled_b12_plugin_is_reported(home, monkeypatch):
    extra = dict(B12, name="b12.bak-preinstall")
    _agy(monkeypatch, home, {"plugin list --json": (0, json.dumps([B12, extra]))})
    ok, detail = H._antigravity_plugin_loadable()
    assert not ok and "b12.bak-preinstall" in detail and "twice" in detail


def test_older_agy_without_json_flag_still_works(home, monkeypatch):
    calls = _agy(monkeypatch, home, {"plugin list --json": (2, ""),
                                     "plugin list": (0, json.dumps({"imports": [{"name": "b12"}]}))})
    ok, detail = H._antigravity_plugin_loadable()
    assert ok, detail
    assert [c[1:] for c in calls] == [["plugin", "list", "--json"], ["plugin", "list"]]
