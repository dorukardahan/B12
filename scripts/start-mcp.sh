#!/bin/bash
# B12 MCP Server Bootstrap
# Detects the B12 Python venv and launches the MCP server.
# Used by the plugin's .mcp.json — not needed for manual installs.

VENV="$HOME/.local/b12-venv"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# P8: give a just-launched launchd daemon a brief window to bind its socket
# before b12_mcp_server.py decides proxy-vs-legacy. Without this, tabs opened
# during the login window race ahead of the daemon and fall into the slow
# legacy in-process path (direct sqlite connect + _ensure_schema). Bounded to
# ~2s (4 × 0.5s) so a genuinely-absent daemon still falls through to legacy
# fast. Skipped entirely when the caller forces stdio/legacy.
if [ -z "$B12_MCP_FORCE_STDIO" ]; then
  _b12_sock="${B12_MCP_DAEMON_SOCK:-/tmp/b12-mcp-$(id -u).sock}"
  _b12_i=0
  while [ "$_b12_i" -lt 4 ] && [ ! -S "$_b12_sock" ]; do
    sleep 0.5
    _b12_i=$((_b12_i + 1))
  done
fi

# Windows/Git Bash compatibility
if [ -f "$VENV/Scripts/python.exe" ]; then
  exec "$VENV/Scripts/python.exe" "$SCRIPT_DIR/b12_mcp_server.py"
elif [ -f "$VENV/bin/python3" ]; then
  exec "$VENV/bin/python3" "$SCRIPT_DIR/b12_mcp_server.py"
else
  echo "Error: B12 venv not found at $VENV" >&2
  echo "Run: ./install.sh --full  (from the B12 repo directory)" >&2
  exit 1
fi
