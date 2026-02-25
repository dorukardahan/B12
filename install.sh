#!/bin/bash
# B12 Memory System — Setup Installer
#
# Usage:
#   ./install.sh                    # Standard install (hooks + config only)
#   ./install.sh --all              # Install to all ~/.claude* setups
#   ./install.sh --full             # Full setup: venv + deps + hooks + MCP config
#   ./install.sh --full --all       # Full setup for all setups
#   ./install.sh --codex            # Install B12 MCP server to Codex CLI
#   ./install.sh --full --codex     # Full setup + Codex CLI support
#
# What --full does (in addition to standard install):
#   1. Creates ~/.local/b12-venv if it doesn't exist
#   2. Installs Python dependencies (mcp, sentence-transformers, sqlite-vec)
#   3. Adds B12 MCP server config to ~/.claude.json (with correct absolute paths)
#
# What --codex does:
#   1. Injects B12 MCP server into ~/.codex/config.toml
#   2. Appends B12 memory instructions to ~/.codex/AGENTS.md
#   3. Configures notify hook for session-end processing
#   4. Installs B12 skill to ~/.codex/skills/b12/
#   (Requires venv — use with --full on first run)
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
INSTALL_CODEX=false
TARGET_DIR=""
for arg in "$@"; do
  case "$arg" in
    --full)  FULL_SETUP=true ;;
    --all)   INSTALL_ALL=true ;;
    --codex) INSTALL_CODEX=true ;;
    *)       TARGET_DIR="$arg" ;;
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
  # Copy Codex notify hook if present
  if [ -f "$HOOK_SOURCE/b12-codex-notify.sh" ]; then
    cp "$HOOK_SOURCE/b12-codex-notify.sh" "$HOOK_DEST/"
    chmod +x "$HOOK_DEST/b12-codex-notify.sh"
    count=$((count + 1))
  fi
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
# Codex CLI: inject MCP config into config.toml
# ─────────────────────────────────────────────
inject_codex_mcp_config() {
  local CODEX_DIR="$HOME/.codex"
  local CONFIG_TOML="$CODEX_DIR/config.toml"
  local SERVER_SCRIPT="$SCRIPT_DEST/b12_mcp_server.py"

  if [ ! -d "$CODEX_DIR" ]; then
    warn "Codex directory not found at $CODEX_DIR — is Codex CLI installed?"
    return 1
  fi

  # Verify paths exist
  if [ ! -x "$VENV_PYTHON" ]; then
    warn "Venv Python not found at $VENV_PYTHON"
    warn "Run with --full to create the venv first: ./install.sh --full --codex"
    return 1
  fi
  if [ ! -f "$SERVER_SCRIPT" ]; then
    warn "MCP server script not found at $SERVER_SCRIPT — run standard install first"
    return 1
  fi

  # Create config.toml if it doesn't exist
  if [ ! -f "$CONFIG_TOML" ]; then
    touch "$CONFIG_TOML"
    info "Created $CONFIG_TOML"
  fi

  # Always use Python for TOML manipulation (handles both add and update)
  if ! python3 - "$CONFIG_TOML" "$VENV_PYTHON" "$SERVER_SCRIPT" << 'PYEOF'
import sys

config_path = sys.argv[1]
venv_python = sys.argv[2]
server_script = sys.argv[3]

with open(config_path, 'r') as f:
    lines = f.readlines()

# Remove all B12-related sections using line-by-line section tracking.
# A TOML section header is a line starting with [ (not [[).
# We skip lines belonging to [mcp_servers.B12] or [mcp_servers.B12.*].
filtered = []
in_b12_section = False
for line in lines:
    stripped = line.strip()
    # Detect TOML section headers (but not array-of-tables [[...]])
    if stripped.startswith('[') and not stripped.startswith('[['):
        # Extract table name: everything between first [ and last ]
        table_name = stripped.split(']')[0].lstrip('[').strip()
        if table_name == 'mcp_servers.B12' or table_name.startswith('mcp_servers.B12.'):
            in_b12_section = True
            continue
        else:
            in_b12_section = False
    if in_b12_section:
        continue
    filtered.append(line)

# Remove trailing blank lines, then append B12 block
content = ''.join(filtered).rstrip() + '\n'
content += f'''
[mcp_servers.B12]
command = "{venv_python}"
args = ["{server_script}"]
enabled = true
startup_timeout_sec = 30

[mcp_servers.B12.env]
MCP_EMBEDDING_MODEL = "paraphrase-multilingual-MiniLM-L12-v2"
MCP_MAX_RESPONSE_CHARS = "40000"
'''

with open(config_path, 'w') as f:
    f.write(content)

PYEOF
  then
    error "Failed to update B12 config in $CONFIG_TOML"
  fi

  # Report add vs update
  if grep -q '^\[mcp_servers\.B12\]' "$CONFIG_TOML" 2>/dev/null; then
    info "B12 MCP server configured in $CONFIG_TOML"
  else
    error "B12 config injection failed"
  fi

  echo "     command: $VENV_PYTHON"
  echo "     script:  $SERVER_SCRIPT"

  # Inject notify hook for session-end processing
  local NOTIFY_HOOK="$HOOK_DEST/b12-codex-notify.sh"
  if [ -f "$NOTIFY_HOOK" ]; then
    if ! python3 - "$CONFIG_TOML" "$NOTIFY_HOOK" << 'PYEOF'
import sys

config_path = sys.argv[1]
notify_hook = sys.argv[2]

with open(config_path, 'r') as f:
    lines = f.readlines()

# Check if notify line already exists
has_notify = False
for i, line in enumerate(lines):
    stripped = line.strip()
    if stripped.startswith('notify'):
        has_notify = True
        # Update existing notify line to include B12 hook
        if notify_hook not in stripped:
            lines[i] = f'notify = ["bash", "-lc", "{notify_hook}"]\n'
        break

if not has_notify:
    # Insert notify at top of file (root-level config, before any sections)
    insert_at = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith('['):
            insert_at = i
            break
    else:
        insert_at = len(lines)
    lines.insert(insert_at, f'notify = ["bash", "-lc", "{notify_hook}"]\n')

with open(config_path, 'w') as f:
    f.writelines(lines)

PYEOF
    then
      warn "Failed to inject notify hook into $CONFIG_TOML"
    else
      info "Notify hook configured in $CONFIG_TOML"
    fi
  fi
}

