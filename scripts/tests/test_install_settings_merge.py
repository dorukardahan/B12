"""Guards for install_to_setup() and setup-directory detection in install.sh.

install_to_setup() used to replace a setup's whole "hooks" block with the B12
template, which silently deleted hooks the user had added for other tools.
The install loops also treated every ~/.claude-* directory as a setup, so a
transcript archive got a settings.json and a skill copied into it.

The functions are extracted from install.sh and run under bash with a temp
HOME, so nothing outside the test's tmp_path is touched.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALL = REPO_ROOT / "install.sh"
TEMPLATE_HOOKS = json.loads((REPO_ROOT / "config" / "settings-template.json").read_text())["hooks"]

FOREIGN_ELICITATION = {"matcher": "my-mcp", "hooks": [{"type": "command", "command": "/opt/tools/approve", "timeout": 10}]}
FOREIGN_BASH = {"type": "command", "command": "~/bin/audit-bash.sh"}


def _function(name: str) -> str:
    m = re.search(rf"\n{name}\(\)\s*\{{.*?\n\}}\n", INSTALL.read_text(), re.DOTALL)
    assert m, f"{name}() not found in install.sh"
    return m.group(0)


def _bash(script: str, home: Path) -> subprocess.CompletedProcess:
    prelude = (
        'info() { :; }; warn() { :; }; error() { echo "$*" >&2; exit 1; }\n'
        f'SCRIPT_DIR="{REPO_ROOT}"\n'
        + _function("is_claude_setup_dir")
        + _function("install_to_setup")
    )
    return subprocess.run(["bash", "-c", prelude + script], env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
                          capture_output=True, text=True, timeout=60)


def _install(setup: Path, home: Path) -> dict:
    result = _bash(f'install_to_setup "{setup}"\n', home)
    assert result.returncode == 0, result.stderr
    return json.loads((setup / "settings.json").read_text())


def _commands(hooks: dict) -> list[str]:
    return [h.get("command", "") for groups in hooks.values() for g in groups for h in g.get("hooks", [])]


def test_foreign_hooks_survive_and_b12_hooks_match_template(tmp_path):
    setup = tmp_path / ".claude-work"
    setup.mkdir()
    (setup / "settings.json").write_text(json.dumps({
        "model": "keep-me",
        "permissions": {"allow": ["Bash(ls:*)"]},
        "hooks": {
            "Elicitation": [FOREIGN_ELICITATION],
            "PreToolUse": [
                {"matcher": "Bash", "hooks": [{"type": "command", "command": "~/.B12/hooks/memory-tag-enforce.sh"}, FOREIGN_BASH]},
            ],
            "Stop": [{"hooks": [{"type": "command", "command": "~/.B12/hooks/memory-retired-hook.sh"}]}],
            "SessionStart": [{"hooks": [{"type": "command", "command": "~/.claude/hooks/memory-session-start.sh"}]}],
        },
    }))
    settings = _install(setup, tmp_path)
    hooks = settings["hooks"]
    assert settings["model"] == "keep-me" and settings["permissions"] == {"allow": ["Bash(ls:*)"]}
    assert hooks["Elicitation"] == [FOREIGN_ELICITATION]
    assert {"matcher": "Bash", "hooks": [FOREIGN_BASH]} in hooks["PreToolUse"]
    for event, groups in TEMPLATE_HOOKS.items():
        assert hooks[event][: len(groups)] == groups, f"{event}: B12 template groups must come first, unchanged"
    commands = _commands(hooks)
    assert "~/.B12/hooks/memory-retired-hook.sh" not in commands, "stale B12 hooks must be dropped"
    assert "~/.claude/hooks/memory-session-start.sh" not in commands, "legacy B12 paths must be dropped"
    assert sorted(c for c in commands if "/.B12/hooks/" not in c) == sorted(["/opt/tools/approve", "~/bin/audit-bash.sh"])


def test_only_known_b12_names_count_as_legacy_b12_hooks(tmp_path):
    # A legacy ~/.claude*/hooks/ path is B12's only when it names a hook B12
    # ships or once shipped; another tool's memory-*.sh must survive.
    setup = tmp_path / ".claude-work"
    setup.mkdir()
    foreign = {"type": "command", "command": "~/.claude-work/hooks/memory-sync.sh"}
    (setup / "settings.json").write_text(json.dumps({"hooks": {"Stop": [
        {"hooks": [foreign, {"type": "command", "command": "~/.claude/hooks/memory-upgrade.sh"}]},
    ]}}))
    commands = _commands(_install(setup, tmp_path)["hooks"])
    assert "~/.claude-work/hooks/memory-sync.sh" in commands
    assert "~/.claude/hooks/memory-upgrade.sh" not in commands


def test_merge_is_idempotent(tmp_path):
    setup = tmp_path / ".claude"
    setup.mkdir()
    (setup / "settings.json").write_text(json.dumps({"hooks": {"Elicitation": [FOREIGN_ELICITATION]}}))
    first = _install(setup, tmp_path)
    second = _install(setup, tmp_path)
    assert first == second


def test_missing_settings_file_gets_the_template(tmp_path):
    setup = tmp_path / ".claude"
    setup.mkdir()
    assert _install(setup, tmp_path)["hooks"] == TEMPLATE_HOOKS


def _is_setup(path: Path, home: Path) -> bool:
    return _bash(f'is_claude_setup_dir "{path}"\n', home).returncode == 0


@pytest.mark.parametrize("marker", ["CLAUDE.md", "skills/", "commands/", ".credentials.json", "history.jsonl", "settings.local.json"])
def test_alternate_setup_without_settings_is_recognized(tmp_path, marker):
    setup = tmp_path / ".claude-work"
    setup.mkdir()
    if marker.endswith("/"):
        (setup / marker.rstrip("/")).mkdir()
    else:
        (setup / marker).write_text("")
    assert _is_setup(setup, tmp_path)


def test_b12_ignore_opts_out(tmp_path):
    backup = tmp_path / ".claude-backup"
    backup.mkdir()
    (backup / "settings.json").write_text("{}")
    (backup / ".b12-ignore").write_text("")
    assert not _is_setup(backup, tmp_path)


def test_install_to_setup_honors_b12_ignore(tmp_path):
    # The default ~/.claude and an explicit target call install_to_setup()
    # directly, without the --all loop's detection.
    setup = tmp_path / ".claude"
    setup.mkdir()
    (setup / "settings.json").write_text('{"keep": true}')
    (setup / ".b12-ignore").write_text("")
    result = _bash(f'install_to_setup "{setup}"\n', tmp_path)
    assert result.returncode == 0, result.stderr
    assert json.loads((setup / "settings.json").read_text()) == {"keep": True}


def test_setup_dir_detection(tmp_path):
    plain = tmp_path / ".claude"
    plain.mkdir()
    archive = tmp_path / ".claude-transcript-archive"
    (archive / "2026-09-01").mkdir(parents=True)
    by_settings = tmp_path / ".claude-work"
    by_settings.mkdir()
    (by_settings / "settings.json").write_text("{}")
    by_state = tmp_path / ".claude-alt"
    by_state.mkdir()
    (by_state / ".claude.json").write_text("{}")
    by_projects = tmp_path / ".claude-new"
    (by_projects / "projects").mkdir(parents=True)
    a_file = tmp_path / ".claude.json"
    a_file.write_text("{}")

    assert _is_setup(plain, tmp_path), "~/.claude is always a setup"
    assert not _is_setup(archive, tmp_path), "an archive folder is not a setup"
    assert _is_setup(by_settings, tmp_path) and _is_setup(by_state, tmp_path) and _is_setup(by_projects, tmp_path)
    assert not _is_setup(a_file, tmp_path)


def test_install_loops_use_setup_detection():
    src = INSTALL.read_text()
    loops = re.findall(r'for dir in "\$HOME"/\.claude\*; do\n\s*(.+)', src)
    assert len(loops) == 2, loops
    assert all(line.strip() == 'is_claude_setup_dir "$dir" || continue' for line in loops), loops
