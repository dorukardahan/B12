"""PR-3b: Codex SessionEnd store_memory secret-cap test.

store_memory scrubs content (credential -> [REDACTED:...]) and must then cap a
credential-bearing memory's importance at baseline via b12_importance.is_secret,
while preserving the caller's deliberate per-category importance for non-secrets.

We pass content that already carries the scrubber's [REDACTED:...] marker (the
post-scrub shape) so no real secret literal lives in this file — the scrub step
itself is covered by test_b12_pii_scrubber.
"""
import json
import os
import sqlite3
import subprocess
import sys
import time
import tomllib
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

ROOT = Path(__file__).resolve().parents[2]

import b12_importance as imp  # noqa: E402
import codex_session_end as cse  # noqa: E402


def _make_db(path):
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE memories (
            id INTEGER PRIMARY KEY, content TEXT, metadata TEXT, tags TEXT,
            content_hash TEXT, memory_type TEXT, created_at REAL, updated_at REAL,
            created_at_iso TEXT, updated_at_iso TEXT, strength REAL, deleted_at REAL
        )
        """
    )
    conn.commit()
    conn.close()


def _stored_importance(db_path, mem_id):
    conn = sqlite3.connect(db_path)
    md = conn.execute("SELECT metadata FROM memories WHERE id = ?", (mem_id,)).fetchone()[0]
    conn.close()
    return json.loads(md)["importance_score"]


def test_codex_store_caps_secret_importance(tmp_path):
    db = str(tmp_path / "codex.db")
    _make_db(db)
    # Content already carries the scrubber's marker (post-scrub shape).
    mid = cse.store_memory(
        db, "deploy token=[REDACTED:generic] keep secret",
        json.dumps({"type": "decision", "importance_score": 0.8}),
        "proj:test", embedding=None, memory_type="decision",
    )
    assert mid is not None
    assert _stored_importance(db, mid) == imp.IMPORTANCE_BASELINE


def test_codex_store_preserves_nonsecret_importance(tmp_path):
    db = str(tmp_path / "codex.db")
    _make_db(db)
    mid = cse.store_memory(
        db, "we shipped the migration cleanly this week",
        json.dumps({"type": "decision", "importance_score": 0.8}),
        "proj:test", embedding=None, memory_type="decision",
    )
    assert mid is not None
    assert _stored_importance(db, mid) == 0.8  # caller's deliberate value preserved


def test_codex_store_caps_secret_in_legacy_metadata(tmp_path):
    # Codex review: the legacy f-string metadata format ("type:x, importance:0.8")
    # must still be capped. The cap runs AFTER validate_metadata, which normalizes
    # the legacy form to JSON (importance -> importance_score) first.
    db = str(tmp_path / "codex.db")
    _make_db(db)
    mid = cse.store_memory(
        db, "deploy token=[REDACTED:generic] keep secret",
        "type:decision, importance:0.8",  # legacy f-string format (not JSON)
        "proj:test", embedding=None, memory_type="decision",
    )
    assert mid is not None
    assert _stored_importance(db, mid) == imp.IMPORTANCE_BASELINE


def test_codex_store_preserves_legacy_metadata_nonsecret(tmp_path):
    db = str(tmp_path / "codex.db")
    _make_db(db)
    mid = cse.store_memory(
        db, "we shipped the migration cleanly this week",
        "type:decision, importance:0.8",  # legacy f-string -> importance_score 0.8
        "proj:test", embedding=None, memory_type="decision",
    )
    assert mid is not None
    assert _stored_importance(db, mid) == 0.8


def test_codex_session_summary_fires_upsert_latest_content(tmp_path, monkeypatch):
    db = str(tmp_path / "codex.db")
    _make_db(db)
    ticks = iter((100.0, 200.0, 300.0, 400.0))
    monkeypatch.setattr(cse.time, "time", lambda: next(ticks))
    ids = [
        cse.store_memory(
            db,
            f"session summary version {version}",
            json.dumps({"session_id": "codex-session-123", "platform": "codex"}),
            "proj:test,session-summary",
            embedding=None,
            memory_type="session_summary",
        )
        for version in range(1, 5)
    ]

    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT id, content, created_at, updated_at FROM memories"
    ).fetchall()
    conn.close()
    assert len(set(ids)) == 1
    assert rows == [(ids[0], "session summary version 4", 100.0, 400.0)]


def test_codex_session_end_hook_detaches_and_forwards_transcript(tmp_path):
    home = tmp_path / "home"
    fake_python = home / ".local/b12-venv/bin/python3"
    fake_python.parent.mkdir(parents=True)
    called = home / "called.txt"
    fake_python.write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$HOME/called.txt\"\n"
    )
    fake_python.chmod(0o755)
    rollout = tmp_path / "rollout-session.jsonl"
    rollout.write_text("{}\n")
    env = os.environ | {
        "HOME": str(home),
        "B12_DATA_DIR": str(home / ".B12"),
    }
    started = time.monotonic()
    result = subprocess.run(
        ["bash", str(ROOT / "hooks/memory-codex-session-end.sh")],
        input=json.dumps({"session_id": "session-full-id", "transcript_path": str(rollout)}),
        text=True,
        capture_output=True,
        env=env,
        timeout=3,
        check=False,
    )
    assert result.returncode == 0
    assert time.monotonic() - started < 3
    for _ in range(50):
        if called.exists():
            break
        time.sleep(0.02)
    assert called.read_text().splitlines()[-1] == str(rollout)


RETIRED_NOTIFY_LINE = (
    "\x1b[0;32m[OK]\x1b[0m "
    "Retired legacy B12 Codex notify adapter after SessionEnd migration"
)
KEPT_NOTIFY_LINE = (
    "\x1b[1;33m[!]\x1b[0m "
    "Keeping legacy B12 Codex notify adapter: SessionEnd migration is incomplete"
)


def _codex_retirement_fixture(tmp_path, notify_factory, *, adapter_exists=True):
    home = tmp_path / "home"
    hooks, codex = home / ".B12/hooks", home / ".codex"
    hooks.mkdir(parents=True)
    codex.mkdir()
    legacy = hooks / "b12-codex-notify.sh"
    if adapter_exists:
        legacy.write_text("#!/bin/sh\n")
    notify = notify_factory(str(legacy))
    config = codex / "config.toml"
    config.write_text(
        f"notify = {json.dumps(notify)}\n"
        'notify_backup = ["keep"]\n'
    )
    (codex / "hooks.json").write_text('{"hooks": {}}\n')
    python = home / ".local/b12-venv/bin/python3"
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)
    env = os.environ | {"HOME": str(home), "B12_DATA_DIR": str(home / ".B12")}

    def install():
        result = subprocess.run(
            ["bash", str(ROOT / "install.sh"), "--codex", "--no-gc-cron"],
            env=env, text=True, capture_output=True, timeout=30, check=True,
        )
        return result.stdout

    return legacy, config, install


def _retirement_lines(output):
    return [
        line for line in output.splitlines()
        if "legacy B12 Codex notify adapter" in line
    ]


def _escaped_previous_notify(*commands):
    return json.dumps(list(commands)).replace("/", r"\/")


def test_codex_installer_retires_bare_sole_legacy_notify(tmp_path):
    legacy, config, install = _codex_retirement_fixture(
        tmp_path, lambda path: [path]
    )

    output = install()

    assert not legacy.exists()
    assert "notify" not in tomllib.loads(config.read_text())
    assert _retirement_lines(output) == [RETIRED_NOTIFY_LINE]


def test_codex_installer_decodes_escaped_wrapped_legacy_notify(tmp_path):
    legacy, config, install = _codex_retirement_fixture(
        tmp_path,
        lambda path: [
            "/computer-use", "turn-ended", "--previous-notify",
            _escaped_previous_notify(path),
        ],
    )
    encoded = tomllib.loads(config.read_text())["notify"][3]
    assert r"\/" in encoded
    assert str(legacy) not in encoded
    assert json.loads(encoded) == [str(legacy)]

    output = install()

    assert not legacy.exists()
    assert tomllib.loads(config.read_text())["notify"] == [
        "/computer-use", "turn-ended",
    ]
    assert _retirement_lines(output) == [RETIRED_NOTIFY_LINE]


def test_codex_installer_preserves_foreign_wrapped_notify_order(tmp_path):
    foreign = ["/foreign-notify", "finished"]
    legacy, config, install = _codex_retirement_fixture(
        tmp_path,
        lambda path: [
            "/computer-use", "turn-ended", "--previous-notify",
            _escaped_previous_notify(path, *foreign), "--after", "keep",
        ],
    )

    output = install()

    notify = tomllib.loads(config.read_text())["notify"]
    assert notify[:3] == ["/computer-use", "turn-ended", "--previous-notify"]
    assert json.loads(notify[3]) == foreign
    assert notify[4:] == ["--after", "keep"]
    assert not legacy.exists()
    assert _retirement_lines(output) == [RETIRED_NOTIFY_LINE]


def test_codex_installer_repairs_dangling_wrapped_notify_idempotently(tmp_path):
    legacy, config, install = _codex_retirement_fixture(
        tmp_path,
        lambda path: [
            "/computer-use", "turn-ended", "--previous-notify",
            _escaped_previous_notify(path),
        ],
        adapter_exists=False,
    )

    first_output = install()
    first_notify = tomllib.loads(config.read_text())["notify"]
    second_output = install()

    assert not legacy.exists()
    assert first_notify == ["/computer-use", "turn-ended"]
    assert tomllib.loads(config.read_text())["notify"] == first_notify
    assert _retirement_lines(first_output) == [RETIRED_NOTIFY_LINE]
    assert _retirement_lines(second_output) == [RETIRED_NOTIFY_LINE]


def test_codex_installer_fails_closed_on_unknown_previous_notify_shape(tmp_path):
    legacy, config, install = _codex_retirement_fixture(
        tmp_path,
        lambda path: [
            "/computer-use", "turn-ended", "--previous-notify",
            json.dumps({"argv": [path]}).replace("/", r"\/"),
        ],
    )
    original_notify = tomllib.loads(config.read_text())["notify"]

    output = install()

    assert legacy.exists()
    assert tomllib.loads(config.read_text())["notify"] == original_notify
    assert _retirement_lines(output) == [KEPT_NOTIFY_LINE]


def test_legacy_notify_is_retired_only_after_codex_migration(tmp_path):
    home = tmp_path / "home"
    hooks, codex = home / ".B12/hooks", home / ".codex"
    hooks.mkdir(parents=True)
    codex.mkdir()
    legacy = hooks / "b12-codex-notify.sh"
    user_named = home / "custom/b12-codex-notify.sh"
    user_session_end = home / "custom/memory-codex-session-end.sh"
    legacy.write_text("#!/bin/sh\n")
    config = codex / "config.toml"
    config.write_text(f'notify = [\n  "/custom-notify",\n  "{user_named}",\n  "{legacy}",\n]\n'
                      'notify_backup = ["keep"]\n')
    (codex / "hooks.json").write_text('{"hooks": {}}\n')
    env = os.environ | {"HOME": str(home), "B12_DATA_DIR": str(home / ".B12")}
    def install(flag, check=True):
        subprocess.run(
            ["bash", str(ROOT / "install.sh"), flag, "--no-gc-cron"], env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30, check=check,
        )

    install("--minimal")
    assert legacy.exists()
    python = home / ".local/b12-venv/bin/python3"
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)
    broken = {"hooks": {"SessionEnd": [{"hooks": [{"command": str(user_session_end)}]}]}}
    (codex / "hooks.json").write_text(json.dumps(broken))
    (codex / "hooks.json").chmod(0o400)
    install("--codex", check=False)
    assert legacy.exists()
    assert str(legacy) in tomllib.loads(config.read_text())["notify"]
    (codex / "hooks.json").chmod(0o600)
    (codex / "hooks.json").write_text('{"hooks": {}}\n')
    install("--codex")
    assert not legacy.exists()
    parsed = tomllib.loads(config.read_text())
    assert (parsed["notify"], parsed["notify_backup"]) == (["/custom-notify", str(user_named)], ["keep"])
    assert "SessionEnd" in json.loads((codex / "hooks.json").read_text())["hooks"]


def test_codex_installer_preserves_symlink_and_mixed_hook_siblings(tmp_path):
    home = tmp_path / "home"
    hooks, codex = home / ".B12/hooks", home / ".codex"
    hooks.mkdir(parents=True)
    codex.mkdir()
    legacy = hooks / "b12-codex-notify.sh"
    legacy.mkdir()  # os.unlink must fail so config retirement has to roll back.
    config_target = home / "dotfiles/codex-config.toml"
    config_target.parent.mkdir()
    config_target.write_text(f'notify = ["/custom-notify", "{legacy}"]\n')
    config = codex / "config.toml"
    config.symlink_to(config_target)
    hooks_json = codex / "hooks.json"
    hooks_json.write_text('{"hooks": {}}\n')
    python = home / ".local/b12-venv/bin/python3"
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)
    env = os.environ | {"HOME": str(home), "B12_DATA_DIR": str(home / ".B12")}

    def install(check=True):
        subprocess.run(
            ["bash", str(ROOT / "install.sh"), "--codex", "--no-gc-cron"], env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30, check=check,
        )

    install(check=False)
    assert config.is_symlink()
    assert str(legacy) in tomllib.loads(config.read_text())["notify"]
    assert legacy.is_dir()

    legacy.rmdir()
    legacy.write_text("#!/bin/sh\n")
    install()
    assert config.is_symlink()
    assert not legacy.exists()
    assert tomllib.loads(config.read_text())["notify"] == ["/custom-notify"]

    registered = json.loads(hooks_json.read_text())
    mixed = registered["hooks"]["Stop"][0]
    mixed["owner"] = "user-metadata"
    mixed["hooks"].append({
        "type": "command", "command": "/user/sibling-handler.sh", "timeout": 7,
    })
    hooks_json.write_text(json.dumps(registered))
    install()

    rerun = json.loads(hooks_json.read_text())["hooks"]["Stop"]
    assert rerun[0]["owner"] == "user-metadata"
    assert rerun[0]["hooks"][1] == {
        "type": "command", "command": "/user/sibling-handler.sh", "timeout": 7,
    }


def test_codex_installer_splits_turn_and_session_end_responsibilities():
    install = (ROOT / "install.sh").read_text()
    processor = (ROOT / "scripts" / "codex_session_end.py").read_text()
    stop_hook = (ROOT / "hooks" / "memory-codex-stop.sh").read_text()
    session_hook = (ROOT / "hooks" / "memory-codex-session-end.sh").read_text()

    assert "('SessionEnd'," in install
    assert "'memory-codex-session-end.sh'" in install
    assert "('Stop',             'memory-codex-stop.sh'" in install
    assert "codex_session_end.py" not in stop_hook
    assert "codex_session_end.py" in session_hook
    assert "sleep 120" not in session_hook
    assert "process_rollout(path, force=True)" in processor
    assert not (ROOT / "hooks" / "b12-codex-notify.sh").exists()
    assert "explicitly disabled" in install


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