# ─────────────────────────────────────────────
# Codex CLI: append B12 instructions to AGENTS.md
# ─────────────────────────────────────────────
inject_codex_agents() {
  local CODEX_DIR="$HOME/.codex"
  local AGENTS_MD="$CODEX_DIR/AGENTS.md"
  local TEMPLATE="$SCRIPT_DIR/config/codex-agents-template.md"

  if [ ! -d "$CODEX_DIR" ]; then
    warn "Codex directory not found at $CODEX_DIR"
    return 1
  fi

  if [ ! -f "$TEMPLATE" ]; then
    warn "Codex AGENTS.md template not found at $TEMPLATE"
    return 1
  fi

  # Create AGENTS.md if it doesn't exist
  if [ ! -f "$AGENTS_MD" ]; then
    touch "$AGENTS_MD"
  fi

  # Check if B12 section already exists
  if grep -q '<!-- B12-MEMORY-START -->' "$AGENTS_MD" 2>/dev/null; then
    # Replace existing B12 section
    if ! python3 - "$AGENTS_MD" "$TEMPLATE" << 'PYEOF'
import sys, re

agents_path = sys.argv[1]
template_path = sys.argv[2]

with open(agents_path, 'r') as f:
    content = f.read()

with open(template_path, 'r') as f:
    template = f.read()

# Replace between markers
b12_section = f'\n<!-- B12-MEMORY-START -->\n{template}\n<!-- B12-MEMORY-END -->\n'
content = re.sub(
    r'<!-- B12-MEMORY-START -->.*?<!-- B12-MEMORY-END -->',
    b12_section.strip(),
    content,
    flags=re.DOTALL
)

with open(agents_path, 'w') as f:
    f.write(content)

PYEOF
    then
      error "Failed to update B12 section in $AGENTS_MD"
    fi
    info "Updated B12 section in $AGENTS_MD"
  else
    # Append B12 section with markers
    {
      echo ""
      echo "<!-- B12-MEMORY-START -->"
      cat "$TEMPLATE"
      echo ""
      echo "<!-- B12-MEMORY-END -->"
    } >> "$AGENTS_MD"
    info "Added B12 memory instructions to $AGENTS_MD"
  fi
}

