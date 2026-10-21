"""Guards for collapsing known MCP protocol-probe warnings in server stderr."""
from __future__ import annotations

import io
import logging
import re
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "scripts"))

import b12_log_noise  # noqa: E402

DISCOVER = (
    "Failed to validate request: 31 validation errors for ClientRequest\n"
    "PingRequest.method\n  Input should be 'ping' [type=literal_error, input_value='server/discover', input_type=str]"
)
ROOTS = (
    "Failed to validate notification: Received notification before initialization was complete. "
    "Message was: method='notifications/roots/list_changed' params={} jsonrpc='2.0'"
)


@pytest.fixture
def root_capture():
    root = logging.getLogger()
    sdk = logging.getLogger(b12_log_noise._SDK_REQUEST_LOGGER)
    saved_filters, saved_level, saved_sdk_level = list(root.filters), root.level, sdk.level
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    for f in list(root.filters):
        if isinstance(f, b12_log_noise.KnownProbeWarningFilter):
            root.removeFilter(f)
    yield stream
    root.removeHandler(handler)
    root.filters[:] = saved_filters
    root.setLevel(saved_level)
    sdk.setLevel(saved_sdk_level)


def test_known_probe_warnings_collapse_to_one_line_each(root_capture):
    b12_log_noise.quiet_mcp_protocol_noise()
    for _ in range(3):
        logging.warning(DISCOVER)
        logging.warning(ROOTS)
    lines = root_capture.getvalue().splitlines()
    assert len(lines) == 2, lines
    assert "server/discover request" in lines[0] and "suppressed" in lines[0]
    assert "roots/list_changed" in lines[1]
    assert not any("validation errors" in line for line in lines)


def test_real_failures_stay_visible(root_capture):
    b12_log_noise.quiet_mcp_protocol_noise()
    tools_call = "Failed to validate request: 2 validation errors, input_value='tools/call'"
    other_prefix = "Unexpected client probe 'server/discover' during shutdown"
    logging.warning(tools_call)
    logging.warning(tools_call)
    logging.warning(other_prefix)
    logging.error("boom %s", DISCOVER)
    out = root_capture.getvalue()
    assert out.count("tools/call") == 2
    assert other_prefix in out
    assert "ERROR boom" in out and "validation errors for ClientRequest" in out


def _sdk_request_dump(payload: dict) -> str:
    types = pytest.importorskip("mcp.types")
    try:
        types.ClientRequest.model_validate(payload)
    except Exception as exc:  # pydantic ValidationError, as the SDK logs it
        return f"Failed to validate request: {exc}"
    raise AssertionError("payload unexpectedly validated")


def test_real_sdk_dumps_probe_collapses_but_payload_lookalike_stays(root_capture):
    probe = _sdk_request_dump(
        {"method": "server/discover", "params": {"_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}}}
    )
    lookalike = _sdk_request_dump({"method": "tools/call", "params": {"name": "x", "arguments": "server/discover"}})
    assert "server/discover" in lookalike  # the payload text is echoed in the dump
    b12_log_noise.quiet_mcp_protocol_noise()
    logging.warning(probe)
    logging.warning(probe)
    logging.warning(lookalike)
    out = root_capture.getvalue()
    assert out.count("server/discover request") == 1
    assert "'arguments': 'server/discover'" in out, "a real tools/call failure must stay visible"


def test_notification_match_requires_the_method(root_capture):
    b12_log_noise.quiet_mcp_protocol_noise()
    other = (
        "Failed to validate notification: Received notification before initialization was complete. "
        "Message was: method='notifications/progress' params={'note': 'notifications/roots/list_changed'} jsonrpc='2.0'"
    )
    logging.warning(ROOTS)
    logging.warning(other)
    assert "notifications/progress" in root_capture.getvalue()


def test_post_initialization_roots_failure_stays_visible(root_capture):
    b12_log_noise.quiet_mcp_protocol_noise()
    malformed = (
        "Failed to validate notification: 1 validation error for RootsListChangedNotification\n"
        "params\n  Input should be a valid dictionary [type=dict_type, input_value='bad', input_type=str]. "
        "Message was: method='notifications/roots/list_changed' params='bad' jsonrpc='2.0'"
    )
    logging.warning(ROOTS)
    logging.warning(malformed)
    logging.warning(malformed)
    out = root_capture.getvalue()
    assert out.count("RootsListChangedNotification") == 2, "post-init failures must not be collapsed"


def test_sdk_request_info_lines_are_dropped(root_capture):
    b12_log_noise.quiet_mcp_protocol_noise()
    logging.getLogger(b12_log_noise._SDK_REQUEST_LOGGER).info("Processing request of type %s", "ListToolsRequest")
    logging.getLogger("b12_mcp_server.memory_delete").info("audit line")
    out = root_capture.getvalue()
    assert "Processing request" not in out
    assert "audit line" in out


def test_install_is_idempotent(root_capture):
    first = b12_log_noise.quiet_mcp_protocol_noise()
    second = b12_log_noise.quiet_mcp_protocol_noise()
    assert first is second
    assert sum(isinstance(f, b12_log_noise.KnownProbeWarningFilter) for f in logging.getLogger().filters) == 1


def test_module_is_packaged():
    import tomllib

    modules = tomllib.loads((_ROOT / "pyproject.toml").read_text())["tool"]["setuptools"]["py-modules"]
    assert "b12_log_noise" in modules, "b12_mcp_server imports b12_log_noise; it must ship in the wheel"


def test_server_installs_filter_after_fastmcp_logging_setup():
    src = (_ROOT / "scripts" / "b12_mcp_server.py").read_text()
    m = re.search(r'^server = FastMCP\("B12".*?$(.*?)^# ── Helpers', src, re.DOTALL | re.MULTILINE)
    assert m, "server construction block not found"
    assert "quiet_mcp_protocol_noise()" in m.group(1), "filter must be installed right after FastMCP configures logging"
