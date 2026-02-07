#!/bin/bash
# B12 Memory System - MCP Memory Service Upgrade Script
# Upgrades mcp-memory-service via pipx and re-applies known patches
#
# Usage: bash ~/.claude/hooks/memory-upgrade.sh
#
# Why this exists: pipx upgrade replaces all site-packages files,
# reverting any local patches. This script automates re-applying them.
#
# Patches applied:
#   1. response_limiter import fix (memory.py) — upstream bug
#   2. FTS5 hybrid search (sqlite_vec.py) — B12 patch for keyword + vector
#
# Note: FTS5 database schema (table + triggers) persists across upgrades.
# Only the Python code changes need re-applying.

set -e

echo "=== mcp-memory-service upgrade ==="

# Step 1: Upgrade
echo "[1/4] Running pipx upgrade..."
pipx upgrade mcp-memory-service

# Step 2: Find files to patch
MEMORY_PY=$(find "$HOME/.local/pipx/venvs/mcp-memory-service" -path "*/server/handlers/memory.py" -type f 2>/dev/null | head -1)
SQLITE_VEC_PY=$(find "$HOME/.local/pipx/venvs/mcp-memory-service" -path "*/storage/sqlite_vec.py" -type f 2>/dev/null | head -1)

if [ -z "$MEMORY_PY" ]; then
  echo "[ERROR] memory.py not found in venv"
  exit 1
fi

echo "[2/4] Patching response_limiter import in: $MEMORY_PY"

# The upstream bug: `from ...utils.response_limiter` (3 dots = grandparent)
# Correct:          `from ..utils.response_limiter`  (2 dots = parent)

if grep -q 'from \.\.\.utils\.response_limiter' "$MEMORY_PY"; then
  sed -i '' '/response_limiter/s/from \.\.\.utils/from ..utils/g' "$MEMORY_PY"
  echo "  Fixed: ...utils -> ..utils (response_limiter only)"
elif grep -q 'from \.\.utils\.response_limiter' "$MEMORY_PY"; then
  echo "  Already patched (..utils) - no change needed"
else
  echo "  [WARN] response_limiter import line not found - check manually"
fi

# Step 3: Check FTS5 hybrid search patch
echo "[3/4] Checking FTS5 hybrid search patch in: $SQLITE_VEC_PY"

if [ -z "$SQLITE_VEC_PY" ]; then
  echo "  [WARN] sqlite_vec.py not found - skipping FTS5 check"
else
  if grep -q '_init_fts5' "$SQLITE_VEC_PY"; then
    echo "  FTS5 hybrid search patch already applied"
  else
    echo "  [WARN] FTS5 hybrid search patch NOT found in sqlite_vec.py"
    echo "  The FTS5 database schema (table + triggers) is still intact,"
    echo "  but hybrid search scoring will revert to pure vector search."
    echo ""
    echo "  To re-apply the FTS5 hybrid patch, ask Claude to:"
    echo "    'Re-apply FTS5 hybrid search patch to sqlite_vec.py'"
    echo "  Or manually apply from B12 repo: ~/Desktop/B12/patches/"
  fi
fi

# Step 4: Clear Python bytecache
echo "[4/4] Clearing bytecache..."
find "$HOME/.local/pipx/venvs/mcp-memory-service" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
find "$HOME/.local/pipx/venvs/mcp-memory-service" -name "*.pyc" -delete 2>/dev/null || true

# Get version
NEW_VER=$(pipx list --json 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['venvs']['mcp-memory-service']['metadata']['main_package']['package_version'])" 2>/dev/null || echo "unknown")

echo ""
echo "=== Done ==="
echo "Version: $NEW_VER"
echo "IMPORTANT: Restart Claude Code for changes to take effect"
echo "           (MCP server caches modules in memory)"
