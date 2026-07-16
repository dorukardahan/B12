"""Focused regression coverage for the Grok lifecycle hook entry points."""

from __future__ import annotations

import importlib.util
import io
import json
import runpy
import sys
import types
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
HOOK_DIR = ROOT / ".grok" / "plugins-available" / "b12" / "hooks" / "scripts"
ENTRY_POINTS = ("b12-precompact.py", "b12-session-end.py")


def test_manifest_routes_both_lifecycle_events_to_the_entry_points():
    manifest = json.loads((HOOK_DIR.parent / "hooks.json").read_text(encoding="utf-8"))
    hooks = manifest["hooks"]
    expected = {
        "PreCompact": "b12-precompact.py",
        "SessionEnd": "b12-session-end.py",
    }
    assert set(hooks) == set(expected)
    for event, entry_point in expected.items():
        commands = [
            hook["command"]
            for group in hooks[event]
            for hook in group["hooks"]
        ]
        assert len(commands) == 1
        assert commands[0].endswith(f"/hooks/scripts/{entry_point}")


def _fake_core(receipts: list[list[tuple]]) -> types.ModuleType:
    """Return the public surface consumed by both thin entry points."""
    core = types.ModuleType("_b12_grok_core")

    def store_items(items):
        receipts.append(items)
        return len(items)

    vars(core).update(
        {
            "CORE_OK": True,
            "IMPORT_ERROR": "",
            "extract_decisions": lambda _text: ["use the public fixture decision"],
            "extract_gotchas": lambda _text: ["public fixture gotcha"],
            "extract_learnings": lambda _text: ["public fixture learning"],
            "extract_preferences": lambda _text: ["prefer the public fixture"],
            "store_items": store_items,
        }
    )
    return core


def _run_entry(monkeypatch, name: str, raw_payload: str, core: types.ModuleType) -> int:
    monkeypatch.setitem(sys.modules, "_b12_grok_core", core)
    monkeypatch.setattr(sys, "stdin", io.StringIO(raw_payload))
    original_path = list(sys.path)
    try:
        with pytest.raises(SystemExit) as exc:
            runpy.run_path(str(HOOK_DIR / name), run_name="__main__")
    finally:
        sys.path[:] = original_path
    return int(exc.value.code or 0)


def _write_transcript(home: Path, cwd: str, session_id: str) -> None:
    session_dir = home / ".grok" / "sessions" / cwd.replace("/", "%2F") / session_id
    session_dir.mkdir(parents=True)
    records = [
        {
            "type": "assistant",
            "content": (
                "This representative public fixture is deliberately long enough "
                "for both Grok lifecycle adapters to send it to shared extraction."
            ),
        },
        {"type": "user", "content": [{"text": "I prefer deterministic public fixtures."}]},
        "malformed transcript record",
    ]
    with (session_dir / "chat_history.jsonl").open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")
        handle.write("{not-json\n")


def test_valid_payloads_delegate_to_shared_core(monkeypatch, tmp_path, capsys):
    home = tmp_path / "home"
    cwd = str(tmp_path / "public-project")
    session_id = "grok-public-fixture"
    _write_transcript(home, cwd, session_id)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("B12_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("B12_EMBED_RUNTIME_DIR", str(tmp_path / "runtime"))

    receipts: list[list[tuple]] = []
    core = _fake_core(receipts)
    payload = json.dumps({"session_id": session_id, "cwd": cwd})

    assert _run_entry(monkeypatch, "b12-precompact.py", payload, core) == 0
    assert _run_entry(monkeypatch, "b12-session-end.py", payload, core) == 0

    assert len(receipts) == 2
    assert [item[0] for item in receipts[0]] == ["decision", "error_fix", "learning"]
    assert [item[0] for item in receipts[1]] == [
        "decision",
        "error_fix",
        "learning",
        "preference",
        "preference",
    ]
    assert all("source:grok" in item[2] for batch in receipts for item in batch)
    assert {item[3]["source"] for item in receipts[0]} == {"grok-precompact"}
    assert {item[3]["source"] for item in receipts[1]} == {"grok-session-end"}
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("entry_point", ENTRY_POINTS)
@pytest.mark.parametrize(
    "raw_payload",
    ("", "{not-json", "[]", '{"session_id":7,"cwd":[]}'),
    ids=("empty", "invalid-json", "non-object", "wrong-field-types"),
)
def test_incomplete_or_malformed_payload_is_safe(
    monkeypatch, tmp_path, entry_point, raw_payload
):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("B12_DATA_DIR", str(tmp_path / "data"))
    receipts: list[list[tuple]] = []

    assert _run_entry(monkeypatch, entry_point, raw_payload, _fake_core(receipts)) == 0
    assert receipts == []
    assert not (tmp_path / "data").exists()


def test_shared_core_missing_database_is_an_isolated_noop(monkeypatch, tmp_path):
    """The real core must not create or discover state outside the temp HOME."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("B12_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("B12_HOOK_DIR", str(ROOT))
    monkeypatch.setenv("B12_EMBED_RUNTIME_DIR", str(tmp_path / "runtime"))

    spec = importlib.util.spec_from_file_location(
        "_b12_grok_core_isolated_test", HOOK_DIR / "_b12_grok_core.py"
    )
    assert spec and spec.loader
    core = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(core)

    assert core.CORE_OK, core.IMPORT_ERROR
    assert Path(core._SCRIPTS_DIR) == ROOT / "scripts"
    assert core.store_items(
        [("learning", "public fixture", ["source:grok"], {"source": "test"})]
    ) == 0
    assert not list(tmp_path.rglob("*.db"))