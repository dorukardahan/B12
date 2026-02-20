#!/bin/bash
# B12 Memory System — Setup Installer
#
# Usage:
#   ./install.sh                    # Standard install (hooks + config only)
#   ./install.sh --all              # Install to all ~/.claude* setups
#   ./install.sh --full             # Full setup: venv + deps + hooks + MCP config
#   ./install.sh --full --all       # Full setup for all setups
#
# What --full does (in addition to standard install):
#   1. Creates ~/.local/b12-venv if it doesn't exist
#   2. Installs Python dependencies (mcp, sentence-transformers, sqlite-vec)
#   3. Adds B12 MCP server config to ~/.claude.json (with correct absolute paths)
#
# Standard install:
#   1. Copies hook scripts to ~/.claude/hooks/ (shared location)
#   2. Copies support scripts to ~/.claude/hooks/scripts/
#   3. Merges hook config into target setup's settings.json
#   4. Creates required directories

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
HOOK_SOURCE="$SCRIPT_DIR/hooks"
SCRIPT_SOURCE="$SCRIPT_DIR/scripts"
HOOK_DEST="$HOME/.claude/hooks"
SCRIPT_DEST="$HOME/.claude/hooks/scripts"
VENV_PATH="$HOME/.local/b12-venv"
# Windows/Git Bash uses Scripts/python instead of bin/python3
if [ -f "$VENV_PATH/Scripts/python.exe" ]; then
  VENV_PYTHON="$VENV_PATH/Scripts/python.exe"
else
  VENV_PYTHON="$VENV_PATH/bin/python3"
fi

# Color output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

info()  { echo -e "${GREEN}[OK]${NC} $1"; }
warn()  { echo -e "${YELLOW}[!]${NC} $1"; }
error() { echo -e "${RED}[ERR]${NC} $1"; exit 1; }

# Parse flags
FULL_SETUP=false
INSTALL_ALL=false
TARGET_DIR=""
for arg in "$@"; do
  case "$arg" in
    --full) FULL_SETUP=true ;;
    --all)  INSTALL_ALL=true ;;
    *)      TARGET_DIR="$arg" ;;
  esac
