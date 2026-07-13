"""Regression tests for the package version synchronization CI check."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CHECK = ROOT / "scripts" / "check_package_versions.py"


def _run_check(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CHECK), "--root", str(root)],
        capture_output=True,
        text=True,
        check=False,
    )


def _write_metadata(
    root: Path,
    python_version: str,
    node_version: str,
    lock_version: str | None = None,
    lock_root_version: str | None = None,
) -> None:
    lock_version = lock_version or node_version
    lock_root_version = lock_root_version or node_version
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "fixture"\nversion = "{python_version}"\n',
        encoding="utf-8",
    )
    (root / "package.json").write_text(
        json.dumps({"name": "fixture", "version": node_version}),
        encoding="utf-8",
    )
    (root / "package-lock.json").write_text(
        json.dumps({
            "name": "fixture",
            "version": lock_version,
            "lockfileVersion": 3,
            "packages": {"": {"name": "fixture", "version": lock_root_version}},
        }),
        encoding="utf-8",
    )


def test_repository_package_versions_match():
    result = _run_check(ROOT)

    assert result.returncode == 0, result.stderr
    assert "pyproject.toml, package.json, and package-lock.json versions match" in result.stdout


def test_check_succeeds_for_matching_versions(tmp_path):
    _write_metadata(tmp_path, "1.2.3", "1.2.3")

    result = _run_check(tmp_path)

    assert result.returncode == 0, result.stderr
    assert "(1.2.3)" in result.stdout


def test_check_fails_with_all_version_touchpoints_on_package_drift(tmp_path):
    _write_metadata(tmp_path, "1.2.3", "1.2.4")

    result = _run_check(tmp_path)

    assert result.returncode == 1
    assert "package versions are out of sync" in result.stderr
    assert "pyproject.toml [project].version = '1.2.3'" in result.stderr
    assert "package.json version = '1.2.4'" in result.stderr
    assert "package-lock.json version = '1.2.4'" in result.stderr
    assert "package-lock.json packages[''].version = '1.2.4'" in result.stderr


def test_check_fails_when_lockfile_top_level_version_drifts(tmp_path):
    _write_metadata(tmp_path, "1.2.3", "1.2.3", lock_version="1.2.2")

    result = _run_check(tmp_path)

    assert result.returncode == 1
    assert "package-lock.json version = '1.2.2'" in result.stderr
    assert "package-lock.json packages[''].version = '1.2.3'" in result.stderr


def test_check_fails_when_lockfile_root_package_version_drifts(tmp_path):
    _write_metadata(tmp_path, "1.2.3", "1.2.3", lock_root_version="1.2.2")

    result = _run_check(tmp_path)

    assert result.returncode == 1
    assert "package-lock.json version = '1.2.3'" in result.stderr
    assert "package-lock.json packages[''].version = '1.2.2'" in result.stderr
