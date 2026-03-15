#!/bin/bash
# B12 MCP Server Bootstrap
# Detects the B12 Python venv and launches the MCP server.
# Used by the plugin's .mcp.json — not needed for manual installs.

VENV="$HOME/.local/b12-venv"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

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
