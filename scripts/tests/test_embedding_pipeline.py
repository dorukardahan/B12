"""Regression tests for the embedding-pipeline fixes (2026-06-27 audit #9 + M1).

#9: the embed-daemon client read timeout was 5s while the daemon's own encode
    budget (CONN_TIMEOUT) is 15s and BGE-M3 batches can run >10s — so a slow
    encode timed out client-side and the embedding was SILENTLY dropped (memory
    stored but not vector-searchable). The client timeout must cover the daemon.
M1: import sent `{"op":"backfill"}` to the embed daemon, which has no such op
    (`unknown_op: backfill`) — so imported memories never got embeddings. Import
    must use the real `encode_batch`-based backfill.
"""
from __future__ import annotations

import os
import re
import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))


# ── #9: client timeout covers the daemon's encode budget ─────────────────────

def test_daemon_client_timeout_covers_daemon_budget():
    try:
        import b12_mcp_server as M
    except Exception as e:
        print(f"SKIP ({e})"); return
    # Must cover the daemon's CONN_TIMEOUT (15s) so a slow encode_batch isn't cut off.
    assert M._DAEMON_CLIENT_TIMEOUT >= 15, f"client timeout {M._DAEMON_CLIENT_TIMEOUT} < daemon 15s budget"


def test_daemon_request_uses_the_constant_not_5s():
    src = (REPO_ROOT / "b12_mcp_server.py").read_text()
    # The daemon_request socket must use the configurable constant, not a bare 5s.
    assert "s.settimeout(_DAEMON_CLIENT_TIMEOUT)" in src, "daemon_request not using _DAEMON_CLIENT_TIMEOUT"
    # And memory_refine's encode socket must no longer hardcode 5s.
    rsrc = (REPO_ROOT / "memory_refine.py").read_text()
    assert "s.settimeout(5)" not in rsrc, "memory_refine still hardcodes a 5s encode timeout"


def test_store_path_logs_skipped_embedding():
    src = (REPO_ROOT / "b12_mcp_server.py").read_text()
    assert "embedding skipped for id=" in src, "store path does not surface a skipped embedding (silent #9)"


# ── M1: import backfill uses the real encode_batch path ──────────────────────

def test_import_no_longer_sends_dead_backfill_op():
    src = (REPO_ROOT / "export_import.py").read_text()
    # The dead code built a request dict with a `"op": "backfill",` entry (trailing
    # comma) and sent it over a socket. The docstring may still *mention* the old
    # op, so match the dict-entry form + the actual send, not a bare substring.
    assert '"op": "backfill",' not in src, "import still builds the dead op:backfill request dict"
    assert "s.sendall((request" not in src, "import still sends a raw backfill socket request"
    assert "embedding_backfill.backfill(" in src, "import does not call the real embedding_backfill.backfill"


def test_request_backfill_calls_real_backfill(monkeypatch):
    try:
        import export_import as EI
        import embedding_backfill as BF
    except Exception as e:
        pytest.skip(f"module unavailable: {e}")
    seen = {}

    def fake_backfill(db_path, limit=None, missing=None, content_hashes=None):
        seen["db_path"] = db_path
        seen["limit"] = limit
        seen["content_hashes"] = content_hashes
        return (0, 0)

    monkeypatch.setattr(BF, "backfill", fake_backfill)
    EI._request_embedding_backfill("/tmp/x.db", {"h1", "h2"})
    assert seen.get("db_path") == "/tmp/x.db"
    # Import must scope the backfill to JUST its rows (by content_hash) — not a
    # global limited backfill that could skip imported rows (GPT-5.5 #1) or hog the
    # daemon over every pre-existing gap (GLM #134 review).
    assert seen.get("content_hashes") == {"h1", "h2"}
    assert seen.get("limit") is None


