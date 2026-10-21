"""Memories without a memory_type must still render a display line.

The display string was built as '[' || m.memory_type || '] ' || content. In
SQLite concatenating NULL yields NULL, so a memory stored without a type (for
example some session summaries written by other hosts) surfaced as a bare
"null" with its content lost, in proactive surfacing, recall and the session
context blocks.
"""
from __future__ import annotations

import json
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("sqlite_vec")

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import embed_daemon  # noqa: E402


class _FakeModel:
    def encode(self, texts, normalize_embeddings=True):
        return np.array([[1.0, 0.0, 0.0, 0.0]] * len(texts), dtype=np.float32)


def _make_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE memories (id INTEGER PRIMARY KEY, content TEXT, content_hash TEXT, memory_type TEXT,"
        " metadata TEXT, tags TEXT, deleted_at REAL, valid_until TEXT)"
    )
    conn.execute("CREATE TABLE memory_embeddings (rowid INTEGER PRIMARY KEY, content_embedding BLOB)")
    vector = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32).tobytes()
    for mem_id, mtype, content in ((1, None, "untyped memory text"), (2, "decision", "typed memory text")):
        conn.execute(
            "INSERT INTO memories (id, content, content_hash, memory_type, metadata, tags) VALUES (?, ?, ?, ?, '{}', '')",
            (mem_id, content, f"h{mem_id}", mtype),
        )
        conn.execute("INSERT INTO memory_embeddings (rowid, content_embedding) VALUES (?, ?)", (mem_id, vector))
    conn.commit()
    conn.close()


@pytest.mark.parametrize("op", [embed_daemon._recall, embed_daemon._semantic_search])
def test_untyped_memory_gets_a_general_display(tmp_path, op):
    db = tmp_path / "mem.db"
    _make_db(db)
    response = op(_FakeModel(), {"query": "anything", "db_path": str(db), "threshold": 0.1, "limit": 5})
    displays = {r["id"]: r["display"] for r in response["results"]}
    assert displays[1] == "[general] untyped memory text"
    assert displays[2] == "[decision] typed memory text"
    if op is embed_daemon._recall:
        # recall also sends the type on its own; hooks build "[type|src:...]" from it.
        assert {r["id"]: r["memory_type"] for r in response["results"]} == {1: "general", 2: "decision"}


def test_resurface_hits_carry_a_general_type(tmp_path):
    import b12_long_session

    db = tmp_path / "mem.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE memories (id INTEGER PRIMARY KEY, content TEXT, content_hash TEXT, memory_type TEXT,"
        " metadata TEXT, created_at REAL, deleted_at REAL, valid_until TEXT)"
    )
    meta = json.dumps({"source_session": "session-abcd", "importance_score": 0.9})
    conn.execute("INSERT INTO memories (id, content, content_hash, memory_type, metadata) VALUES (1, 'x', 'h1', NULL, ?)", (meta,))
    conn.commit()
    conn.close()
    hits = b12_long_session.pick_resurface_ids(str(db), "session-abcdef-0001")
    assert [h["memory_type"] for h in hits] == ["general"]


def test_surfacing_context_labels_untyped_memories():
    import surfacing_engine

    result = surfacing_engine.SurfacingResult(
        memories=[{"id": 1, "content": "untyped", "memory_type": None, "score": 0.9}], surfaced=True
    )
    assert "  [general] untyped" in surfacing_engine.format_for_context(result).splitlines()


def _sql_sources() -> list[Path]:
    # Every file that can carry B12 SQL; tests are excluded because they quote
    # the broken patterns on purpose.
    sources = [
        *REPO_ROOT.glob("hooks/**/*.sh"),
        *REPO_ROOT.glob("scripts/**/*.py"),
        *REPO_ROOT.glob("plugins/opencode/src/**/*.ts"),
    ]
    return [p for p in sources if "tests" not in p.relative_to(REPO_ROOT).parts]


def _offending_lines(pattern: re.Pattern, allow: re.Pattern | None = None) -> list[str]:
    return [
        f"{path.relative_to(REPO_ROOT)}:{n}"
        for path in _sql_sources()
        for n, line in enumerate(path.read_text().splitlines(), 1)
        if pattern.search(line) and not (allow and allow.search(line))
    ]


