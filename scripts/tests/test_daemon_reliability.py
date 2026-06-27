"""Guards for the daemon-reliability fixes (2026-06-27 audit #12 + #13).

#12: the MCP daemon's RSS self-guard os._exit(1) skipped atexit + the lifespan
     finally, dropping an in-progress (global) session-tracker summary. It must
     flush best-effort before exiting.
#13: embed-daemon handlers open their own SQLite conn via _open_db and close it
     on normal paths, but an exception mid-handler leaked it. handle_request now
     closes every conn opened during the request, regardless of how it exits.
"""
from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))


def test_rss_guard_flushes_tracker_before_exit():
    src = (ROOT / "scripts" / "b12_mcp_daemon.py").read_text()
    m = re.search(r"def _rss_self_guard_timer.*?os\._exit\(1\)", src, re.DOTALL)
    assert m, "RSS self-guard / os._exit not found"
    assert "_atexit_flush()" in m.group(0), "RSS guard must flush the session tracker before os._exit (#12)"


def test_handle_request_closes_conn_on_handler_exception(tmp_path, monkeypatch):
    try:
        import embed_daemon as D
    except Exception as e:
        pytest.skip(f"embed_daemon unavailable: {e}")
    try:
        import sqlite_vec  # noqa: F401
    except Exception:
        pytest.skip("sqlite_vec not installed")

    db = str(tmp_path / "m.db")
    captured = {}

    def boom(model, data):
        captured["c"] = D._open_db(data["db_path"])   # opens + registers a conn
        raise RuntimeError("kaboom")

    monkeypatch.setattr(D, "_semantic_search", boom)
    resp = D.handle_request(object(), {"op": "semantic_search", "db_path": db}, 0.0, 0)

    assert resp.get("ok") is False, "handler exception should be caught"
    assert D._open_conns == [], "tracked conns not cleared after the request (#13)"
    with pytest.raises(sqlite3.ProgrammingError):
        captured["c"].execute("SELECT 1")   # closed conn -> ProgrammingError


def test_handle_request_closes_conn_on_normal_return(tmp_path, monkeypatch):
    try:
        import embed_daemon as D
    except Exception as e:
        pytest.skip(f"embed_daemon unavailable: {e}")
    try:
        import sqlite_vec  # noqa: F401
    except Exception:
        pytest.skip("sqlite_vec not installed")

    db = str(tmp_path / "m.db")
    captured = {}

    def ok_handler(model, data):
        captured["c"] = D._open_db(data["db_path"])
        return {"ok": True, "results": []}

    monkeypatch.setattr(D, "_recall", ok_handler)
    resp = D.handle_request(object(), {"op": "recall", "db_path": db}, 0.0, 0)
    assert resp.get("ok") is True
    assert D._open_conns == []
    with pytest.raises(sqlite3.ProgrammingError):
        captured["c"].execute("SELECT 1")



def test_open_db_registers_conn_before_setup():
    """#13 (GPT review): _open_db must track the conn BEFORE sqlite_vec.load /
    PRAGMA, so a setup failure after connect() still leaves the conn closeable."""
    src = (ROOT / "scripts" / "embed_daemon.py").read_text()
    body = re.search(r"def _open_db.*?return conn", src, re.DOTALL)
    assert body, "_open_db not found"
    b = body.group(0)
    assert b.index("_open_conns.append") < b.index("sqlite_vec.load"), \
        "_open_db registers the conn AFTER load — a load failure would leak it (#13)"


def test_atexit_flush_persists_active_contextvar_tracker(tmp_path, monkeypatch):
    """#12 (GPT/GLM review): _atexit_flush must flush the ACTIVE (contextvar)
    tracker — the one the daemon populates — not the always-empty global. Populate
    the contextvar, flush, and assert a session_summary row lands."""
    try:
        import b12_mcp_server as M
    except Exception as e:
        pytest.skip(f"b12_mcp_server unavailable: {e}")

    db = str(tmp_path / "m.db")
    conn = sqlite3.connect(db)
    conn.execute(
        """CREATE TABLE memories (id INTEGER PRIMARY KEY AUTOINCREMENT,
           content TEXT, content_hash TEXT UNIQUE, metadata TEXT, tags TEXT,
           memory_type TEXT, created_at REAL, updated_at REAL, strength REAL)"""
    )
    conn.commit(); conn.close()
    monkeypatch.setattr(M, "DB_PATH", db)

    tracker = M._new_session_tracker()
    tracker["tool_calls"] = 5
    tracker["project"] = "B12"
    tracker["search_queries"] = ["alpha"]
    tok = M._session_tracker_var.set(tracker)
    try:
        M._atexit_flush()
    finally:
        M._session_tracker_var.reset(tok)

    n = sqlite3.connect(db).execute(
        "SELECT COUNT(*) FROM memories WHERE memory_type='session_summary'"
    ).fetchone()[0]
    assert n == 1, "RSS-exit flush did not persist the active contextvar tracker (#12)"



if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
