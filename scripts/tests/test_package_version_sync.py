"""Regression tests for the package version synchronization CI check."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CHECK = ROOT / "scripts" / "check_package_versions.py"
RELEASE = ROOT / "scripts" / "release.sh"


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
    opencode_version: str | None = None,
    changelog_version: str | None = None,
    include_lockfile: bool = True,
) -> None:
    lock_version = lock_version or node_version
    lock_root_version = lock_root_version or node_version
    opencode_version = opencode_version or node_version
    changelog_version = changelog_version or node_version
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "fixture"\nversion = "{python_version}"\n',
        encoding="utf-8",
    )
    (root / "package.json").write_text(
        json.dumps({"name": "fixture", "version": node_version}),
        encoding="utf-8",
    )
    if include_lockfile:
        (root / "package-lock.json").write_text(
            json.dumps({
                "name": "fixture",
                "version": lock_version,
                "lockfileVersion": 3,
                "packages": {"": {"name": "fixture", "version": lock_root_version}},
            }),
            encoding="utf-8",
        )
    (root / "plugins" / "opencode").mkdir(parents=True)
    (root / "plugins" / "opencode" / "package.json").write_text(
        json.dumps({"name": "fixture-opencode", "version": opencode_version}),
        encoding="utf-8",
    )
    (root / "CHANGELOG.md").write_text(
        f"# Changelog\n\n## [v{changelog_version}] — 2026-08-08\n\n### Fixed\n- Fixture.\n",
        encoding="utf-8",
    )


def test_repository_package_versions_match():
    result = _run_check(ROOT)

    assert result.returncode == 0, result.stderr
    assert "CHANGELOG.md" in result.stdout
    assert "plugins/opencode/package.json" in result.stdout
    assert "versions match" in result.stdout


def test_check_succeeds_for_matching_versions(tmp_path):
    _write_metadata(tmp_path, "1.2.3", "1.2.3")

    result = _run_check(tmp_path)

    assert result.returncode == 0, result.stderr
    assert "CHANGELOG.md" in result.stdout
    assert "plugins/opencode/package.json" in result.stdout
    assert "(1.2.3)" in result.stdout


def test_check_fails_when_latest_changelog_version_drifts(tmp_path):
    _write_metadata(tmp_path, "1.2.3", "1.2.3", changelog_version="1.2.2")

    result = _run_check(tmp_path)

    assert result.returncode == 1
    assert "package versions are out of sync" in result.stderr
    assert "CHANGELOG.md first release version = '1.2.2'" in result.stderr
    assert "pyproject.toml [project].version = '1.2.3'" in result.stderr


def test_check_succeeds_without_optional_package_lock(tmp_path):
    _write_metadata(tmp_path, "1.2.3", "1.2.3", include_lockfile=False)

    result = _run_check(tmp_path)

    assert result.returncode == 0, result.stderr
    assert "CHANGELOG.md" in result.stdout
    assert "package-lock.json" not in result.stdout
    assert "versions match (1.2.3)" in result.stdout


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


def test_check_fails_when_opencode_plugin_version_drifts(tmp_path):
    _write_metadata(tmp_path, "1.2.3", "1.2.3", opencode_version="1.2.2")

    result = _run_check(tmp_path)

    assert result.returncode == 1
    assert "plugins/opencode/package.json version = '1.2.2'" in result.stderr
    assert "Update all package version fields" in result.stderr


def test_release_script_syncs_verifies_and_stages_opencode_manifest():
    script = RELEASE.read_text(encoding="utf-8")

    assert '"plugins/opencode/package.json"' in script
    assert '"plugins/opencode/package.json:\\"version\\": \\"$NEW\\""' in script
    git_add_region = script.split("git add CHANGELOG.md", 1)[1].split("git commit", 1)[0]
    assert "plugins/opencode/package.json" in git_add_region
    pre_commit_region = script.split("git commit", 1)[0]
    assert "python3 scripts/check_package_versions.py" in pre_commit_region