done

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
  local EXCLUDE="patch_validate_input.py fix_empty_tags.py"
  for f in "$SCRIPT_SOURCE"/*.py; do
    [ -f "$f" ] || continue
    local base=$(basename "$f")
    case " $EXCLUDE " in *" $base "*) continue ;; esac
    cp "$f" "$SCRIPT_DEST/"
    count=$((count + 1))
  done
  # Make MCP server executable
  if [ -f "$SCRIPT_DEST/b12_mcp_server.py" ]; then
    chmod +x "$SCRIPT_DEST/b12_mcp_server.py"
  fi
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
  if ! python3 - "$SETTINGS_FILE" "$SCRIPT_DIR/config/settings-template.json" << 'PYEOF'
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
  then
    error "Failed to merge hook config into $SETTINGS_FILE (is it valid JSON?)"
  fi

  info "Hook config merged into $SETTINGS_FILE"
}

# ─────────────────────────────────────────────
# Full setup: create venv + install deps
# ─────────────────────────────────────────────
setup_venv() {
  if [ -x "$VENV_PYTHON" ]; then
    info "B12 venv already exists at $VENV_PATH"
  else
    echo "Creating B12 Python environment at $VENV_PATH..."
    python3 -m venv "$VENV_PATH" || error "Failed to create venv (is python3 installed?)"
    info "Created venv at $VENV_PATH"
  fi

  echo "Installing dependencies (mcp, sentence-transformers, sqlite-vec)..."
  "$VENV_PYTHON" -m pip install --quiet mcp sentence-transformers sqlite-vec || error "pip install failed"
  info "Dependencies installed"
}

# ─────────────────────────────────────────────
# Full setup: inject MCP config into ~/.claude.json
# ─────────────────────────────────────────────
inject_mcp_config() {
  local CLAUDE_JSON="$HOME/.claude.json"
  local SERVER_SCRIPT="$SCRIPT_DEST/b12_mcp_server.py"

  # Verify paths exist
  if [ ! -x "$VENV_PYTHON" ]; then
    warn "Venv Python not found at $VENV_PYTHON — skipping MCP config"
    return
  fi
  if [ ! -f "$SERVER_SCRIPT" ]; then
    warn "MCP server script not found at $SERVER_SCRIPT — skipping MCP config"
    return
  fi

  # Create ~/.claude.json if it doesn't exist
  if [ ! -f "$CLAUDE_JSON" ]; then
    echo '{}' > "$CLAUDE_JSON"
  fi

  # Inject B12 MCP server config using Python (preserves existing config)
  if ! python3 - "$CLAUDE_JSON" "$VENV_PYTHON" "$SERVER_SCRIPT" << 'PYEOF'
import sys, json

config_path = sys.argv[1]
venv_python = sys.argv[2]
server_script = sys.argv[3]

with open(config_path, 'r') as f:
    config = json.load(f)

if 'mcpServers' not in config:
    config['mcpServers'] = {}

config['mcpServers']['B12'] = {
    "command": venv_python,
    "args": [server_script],
    "env": {
        "MCP_EMBEDDING_MODEL": "paraphrase-multilingual-MiniLM-L12-v2",
        "MCP_MAX_RESPONSE_CHARS": "40000"
    }
}

with open(config_path, 'w') as f:
    json.dump(config, f, indent=2)
    f.write('\n')

PYEOF
  then
    error "Failed to update $CLAUDE_JSON (is it valid JSON?)"
  fi

  info "B12 MCP server added to $CLAUDE_JSON"
  echo "     command: $VENV_PYTHON"
  echo "     script:  $SERVER_SCRIPT"
}

# ─────────────────────────────────────────────
# Run DB migration (if database exists)
# ─────────────────────────────────────────────
run_migration() {
  local MIGRATE_SCRIPT="$SCRIPT_DIR/scripts/migrate_v10_13.py"
  if [ ! -f "$MIGRATE_SCRIPT" ]; then
    return
  fi

  # Check if database exists (macOS, Linux, or Windows paths)
  local DB_PATH=""
  if [ "$(uname)" = "Darwin" ]; then
    DB_PATH="$HOME/Library/Application Support/mcp-memory/sqlite_vec.db"
  elif [ -d "$HOME/AppData" ]; then
    DB_PATH="$HOME/AppData/Local/mcp-memory/sqlite_vec.db"
  else
    DB_PATH="$HOME/.local/share/mcp-memory/sqlite_vec.db"
  fi
  if [ ! -f "$DB_PATH" ]; then
    info "No existing database found (will be created on first use by MCP server)"
    return
  fi

  python3 "$MIGRATE_SCRIPT" --db "$DB_PATH"
  if [ $? -eq 0 ]; then
    info "Database migration check passed"
  else
    warn "Database migration had issues (see output above)"
  fi
}

# ─────────────────────────────────────────────
# Verify installation
# ─────────────────────────────────────────────
verify() {
  local errors=0

  # Venv and MCP config checks only apply to --full installs
  if $FULL_SETUP; then
    # Check venv
    if [ -x "$VENV_PYTHON" ]; then
      if "$VENV_PYTHON" -c "import mcp" 2>/dev/null; then
        info "Verify: mcp package OK"
      else
        warn "Verify: mcp package NOT found in b12-venv"
        errors=$((errors + 1))
      fi
    else
      warn "Verify: b12-venv not found at $VENV_PATH"
      errors=$((errors + 1))
    fi

    # Check MCP config in ~/.claude.json
    if [ -f "$HOME/.claude.json" ]; then
      if python3 -c "import json; c=json.load(open('$HOME/.claude.json')); assert 'B12' in c.get('mcpServers',{})" 2>/dev/null; then
        info "Verify: B12 MCP server configured in ~/.claude.json"
      else
        warn "Verify: B12 NOT found in ~/.claude.json mcpServers"
        errors=$((errors + 1))
      fi
    else
      warn "Verify: ~/.claude.json not found"
      errors=$((errors + 1))
    fi
  fi

  # Always check: MCP server script deployed
  if [ -f "$SCRIPT_DEST/b12_mcp_server.py" ]; then
    info "Verify: MCP server script deployed"
  else
    warn "Verify: MCP server script NOT found"
    errors=$((errors + 1))
  fi

  # Always check: hooks deployed
  local hook_count=0
  for f in "$HOOK_DEST"/memory-*.sh; do
    [ -f "$f" ] && hook_count=$((hook_count + 1))
  done
  if [ "$hook_count" -gt 5 ]; then
    info "Verify: $hook_count hook scripts deployed"
  else
    warn "Verify: Only $hook_count hooks found (expected 7+)"
    errors=$((errors + 1))
  fi

  return $errors
}

# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────

echo "B12 Memory System Installer (v10.0 — custom MCP server)"
echo "─────────────────────────────────"

# Full setup: create venv first
if $FULL_SETUP; then
  setup_venv
  echo ""
fi

# Always create dirs and copy files
create_dirs
copy_hooks
copy_scripts

# Verify MCP package
if [ -x "$VENV_PYTHON" ]; then
  if "$VENV_PYTHON" -c "import mcp" 2>/dev/null; then
    info "MCP Python package available (via b12-venv)"
  else
    warn "MCP Python package not found in b12-venv"
    warn "Run: $VENV_PYTHON -m pip install mcp sentence-transformers sqlite-vec"
  fi
else
  if ! $FULL_SETUP; then
    warn "B12 venv not found. Run with --full for automatic setup, or manually:"
    echo "       python3 -m venv $VENV_PATH"
    echo "       $VENV_PYTHON -m pip install mcp sentence-transformers sqlite-vec"
  fi
fi

# Install hooks to setups
if $INSTALL_ALL; then
  for dir in "$HOME"/.claude*; do
    [ -d "$dir" ] || continue
    base=$(basename "$dir")
    case "$base" in
      .claude|.claude-*) install_to_setup "$dir" ;;
    esac
  done
elif [ -n "$TARGET_DIR" ]; then
  install_to_setup "$TARGET_DIR"
else
  install_to_setup "$HOME/.claude"
fi

echo ""

# Full setup: inject MCP config
if $FULL_SETUP; then
  inject_mcp_config
  echo ""
fi

# Run migration
run_migration

echo ""
echo "─────────────────────────────────"

# Run verification
verify
VERIFY_RESULT=$?

echo ""
echo "─────────────────────────────────"
if [ $VERIFY_RESULT -eq 0 ]; then
  info "Installation complete! Restart Claude Code to activate B12."
else
  warn "Installation complete with $VERIFY_RESULT warning(s). See above."
fi

if ! $FULL_SETUP; then
  echo ""
  echo "Tip: Run './install.sh --full' for automatic venv + MCP config setup."
fi

echo ""
echo "Next: Restart Claude Code, then run /mcp to verify B12 is connected."
