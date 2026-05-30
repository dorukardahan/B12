"""End-to-end regression test for the Grok lifecycle hooks (R&D PR-5 / GRK-1).

Before this fix the hooks were dead-on-arrival: wrong `__file__`-depth path
resolution, imports of non-existent `extract_*` helpers, and a wrong
`merge_or_insert` signature. This test runs each hook as a subprocess against a
synthetic transcript + a temp DB (isolated HOME, no daemon → FTS-only fallback)
and asserts that memories are actually stored.

Run via:  python3 -m pytest scripts/tests/test_grok_hooks.py -v
      or:  python3 scripts/tests/test_grok_hooks.py
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_HOOK_DIR = os.path.join(_REPO, ".grok", "plugins-available", "b12", "hooks", "scripts")


def _platform_db_path(home):
    if sys.platform == "darwin":
        return os.path.join(home, "Library", "Application Support", "mcp-memory", "sqlite_vec.db")
    if sys.platform == "win32":
        return os.path.join(home, "AppData", "Local", "mcp-memory", "sqlite_vec.db")
    return os.path.join(home, ".local", "share", "mcp-memory", "sqlite_vec.db")


def _run_hooks_and_collect():
    thome = tempfile.mkdtemp(prefix="grok-e2e-home-")
    runtime = tempfile.mkdtemp(prefix="grok-e2e-rt-")  # no daemon socket → fallback path
    try:
        db = _platform_db_path(thome)
        os.makedirs(os.path.dirname(db), exist_ok=True)
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE memories(id INTEGER PRIMARY KEY, content TEXT, content_hash TEXT UNIQUE, "
            "tags TEXT, memory_type TEXT, metadata TEXT, created_at REAL, updated_at REAL, "
            "created_at_iso TEXT, updated_at_iso TEXT, deleted_at TEXT, valid_until TEXT)"
        )
        conn.commit()
        conn.close()

        cwd, sess = "/tmp/myproj", "testsession123"
        sdir = os.path.join(thome, ".grok", "sessions", cwd.replace("/", "%2F"), sess)
        os.makedirs(sdir, exist_ok=True)
        # Fake secret built by concatenation (no contiguous literal → gitleaks-safe),
        # placed inside a decision window to prove the FTS-only fallback scrubs.
        fake_key = "sk-ant-" + "a" * 48
        transcript = [
            {"type": "assistant", "content": f"We decided to rotate the leaked key {fake_key} and keep creds in a vault instead."},
            {"type": "assistant", "content": "Fixed the race condition in the embed daemon by adding a lock around the socket accept loop; the bug was caused by concurrent clients."},
            {"type": "assistant", "content": "TIL: the embedding model must be loaded before binding the unix socket, otherwise the first clients hang and time out."},
            {"type": "user", "content": [{"text": "User prefers black formatting and never use tabs in this repo."}]},
        ]
        with open(os.path.join(sdir, "chat_history.jsonl"), "w") as f:
            for obj in transcript:
                f.write(json.dumps(obj) + "\n")

        env = dict(os.environ, HOME=thome, B12_EMBED_RUNTIME_DIR=runtime)
        env.pop("B12_HOOK_DIR", None)  # force in-repo scripts-dir resolution
        event = json.dumps({"session_id": sess, "cwd": cwd})
        for hook in ("b12-precompact.py", "b12-session-end.py"):
            p = subprocess.run(
                [sys.executable, os.path.join(_HOOK_DIR, hook)],
                input=event, capture_output=True, text=True, env=env,
            )
            assert p.returncode == 0, f"{hook} exited {p.returncode}: {p.stderr}"

        conn = sqlite3.connect(db)
        rows = conn.execute("SELECT memory_type, content, tags FROM memories ORDER BY id").fetchall()
        conn.close()
        return rows
    finally:
        shutil.rmtree(thome, ignore_errors=True)
        shutil.rmtree(runtime, ignore_errors=True)


def test_grok_hooks_extract_and_store():
    if not os.path.isdir(_HOOK_DIR):
        print(f"SKIP test_grok_hooks_extract_and_store (no {_HOOK_DIR})")
        return
    rows = _run_hooks_and_collect()
    types = {r[0] for r in rows}
    assert len(rows) >= 3, f"expected >=3 memories stored, got {len(rows)}"
    assert {"decision", "error_fix", "learning"} <= types, f"missing extracted types: {types}"
    assert all("source:grok" in r[2] for r in rows), "grok provenance tag missing on a row"
    # FTS-only fallback must scrub: the fake key must not be stored raw.
    joined = " ".join(r[1] for r in rows)
    assert ("sk-ant-" + "a" * 48) not in joined, "fallback insert stored a raw secret (scrub bypassed)"
    assert any("[REDACTED" in r[1] for r in rows), "expected a redacted secret in a stored row"


def test_grok_hook_resurrects_soft_deleted():
    """Store → soft-delete → re-store must REVIVE the row (deleted_at=NULL) with
    no duplicate — exercises the resurrection hoisted ahead of the daemon/merge
    split. Runs daemon-less (FTS-only) so it's hermetic."""
    if not os.path.isdir(_HOOK_DIR):
        print("SKIP test_grok_hook_resurrects_soft_deleted (no hooks dir)")
        return
    thome = tempfile.mkdtemp(prefix="grok-res-")
    runtime = tempfile.mkdtemp(prefix="grok-res-rt-")
    try:
        db = _platform_db_path(thome)
        os.makedirs(os.path.dirname(db), exist_ok=True)
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE memories(id INTEGER PRIMARY KEY, content TEXT, content_hash TEXT UNIQUE, "
            "tags TEXT, memory_type TEXT, metadata TEXT, created_at REAL, updated_at REAL, "
            "created_at_iso TEXT, updated_at_iso TEXT, deleted_at TEXT, valid_until TEXT)"
        )
        conn.commit()
        conn.close()
        cwd, sess = "/tmp/resproj", "ressession"
        sdir = os.path.join(thome, ".grok", "sessions", cwd.replace("/", "%2F"), sess)
        os.makedirs(sdir, exist_ok=True)
        with open(os.path.join(sdir, "chat_history.jsonl"), "w") as f:
            f.write(json.dumps({"type": "assistant", "content": "We decided to use Redis instead of Memcached for the cache because of persistence."}) + "\n")
        env = dict(os.environ, HOME=thome, B12_EMBED_RUNTIME_DIR=runtime)
        env.pop("B12_HOOK_DIR", None)
        ev = json.dumps({"session_id": sess, "cwd": cwd})

        def run():
            return subprocess.run([sys.executable, os.path.join(_HOOK_DIR, "b12-precompact.py")],
                                  input=ev, capture_output=True, text=True, env=env)

        run()  # pass 1: stores
        conn = sqlite3.connect(db)
        n1 = conn.execute("SELECT COUNT(*) FROM memories WHERE deleted_at IS NULL").fetchone()[0]
        conn.execute("UPDATE memories SET deleted_at = ?", (1.0,))  # soft-delete all
        conn.commit()
        conn.close()
        run()  # pass 2: must resurrect, not skip and not duplicate
        conn = sqlite3.connect(db)
        live = conn.execute("SELECT COUNT(*) FROM memories WHERE deleted_at IS NULL").fetchone()[0]
        total = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        conn.close()
        assert n1 >= 1, "setup: pass 1 stored nothing"
        assert live == n1, "soft-deleted row was not resurrected"
        assert total == n1, "duplicate row inserted instead of resurrecting"

        # Expired (valid_until in the past) but NOT deleted: re-store must clear
        # valid_until (revive), not skip as a duplicate.
        conn = sqlite3.connect(db)
        conn.execute("UPDATE memories SET valid_until = '2000-01-01 00:00:00'")  # expire
        conn.commit()
        conn.close()
        run()  # pass 3
        conn = sqlite3.connect(db)
        expired = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE valid_until IS NOT NULL AND valid_until <= datetime('now')"
        ).fetchone()[0]
        total3 = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        conn.close()
        assert expired == 0, "expired row was not revived (valid_until still in the past)"
        assert total3 == n1, "duplicate inserted instead of reviving the expired row"
    finally:
        shutil.rmtree(thome, ignore_errors=True)
        shutil.rmtree(runtime, ignore_errors=True)


if __name__ == "__main__":
    rc = 0
    fns = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    for fn in fns:
        try:
            fn()
            print(f"OK: {fn.__name__}")
        except AssertionError as e:
            print(f"FAIL: {fn.__name__}: {e}")
            rc = 1
    sys.exit(rc)
