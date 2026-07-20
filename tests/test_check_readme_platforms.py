"""Regression tests for the README supported-platform count CI guard."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHECK = ROOT / "scripts" / "check_readme_platforms.py"


def _run_check(readme: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CHECK), "--readme", str(readme)],
        capture_output=True,
        text=True,
        check=False,
    )


def _write_readme(path: Path, badge_count: int, platforms: str) -> None:
    path.write_text(
        "\n".join(
            [
                "# Fixture",
                "",
                "[![Platforms](https://img.shields.io/badge/"
                f"platforms-{badge_count}-green)](#supported-platforms)",
                "",
                "- **Cross-tool memory** — the same DB powers " + platforms,
                "",
            ]
        ),
        encoding="utf-8",
    )


def test_repository_readme_has_15_unique_supported_platforms():
    result = _run_check(ROOT / "README.md")

    assert result.returncode == 0, result.stderr
    assert "matches 15 normalized unique platforms" in result.stdout


def test_normalization_deduplicates_case_and_whitespace(tmp_path):
    readme = tmp_path / "README.md"
    _write_readme(readme, 2, "Claude Code, claude   code, Cursor")

    result = _run_check(readme)

    assert result.returncode == 0, result.stderr
    assert "matches 2 normalized unique platforms" in result.stdout


def test_badge_count_drift_fails_with_actionable_error(tmp_path):
    readme = tmp_path / "README.md"
    _write_readme(readme, 2, "Claude Code, Codex CLI, Cursor")

    result = _run_check(readme)

    assert result.returncode == 1
    assert "badge declares 2" in result.stderr
    assert "summary contains 3 normalized unique platforms" in result.stderr
    assert "Update the Platforms badge or Cross-tool memory summary" in result.stderr


def test_platform_list_drift_fails_with_actionable_error(tmp_path):
    readme = tmp_path / "README.md"
    _write_readme(readme, 3, "Claude Code, Codex CLI")

    result = _run_check(readme)

    assert result.returncode == 1
    assert "badge declares 3" in result.stderr
    assert "summary contains 2 normalized unique platforms" in result.stderr
    assert "Update the Platforms badge or Cross-tool memory summary" in result.stderr


def test_missing_summary_fails_with_parse_guidance(tmp_path):
    readme = tmp_path / "README.md"
    readme.write_text(
        "[![Platforms](https://img.shields.io/badge/"
        "platforms-1-green)](#supported-platforms)\n",
        encoding="utf-8",
    )

    result = _run_check(readme)

    assert result.returncode == 1
    assert "expected exactly one Cross-tool memory platform summary" in result.stderr
    assert "Keep one Platforms badge and one comma-separated" in result.stderr
