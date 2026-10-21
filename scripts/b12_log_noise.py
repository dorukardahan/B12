"""Keep known MCP SDK protocol noise out of the B12 server/daemon stderr.

Claude Code speaks MCP protocol 2026-07-28 and opens every connection with a
`server/discover` request; the pinned mcp<2 SDK does not know that method and
logs a ~30-error pydantic dump (about 130 lines under Rich) before the client
falls back to `initialize`. A `notifications/roots/list_changed` that arrives
before initialization is logged the same way. The SDK also logs one INFO line
per request. In daemon mode stderr is a launchd log file with no rotation, so
this noise grew it by megabytes per day.

Both warnings go through the root logger (`logging.warning` in
mcp/shared/session.py), so a root-logger filter sees them regardless of which
handlers are installed later. The first occurrence of each is kept as a single
line so the evidence stays visible; repeats are dropped. Every other warning,
including validation failures for real methods, passes through unchanged.
"""
from __future__ import annotations

import logging

_KNOWN_PROBES = (
    ("Failed to validate request", "'server/discover'", "server/discover request"),
    ("Failed to validate notification", "notifications/roots/list_changed", "pre-initialization roots/list_changed notification"),
)

_SDK_REQUEST_LOGGER = "mcp.server.lowlevel.server"


class KnownProbeWarningFilter(logging.Filter):
    """Collapse warnings for newer-protocol probes the installed SDK predates."""

    def __init__(self) -> None:
        super().__init__()
        self._seen: set[str] = set()

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno != logging.WARNING:
            return True
        message = record.getMessage()
        for prefix, marker, label in _KNOWN_PROBES:
            if message.startswith(prefix) and marker in message:
                if label in self._seen:
                    return False
                self._seen.add(label)
                record.msg = (
                    f"MCP SDK rejected a {label} sent by a newer-protocol client "
                    "(expected with mcp<2); further occurrences are suppressed."
                )
                record.args = ()
                return True
        return True


def quiet_mcp_protocol_noise() -> KnownProbeWarningFilter:
    """Install the probe filter on the root logger (idempotent) and drop SDK per-request INFO lines."""
    root = logging.getLogger()
    for existing in root.filters:
        if isinstance(existing, KnownProbeWarningFilter):
            probe_filter = existing
            break
    else:
        probe_filter = KnownProbeWarningFilter()
        root.addFilter(probe_filter)
    logging.getLogger(_SDK_REQUEST_LOGGER).setLevel(logging.WARNING)
    return probe_filter
