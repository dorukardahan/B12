#!/usr/bin/env python3
"""Check that README's platform badge matches its cross-tool platform summary."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_README = ROOT / "README.md"

_BADGE_RE = re.compile(
    r"^\[!\[Platforms\]\([^\n)]*/badge/platforms-(\d+)-[^\n)]+\)\]\([^\n)]+\)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_SUMMARY_RE = re.compile(
    r"^-\s+\*\*Cross-tool memory\*\*\s+[—-]\s+the same DB powers\s+(.+?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def _single_match(pattern: re.Pattern[str], text: str, label: str) -> str:
    """Return one captured README value or raise an actionable parse error."""
    matches = pattern.findall(text)
    if len(matches) != 1:
        raise ValueError(
            f"expected exactly one {label} in README.md, found {len(matches)}. "
            "Keep one Platforms badge and one comma-separated "
            "'Cross-tool memory' platform summary."
        )
    return matches[0]


def _platform_names(summary: str) -> list[str]:
    """Return unique platform labels after whitespace and case normalization."""
    raw_names = summary.split(",")
    if any(not name.strip() for name in raw_names):
        raise ValueError(
            "the Cross-tool memory platform summary contains an empty comma-separated "
            "platform name. Remove the extra comma or add the missing name."
        )

    unique: dict[str, str] = {}
    for name in raw_names:
        display_name = re.sub(r"\s+", " ", name).strip()
        unique.setdefault(display_name.casefold(), display_name)
    return list(unique.values())


def check_readme(readme: Path) -> tuple[bool, str]:
    """Compare README platform metadata and return status plus a useful message."""
    try:
        text = readme.read_text(encoding="utf-8")
        badge_count = int(_single_match(_BADGE_RE, text, "Platforms badge"))
        platforms = _platform_names(
            _single_match(_SUMMARY_RE, text, "Cross-tool memory platform summary")
        )
    except (OSError, UnicodeError, ValueError) as exc:
        return False, f"ERROR: could not validate README platform count: {exc}"

    summary_count = len(platforms)
    if badge_count != summary_count:
        return False, (
            "ERROR: README platform count mismatch: "
            f"the badge declares {badge_count}, but the Cross-tool memory summary "
            f"contains {summary_count} normalized unique platforms "
            f"({', '.join(platforms)}).\n"
            "Update the Platforms badge or Cross-tool memory summary so both report "
            "the same platforms."
        )

    return True, (
        f"OK: README platform badge matches {summary_count} normalized unique "
        "platforms."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--readme",
        type=Path,
        default=DEFAULT_README,
        help="README file to validate (defaults to the repository README.md)",
    )
    args = parser.parse_args(argv)

    matches, message = check_readme(args.readme)
    print(message, file=sys.stdout if matches else sys.stderr)
    return 0 if matches else 1


if __name__ == "__main__":
    raise SystemExit(main())
