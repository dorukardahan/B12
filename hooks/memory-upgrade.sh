#!/bin/bash
# B12 Memory System - MCP Memory Service Upgrade Script
# Upgrades mcp-memory-service via pipx and re-applies known patches
#
# Usage: bash ~/.claude/hooks/memory-upgrade.sh
#
# Why this exists: pipx upgrade replaces all site-packages files,
# reverting any local patches. This script automates re-applying them.

set -e

echo "=== mcp-memory-service upgrade ==="

# Step 1: Upgrade
echo "[1/3] Running pipx upgrade..."
pipx upgrade mcp-memory-service

# Step 2: Find memory.py and apply response_limiter import fix
MEMORY_PY=$(find "$HOME/.local/pipx/venvs/mcp-memory-service" -path "*/server/handlers/memory.py" -type f 2>/dev/null | head -1)

if [ -z "$MEMORY_PY" ]; then
  echo "[ERROR] memory.py not found in venv"
  exit 1
fi

echo "[2/3] Patching response_limiter import in: $MEMORY_PY"

# The upstream bug: `from ...utils.response_limiter` (3 dots = grandparent)
# Correct:          `from ..utils.response_limiter`  (2 dots = parent)
# Location: inside the `if max_response_chars:` block

if grep -q 'from \.\.\.utils\.response_limiter' "$MEMORY_PY"; then
  sed -i '' '/response_limiter/s/from \.\.\.utils/from ..utils/g' "$MEMORY_PY"
  echo "  Fixed: ...utils -> ..utils (response_limiter only)"
elif grep -q 'from \.\.utils\.response_limiter' "$MEMORY_PY"; then
  echo "  Already patched (..utils) - no change needed"
else
  echo "  [WARN] response_limiter import line not found - check manually"
fi

# Step 3: Clear Python bytecache
echo "[3/3] Clearing bytecache..."
find "$HOME/.local/pipx/venvs/mcp-memory-service" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
find "$HOME/.local/pipx/venvs/mcp-memory-service" -name "*.pyc" -delete 2>/dev/null || true

# Get version
NEW_VER=$(pipx list --json 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['venvs']['mcp-memory-service']['metadata']['main_package']['package_version'])" 2>/dev/null || echo "unknown")

echo ""
echo "=== Done ==="
echo "Version: $NEW_VER"
echo "IMPORTANT: Restart Claude Code for changes to take effect"
echo "           (MCP server caches modules in memory)"
