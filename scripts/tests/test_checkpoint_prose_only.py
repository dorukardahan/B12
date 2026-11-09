"""The checkpoint hook stores sentences, not pieces of commands or code.

Its prose patterns ran over the shell command, the search pattern, the file path,
written file content and tool output. `--since 2026-07-16 --full 2>&1 | tail -8`
matched the reasoning pattern at "since", and pieces like that made up 11% of the
active memories on one install, typed as knowledge or error_fix.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))

import shared_patterns as sp  # noqa: E402


@pytest.mark.parametrize("text", [
    "since 2026-07-16 --full 2>&1 | tail -8",
    "since 2026-09-20 --full --no-meta 2>&1 | tail -2; slack_export.py D0B --since 2026-09-20",
    "resolved physically, TOML-safe. |''')",
    "Resolved comments(first:1){nodes{databaseId path body}}}}}}}' --jq '.data.repository'",
    "since start'; journalctl -u gateway --since \"20:31:05\" --no-pager -o cat",
])
def test_commands_and_code_are_not_sentences(text):
    assert sp.looks_like_code(text)


@pytest.mark.parametrize("text", [
    "because the scanner rejects symlinks outside the home folder, which is safer.",
    "çünkü ürün stratejisi içeriyor ve iki alt-repo'dan birine gömmek istememiştim.",
    "workaround for the same clip cost about 1.3 0G and still cut 5 cue endings early ([[transcribe-notes]]).",
    "yerine `0g.ai/{path}` alıyor (0g-edge proxy). get.0g.ai zaten canlı surface'i.",
    "because the router call to the preview endpoint timed out. Standard tier was fine.",
])
def test_sentences_are_kept(text):
    assert not sp.looks_like_code(text)


def test_reason_pattern_ignores_cli_flags():
    assert not list(sp.REASON_RE.finditer("gh api x --since 2026-07-16 --full | tail -3"))
    hits = [m.group(0) for m in sp.REASON_RE.finditer("We kept it because the daemon restarts on demand anyway.")]
    assert hits == ["because the daemon restarts on demand anyway."]


def _buffer_after(tmp_path: Path, payload: dict) -> list[str]:
    home = tmp_path / "home"
    data = home / ".B12"
    (data / "memory-logs").mkdir(parents=True)
    hookroot = tmp_path / "hookroot"
    hookroot.mkdir()
    for name in ("_b12_common.sh", "memory-checkpoint.sh"):
        (hookroot / name).symlink_to(REPO / "hooks" / name)
    (hookroot / "scripts").symlink_to(REPO / "scripts")
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home), "B12_DATA_DIR": str(data),
           "B12_HOOK_DIR": str(hookroot), "B12_EMBED_RUNTIME_DIR": str(tmp_path)}
    r = subprocess.run(["bash", str(hookroot / "memory-checkpoint.sh")], input=json.dumps(payload), env=env,
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    log = data / "memory-logs" / "checkpoint-telemetry.jsonl"
    deadline = time.time() + 15
    while time.time() < deadline:  # the scan runs in a detached worker
        if log.exists() and log.read_text().strip():
            break
        time.sleep(0.1)
    buf = data / "memory-staging" / "checkpoint" / ".buffer-cp-prose-tes.jsonl"
    return [json.loads(l)["content"] for l in buf.read_text().splitlines()] if buf.exists() else []


def test_hook_keeps_the_reason_and_drops_command_pieces(tmp_path):
    items = _buffer_after(tmp_path, {
        "session_id": "cp-prose-test", "cwd": str(tmp_path), "tool_name": "Bash",
        "tool_input": {"command": "slack_export.py C0 --since 2026-07-16 --full 2>&1 | tail -8 && "
                                  "git commit -m 'Pin the cache because the old key leaked into the logs'"},
        "tool_result": ("We kept SQLite because it needs no server and the data fits on one machine.\n"
                        "rerun: since 2026-09-20 --full --no-meta 2>&1 | tail -2\n"),
    })
    assert any("because it needs no server" in i for i in items), items
    assert not any("2026-07-16" in i or "2>&1" in i for i in items), items
    assert not any("old key leaked" in i for i in items), items  # commands are not scanned at all
