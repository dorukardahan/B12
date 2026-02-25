#!/bin/bash
# B12 Memory System - MCP Memory Service Upgrade Script
# Upgrades mcp-memory-service via pipx and runs DB migration
#
# Usage: bash ~/.B12/hooks/memory-upgrade.sh
#
# What it does:
#   1. pipx upgrade mcp-memory-service
#   2. Run DB migration (ensure memory_content_fts exists)
#   3. Re-apply validate_input patch (upgrade wipes it)
#   4. Clear Python bytecache
#
# Note: B12 hooks are fully independent of server-side code (v9.0+).
# No server patches needed — hooks do their own hybrid search directly.

set -e

# ── DEPRECATED ─────────────────────────────────────────────────────
# B12 now uses b12_mcp_server.py (custom MCP server) instead of mcp-memory-service.
# This script's pipx upgrade flow is no longer applicable.
# To upgrade the B12 venv:
#   ~/.local/b12-venv/bin/pip install --upgrade mcp sentence-transformers
echo "[DEPRECATED] memory-upgrade.sh is no longer needed."
echo "B12 now uses b12_mcp_server.py (custom MCP server) instead of mcp-memory-service."
echo "To upgrade the B12 venv: ~/.local/b12-venv/bin/pip install --upgrade mcp sentence-transformers"
exit 0
