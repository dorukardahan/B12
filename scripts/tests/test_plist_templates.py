"""Strict XML validation for launchd plist templates.

Apple's launchd tooling may accept malformed XML that Python's plistlib rejects.
The health checker uses plistlib, so every shipped template must satisfy the
strict parser as well as launchd.
"""

from __future__ import annotations

import plistlib
import sys
from pathlib import Path

import pytest


_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "scripts"))

import b12_health


_PLIST_TEMPLATES = sorted((_ROOT / "config").glob("*.plist"))


def test_plist_templates_are_discovered() -> None:
    assert _PLIST_TEMPLATES, "no plist templates discovered"


@pytest.mark.parametrize("template", _PLIST_TEMPLATES, ids=lambda path: path.name)
def test_plist_template_is_strictly_parseable(template: Path) -> None:
    try:
        with template.open("rb") as handle:
            plistlib.load(handle)
    except Exception as exc:
        pytest.fail(f"{template.relative_to(_ROOT)} is not valid plist XML: {exc}")


def test_health_check_accepts_deployed_daemon_plist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    launch_agents = tmp_path / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True)
    template = (_ROOT / "config" / "com.b12.mcp.daemon.plist").read_text(encoding="utf-8")
    deployed = (
        template.replace("B12_HOME_PYTHON", "/path/to/test-home/.local/b12-venv/bin/python3")
        .replace("B12_HOME_DAEMON", "/path/to/test-home/.B12/hooks/scripts/b12_mcp_daemon.py")
        .replace("B12_HOME_DATA_DIR", "/path/to/test-home/.B12")
    )
    (launch_agents / "com.b12.mcp.daemon.plist").write_text(deployed, encoding="utf-8")

    monkeypatch.setattr(b12_health.sys, "platform", "darwin")
    monkeypatch.setattr(b12_health, "_HOME", tmp_path)

    result = b12_health.check_launchd_plists()

    assert result.status == b12_health.Status.OK
    assert result.label == "Launchd plists configured (1 plists)"
