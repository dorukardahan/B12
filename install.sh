#!/bin/bash
# B12 Memory System — Setup Installer
# Merges hook configuration into any Claude Code setup's settings.json
#
# Usage:
#   ./install.sh                    # Install to ~/.claude (default)
#   ./install.sh ~/.claude-work     # Install to specific setup
#   ./install.sh --all              # Install to all ~/.claude* setups
#
# What it does:
#   1. Copies hook scripts to ~/.claude/hooks/ (shared location)
#   2. Copies support scripts to ~/.claude/hooks/scripts/ (for write-time merge etc.)
#   3. Merges hook config into target setup's settings.json
#   4. Creates required directories
#   5. Does NOT touch .claude.json (MCP config must be added separately)

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
HOOK_SOURCE="$SCRIPT_DIR/hooks"
SCRIPT_SOURCE="$SCRIPT_DIR/scripts"
HOOK_DEST="$HOME/.claude/hooks"
SCRIPT_DEST="$HOME/.claude/hooks/scripts"

# Color output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

info()  { echo -e "${GREEN}[OK]${NC} $1"; }
warn()  { echo -e "${YELLOW}[!]${NC} $1"; }
error() { echo -e "${RED}[ERR]${NC} $1"; exit 1; }

# ─────────────────────────────────────────────
# Step 1: Create required directories
# ─────────────────────────────────────────────
create_dirs() {
  mkdir -p "$HOOK_DEST"
  mkdir -p "$SCRIPT_DEST"
  mkdir -p "$HOME/.claude/memory-staging"
  mkdir -p "$HOME/.claude/memory-logs"
  mkdir -p "$HOME/.claude/memory-summaries"
  info "Created required directories"
}

# ─────────────────────────────────────────────
# Step 2: Copy hook scripts to shared location
# ─────────────────────────────────────────────
copy_hooks() {
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
# Step 3: Copy support scripts
# ─────────────────────────────────────────────
copy_scripts() {
  local count=0
  for f in "$SCRIPT_SOURCE"/*.py; do
    [ -f "$f" ] || continue
    cp "$f" "$SCRIPT_DEST/"
    count=$((count + 1))
  done
  if [ "$count" -gt 0 ]; then
    info "Copied $count support scripts to $SCRIPT_DEST"
  fi
}

# ─────────────────────────────────────────────
# Step 4: Merge hook config into settings.json
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

echo "B12 Memory System Installer (v8)"
echo "─────────────────────────────────"

# Always create dirs and copy files first
create_dirs
copy_hooks
copy_scripts

if [ "$1" = "--all" ]; then
  # Install to all ~/.claude* directories that look like setups
  for dir in "$HOME"/.claude*; do
    [ -d "$dir" ] || continue
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

# ─────────────────────────────────────────────
# Step 5: Run DB migration (if database exists)
# ─────────────────────────────────────────────
run_migration() {
  local MIGRATE_SCRIPT="$SCRIPT_DIR/scripts/migrate_v10_13.py"
  if [ ! -f "$MIGRATE_SCRIPT" ]; then
    return
  fi

  # Check if database exists (macOS or Linux default paths)
  local DB_PATH="$HOME/Library/Application Support/mcp-memory/sqlite_vec.db"
  if [ ! -f "$DB_PATH" ]; then
    DB_PATH="$HOME/.local/share/mcp-memory/sqlite_vec.db"
  fi
  if [ ! -f "$DB_PATH" ]; then
    warn "Memory database not found (will be created on first use)"
    return
  fi

  python3 "$MIGRATE_SCRIPT" --db "$DB_PATH"
  if [ $? -eq 0 ]; then
    info "Database migration check passed"
  else
    warn "Database migration had issues (see output above)"
  fi
}

run_migration

echo "─────────────────────────────────"
info "Installation complete!"
echo ""
echo "Next steps:"
echo "  1. Install mcp-memory-service: pipx install mcp-memory-service"
echo "  2. Add the memory MCP server to ~/.claude.json"
echo "     (see config/mcp-server-template.json)"
echo "  3. Restart Claude Code to pick up the new hooks"
echo ""
echo "Optional:"
echo "  - Set up launchd agents for automated tasks (see config/*.plist)"
echo "  - Create a user profile (see templates/user-profile.md)"
