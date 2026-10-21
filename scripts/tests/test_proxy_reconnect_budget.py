"""The proxy's default reconnect budget must outlast one throttled respawn.

After a daemon restart whose first spawn exits immediately, launchd waits the
plist's ThrottleInterval before spawning again, and the new daemon needs a cold
start. With the old 30-second default (equal to the throttle) every open
session gave up just before the daemon came back and showed B12 disconnected.
"""
from __future__ import annotations

import plistlib
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
COLD_START_ALLOWANCE_S = 10


def test_default_budget_outlasts_launchd_throttle_plus_cold_start():
    src = (REPO_ROOT / "scripts" / "b12_mcp_server.py").read_text()
    default = float(re.search(r'"B12_MCP_RECONNECT_BUDGET", "([0-9.]+)"', src).group(1))
    fallback = float(re.search(r"_RECONNECT_BUDGET_S = ([0-9.]+)\n", src).group(1))
    template = (REPO_ROOT / "config" / "com.b12.mcp.daemon.plist").read_text()
    # The template carries placeholders, but it is still a valid plist document.
    throttle = plistlib.loads(template.encode())["ThrottleInterval"]
    assert default == fallback
    assert default >= throttle + COLD_START_ALLOWANCE_S, (default, throttle)
