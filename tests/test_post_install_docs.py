"""Keep host-specific post-install guidance aligned with the README matrix."""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
SETUP = ROOT / "docs" / "setup.md"


def _section(text: str, heading: str) -> str:
    start = text.index(heading) + len(heading)
    match = re.search(r"^#{1,4} ", text[start:], re.MULTILINE)
    return text[start : start + match.start()] if match else text[start:]


def _table(section: str) -> list[list[str]]:
    rows = []
    for line in section.splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if cells[0] == "Platform" or set(cells[0]) <= {"-", ":"}:
            continue
        rows.append(cells)
    return rows


def _slug(heading: str) -> str:
    value = heading.strip().lower()
    value = re.sub(r"[^a-z0-9 -]", "", value)
    return re.sub(r"[ -]+", "-", value).strip("-")


def test_every_readme_platform_has_one_verification_row():
    readme_rows = _table(_section(README.read_text(), "## Supported Platforms"))
    setup_rows = _table(
        _section(SETUP.read_text(), "#### Post-install verification by platform")
    )
    readme_platforms = [row[0] for row in readme_rows]
    setup_platforms = [row[0] for row in setup_rows]

    assert len(readme_platforms) == 15
    assert Counter(setup_platforms) == Counter(readme_platforms)
    assert all(count == 1 for count in Counter(setup_platforms).values())

    readme_capture = {
        row[0]: "MCP-only" if "MCP-only" in row[2] else "Automatic"
        for row in readme_rows
    }
    setup_capture = {row[0]: row[1].replace("**", "") for row in setup_rows}
    assert setup_capture == readme_capture

    continue_extra = next(row[4] for row in setup_rows if row[0] == "Continue.dev")
    assert "~/.continue/settings.json" in continue_extra
    claude_settings = "~/" + ".claude/settings.json"
    assert claude_settings in continue_extra

    for platform, capture, _restart, status, extra in setup_rows:
        assert "B12" in status, f"{platform} does not identify the B12 status surface"
        if capture.replace("**", "") == "MCP-only":
            assert "No lifecycle hook check" in extra
        else:
            assert "No lifecycle hook check" not in extra


def test_readme_quick_start_link_resolves_to_verification_section():
    readme = README.read_text()
    match = re.search(
        r"\[[^]]*post-install verification[^]]*\]\((docs/setup\.md)\#([^)]+)\)",
        readme,
    )
    assert match, "README quick start must link directly to host verification"
    target = ROOT / match.group(1)
    assert target.is_file()
    anchors = {
        _slug(line.lstrip("# "))
        for line in target.read_text().splitlines()
        if line.startswith("#")
    }
    assert match.group(2) in anchors