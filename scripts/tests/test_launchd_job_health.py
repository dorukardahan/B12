"""Guards for check_launchd_jobs: launchd failures must not stay silent.

A wrong interpreter made the consolidation job fail every night for months, and a
Homebrew Python upgrade got jobs killed by a launch constraint. launchd recorded
both; nothing reported them. The check reads `launchctl print` for each B12 job.
"""
from __future__ import annotations

import plistlib
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

import b12_health  # noqa: E402

DAEMON = "com.b12.mcp.daemon"
NIGHTLY = "com.b12.graph-enrich"
OFF = "com.b12.memory-consolidate"


@pytest.fixture
def jobs(tmp_path, monkeypatch):
    agents = tmp_path / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    for label, spec in ((DAEMON, {"KeepAlive": True}), (NIGHTLY, {}), (OFF, {})):
        (agents / f"{label}.plist").write_bytes(
            plistlib.dumps({"Label": label, "ProgramArguments": ["/bin/sh"], **spec}))
    monkeypatch.setattr(b12_health, "_HOME", tmp_path)
    monkeypatch.setattr(b12_health.sys, "platform", "darwin")
    outputs = {
        "print-disabled": f'\t\t"{OFF}" => disabled\n',
        DAEMON: "\tstate = running\n\truns = 2\n\tlast exit reason = OS_REASON_CODESIGNING\n",
        NIGHTLY: "\tstate = not running\n\truns = 1\n\tlast exit code = 0\n",
    }

    def fake(*args):
        key = args[0] if args[0] == "print-disabled" else args[1].rsplit("/", 1)[-1]
        return outputs.get(key, "")

    monkeypatch.setattr(b12_health, "_launchctl", fake)
    return outputs


def test_healthy_jobs_and_disabled_job_are_ok(jobs):
    r = b12_health.check_launchd_jobs()
    assert r.status == "OK"
    assert "2 healthy, 1 disabled" in r.label
    assert OFF in r.detail


def test_running_keepalive_job_ignores_an_old_kill(jobs):
    # The daemon was killed once and respawned; it is running now, so it is healthy.
    assert b12_health.check_launchd_jobs().status == "OK"


@pytest.mark.parametrize("last", [
    "last exit code = 1",
    "last exit reason = OS_REASON_CODESIGNING",
    "last terminating signal = Killed: 9",
])
def test_failed_scheduled_run_warns(jobs, last):
    jobs[NIGHTLY] = f"\tstate = not running\n\truns = 3\n\t{last}\n"
    r = b12_health.check_launchd_jobs()
    assert r.status == "WARN"
    assert NIGHTLY in r.detail and "last run failed" in r.detail


def test_stopped_keepalive_job_warns(jobs):
    jobs[DAEMON] = "\tstate = not running\n\truns = 5\n"
    r = b12_health.check_launchd_jobs()
    assert r.status == "WARN" and f"{DAEMON}: not running" in r.detail


def test_unloaded_job_warns(jobs):
    del jobs[NIGHTLY]
    r = b12_health.check_launchd_jobs()
    assert r.status == "WARN" and f"{NIGHTLY}: not loaded" in r.detail


def test_skipped_off_macos(monkeypatch):
    monkeypatch.setattr(b12_health.sys, "platform", "linux")
    assert b12_health.check_launchd_jobs().status == "OK"


def test_docs_named_plists_are_checked_by_label(jobs, tmp_path):
    # docs/setup.md installs scheduled templates as launchd-*.plist; their labels are com.b12.*.
    agents = tmp_path / "Library" / "LaunchAgents"
    (agents / "launchd-backup.plist").write_bytes(
        plistlib.dumps({"Label": "com.b12.memory-backup", "ProgramArguments": ["/bin/bash"]}))
    (agents / "launchd-tool.plist").write_bytes(
        plistlib.dumps({"Label": "org.example.tool", "ProgramArguments": ["/bin/true"]}))
    jobs["com.b12.memory-backup"] = "\tstate = not running\n\tlast exit code = 2\n"
    r = b12_health.check_launchd_jobs()
    assert r.status == "WARN" and "com.b12.memory-backup: last run failed" in r.detail
    assert "org.example.tool" not in r.detail
