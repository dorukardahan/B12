#!/usr/bin/env python3
"""Verify that package metadata and the latest changelog release use one version."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import tomllib

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT_FILE = "pyproject.toml"
PACKAGE_FILE = "package.json"
PACKAGE_LOCK_FILE = "package-lock.json"
OPENCODE_PACKAGE_FILE = "plugins/opencode/package.json"
CHANGELOG_FILE = "CHANGELOG.md"
SEMVER = r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
CHANGELOG_RELEASE_PATTERN = re.compile(
    rf"^[ ]{{0,3}}##\s+(?:\[v?(?P<bracket>{SEMVER})\]|v?(?P<plain>{SEMVER})(?:\s|$))",
    re.IGNORECASE,
)
FENCE_PATTERN = re.compile(r"^[ ]{0,3}(?P<fence>`{3,}|~{3,})")


def _strip_html_comments(line: str, in_comment: bool) -> tuple[str, bool]:
    """Return visible Markdown text while tracking multiline HTML comments."""
    visible: list[str] = []
    cursor = 0
    while cursor < len(line):
        if in_comment:
            end = line.find("-->", cursor)
            if end == -1:
                return "".join(visible), True
            cursor = end + 3
            in_comment = False
            continue

        start = line.find("<!--", cursor)
        if start == -1:
            visible.append(line[cursor:])
            break
        visible.append(line[cursor:start])
        cursor = start + 4
        in_comment = True
    return "".join(visible), in_comment


def read_changelog_version(root: Path) -> str:
    """Return the first semantic-version release heading from the changelog."""
    changelog = (root / CHANGELOG_FILE).read_text(encoding="utf-8")
    fence_char: str | None = None
    fence_length = 0
    in_html_comment = False

    for raw_line in changelog.splitlines():
        if fence_char is not None:
            closing_fence = re.compile(
                rf"^[ ]{{0,3}}{re.escape(fence_char)}{{{fence_length},}}[ \t]*$"
            )
            if closing_fence.fullmatch(raw_line):
                fence_char = None
                fence_length = 0
            continue

        if not in_html_comment and (
            raw_line.startswith("    ") or raw_line.startswith("\t")
        ):
            continue

        line, in_html_comment = _strip_html_comments(raw_line, in_html_comment)
        if line.startswith("    ") or line.startswith("\t"):
            continue

        fence_match = FENCE_PATTERN.match(line)
        if fence_match:
            fence = fence_match.group("fence")
            fence_char = fence[0]
            fence_length = len(fence)
            continue

        match = CHANGELOG_RELEASE_PATTERN.match(line)
        if match:
            return str(match.group("bracket") or match.group("plain"))
    raise ValueError("no semantic-version release heading found")


def read_versions(root: Path) -> tuple[str, str, str | None, str | None, str, str]:
    """Return versions from package metadata and the first changelog release."""
    with (root / PYPROJECT_FILE).open("rb") as handle:
        pyproject = tomllib.load(handle)
    with (root / PACKAGE_FILE).open(encoding="utf-8") as handle:
        package = json.load(handle)
    try:
        with (root / PACKAGE_LOCK_FILE).open(encoding="utf-8") as handle:
            package_lock = json.load(handle)
    except FileNotFoundError:
        package_lock = None
    with (root / OPENCODE_PACKAGE_FILE).open(encoding="utf-8") as handle:
        opencode_package = json.load(handle)

    return (
        str(pyproject["project"]["version"]),
        str(package["version"]),
        str(package_lock["version"]) if package_lock is not None else None,
        str(package_lock["packages"][""]["version"])
        if package_lock is not None
        else None,
        str(opencode_package["version"]),
        read_changelog_version(root),
    )


def check_versions(root: Path) -> tuple[bool, str]:
    """Compare package versions and return a status plus an actionable message."""
    try:
        (
            python_version,
            node_version,
            lock_version,
            lock_root_version,
            opencode_version,
            changelog_version,
        ) = read_versions(root)
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError, tomllib.TOMLDecodeError) as exc:
        return False, (
            f"ERROR: could not read package versions from {PYPROJECT_FILE}, "
            f"{PACKAGE_FILE}, {PACKAGE_LOCK_FILE}, {OPENCODE_PACKAGE_FILE}, "
            f"and {CHANGELOG_FILE}: {exc}"
        )

    versions = {
        version
        for version in (
            python_version,
            node_version,
            lock_version,
            lock_root_version,
            opencode_version,
            changelog_version,
        )
        if version is not None
    }
    if len(versions) != 1:
        package_lock_lines = ""
        if lock_version is not None:
            package_lock_lines = (
                f"  {PACKAGE_LOCK_FILE} version = {lock_version!r}\n"
                f"  {PACKAGE_LOCK_FILE} packages[''].version = {lock_root_version!r}\n"
            )
        return False, (
            "ERROR: package versions are out of sync:\n"
            f"  {PYPROJECT_FILE} [project].version = {python_version!r}\n"
            f"  {PACKAGE_FILE} version = {node_version!r}\n"
            f"{package_lock_lines}"
            f"  {OPENCODE_PACKAGE_FILE} version = {opencode_version!r}\n"
            f"  {CHANGELOG_FILE} first release version = {changelog_version!r}\n"
            "Update all package version fields and the changelog release header in the same version change."
        )

    surfaces = [PYPROJECT_FILE, PACKAGE_FILE]
    if lock_version is not None:
        surfaces.append(PACKAGE_LOCK_FILE)
    surfaces.extend([OPENCODE_PACKAGE_FILE, CHANGELOG_FILE])
    return True, (
        f"OK: {', '.join(surfaces[:-1])}, and {surfaces[-1]} versions match ({python_version})."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=ROOT,
        help="repository root (defaults to the parent of scripts/)",
    )
    args = parser.parse_args(argv)

    matches, message = check_versions(args.root)
    print(message, file=sys.stdout if matches else sys.stderr)
    return 0 if matches else 1


if __name__ == "__main__":
    raise SystemExit(main())
