"""Regression tests for the package version synchronization CI check."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tomllib
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


def test_check_ignores_non_release_markdown_version_examples(tmp_path):
    _write_metadata(tmp_path, "1.2.3", "1.2.3")
    (tmp_path / "CHANGELOG.md").write_text(
        """# Changelog

## Unreleased

```markdown
## [v9.9.9] — fenced example
```

    ## [v8.8.8] — indented example

<!--
## [v7.7.7] — commented example
-->

- Document the literal `<!--` marker in inline code.

### v6.6.6 compatibility
- This is an Unreleased subsection, not a release.

Explanation. <!-- hidden draft
## [v5.5.5] — hidden inside a mid-line comment
-->

## [v1.2.3] — 2026-08-08

### Fixed
- Fixture.
""",
        encoding="utf-8",
    )

    result = _run_check(tmp_path)

    assert result.returncode == 0, result.stderr
    assert "versions match (1.2.3)" in result.stdout


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


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )


def _copy_tracked_checkout(destination: Path) -> None:
    """Create a clean main-branch checkout from the candidate working tree."""
    tracked = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        capture_output=True,
        check=True,
    ).stdout.split(b"\0")
    for raw_path in tracked:
        if not raw_path:
            continue
        relative = Path(os.fsdecode(raw_path))
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target, follow_symlinks=False)

    _git(destination, "init", "--initial-branch=main", "--quiet")
    disabled_hooks = destination / ".git" / "disabled-hooks"
    disabled_hooks.mkdir()
    _git(destination, "add", "--all")
    _git(
        destination,
        "-c",
        "user.name=B12 Tests",
        "-c",
        "user.email=test@invalid",
        "-c",
        "commit.gpgSign=false",
        "-c",
        f"core.hooksPath={disabled_hooks}",
        "commit",
        "--quiet",
        "-m",
        "test fixture",
    )


def test_release_dry_run_syncs_all_package_versions_without_git_side_effects(tmp_path, monkeypatch):
    rejecting_hooks = tmp_path / "rejecting-hooks"
    rejecting_hooks.mkdir()
    pre_commit = rejecting_hooks / "pre-commit"
    pre_commit.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    pre_commit.chmod(0o755)

    monkeypatch.setenv("GIT_CONFIG_COUNT", "3")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "commit.gpgSign")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "true")
    monkeypatch.setenv("GIT_CONFIG_KEY_1", "gpg.program")
    monkeypatch.setenv("GIT_CONFIG_VALUE_1", "/bin/false")
    monkeypatch.setenv("GIT_CONFIG_KEY_2", "core.hooksPath")
    monkeypatch.setenv("GIT_CONFIG_VALUE_2", str(rejecting_hooks))

    checkout = tmp_path / "checkout"
    checkout.mkdir()
    _copy_tracked_checkout(checkout)

    with (checkout / "pyproject.toml").open("rb") as handle:
        current = tomllib.load(handle)["project"]["version"]
    major, minor, patch = (int(part) for part in current.split("."))
    candidate = f"{major}.{minor}.{patch + 1}"
    notes = checkout / "release-notes.md"
    notes.write_text("### Fixed\n\n- Release test fixture.\n", encoding="utf-8")

    head_before = _git(checkout, "rev-parse", "HEAD").stdout.strip()
    tags_before = _git(checkout, "tag", "--list").stdout.splitlines()
    release = subprocess.run(
        ["bash", "scripts/release.sh", "--dry-run", candidate, str(notes)],
        cwd=checkout,
        capture_output=True,
        text=True,
        check=False,
    )

    assert release.returncode == 0, release.stdout + release.stderr

    check = subprocess.run(
        [sys.executable, "scripts/check_package_versions.py"],
        cwd=checkout,
        capture_output=True,
        text=True,
        check=False,
    )
    assert check.returncode == 0, check.stdout + check.stderr

    with (checkout / "pyproject.toml").open("rb") as handle:
        pyproject = tomllib.load(handle)
    package = json.loads((checkout / "package.json").read_text(encoding="utf-8"))
    package_lock = json.loads((checkout / "package-lock.json").read_text(encoding="utf-8"))
    opencode = json.loads(
        (checkout / "plugins" / "opencode" / "package.json").read_text(encoding="utf-8")
    )
    versions = {
        "pyproject.toml [project].version": pyproject["project"]["version"],
        "package.json version": package["version"],
        "package-lock.json version": package_lock["version"],
        "package-lock.json packages[''].version": package_lock["packages"][""]["version"],
        "plugins/opencode/package.json version": opencode["version"],
    }
    assert versions == {field: candidate for field in versions}, versions
    assert _git(checkout, "rev-parse", "HEAD").stdout.strip() == head_before
    assert _git(checkout, "tag", "--list").stdout.splitlines() == tags_before
