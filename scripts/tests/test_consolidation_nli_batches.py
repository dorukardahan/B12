"""NLI in the nightly consolidation: full coverage and honest reasons.

The embed daemon answers at most 20 pairs per nli_check request and drops the
rest without saying so, and it exits after two idle hours. The 02:00 run sent a
whole cluster's pairs in one request and never started the daemon, so on one
install 24,597 of 28,291 pairs had no verdict one night and all 28,459 the next,
and the report blamed "daemon down or text too short" for both. With every pair
checked, NLI flagged 16,914 of 28,291 pairs as contradictions in ~20 minutes, so
the nightly run, which does not merge, now skips NLI and says so.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
ce = pytest.importorskip("consolidation_engine")


def _fake_daemon(cap=20, fail_chunk=None):
    calls = []

    def request(payload, timeout=30):
        pairs = payload["pairs"]
        calls.append(len(pairs))
        if fail_chunk is not None and len(calls) - 1 == fail_chunk:
            return None
        kept = pairs[:cap]  # the daemon's silent truncation
        return {"ok": True, "results": [{"label": "neutral", "scores": {"contradiction": 0.01}} for _ in kept]}

    return request, calls


def test_every_pair_gets_a_verdict_in_requests_of_twenty(monkeypatch):
    request, calls = _fake_daemon()
    monkeypatch.setattr(ce, "_daemon_alive", lambda: True)
    monkeypatch.setattr(ce, "_daemon_request", request)
    out = ce._nli_check_pairs([["a", "b"]] * 45)
    assert len(out) == 45 and all(r is not None for r in out)
    assert calls == [20, 20, 5]


def test_a_failed_request_only_loses_its_own_pairs(monkeypatch):
    request, _ = _fake_daemon(fail_chunk=1)
    monkeypatch.setattr(ce, "_daemon_alive", lambda: True)
    monkeypatch.setattr(ce, "_daemon_request", request)
    out = ce._nli_check_pairs([["a", "b"]] * 45)
    assert [r is None for r in out] == [False] * 20 + [True] * 20 + [False] * 5


def test_no_daemon_means_no_results(monkeypatch):
    monkeypatch.setattr(ce, "_daemon_alive", lambda: False)
    assert ce._nli_check_pairs([["a", "b"]]) is None


LONG = "the nightly job checks every similar pair with the NLI model before reporting it"


def _pair(text_b=LONG):
    e = np.ones(8, dtype=np.float32)
    mk = lambda i, t: ce.MemoryRecord(id=i, content_hash=f"h{i}", content=t, tags="proj:x",
                                      memory_type="learning", metadata_str="{}", strength=1.0, embedding=e)
    return [mk(1, LONG), mk(2, text_b)]


@pytest.fixture
def no_dedup(monkeypatch):
    monkeypatch.setattr(ce, "_is_near_duplicate", lambda a, b: False)


def _reasons(report):
    return [e.get("reason") for e in report if e["type"] == "merge_unverified"]


def test_reason_daemon_not_running(monkeypatch, no_dedup):
    monkeypatch.setattr(ce, "_daemon_alive", lambda: False)
    _, report = ce._analyze_cluster(_pair(), use_nli=True)
    assert _reasons(report) == ["nli_unavailable"]


def test_reason_short_text(monkeypatch, no_dedup):
    monkeypatch.setattr(ce, "_daemon_alive", lambda: True)
    _, report = ce._analyze_cluster(_pair(text_b="too short"), use_nli=True)
    assert _reasons(report) == ["short_text"]


def test_reason_failed_request(monkeypatch, no_dedup):
    monkeypatch.setattr(ce, "_daemon_alive", lambda: True)
    monkeypatch.setattr(ce, "_nli_check_pairs", lambda pairs: [None] * len(pairs))
    _, report = ce._analyze_cluster(_pair(), use_nli=True)
    assert _reasons(report) == ["nli_failed"]


def test_skipped_nli_is_reported_as_skipped(monkeypatch, no_dedup):
    monkeypatch.setattr(ce, "_daemon_alive", lambda: True)
    monkeypatch.setattr(ce, "_nli_check_pairs", lambda pairs: pytest.fail("NLI must not run"))
    _, report = ce._analyze_cluster(_pair(), use_nli=False, off_reason="nli_skipped")
    assert _reasons(report) == ["nli_skipped"]