def test_request_backfill_swallows_and_logs_on_failure(monkeypatch, capsys):
    try:
        import export_import as EI
        import embedding_backfill as BF
    except Exception as e:
        pytest.skip(f"module unavailable: {e}")

    def boom(*a, **k):
        raise RuntimeError("daemon exploded")

    monkeypatch.setattr(BF, "backfill", boom)
    EI._request_embedding_backfill("/tmp/x.db", {"h1"})   # must NOT raise — import already succeeded
    err = capsys.readouterr().err
    assert "backfill failed" in err, "import backfill failure not surfaced to stderr (silent)"


def test_backfill_scoped_to_hashes_skips_other_gaps(tmp_path, monkeypatch):
    """Hash-scoped backfill embeds ONLY the given content_hashes' rows, not every
    pre-existing gap — so an import can't monopolize the daemon (GLM #134)."""
    try:
        import embedding_backfill as BF
    except Exception as e:
        pytest.skip(f"module unavailable: {e}")

    db = str(tmp_path / "m.db")
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE memories(id INTEGER PRIMARY KEY, content TEXT, tags TEXT, content_hash TEXT, deleted_at REAL)")
    conn.execute("CREATE TABLE memory_embeddings_rowids(rowid INTEGER PRIMARY KEY)")
    # id 1/2 = freshly imported (hashes h1/h2); id 3 = a pre-existing gap (h3).
    conn.execute("INSERT INTO memories VALUES (1,'imported a','','h1',NULL)")
    conn.execute("INSERT INTO memories VALUES (2,'imported b','','h2',NULL)")
    conn.execute("INSERT INTO memories VALUES (3,'old gap','','h3',NULL)")
    conn.commit(); conn.close()

    stored = []
    monkeypatch.setattr(BF, "daemon_request", lambda texts: ["b64-" + str(i) for i in range(len(texts))])
    monkeypatch.setattr(BF, "store_embedding", lambda dbp, mid, emb: stored.append(mid) or True)

    success, failed = BF.backfill(db, content_hashes={"h1", "h2"})
    assert (success, failed) == (2, 0), (success, failed)
    assert sorted(stored) == [1, 2], f"backfill embedded rows outside the imported set: {stored}"


def test_backfill_orchestrates_encode_and_store(tmp_path, monkeypatch):
    """embedding_backfill.backfill: find unembedded memories -> encode_batch ->
    store. Mock the daemon + store so no extension/socket is needed; assert the
    orchestration embeds exactly the missing rows."""
    try:
        import embedding_backfill as BF
    except Exception as e:
        pytest.skip(f"module unavailable: {e}")

    db = str(tmp_path / "m.db")
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE memories(id INTEGER PRIMARY KEY, content TEXT, tags TEXT, deleted_at REAL)")
    conn.execute("CREATE TABLE memory_embeddings_rowids(rowid INTEGER PRIMARY KEY)")
    conn.execute("INSERT INTO memories VALUES (1,'alpha fact','',NULL)")
    conn.execute("INSERT INTO memories VALUES (2,'beta fact','',NULL)")
    conn.commit(); conn.close()

    encoded = {}
    stored = []
    monkeypatch.setattr(BF, "daemon_request", lambda texts: ["b64-" + str(i) for i in range(len(texts))])
    monkeypatch.setattr(BF, "store_embedding", lambda dbp, mid, emb: stored.append(mid) or True)

    success, failed = BF.backfill(db)
    assert (success, failed) == (2, 0), (success, failed)
    assert sorted(stored) == [1, 2], stored


def test_backfill_is_noop_safe_when_daemon_down(tmp_path, monkeypatch):
    try:
        import embedding_backfill as BF
    except Exception as e:
        pytest.skip(f"module unavailable: {e}")
    db = str(tmp_path / "m.db")
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE memories(id INTEGER PRIMARY KEY, content TEXT, tags TEXT, deleted_at REAL)")
    conn.execute("CREATE TABLE memory_embeddings_rowids(rowid INTEGER PRIMARY KEY)")
    conn.execute("INSERT INTO memories VALUES (1,'alpha','',NULL)")
    conn.commit(); conn.close()
    monkeypatch.setattr(BF, "daemon_request", lambda texts: None)  # daemon down
    success, failed = BF.backfill(db)
    assert success == 0 and failed == 1, (success, failed)


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
