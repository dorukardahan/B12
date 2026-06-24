"""Unit tests for the daemon RSS self-guard (shared_patterns.rss_bytes /
rss_exceeds), the getrusage-based memory backstop wired into embed_daemon.py
and b12_mcp_daemon.py by the 2026-06 OOM fix.

The pure functions are what the daemons depend on, so they're the right unit to
pin: the platform byte-vs-KiB normalization (a regression could silently flip
it and make a Linux daemon never trip — or a macOS one trip 1024× too eagerly),
the disabled/fail-open contracts, and the over-ceiling return.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import resource

import shared_patterns as sp


def test_rss_exceeds_disabled_with_nonpositive_ceiling():
    assert sp.rss_exceeds(0) == 0
    assert sp.rss_exceeds(-5) == 0


def test_rss_exceeds_under_ceiling_returns_zero():
    # No real process is anywhere near 1 PB.
    assert sp.rss_exceeds(10**9) == 0


def test_rss_exceeds_over_tiny_ceiling_returns_mb():
    # Any live interpreter is >1MB resident → returns its peak in MB (>0).
    over = sp.rss_exceeds(1)
    assert isinstance(over, int) and over > 1


def test_rss_exceeds_fail_open_on_measurement_error(monkeypatch):
    def _boom():
        raise RuntimeError("getrusage unavailable")
    monkeypatch.setattr(sp, "rss_bytes", _boom)
    # A guard that can't measure must NOT kill a healthy daemon → 0.
    assert sp.rss_exceeds(1) == 0


class _FakeRusage:
    def __init__(self, maxrss):
        self.ru_maxrss = maxrss


def test_rss_bytes_darwin_treats_value_as_bytes(monkeypatch):
    monkeypatch.setattr(sp.sys, "platform", "darwin")
    monkeypatch.setattr(resource, "getrusage", lambda who: _FakeRusage(5_000_000))
    assert sp.rss_bytes() == 5_000_000  # macOS reports bytes — used verbatim


def test_rss_bytes_linux_scales_kib_to_bytes(monkeypatch):
    monkeypatch.setattr(sp.sys, "platform", "linux")
    monkeypatch.setattr(resource, "getrusage", lambda who: _FakeRusage(5000))
    assert sp.rss_bytes() == 5000 * 1024  # Linux reports KiB → ×1024


def test_normalization_makes_ceiling_consistent_across_platforms(monkeypatch):
    """The whole point of normalization: the SAME physical footprint compares
    the same against a ceiling regardless of platform units. A 3 GB process
    must trip a 2 GB ceiling on both macOS (bytes) and Linux (KiB)."""
    three_gb_bytes = 3 * 1024 * 1024 * 1024
    # macOS: ru_maxrss already in bytes
    monkeypatch.setattr(sp.sys, "platform", "darwin")
    monkeypatch.setattr(resource, "getrusage", lambda who: _FakeRusage(three_gb_bytes))
    assert sp.rss_exceeds(2048) > 2048
    # Linux: ru_maxrss in KiB → same physical 3 GB
    monkeypatch.setattr(sp.sys, "platform", "linux")
    monkeypatch.setattr(resource, "getrusage", lambda who: _FakeRusage(three_gb_bytes // 1024))
    assert sp.rss_exceeds(2048) > 2048
