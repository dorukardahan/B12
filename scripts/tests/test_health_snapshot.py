"""Guards for b12_health.write_snapshot, the file SessionStart reads."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import b12_health  # noqa: E402


def _patch(monkeypatch, coverage, jobs):
    monkeypatch.setattr(b12_health, "check_vector_coverage", coverage)
    monkeypatch.setattr(b12_health, "check_launchd_jobs", jobs)


def test_snapshot_lists_only_non_ok_checks(tmp_path, monkeypatch):
    _patch(monkeypatch,
           lambda: b12_health.CheckResult("OK", "Vector coverage 100.0% (10/10), 0 waiting over 1h"),
           lambda: b12_health.CheckResult("WARN", "B12 launchd jobs: 1 healthy, 1 failing", "x: not loaded"))
    path = tmp_path / "s" / "health-snapshot.json"
    assert b12_health.write_snapshot(path) is True
    data = json.loads(path.read_text())
    assert data["issues"] == ["B12 launchd jobs: 1 healthy, 1 failing: x: not loaded"]
    assert abs(data["ts"] - time.time()) < 60


def test_crashing_check_is_reported_not_raised(tmp_path, monkeypatch):
    def boom():
        raise RuntimeError("db locked")
    _patch(monkeypatch, boom, lambda: b12_health.CheckResult("OK", "jobs"))
    path = tmp_path / "health-snapshot.json"
    b12_health.write_snapshot(path)
    assert "crashed" in json.loads(path.read_text())["issues"][0]


def test_fresh_snapshot_is_not_rewritten(tmp_path, monkeypatch):
    _patch(monkeypatch, lambda: b12_health.CheckResult("OK", "a"), lambda: b12_health.CheckResult("OK", "b"))
    path = tmp_path / "health-snapshot.json"
    path.write_text('{"ts": 1, "issues": []}')
    assert b12_health.write_snapshot(path, min_interval=3600) is False
    old = time.time() - 7200
    os.utime(path, (old, old))
    assert b12_health.write_snapshot(path, min_interval=3600) is True
