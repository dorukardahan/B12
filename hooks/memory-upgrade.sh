#!/bin/bash
# B12 Memory System - MCP Memory Service Upgrade Script
# Upgrades mcp-memory-service via pipx and runs DB migration
#
# Usage: bash ~/.claude/hooks/memory-upgrade.sh
#
# What it does:
#   1. pipx upgrade mcp-memory-service
#   2. Run DB migration (ensure memory_content_fts exists)
#   3. Clear Python bytecache
#
# Note: B12 hooks are fully independent of server-side code (v9.0+).
# No server patches needed — hooks do their own hybrid search directly.

set -e

echo "=== mcp-memory-service upgrade ==="

# Step 1: Upgrade
echo "[1/3] Running pipx upgrade..."
pipx upgrade mcp-memory-service

# Step 2: Run DB migration (ensure memory_content_fts exists)
echo "[2/3] Running DB migration..."
MIGRATE_SCRIPT="${B12_REPO}/scripts/migrate_v10_13.py"
if [ -z "$B12_REPO" ]; then
  # Try common locations
  for candidate in "$HOME/Desktop/B12" "$HOME/B12" "$HOME/projects/B12"; do
    if [ -f "$candidate/scripts/migrate_v10_13.py" ]; then
      MIGRATE_SCRIPT="$candidate/scripts/migrate_v10_13.py"
      break
    fi
  done
fi

if [ -f "$MIGRATE_SCRIPT" ]; then
  python3 "$MIGRATE_SCRIPT"
else
  echo "  [WARN] Migration script not found. Set B12_REPO env var."
  echo "  Example: export B12_REPO=\$HOME/Desktop/B12"
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
