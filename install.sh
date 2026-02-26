#!/bin/bash
# B12 Memory System — Setup Installer
#
# Usage:
#   ./install.sh                    # Standard install (hooks + config only)
#   ./install.sh --all              # Install to all ~/.claude* setups
#   ./install.sh --full             # Full setup: venv + deps + hooks + MCP config
#   ./install.sh --full --all       # Full setup for all setups
#   ./install.sh --codex            # Install B12 MCP server to Codex CLI
#   ./install.sh --gemini           # Install B12 MCP server to Gemini CLI
#   ./install.sh --vscode           # Install B12 MCP server to VS Code / Copilot
#   ./install.sh --cursor           # Install B12 MCP server to Cursor
#   ./install.sh --kimi             # Install B12 MCP server to Kimi Code
#   ./install.sh --windsurf         # Install B12 MCP server to Windsurf
#   ./install.sh --cline            # Install B12 MCP server to Cline (VS Code ext)
#   ./install.sh --opencode         # Install B12 MCP server to OpenCode
#   ./install.sh --full --codex     # Full setup + Codex CLI support
#   ./install.sh --full --gemini --cursor  # Full setup + Gemini + Cursor
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
#   1. Copies hook scripts to ~/.B12/hooks/ (shared location)
#   2. Copies support scripts to ~/.B12/hooks/scripts/
#   3. Merges hook config into target setup's settings.json
#   4. Creates required directories (migrates from ~/.claude/ if needed)

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
HOOK_SOURCE="$SCRIPT_DIR/hooks"
SCRIPT_SOURCE="$SCRIPT_DIR/scripts"
HOOK_DEST="$HOME/.B12/hooks"
SCRIPT_DEST="$HOME/.B12/hooks/scripts"
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

# ─────────────────────────────────────────────
# Shared helper: inject B12 section into a file
# ─────────────────────────────────────────────
# Injects content between <!-- B12-MEMORY-START/END --> markers.
# If markers exist, replaces the section. Otherwise appends.
# Usage: inject_b12_section TARGET_FILE TEMPLATE_FILE DISPLAY_NAME
inject_b12_section() {
  local TARGET="$1"
  local TEMPLATE="$2"
  local NAME="$3"

  if [ ! -f "$TEMPLATE" ]; then
    warn "$NAME template not found at $TEMPLATE"
    return 1
  fi

  [ -f "$TARGET" ] || touch "$TARGET"

  if grep -q '<!-- B12-MEMORY-START -->' "$TARGET" 2>/dev/null; then
    # Replace existing section
    if ! python3 - "$TARGET" "$TEMPLATE" << 'PYEOF'
import sys, re
target_path, template_path = sys.argv[1], sys.argv[2]
with open(target_path, 'r') as f: content = f.read()
with open(template_path, 'r') as f: template = f.read()
b12_section = f'\n<!-- B12-MEMORY-START -->\n{template}\n<!-- B12-MEMORY-END -->\n'
content = re.sub(r'<!-- B12-MEMORY-START -->.*?<!-- B12-MEMORY-END -->', b12_section.strip(), content, flags=re.DOTALL)
with open(target_path, 'w') as f: f.write(content)
PYEOF
    then
      warn "Failed to update B12 section in $TARGET"
      return 1
    fi
    info "Updated B12 section in $TARGET"
  else
    # Append new section
    { echo ""; echo "<!-- B12-MEMORY-START -->"; cat "$TEMPLATE"; echo ""; echo "<!-- B12-MEMORY-END -->"; } >> "$TARGET"
    info "Added B12 memory instructions to $TARGET"
  fi
}

# Parse flags
FULL_SETUP=false
INSTALL_ALL=false
INSTALL_CODEX=false
INSTALL_GEMINI=false
INSTALL_VSCODE=false
INSTALL_CURSOR=false
INSTALL_KIMI=false
INSTALL_WINDSURF=false
INSTALL_CLINE=false
INSTALL_OPENCODE=false
TARGET_DIR=""
for arg in "$@"; do
  case "$arg" in
    --full)      FULL_SETUP=true ;;
    --all)       INSTALL_ALL=true ;;
    --codex)     INSTALL_CODEX=true ;;
    --gemini)    INSTALL_GEMINI=true ;;
    --vscode)    INSTALL_VSCODE=true ;;
    --cursor)    INSTALL_CURSOR=true ;;
    --kimi)      INSTALL_KIMI=true ;;
    --windsurf)  INSTALL_WINDSURF=true ;;
    --cline)     INSTALL_CLINE=true ;;
    --opencode)  INSTALL_OPENCODE=true ;;
    *)           TARGET_DIR="$arg" ;;
  esac
done

