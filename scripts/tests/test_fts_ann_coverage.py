"""Regression tests for the FTS/ANN recall-coverage fixes (audit #19 + M3 + #10).

#19/M3: the hybrid-search 'or' fallback kept 2-char tokens, but the default FTS
        table is trigram (memory_content_fts), which produces NO tokens for
        <3-char strings → a 2-char term matches 0 rows. The filter must require
        >=3 chars for trigram (2 for the stemmed/unicode table).
#10:    the embed daemon's ANN gate defaulted enabled=False / threshold=10000,
        contradicting the documented default-on guarantee — every install without
        the install.sh config seed silently fell back to ORDER BY id DESC LIMIT.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))


# ── #19: trigram can't match 2-char; the OR filter must respect the min ──────

def test_trigram_fts_cannot_match_2char():
    """Empirical premise for #19: trigram FTS returns 0 for a 2-char MATCH but
    matches a 3+-char term."""
    db = sqlite3.connect(":memory:")
    db.execute("CREATE VIRTUAL TABLE t USING fts5(content, tokenize='trigram')")
    db.execute("INSERT INTO t VALUES ('the db daemon')")
    assert db.execute("SELECT COUNT(*) FROM t WHERE t MATCH '\"db\"'").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM t WHERE t MATCH '\"daemon\"'").fetchone()[0] == 1


def test_or_fallback_min_token_logic():
    """The fix: drop <3-char tokens for trigram (non-stemmed), keep 2-char for
    stemmed. (Mirrors the in-source `_min_tok = 2 if stemmed else 3` filter.)"""
    def or_words(query, stemmed):
        _min_tok = 2 if stemmed else 3
        return [w.strip() for w in query.split() if len(w.strip()) >= _min_tok]

    assert or_words("db ci", stemmed=False) == []                 # all 2-char -> dropped (skip FTS)
    assert or_words("db ci", stemmed=True) == ["db", "ci"]        # stemmed keeps 2-char
    assert or_words("daemon db", stemmed=False) == ["daemon"]     # 3+ kept, 2-char dropped


def test_mcp_source_has_min_token_guard():
    src = (ROOT / "scripts" / "b12_mcp_server.py").read_text()
    assert "_min_tok = 2 if stemmed else 3" in src, "MCP OR-fallback missing the trigram min-token guard (#19)"
    assert "len(w.strip()) > 1" not in src, "MCP OR-fallback reverted to the 2-char filter (#19)"


def test_opencode_source_and_dist_have_min_token_guard():
    src = (ROOT / "plugins" / "opencode" / "src" / "lib" / "db.ts").read_text()
    assert "w.length >= (stemmed ? 2 : 3)" in src, "db.ts OR-fallback not min-token-guarded (M3)"
    assert "w.length > 1" not in src, "db.ts still has the old 2-char filter"
    dist = (ROOT / "plugins" / "opencode" / "dist" / "index.js").read_text()
    assert dist.count("stemmed ? 2 : 3") >= 2, "dist bundle not rebuilt with the M3 fix (both sites)"


# ── #10: ANN defaults ON when no config is present ───────────────────────────

class _Conn:
    def __init__(self, count):
        self._count = count

    def execute(self, *a, **k):
        count = self._count
        class _Cur:
            def fetchone(self_inner):
                return (count,)
        return _Cur()


def test_ann_default_on_without_config(monkeypatch):
    try:
        import embed_daemon as D
    except Exception as e:
        pytest.skip(f"embed_daemon unavailable: {e}")
    # Simulate a missing config: _b12_cfg_get returns the caller's `default`.
    monkeypatch.setattr(D, "_b12_cfg_get", lambda *a, default=None, **k: default)
    use_ann, count = D._ann_supported(_Conn(600))
    assert use_ann is True and count == 600, "ANN not default-on at count >= 500 without config (#10)"
    # Boundary: 500 (>=threshold) on, 499 off; below the clamp floor (99) off.
    assert D._ann_supported(_Conn(500))[0] is True
    assert D._ann_supported(_Conn(499))[0] is False
    assert D._ann_supported(_Conn(99))[0] is False


def test_ann_topk_returns_empty_on_error():
    """The load-bearing safety contract for default-on ANN (#10): any sqlite-vec
    error in the KNN MATCH must yield [] so the caller falls back to full-scan."""
    try:
        import embed_daemon as D
    except Exception as e:
        pytest.skip(f"embed_daemon unavailable: {e}")

    class BadConn:
        def execute(self, *a, **k):
            raise sqlite3.OperationalError("no such module: vec0")

    assert D._ann_topk_rowids(BadConn(), [0.1] * 8, 5) == []


def test_ann_source_defaults():
    src = (ROOT / "scripts" / "embed_daemon.py").read_text()
    assert 'default=True' in src and 'default=500' in src, "ANN code defaults not flipped to on/500 (#10)"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
