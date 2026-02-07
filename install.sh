#!/bin/bash
# B12 Memory System — Setup Installer
# Merges hook configuration into any Claude Code setup's settings.json
#
# Usage:
#   ./install.sh                    # Install to ~/.claude (default)
#   ./install.sh ~/.claude-x       # Install to specific setup
#   ./install.sh --all              # Install to all ~/.claude* setups
#
# What it does:
#   1. Copies hook scripts to ~/.claude/hooks/ (shared location)
#   2. Merges hook config into target setup's settings.json
#   3. Does NOT touch .claude.json (MCP config must be added separately)

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
HOOK_SOURCE="$SCRIPT_DIR/hooks"
HOOK_DEST="$HOME/.claude/hooks"

# Color output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

info()  { echo -e "${GREEN}[OK]${NC} $1"; }
warn()  { echo -e "${YELLOW}[!]${NC} $1"; }
error() { echo -e "${RED}[ERR]${NC} $1"; exit 1; }

# ─────────────────────────────────────────────
# Step 1: Copy hook scripts to shared location
# ─────────────────────────────────────────────
copy_hooks() {
  mkdir -p "$HOOK_DEST"
  local count=0
  for f in "$HOOK_SOURCE"/memory-*.sh "$HOOK_SOURCE"/memory-*.py; do
    [ -f "$f" ] || continue
    cp "$f" "$HOOK_DEST/"
    chmod +x "$HOOK_DEST/$(basename "$f")"
    count=$((count + 1))
  done
  info "Copied $count hook scripts to $HOOK_DEST"
}

# ─────────────────────────────────────────────
# Step 2: Merge hook config into settings.json
# ─────────────────────────────────────────────
install_to_setup() {
  local SETUP_DIR="$1"
  local SETTINGS_FILE="$SETUP_DIR/settings.json"

  if [ ! -d "$SETUP_DIR" ]; then
    warn "Setup directory not found: $SETUP_DIR (skipping)"
    return
  fi

  # Create settings.json if it doesn't exist
  if [ ! -f "$SETTINGS_FILE" ]; then
    echo '{}' > "$SETTINGS_FILE"
    info "Created $SETTINGS_FILE"
  fi

  # Merge hooks using python3 (preserves existing settings)
  python3 - "$SETTINGS_FILE" "$SCRIPT_DIR/config/settings-template.json" << 'PYEOF'
import sys, json

settings_path = sys.argv[1]
template_path = sys.argv[2]

with open(settings_path, 'r') as f:
    settings = json.load(f)

with open(template_path, 'r') as f:
    template = json.load(f)

# Merge hooks (replace entirely — template is source of truth)
if 'hooks' in template:
    settings['hooks'] = template['hooks']

with open(settings_path, 'w') as f:
    json.dump(settings, f, indent=2)
    f.write('\n')

PYEOF

  info "Hook config merged into $SETTINGS_FILE"
}

# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────

echo "B12 Memory System Installer"
echo "─────────────────────────────"

# Always copy hooks first
copy_hooks

if [ "$1" = "--all" ]; then
  # Install to all ~/.claude* directories that have settings.json or look like setups
  for dir in "$HOME"/.claude*; do
    [ -d "$dir" ] || continue
    # Skip non-setup directories (like .claude.json file shadow, plugins, etc.)
    base=$(basename "$dir")
    case "$base" in
      .claude|.claude-*) install_to_setup "$dir" ;;
    esac
  done
elif [ -n "$1" ]; then
  install_to_setup "$1"
else
  install_to_setup "$HOME/.claude"
fi

echo ""
echo "─────────────────────────────"
info "Installation complete!"
echo ""
echo "Next steps:"
echo "  1. If this is a new setup, add the memory MCP server to ~/.claude.json"
echo "     (see config/mcp-server-template.json)"
echo "  2. Restart Claude Code to pick up the new hooks"