# ─────────────────────────────────────────────
# Codex CLI: install B12 skill
# ─────────────────────────────────────────────
install_codex_skill() {
  local CODEX_DIR="$HOME/.codex"
  local SKILL_SRC="$SCRIPT_DIR/skills/b12"
  local SKILL_DEST="$CODEX_DIR/skills/b12"

  if [ ! -d "$CODEX_DIR" ]; then
    return
  fi

  if [ ! -f "$SKILL_SRC/SKILL.md" ]; then
    warn "B12 skill template not found at $SKILL_SRC/SKILL.md"
    return
  fi

  mkdir -p "$SKILL_DEST"
  cp "$SKILL_SRC/SKILL.md" "$SKILL_DEST/SKILL.md"
  info "B12 skill installed to $SKILL_DEST"
}

# ─────────────────────────────────────────────
# Codex CLI: verify installation
# ─────────────────────────────────────────────
verify_codex() {
  local errors=0
  local CONFIG_TOML="$HOME/.codex/config.toml"
  local AGENTS_MD="$HOME/.codex/AGENTS.md"

  # Check config.toml has B12
  if grep -q '^\[mcp_servers\.B12\]' "$CONFIG_TOML" 2>/dev/null; then
    info "Verify: B12 MCP server configured in $CONFIG_TOML"
  else
    warn "Verify: B12 NOT found in $CONFIG_TOML"
    errors=$((errors + 1))
  fi

  # Check AGENTS.md has B12
  if grep -q 'B12-MEMORY-START' "$AGENTS_MD" 2>/dev/null; then
    info "Verify: B12 instructions present in $AGENTS_MD"
  else
    warn "Verify: B12 instructions NOT found in $AGENTS_MD"
    errors=$((errors + 1))
  fi

  # Check venv accessible
  if [ -x "$VENV_PYTHON" ]; then
    info "Verify: B12 venv accessible at $VENV_PATH"
  else
    warn "Verify: B12 venv NOT found (Codex MCP server will fail)"
    errors=$((errors + 1))
  fi

  # Check notify hook configured
  if grep -q 'notify' "$CONFIG_TOML" 2>/dev/null; then
    info "Verify: Notify hook configured in $CONFIG_TOML"
  else
    warn "Verify: Notify hook NOT found in $CONFIG_TOML"
    errors=$((errors + 1))
  fi

  # Check B12 skill installed
  if [ -f "$HOME/.codex/skills/b12/SKILL.md" ]; then
    info "Verify: B12 skill installed"
  else
    warn "Verify: B12 skill NOT found"
    errors=$((errors + 1))
  fi

  return $errors
}

# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────

echo "B12 Memory System Installer (v10.3 — Codex CLI full support)"
echo "─────────────────────────────────"

# Full setup: create venv first
if $FULL_SETUP; then
  setup_venv
  echo ""
fi

# Always create dirs and copy files (Claude Code hooks + scripts)
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

# Install hooks to Claude Code setups
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

# Full setup: inject Claude Code MCP config
if $FULL_SETUP; then
  inject_mcp_config
  echo ""
fi

# Codex CLI setup
if $INSTALL_CODEX; then
  echo ""
  echo "── Codex CLI Setup ──────────────"
  inject_codex_mcp_config
  inject_codex_agents
  install_codex_skill
  echo ""
fi

# Run migration
run_migration

echo ""
echo "─────────────────────────────────"

# Run verification
verify
VERIFY_RESULT=$?

# Codex verification (additive)
if $INSTALL_CODEX; then
  echo ""
  echo "── Codex Verification ───────────"
  verify_codex
  CODEX_RESULT=$?
  VERIFY_RESULT=$((VERIFY_RESULT + CODEX_RESULT))
fi

echo ""
echo "─────────────────────────────────"
if [ $VERIFY_RESULT -eq 0 ]; then
  if $INSTALL_CODEX; then
    info "Installation complete! Restart Claude Code and Codex CLI to activate B12."
  else
    info "Installation complete! Restart Claude Code to activate B12."
  fi
else
  warn "Installation complete with $VERIFY_RESULT warning(s). See above."
fi

if ! $FULL_SETUP && ! $INSTALL_CODEX; then
  echo ""
  echo "Tip: Run './install.sh --full' for automatic venv + MCP config setup."
  echo "     Run './install.sh --codex' to add Codex CLI support."
fi

echo ""
if $INSTALL_CODEX; then
  echo "Next: Restart Codex CLI, then type /mcp to verify B12 is connected."
else
  echo "Next: Restart Claude Code, then run /mcp to verify B12 is connected."
fi