# ─────────────────────────────────────────────
# Step 1: Create required directories
# ─────────────────────────────────────────────
create_dirs() {
  mkdir -p "$HOOK_DEST"
  mkdir -p "$SCRIPT_DEST"
  mkdir -p "$HOME/.B12/memory-staging"
  mkdir -p "$HOME/.B12/memory-logs"
  mkdir -p "$HOME/.B12/memory-summaries"

  # Migrate data from old ~/.claude/ location (safe: copies, doesn't delete)
  local migrated=0
  for subdir in memory-staging memory-logs memory-summaries memory-backups memory-state; do
    local old_dir="$HOME/.claude/$subdir"
    local new_dir="$HOME/.B12/$subdir"
    if [ -d "$old_dir" ] && [ "$(ls -A "$old_dir" 2>/dev/null)" ]; then
      mkdir -p "$new_dir"
      cp -rn "$old_dir"/* "$new_dir"/ 2>/dev/null && migrated=$((migrated + 1))
    fi
  done
  # Migrate hooks from old location
  if [ -d "$HOME/.claude/hooks" ] && [ ! -d "$HOME/.B12/hooks" -o -z "$(ls -A "$HOME/.B12/hooks" 2>/dev/null)" ]; then
    mkdir -p "$HOME/.B12/hooks"
    cp -rn "$HOME/.claude/hooks"/* "$HOME/.B12/hooks"/ 2>/dev/null && migrated=$((migrated + 1))
  fi
  if [ "$migrated" -gt 0 ]; then
    info "Migrated $migrated directories from ~/.claude/ to ~/.B12/"
  fi

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

  if python3 "$MIGRATE_SCRIPT" --db "$DB_PATH"; then
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

# ═════════════════════════════════════════════
# Codex CLI
# ═════════════════════════════════════════════

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
            lines[i] = f'notify = ["{notify_hook}"]\n'
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
    lines.insert(insert_at, f'notify = ["{notify_hook}"]\n')

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
  [ -d "$CODEX_DIR" ] || { warn "Codex directory not found"; return 1; }
  inject_b12_section "$CODEX_DIR/AGENTS.md" "$SCRIPT_DIR/config/codex-agents-template.md" "Codex AGENTS.md"
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

# ═════════════════════════════════════════════
# Gemini CLI
# ═════════════════════════════════════════════

# ─────────────────────────────────────────────
# Gemini CLI: inject MCP config into settings.json
# ─────────────────────────────────────────────
inject_gemini_mcp_config() {
  local GEMINI_DIR="$HOME/.gemini"
  local SETTINGS_JSON="$GEMINI_DIR/settings.json"
  local SERVER_SCRIPT="$SCRIPT_DEST/b12_mcp_server.py"

  if [ ! -d "$GEMINI_DIR" ]; then
    warn "Gemini directory not found at $GEMINI_DIR — is Gemini CLI installed?"
    warn "Install with: npm install -g @google/gemini-cli"
    return 1
  fi

  if [ ! -x "$VENV_PYTHON" ]; then
    warn "Venv Python not found at $VENV_PYTHON"
    warn "Run with --full to create the venv first: ./install.sh --full --gemini"
    return 1
  fi
  if [ ! -f "$SERVER_SCRIPT" ]; then
    warn "MCP server script not found at $SERVER_SCRIPT — run standard install first"
    return 1
  fi

  if [ ! -f "$SETTINGS_JSON" ]; then
    echo '{}' > "$SETTINGS_JSON"
    info "Created $SETTINGS_JSON"
  fi

  if ! python3 - "$SETTINGS_JSON" "$VENV_PYTHON" "$SERVER_SCRIPT" << 'PYEOF'
import sys, json

settings_path = sys.argv[1]
venv_python = sys.argv[2]
server_script = sys.argv[3]

with open(settings_path, 'r') as f:
    try:
        settings = json.load(f)
    except json.JSONDecodeError:
        settings = {}

if 'mcpServers' not in settings:
    settings['mcpServers'] = {}

settings['mcpServers']['B12'] = {
    'command': venv_python,
    'args': [server_script],
    'env': {
        'MCP_EMBEDDING_MODEL': 'paraphrase-multilingual-MiniLM-L12-v2',
        'MCP_MAX_RESPONSE_CHARS': '40000'
    },
    'timeout': 30000
}

with open(settings_path, 'w') as f:
    json.dump(settings, f, indent=2)
    f.write('\n')

PYEOF
  then
    error "Failed to update B12 config in $SETTINGS_JSON"
  fi

  info "B12 MCP server configured in $SETTINGS_JSON"
  echo "     command: $VENV_PYTHON"
  echo "     script:  $SERVER_SCRIPT"
}

# ─────────────────────────────────────────────
# Gemini CLI: append B12 instructions to GEMINI.md
# ─────────────────────────────────────────────
inject_gemini_instructions() {
  local GEMINI_DIR="$HOME/.gemini"
  [ -d "$GEMINI_DIR" ] || { warn "Gemini directory not found"; return 1; }
  inject_b12_section "$GEMINI_DIR/GEMINI.md" "$SCRIPT_DIR/config/gemini-instructions-template.md" "Gemini GEMINI.md"
}

# ─────────────────────────────────────────────
# Gemini CLI: verify installation
# ─────────────────────────────────────────────
verify_gemini() {
  local errors=0
  local SETTINGS_JSON="$HOME/.gemini/settings.json"
  local GEMINI_MD="$HOME/.gemini/GEMINI.md"

  if python3 -c "import json; d=json.load(open('$SETTINGS_JSON')); assert 'B12' in d.get('mcpServers', {})" 2>/dev/null; then
    info "Verify: B12 MCP server configured in $SETTINGS_JSON"
  else
    warn "Verify: B12 NOT found in $SETTINGS_JSON"
    errors=$((errors + 1))
  fi

  if grep -q 'B12-MEMORY-START' "$GEMINI_MD" 2>/dev/null; then
    info "Verify: B12 instructions present in $GEMINI_MD"
  else
    warn "Verify: B12 instructions NOT found in $GEMINI_MD"
    errors=$((errors + 1))
  fi

  if [ -x "$VENV_PYTHON" ]; then
    info "Verify: B12 venv accessible at $VENV_PATH"
  else
    warn "Verify: B12 venv NOT found (Gemini MCP server will fail)"
    errors=$((errors + 1))
  fi

  return $errors
}

# ═════════════════════════════════════════════
# VS Code / GitHub Copilot
# ═════════════════════════════════════════════

# ─────────────────────────────────────────────
# VS Code: inject MCP config into user-level mcp.json
# ─────────────────────────────────────────────
inject_vscode_mcp_config() {
  local VSCODE_USER_DIR
  case "$(uname -s)" in
    Darwin)  VSCODE_USER_DIR="$HOME/Library/Application Support/Code/User" ;;
    Linux)   VSCODE_USER_DIR="$HOME/.config/Code/User" ;;
    MINGW*|MSYS*|CYGWIN*) VSCODE_USER_DIR="$APPDATA/Code/User" ;;
    *)       warn "Unknown OS for VS Code config path"; return 1 ;;
  esac

  local MCP_JSON="$VSCODE_USER_DIR/mcp.json"
  local SERVER_SCRIPT="$SCRIPT_DEST/b12_mcp_server.py"

  if [ ! -d "$VSCODE_USER_DIR" ]; then
    warn "VS Code user directory not found at $VSCODE_USER_DIR"
    warn "Launch VS Code at least once, then re-run this installer."
    return 1
  fi

  if [ ! -x "$VENV_PYTHON" ]; then
    warn "Venv Python not found at $VENV_PYTHON"
    warn "Run with --full to create the venv first: ./install.sh --full --vscode"
    return 1
  fi
  if [ ! -f "$SERVER_SCRIPT" ]; then
    warn "MCP server script not found at $SERVER_SCRIPT — run standard install first"
    return 1
  fi

  if [ ! -f "$MCP_JSON" ]; then
    echo '{}' > "$MCP_JSON"
    info "Created $MCP_JSON"
  fi

  if ! python3 - "$MCP_JSON" "$VENV_PYTHON" "$SERVER_SCRIPT" << 'PYEOF'
import sys, json

mcp_path = sys.argv[1]
venv_python = sys.argv[2]
server_script = sys.argv[3]

with open(mcp_path, 'r') as f:
    try:
        config = json.load(f)
    except json.JSONDecodeError:
        config = {}

if "servers" not in config:
    config["servers"] = {}

config["servers"]["B12"] = {
    "type": "stdio",
    "command": venv_python,
    "args": [server_script],
    "env": {
        "MCP_EMBEDDING_MODEL": "paraphrase-multilingual-MiniLM-L12-v2",
        "MCP_MAX_RESPONSE_CHARS": "40000"
    }
}

with open(mcp_path, 'w') as f:
    json.dump(config, f, indent=2)
    f.write('\n')

PYEOF
  then
    error "Failed to update B12 config in $MCP_JSON"
  fi

  info "B12 MCP server configured in $MCP_JSON"
  echo "     command: $VENV_PYTHON"
  echo "     script:  $SERVER_SCRIPT"
}

# ─────────────────────────────────────────────
# VS Code: create copilot-instructions.md in B12 repo
# ─────────────────────────────────────────────
inject_vscode_instructions() {
  local TEMPLATE="$SCRIPT_DIR/config/vscode-instructions-template.md"

  if [ ! -f "$TEMPLATE" ]; then
    warn "VS Code instructions template not found at $TEMPLATE"
    return 1
  fi

  local COPILOT_DIR="$SCRIPT_DIR/.github"
  local COPILOT_MD="$COPILOT_DIR/copilot-instructions.md"

  mkdir -p "$COPILOT_DIR"

  if [ -f "$COPILOT_MD" ]; then
    inject_b12_section "$COPILOT_MD" "$TEMPLATE" "VS Code copilot-instructions.md"
  else
    cp "$TEMPLATE" "$COPILOT_MD"
    info "Created $COPILOT_MD with B12 instructions"
  fi

  echo ""
  echo "     To enable B12 in other projects, copy the instructions:"
  echo "       mkdir -p YOUR_PROJECT/.github"
  echo "       cp $COPILOT_MD YOUR_PROJECT/.github/copilot-instructions.md"
}

# ─────────────────────────────────────────────
# VS Code: verify installation
# ─────────────────────────────────────────────
verify_vscode() {
  local errors=0

  local VSCODE_USER_DIR
  case "$(uname -s)" in
    Darwin)  VSCODE_USER_DIR="$HOME/Library/Application Support/Code/User" ;;
    Linux)   VSCODE_USER_DIR="$HOME/.config/Code/User" ;;
    MINGW*|MSYS*|CYGWIN*) VSCODE_USER_DIR="$APPDATA/Code/User" ;;
    *)       VSCODE_USER_DIR="" ;;
  esac

  local MCP_JSON="$VSCODE_USER_DIR/mcp.json"
  local COPILOT_MD="$SCRIPT_DIR/.github/copilot-instructions.md"

  if [ -f "$MCP_JSON" ] && grep -q '"B12"' "$MCP_JSON" 2>/dev/null; then
    info "Verify: B12 MCP server configured in $MCP_JSON"
  else
    warn "Verify: B12 NOT found in VS Code mcp.json"
    errors=$((errors + 1))
  fi

  if grep -q 'B12 Memory System' "$COPILOT_MD" 2>/dev/null; then
    info "Verify: B12 instructions present in $COPILOT_MD"
  else
    warn "Verify: B12 instructions NOT found in copilot-instructions.md"
    errors=$((errors + 1))
  fi

  if [ -x "$VENV_PYTHON" ]; then
    info "Verify: B12 venv accessible at $VENV_PATH"
  else
    warn "Verify: B12 venv NOT found (VS Code MCP server will fail)"
    errors=$((errors + 1))
  fi

  return $errors
}

# ═════════════════════════════════════════════
# Cursor
# ═════════════════════════════════════════════

# ─────────────────────────────────────────────
# Cursor: inject MCP config into ~/.cursor/mcp.json
# ─────────────────────────────────────────────
inject_cursor_mcp_config() {
  local CURSOR_DIR="$HOME/.cursor"
  local MCP_JSON="$CURSOR_DIR/mcp.json"
  local SERVER_SCRIPT="$SCRIPT_DEST/b12_mcp_server.py"

  if [ ! -d "$CURSOR_DIR" ]; then
    warn "Cursor directory not found at $CURSOR_DIR — is Cursor installed?"
    return 1
  fi

  if [ ! -x "$VENV_PYTHON" ]; then
    warn "Venv Python not found at $VENV_PYTHON"
    warn "Run with --full to create the venv first: ./install.sh --full --cursor"
    return 1
  fi
  if [ ! -f "$SERVER_SCRIPT" ]; then
    warn "MCP server script not found at $SERVER_SCRIPT — run standard install first"
    return 1
  fi

  if [ ! -f "$MCP_JSON" ]; then
    echo '{}' > "$MCP_JSON"
    info "Created $MCP_JSON"
  fi

  if ! python3 - "$MCP_JSON" "$VENV_PYTHON" "$SERVER_SCRIPT" << 'PYEOF'
import sys, json

mcp_json_path = sys.argv[1]
venv_python = sys.argv[2]
server_script = sys.argv[3]

with open(mcp_json_path, 'r') as f:
    try:
        config = json.load(f)
    except json.JSONDecodeError:
        config = {}

if 'mcpServers' not in config:
    config['mcpServers'] = {}

config['mcpServers']['B12'] = {
    'command': venv_python,
    'args': [server_script],
    'env': {
        'MCP_EMBEDDING_MODEL': 'paraphrase-multilingual-MiniLM-L12-v2',
        'MCP_MAX_RESPONSE_CHARS': '40000'
    }
}

with open(mcp_json_path, 'w') as f:
    json.dump(config, f, indent=2)
    f.write('\n')

PYEOF
  then
    error "Failed to update B12 config in $MCP_JSON"
  fi

  info "B12 MCP server configured in $MCP_JSON"
  echo "     command: $VENV_PYTHON"
  echo "     script:  $SERVER_SCRIPT"
}

# ─────────────────────────────────────────────
# Cursor: install B12 rule as .mdc file
# ─────────────────────────────────────────────
inject_cursor_rules() {
  local CURSOR_DIR="$HOME/.cursor"
  local RULES_DIR="$CURSOR_DIR/rules"
  local RULE_FILE="$RULES_DIR/b12-memory.mdc"
  local TEMPLATE="$SCRIPT_DIR/config/cursor-rules-template.md"

  if [ ! -d "$CURSOR_DIR" ]; then
    warn "Cursor directory not found at $CURSOR_DIR"
    return 1
  fi

  if [ ! -f "$TEMPLATE" ]; then
    warn "Cursor rules template not found at $TEMPLATE"
    return 1
  fi

  mkdir -p "$RULES_DIR"

  # Build .mdc file with YAML frontmatter
  {
    echo '---'
    echo 'description: B12 persistent memory system — MCP tools for storing and searching memories across sessions'
    echo 'globs:'
    echo 'alwaysApply: true'
    echo '---'
    echo ''
    cat "$TEMPLATE"
  } > "$RULE_FILE"

  info "B12 rule installed to $RULE_FILE"
}

# ─────────────────────────────────────────────
# Cursor: verify installation
# ─────────────────────────────────────────────
verify_cursor() {
  local errors=0
  local MCP_JSON="$HOME/.cursor/mcp.json"
  local RULE_FILE="$HOME/.cursor/rules/b12-memory.mdc"

  if [ -f "$MCP_JSON" ] && grep -q '"B12"' "$MCP_JSON" 2>/dev/null; then
    info "Verify: B12 MCP server configured in $MCP_JSON"
  else
    warn "Verify: B12 NOT found in $MCP_JSON"
    errors=$((errors + 1))
  fi

  if [ -f "$RULE_FILE" ]; then
    info "Verify: B12 rule installed at $RULE_FILE"
  else
    warn "Verify: B12 rule NOT found at $RULE_FILE"
    errors=$((errors + 1))
  fi

  if [ -x "$VENV_PYTHON" ]; then
    info "Verify: B12 venv accessible at $VENV_PATH"
  else
    warn "Verify: B12 venv NOT found (Cursor MCP server will fail)"
    errors=$((errors + 1))
  fi

  return $errors
}

# ═════════════════════════════════════════════
# Kimi Code
# ═════════════════════════════════════════════

# ─────────────────────────────────────────────
# Kimi Code: inject MCP config into mcp.json
# ─────────────────────────────────────────────
inject_kimi_mcp_config() {
  local KIMI_DIR="$HOME/.kimi"
  local MCP_JSON="$KIMI_DIR/mcp.json"
  local SERVER_SCRIPT="$SCRIPT_DEST/b12_mcp_server.py"

  if [ ! -d "$KIMI_DIR" ]; then
    warn "Kimi directory not found at $KIMI_DIR — is Kimi Code CLI installed?"
    warn "Install with: pip install kimi-cli  (or pipx install kimi-cli)"
    return 1
  fi

  if [ ! -x "$VENV_PYTHON" ]; then
    warn "Venv Python not found at $VENV_PYTHON"
    warn "Run with --full to create the venv first: ./install.sh --full --kimi"
    return 1
  fi
  if [ ! -f "$SERVER_SCRIPT" ]; then
    warn "MCP server script not found at $SERVER_SCRIPT — run standard install first"
    return 1
  fi

  if [ ! -f "$MCP_JSON" ]; then
    echo '{"mcpServers":{}}' > "$MCP_JSON"
    info "Created $MCP_JSON"
  fi

  if ! python3 - "$MCP_JSON" "$VENV_PYTHON" "$SERVER_SCRIPT" << 'PYEOF'
import sys, json

mcp_path = sys.argv[1]
venv_python = sys.argv[2]
server_script = sys.argv[3]

with open(mcp_path, 'r') as f:
    try:
        config = json.load(f)
    except json.JSONDecodeError:
        config = {}

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

with open(mcp_path, 'w') as f:
    json.dump(config, f, indent=2)
    f.write('\n')

PYEOF
  then
    error "Failed to update B12 config in $MCP_JSON"
  fi

  info "B12 MCP server configured in $MCP_JSON"
  echo "     command: $VENV_PYTHON"
  echo "     script:  $SERVER_SCRIPT"
}

# ─────────────────────────────────────────────
# Kimi Code: append B12 instructions to AGENTS.md
# ─────────────────────────────────────────────
inject_kimi_agents() {
  local KIMI_DIR="$HOME/.kimi"
  [ -d "$KIMI_DIR" ] || { warn "Kimi directory not found"; return 1; }
  inject_b12_section "$KIMI_DIR/AGENTS.md" "$SCRIPT_DIR/config/kimi-agents-template.md" "Kimi AGENTS.md"
}

# ─────────────────────────────────────────────
# Kimi Code: verify installation
# ─────────────────────────────────────────────
verify_kimi() {
  local errors=0
  local MCP_JSON="$HOME/.kimi/mcp.json"
  local AGENTS_MD="$HOME/.kimi/AGENTS.md"

  if [ -f "$MCP_JSON" ] && grep -q '"B12"' "$MCP_JSON" 2>/dev/null; then
    info "Verify: B12 MCP server configured in $MCP_JSON"
  else
    warn "Verify: B12 NOT found in $MCP_JSON"
    errors=$((errors + 1))
  fi

  if grep -q 'B12-MEMORY-START' "$AGENTS_MD" 2>/dev/null; then
    info "Verify: B12 instructions present in $AGENTS_MD"
  else
    warn "Verify: B12 instructions NOT found in $AGENTS_MD"
    errors=$((errors + 1))
  fi

  if [ -x "$VENV_PYTHON" ]; then
    info "Verify: B12 venv accessible at $VENV_PATH"
  else
    warn "Verify: B12 venv NOT found (Kimi MCP server will fail)"
    errors=$((errors + 1))
  fi

  return $errors
}

# ═════════════════════════════════════════════
# Windsurf
# ═════════════════════════════════════════════

# ─────────────────────────────────────────────
# Windsurf: inject MCP config into mcp_config.json
# ─────────────────────────────────────────────
inject_windsurf_mcp_config() {
  local WINDSURF_DIR="$HOME/.codeium/windsurf"
  local MCP_CONFIG="$WINDSURF_DIR/mcp_config.json"
  local SERVER_SCRIPT="$SCRIPT_DEST/b12_mcp_server.py"

  if [ ! -d "$HOME/.codeium" ]; then
    warn "Codeium directory not found at $HOME/.codeium — is Windsurf installed?"
    return 1
  fi

  mkdir -p "$WINDSURF_DIR"

  if [ ! -x "$VENV_PYTHON" ]; then
    warn "Venv Python not found at $VENV_PYTHON"
    warn "Run with --full to create the venv first: ./install.sh --full --windsurf"
    return 1
  fi
  if [ ! -f "$SERVER_SCRIPT" ]; then
    warn "MCP server script not found at $SERVER_SCRIPT — run standard install first"
    return 1
  fi

  if [ ! -f "$MCP_CONFIG" ]; then
    echo '{"mcpServers":{}}' > "$MCP_CONFIG"
    info "Created $MCP_CONFIG"
  fi

  if ! python3 - "$MCP_CONFIG" "$VENV_PYTHON" "$SERVER_SCRIPT" << 'PYEOF'
import sys, json

config_path = sys.argv[1]
venv_python = sys.argv[2]
server_script = sys.argv[3]

with open(config_path, 'r') as f:
    try:
        config = json.load(f)
    except json.JSONDecodeError:
        config = {}

if 'mcpServers' not in config:
    config['mcpServers'] = {}

config['mcpServers']['B12'] = {
    'command': venv_python,
    'args': [server_script],
    'env': {
        'MCP_EMBEDDING_MODEL': 'paraphrase-multilingual-MiniLM-L12-v2',
        'MCP_MAX_RESPONSE_CHARS': '40000'
    }
}

with open(config_path, 'w') as f:
    json.dump(config, f, indent=2)
    f.write('\n')

PYEOF
  then
    error "Failed to update B12 config in $MCP_CONFIG"
  fi

  info "B12 MCP server configured in $MCP_CONFIG"
  echo "     command: $VENV_PYTHON"
  echo "     script:  $SERVER_SCRIPT"
}

# ─────────────────────────────────────────────
# Windsurf: add B12 rules to global_rules.md
# ─────────────────────────────────────────────
inject_windsurf_rules() {
  local WINDSURF_DIR="$HOME/.codeium/windsurf"
  local MEMORIES_DIR="$WINDSURF_DIR/memories"
  local GLOBAL_RULES="$MEMORIES_DIR/global_rules.md"

  if [ ! -d "$HOME/.codeium" ]; then
    warn "Codeium directory not found"
    return 1
  fi

  mkdir -p "$MEMORIES_DIR"
  inject_b12_section "$GLOBAL_RULES" "$SCRIPT_DIR/config/windsurf-rules-template.md" "Windsurf global_rules.md"
}

# ─────────────────────────────────────────────
# Windsurf: verify installation
# ─────────────────────────────────────────────
verify_windsurf() {
  local errors=0
  local MCP_CONFIG="$HOME/.codeium/windsurf/mcp_config.json"
  local GLOBAL_RULES="$HOME/.codeium/windsurf/memories/global_rules.md"

  if [ -f "$MCP_CONFIG" ] && grep -q '"B12"' "$MCP_CONFIG" 2>/dev/null; then
    info "Verify: B12 MCP server configured in $MCP_CONFIG"
  else
    warn "Verify: B12 NOT found in $MCP_CONFIG"
    errors=$((errors + 1))
  fi

  if grep -q 'B12-MEMORY-START' "$GLOBAL_RULES" 2>/dev/null; then
    info "Verify: B12 instructions present in $GLOBAL_RULES"
  else
    warn "Verify: B12 instructions NOT found in $GLOBAL_RULES"
    errors=$((errors + 1))
  fi

  if [ -x "$VENV_PYTHON" ]; then
    info "Verify: B12 venv accessible at $VENV_PATH"
  else
    warn "Verify: B12 venv NOT found (Windsurf MCP server will fail)"
    errors=$((errors + 1))
  fi

  return $errors
}

# ═════════════════════════════════════════════
# Cline (VS Code Extension)
# ═════════════════════════════════════════════

# ─────────────────────────────────────────────
# Cline: detect MCP settings file path
# ─────────────────────────────────────────────
get_cline_mcp_settings_path() {
  local VSCODE_STORAGE=""
  case "$(uname -s)" in
    Darwin)
      VSCODE_STORAGE="$HOME/Library/Application Support/Code/User/globalStorage"
      ;;
    Linux)
      VSCODE_STORAGE="$HOME/.config/Code/User/globalStorage"
      ;;
    MINGW*|CYGWIN*|MSYS*)
      VSCODE_STORAGE="$APPDATA/Code/User/globalStorage"
      ;;
  esac

  local CLINE_DIR="$VSCODE_STORAGE/saoudrizwan.claude-dev/settings"
  local CLINE_SETTINGS="$CLINE_DIR/cline_mcp_settings.json"

  # Also check VS Code Insiders
  if [ ! -d "$CLINE_DIR" ]; then
    local INSIDERS_STORAGE=""
    case "$(uname -s)" in
      Darwin)
        INSIDERS_STORAGE="$HOME/Library/Application Support/Code - Insiders/User/globalStorage"
        ;;
      Linux)
        INSIDERS_STORAGE="$HOME/.config/Code - Insiders/User/globalStorage"
        ;;
      MINGW*|CYGWIN*|MSYS*)
        INSIDERS_STORAGE="$APPDATA/Code - Insiders/User/globalStorage"
        ;;
    esac
    local INSIDERS_DIR="$INSIDERS_STORAGE/saoudrizwan.claude-dev/settings"
    if [ -d "$INSIDERS_DIR" ]; then
      CLINE_DIR="$INSIDERS_DIR"
      CLINE_SETTINGS="$CLINE_DIR/cline_mcp_settings.json"
    fi
  fi

  echo "$CLINE_SETTINGS"
}

# ─────────────────────────────────────────────
# Cline: inject B12 MCP server configuration
# ─────────────────────────────────────────────
inject_cline_mcp_config() {
  local CLINE_SETTINGS
  CLINE_SETTINGS="$(get_cline_mcp_settings_path)"
  local CLINE_DIR
  CLINE_DIR="$(dirname "$CLINE_SETTINGS")"
  local SERVER_SCRIPT="$SCRIPT_DEST/b12_mcp_server.py"

  if [ ! -d "$(dirname "$CLINE_DIR")" ]; then
    warn "Cline extension not found — is Cline (saoudrizwan.claude-dev) installed in VS Code?"
    return 1
  fi

  if [ ! -x "$VENV_PYTHON" ]; then
    warn "Venv Python not found at $VENV_PYTHON"
    warn "Run with --full to create the venv first: ./install.sh --full --cline"
    return 1
  fi
  if [ ! -f "$SERVER_SCRIPT" ]; then
    warn "MCP server script not found at $SERVER_SCRIPT — run standard install first"
    return 1
  fi

  mkdir -p "$CLINE_DIR"

  if [ ! -f "$CLINE_SETTINGS" ]; then
    echo '{"mcpServers":{}}' > "$CLINE_SETTINGS"
    info "Created $CLINE_SETTINGS"
  fi

  if ! python3 - "$CLINE_SETTINGS" "$VENV_PYTHON" "$SERVER_SCRIPT" << 'PYEOF'
import sys, json

settings_path = sys.argv[1]
venv_python = sys.argv[2]
server_script = sys.argv[3]

try:
    with open(settings_path, 'r') as f:
        config = json.load(f)
except (json.JSONDecodeError, FileNotFoundError):
    config = {}

if 'mcpServers' not in config:
    config['mcpServers'] = {}

config['mcpServers']['B12'] = {
    "command": venv_python,
    "args": [server_script],
    "env": {
        "MCP_EMBEDDING_MODEL": "paraphrase-multilingual-MiniLM-L12-v2",
        "MCP_MAX_RESPONSE_CHARS": "40000"
    },
    "alwaysAllow": [
        "memory_store",
        "memory_search",
        "memory_update",
        "memory_quality"
    ],
    "disabled": False  # Python False -> JSON false via json.dump()
}

with open(settings_path, 'w') as f:
    json.dump(config, f, indent=2)
    f.write('\n')

PYEOF
  then
    error "Failed to update B12 config in $CLINE_SETTINGS"
  fi

  info "B12 MCP server configured in $CLINE_SETTINGS"
  echo "     command: $VENV_PYTHON"
  echo "     script:  $SERVER_SCRIPT"
}

# ─────────────────────────────────────────────
# Cline: install B12 rules
# ─────────────────────────────────────────────
inject_cline_rules() {
  local TEMPLATE="$SCRIPT_DIR/config/cline-rules-template.md"

  if [ ! -f "$TEMPLATE" ]; then
    warn "Cline rules template not found at $TEMPLATE"
    return 1
  fi

  local GLOBAL_RULES_DIR="$HOME/Documents/Cline/Rules"
  mkdir -p "$GLOBAL_RULES_DIR"

  local GLOBAL_RULE="$GLOBAL_RULES_DIR/b12-memory.md"
  cp "$TEMPLATE" "$GLOBAL_RULE"
  info "B12 rules installed to $GLOBAL_RULE"
  echo "     Copy to your project: cp \"$TEMPLATE\" .clinerules/b12-memory.md"
}

# ─────────────────────────────────────────────
# Cline: verify installation
# ─────────────────────────────────────────────
verify_cline() {
  local errors=0
  local CLINE_SETTINGS
  CLINE_SETTINGS="$(get_cline_mcp_settings_path)"

  if [ -f "$CLINE_SETTINGS" ] && grep -q '"B12"' "$CLINE_SETTINGS" 2>/dev/null; then
    info "Verify: B12 MCP server configured in Cline settings"
  else
    warn "Verify: B12 NOT found in $CLINE_SETTINGS"
    errors=$((errors + 1))
  fi

  local GLOBAL_RULE="$HOME/Documents/Cline/Rules/b12-memory.md"
  if [ -f "$GLOBAL_RULE" ]; then
    info "Verify: B12 global rules present at $GLOBAL_RULE"
  else
    warn "Verify: B12 global rules NOT found at $GLOBAL_RULE"
    errors=$((errors + 1))
  fi

  if [ -x "$VENV_PYTHON" ]; then
    info "Verify: B12 venv accessible at $VENV_PATH"
  else
    warn "Verify: B12 venv NOT found (Cline MCP server will fail)"
    errors=$((errors + 1))
  fi

  return $errors
}

# ═════════════════════════════════════════════
# OpenCode
# ═════════════════════════════════════════════

# ─────────────────────────────────────────────
# OpenCode: inject MCP config into opencode.json
# ─────────────────────────────────────────────
inject_opencode_mcp_config() {
  local OPENCODE_DIR="$HOME/.config/opencode"
  local CONFIG_JSON="$OPENCODE_DIR/opencode.json"
  local SERVER_SCRIPT="$SCRIPT_DEST/b12_mcp_server.py"

  if [ ! -d "$OPENCODE_DIR" ]; then
    warn "OpenCode config directory not found at $OPENCODE_DIR — creating it"
    mkdir -p "$OPENCODE_DIR"
  fi

  if [ ! -x "$VENV_PYTHON" ]; then
    warn "Venv Python not found at $VENV_PYTHON"
    warn "Run with --full to create the venv first: ./install.sh --full --opencode"
    return 1
  fi
  if [ ! -f "$SERVER_SCRIPT" ]; then
    warn "MCP server script not found at $SERVER_SCRIPT — run standard install first"
    return 1
  fi

  if [ ! -f "$CONFIG_JSON" ]; then
    echo '{}' > "$CONFIG_JSON"
    info "Created $CONFIG_JSON"
  fi

  if ! python3 - "$CONFIG_JSON" "$VENV_PYTHON" "$SERVER_SCRIPT" << 'PYEOF'
import sys, json

config_path = sys.argv[1]
venv_python = sys.argv[2]
server_script = sys.argv[3]

with open(config_path, 'r') as f:
    content = f.read().strip()
    if not content:
        content = '{}'
    config = json.loads(content)

if 'mcp' not in config:
    config['mcp'] = {}

config['mcp']['B12'] = {
    'type': 'local',
    'command': [venv_python, server_script],
    'enabled': True,
    'environment': {
        'MCP_EMBEDDING_MODEL': 'paraphrase-multilingual-MiniLM-L12-v2',
        'MCP_MAX_RESPONSE_CHARS': '40000'
    }
}

if '$schema' not in config:
    config['$schema'] = 'https://opencode.ai/config.json'

with open(config_path, 'w') as f:
    json.dump(config, f, indent=2)
    f.write('\n')

PYEOF
  then
    error "Failed to update B12 config in $CONFIG_JSON"
  fi

  info "B12 MCP server configured in $CONFIG_JSON"
}

# ─────────────────────────────────────────────
# OpenCode: append B12 instructions to AGENTS.md
# ─────────────────────────────────────────────
inject_opencode_agents() {
  local OPENCODE_DIR="$HOME/.config/opencode"
  [ -d "$OPENCODE_DIR" ] || mkdir -p "$OPENCODE_DIR"
  inject_b12_section "$OPENCODE_DIR/AGENTS.md" "$SCRIPT_DIR/config/opencode-instructions-template.md" "OpenCode AGENTS.md"
}

# ─────────────────────────────────────────────
# OpenCode: install B12 skill
# ─────────────────────────────────────────────
install_opencode_skill() {
  local OPENCODE_DIR="$HOME/.config/opencode"
  local SKILL_DEST="$OPENCODE_DIR/skills/b12"

  mkdir -p "$SKILL_DEST"

  cat > "$SKILL_DEST/SKILL.md" << 'SKILLEOF'
---
name: b12
description: B12 persistent memory system — search, store, and manage cross-session memories
---

# B12 Memory Skill

Use B12 memory tools to persist knowledge across sessions.

## Tools

- `mcp__B12__memory_search` — Find past memories by query, tags, or semantic similarity
- `mcp__B12__memory_store` — Save decisions, patterns, errors, preferences
- `mcp__B12__memory_update` — Update existing memory metadata or tags
- `mcp__B12__memory_quality` — Check memory quality or system health

## Usage Pattern

1. At session start: search for project context
2. During work: store important findings as you go
3. Before session end: store a session summary

## Tagging

Always tag memories with:
- `proj:{directory_name}` — project scope
- `user:{username}` — user scope
- Type: `architecture`, `decision`, `pattern`, `gotcha`, `preference`, `progress`
SKILLEOF

  info "B12 skill installed to $SKILL_DEST/SKILL.md"
}

# ─────────────────────────────────────────────
# OpenCode: verify installation
# ─────────────────────────────────────────────
verify_opencode() {
  local errors=0
  local CONFIG_JSON="$HOME/.config/opencode/opencode.json"
  local AGENTS_MD="$HOME/.config/opencode/AGENTS.md"

  if [ -f "$CONFIG_JSON" ] && grep -q '"B12"' "$CONFIG_JSON" 2>/dev/null; then
    info "Verify: B12 MCP server configured in $CONFIG_JSON"
  else
    warn "Verify: B12 NOT found in $CONFIG_JSON"
    errors=$((errors + 1))
  fi

  if grep -q 'B12-MEMORY-START' "$AGENTS_MD" 2>/dev/null; then
    info "Verify: B12 instructions present in $AGENTS_MD"
  else
    warn "Verify: B12 instructions NOT found in $AGENTS_MD"
    errors=$((errors + 1))
  fi

  if [ -x "$VENV_PYTHON" ]; then
    info "Verify: B12 venv accessible at $VENV_PATH"
  else
    warn "Verify: B12 venv NOT found (OpenCode MCP server will fail)"
    errors=$((errors + 1))
  fi

  if [ -f "$HOME/.config/opencode/skills/b12/SKILL.md" ]; then
    info "Verify: B12 skill installed"
  else
    warn "Verify: B12 skill NOT found"
    errors=$((errors + 1))
  fi

  return $errors
}

# ═════════════════════════════════════════════
# Main
# ═════════════════════════════════════════════

echo "B12 Memory System Installer (v10.8.0 — multi-platform)"
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

# Disable set -e for platform setup: inject functions return 1 on warnings
# (e.g., "directory not found") which should not kill the script
set +e

# Codex CLI setup
if $INSTALL_CODEX; then
  echo ""
  echo "── Codex CLI Setup ──────────────"
  inject_codex_mcp_config
  inject_codex_agents
  install_codex_skill
  echo ""
fi

# Gemini CLI setup
if $INSTALL_GEMINI; then
  echo ""
  echo "── Gemini CLI Setup ─────────────"
  inject_gemini_mcp_config
  inject_gemini_instructions
  echo ""
fi

# VS Code / Copilot setup
if $INSTALL_VSCODE; then
  echo ""
  echo "── VS Code / Copilot Setup ──────"
  inject_vscode_mcp_config
  inject_vscode_instructions
  echo ""
fi

# Cursor setup
if $INSTALL_CURSOR; then
  echo ""
  echo "── Cursor Setup ─────────────────"
  inject_cursor_mcp_config
  inject_cursor_rules
  echo ""
fi

# Kimi Code setup
if $INSTALL_KIMI; then
  echo ""
  echo "── Kimi Code Setup ──────────────"
  inject_kimi_mcp_config
  inject_kimi_agents
  echo ""
fi

# Windsurf setup
if $INSTALL_WINDSURF; then
  echo ""
  echo "── Windsurf Setup ───────────────"
  inject_windsurf_mcp_config
  inject_windsurf_rules
  echo ""
fi

# Cline setup
if $INSTALL_CLINE; then
  echo ""
  echo "── Cline (VS Code) Setup ────────"
  inject_cline_mcp_config
  inject_cline_rules
  echo ""
fi

# OpenCode setup
if $INSTALL_OPENCODE; then
  echo ""
  echo "── OpenCode Setup ───────────────"
  inject_opencode_mcp_config
  inject_opencode_agents
  install_opencode_skill
  echo ""
fi

# Run migration
run_migration

echo ""
echo "─────────────────────────────────"

# Run verification (set +e already active from platform setup block above)
verify
VERIFY_RESULT=$?

# Platform verifications
if $INSTALL_CODEX; then
  echo ""
  echo "── Codex Verification ───────────"
  verify_codex
  VERIFY_RESULT=$((VERIFY_RESULT + $?))
fi

if $INSTALL_GEMINI; then
  echo ""
  echo "── Gemini Verification ──────────"
  verify_gemini
  VERIFY_RESULT=$((VERIFY_RESULT + $?))
fi

if $INSTALL_VSCODE; then
  echo ""
  echo "── VS Code Verification ─────────"
  verify_vscode
  VERIFY_RESULT=$((VERIFY_RESULT + $?))
fi

if $INSTALL_CURSOR; then
  echo ""
  echo "── Cursor Verification ──────────"
  verify_cursor
  VERIFY_RESULT=$((VERIFY_RESULT + $?))
fi

if $INSTALL_KIMI; then
  echo ""
  echo "── Kimi Verification ────────────"
  verify_kimi
  VERIFY_RESULT=$((VERIFY_RESULT + $?))
fi

if $INSTALL_WINDSURF; then
  echo ""
  echo "── Windsurf Verification ────────"
  verify_windsurf
  VERIFY_RESULT=$((VERIFY_RESULT + $?))
fi

if $INSTALL_CLINE; then
  echo ""
  echo "── Cline Verification ───────────"
  verify_cline
  VERIFY_RESULT=$((VERIFY_RESULT + $?))
fi

if $INSTALL_OPENCODE; then
  echo ""
  echo "── OpenCode Verification ────────"
  verify_opencode
  VERIFY_RESULT=$((VERIFY_RESULT + $?))
fi

set -e

echo ""
echo "─────────────────────────────────"

# Build dynamic list of installed platforms
PLATFORMS_INSTALLED=""
$INSTALL_CODEX && PLATFORMS_INSTALLED="$PLATFORMS_INSTALLED Codex"
$INSTALL_GEMINI && PLATFORMS_INSTALLED="$PLATFORMS_INSTALLED Gemini"
$INSTALL_VSCODE && PLATFORMS_INSTALLED="$PLATFORMS_INSTALLED VS-Code"
$INSTALL_CURSOR && PLATFORMS_INSTALLED="$PLATFORMS_INSTALLED Cursor"
$INSTALL_KIMI && PLATFORMS_INSTALLED="$PLATFORMS_INSTALLED Kimi"
$INSTALL_WINDSURF && PLATFORMS_INSTALLED="$PLATFORMS_INSTALLED Windsurf"
$INSTALL_CLINE && PLATFORMS_INSTALLED="$PLATFORMS_INSTALLED Cline"
$INSTALL_OPENCODE && PLATFORMS_INSTALLED="$PLATFORMS_INSTALLED OpenCode"

if [ $VERIFY_RESULT -eq 0 ]; then
  if [ -n "$PLATFORMS_INSTALLED" ]; then
    info "Installation complete! Restart Claude Code and$PLATFORMS_INSTALLED to activate B12."
  else
    info "Installation complete! Restart Claude Code to activate B12."
  fi
else
  warn "Installation complete with $VERIFY_RESULT warning(s). See above."
fi

# Show helpful tips
ANY_PLATFORM=false
$INSTALL_CODEX || $INSTALL_GEMINI || $INSTALL_VSCODE || $INSTALL_CURSOR || $INSTALL_KIMI || $INSTALL_WINDSURF || $INSTALL_CLINE || $INSTALL_OPENCODE && ANY_PLATFORM=true

if ! $FULL_SETUP && ! $ANY_PLATFORM; then
  echo ""
  echo "Tip: Run './install.sh --full' for automatic venv + MCP config setup."
  echo "     Flags: --codex --gemini --vscode --cursor --kimi --windsurf --cline --opencode"
fi

echo ""
if [ -n "$PLATFORMS_INSTALLED" ]; then
  echo "Next: Restart Claude Code and$PLATFORMS_INSTALLED, then run /mcp to verify B12 is connected."
else
  echo "Next: Restart Claude Code, then run /mcp to verify B12 is connected."
fi