def test_no_unguarded_memory_type_concatenation_remains():
    # Any SQL concatenation touching the column, whatever text surrounds it and
    # whether it is qualified (m.memory_type) or not. A JS `memory_type || "x"`
    # fallback is a logical OR, not SQL, and is allowed.
    concat = re.compile(r"\|\|\s*(?:\w+\.)?memory_type\b|\b(?:\w+\.)?memory_type\s*\|\|(?!\s*\")")
    assert not _offending_lines(concat)


def test_refine_turns_an_explicit_null_type_into_general():
    import asyncio

    import b12_mcp_server as mcp

    if mcp._refine_candidates is None:
        pytest.skip("memory_refine unavailable")
    seen = {}

    def fake_refine(valid, threshold):
        seen["valid"] = valid
        return [{**c, "quality_score": 0.9, "group_size": 1} for c in valid]

    orig, mcp._refine_candidates = mcp._refine_candidates, fake_refine
    try:
        out = asyncio.run(mcp.memory_refine(candidates='[{"content": "a fact", "memory_type": null, "tags": null}]'))
    finally:
        mcp._refine_candidates = orig
    assert seen["valid"] == [{"content": "a fact", "memory_type": "general", "tags": ""}]
    assert "| general | a fact" in out and "None" not in out


def test_python_displays_fall_back_for_untyped_rows():
    # f"[{row['memory_type']}]" prints "None" for an untyped row, and
    # `memory_type or ''` hands hooks an empty type they render as "[|src:...]".
    bare = re.compile(r"""\[['"]memory_type['"]\]\}|['"]memory_type['"]\s*:\s*[^,\n]*\bor\s*(?:''|"")""")
    assert not [hit for hit in _offending_lines(bare) if hit.split(":")[0].endswith(".py")]


def test_type_exclusion_predicates_keep_untyped_rows():
    # In SQL `NULL NOT IN (...)` and `NULL != 'x'` are NULL, which drops untyped
    # memories; every SQL exclusion on memory_type needs an IS NULL branch.
    sql_exclusion = re.compile(r"(?:\w+\.)?memory_type\s*(?:NOT IN\s*\(|!=\s*'|<>\s*')")
    guarded = re.compile(r"(?:\w+\.)?memory_type IS NULL OR")
    assert not _offending_lines(sql_exclusion, guarded)


def _hook_jq(hook: str, var: str) -> str | None:
    match = re.search(var + r"=\$\(echo \"\$_RESP\" \| jq -[rc] '([^']+)'", hook)
    return match.group(1) if match else None


def _jq(flag: str, program: str, data: str) -> str:
    return subprocess.run(["jq", flag, program], input=data, capture_output=True, text=True, check=True).stdout


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq not installed")
def test_proactive_surface_counts_shows_and_records_only_displayed_results():
    # Runs the hook's own jq programs in order: the response filter, then the
    # hit count, the injected lines and the dedup-ledger ids.
    hook = (REPO_ROOT / "hooks" / "memory-proactive-surface.sh").read_text()
    response_filter = _hook_jq(hook, "_RESP") or "."
    mixed = json.dumps({"ok": True, "results": [
        {"id": 1, "display": None}, {"id": 2}, {"id": 3, "display": ""}, {"id": 4, "display": "[decision] kept"},
    ]})
    filtered = _jq("-c", response_filter, mixed)
    assert _jq("-r", _hook_jq(hook, "_HIT_COUNT"), filtered).strip() == "1"
    assert _jq("-r", _hook_jq(hook, "_DISPLAY"), filtered).splitlines() == ["[decision] kept"]
    assert _jq("-r", _hook_jq(hook, "_INJECTED_IDS"), filtered).splitlines() == ["4"]

    all_null = json.dumps({"ok": True, "results": [{"id": 1, "display": None}]})
    assert _jq("-r", _hook_jq(hook, "_HIT_COUNT"), _jq("-c", response_filter, all_null)).strip() == "0"

    filter_at = hook.find("_RESP=$(echo")
    for var in ("_HIT_COUNT", "_DISPLAY", "_INJECTED_IDS"):
        assert hook.index(f"{var}=") > filter_at >= 0, f"{var} must be derived from the filtered response"
