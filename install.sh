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
#   ./install.sh --health           # Run B12 health check diagnostics
#   ./install.sh --all --fix-drift  # Auto-register B12 on any detected
#                                   # non-Claude platform (Codex/Gemini/
#                                   # Kimi/Cursor/Windsurf/OpenCode/Grok)
#                                   # that's missing a B12 entry. Opt-in.
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
#   4. Installs B12 skill to ~/.codex/skills/b12-memory/
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
INSTALL_CONTINUE=false
INSTALL_GROK=false
INSTALL_DAEMON=false
UNINSTALL_DAEMON=false
INSTALL_SMOKE_CRON=false
UNINSTALL_SMOKE_CRON=false
FIX_DRIFT=false
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
    --continue)  INSTALL_CONTINUE=true ;;
    --grok)      INSTALL_GROK=true ;;
    --daemon)    INSTALL_DAEMON=true ;;
    --daemon-uninstall) UNINSTALL_DAEMON=true ;;
    --smoke-cron) INSTALL_SMOKE_CRON=true ;;
    --smoke-cron-uninstall) UNINSTALL_SMOKE_CRON=true ;;
    --fix-drift) FIX_DRIFT=true ;;
    --health)
      # Run health check and exit — only forward --json and --fix
      _health_args=()
      for _ha in "$@"; do
        [[ "$_ha" == "--json" || "$_ha" == "--fix" ]] && _health_args+=("$_ha")
      done
      if [ -x "$VENV_PYTHON" ]; then
        "$VENV_PYTHON" "$SCRIPT_SOURCE/b12_health.py" "${_health_args[@]}"
      elif command -v python3 &>/dev/null; then
        python3 "$SCRIPT_SOURCE/b12_health.py" "${_health_args[@]}"
      else
        echo "Python 3 required for health check"
        exit 1
      fi
      exit $?
      ;;
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
  mkdir -p "$HOME/.B12/memory-state"

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
# Step 1b: Seed ~/.B12/config.toml from template (first-run only).
# Never overwrites — user edits persist across upgrades.
# Source-of-truth: config/b12-config-template.toml
# ─────────────────────────────────────────────
seed_user_config() {
  # Codex review PR #43 round 4 P2: scripts/b12_config.py reads
  # config.toml from $B12_DATA_DIR when set (else falls back to
  # ~/.B12/). Seed to whichever directory the daemon will actually
  # read, so a custom B12_DATA_DIR setup gets a working template.
  local data_dir="${B12_DATA_DIR:-$HOME/.B12}"
  local user_config="$data_dir/config.toml"
  local template="$HOOK_SOURCE/../config/b12-config-template.toml"
  if [ ! -f "$template" ]; then
    template="$(cd "$(dirname "$0")" 2>/dev/null && pwd)/config/b12-config-template.toml"
  fi
  if [ ! -f "$template" ]; then
    return 0  # template absent → silent skip; daemon handles missing config fine
  fi
  mkdir -p "$data_dir" 2>/dev/null || true
  if [ ! -f "$user_config" ]; then
    cp "$template" "$user_config"
    info "Seeded user config at $user_config (from template)"
  fi
}

# ─────────────────────────────────────────────
# 24h smoke cron (Plan §C13) — opt-in via --smoke-cron.
# Registers a daily cron entry that runs b12_smoke.sh against every
# detected ~/.claude* setup. Uses the user crontab (no launchctl admin
# write), so it's fully reversible by `--smoke-cron-uninstall`.
# ─────────────────────────────────────────────
_smoke_cron_marker="# B12_SMOKE_CRON v1 — managed by install.sh; remove via './install.sh --smoke-cron-uninstall'"

install_smoke_cron() {
  local script_path="$SCRIPT_DEST/b12_smoke.sh"
  if [ ! -x "$script_path" ]; then
    warn "Smoke script not deployed at $script_path. Run './install.sh --all' first."
    return 1
  fi
  local current
  current=$(crontab -l 2>/dev/null || true)
  if printf '%s' "$current" | grep -qF "$_smoke_cron_marker"; then
    info "Smoke cron already installed (skipping)"
    return 0
  fi
  # 03:17 daily — off-peak, unlikely to clash with backup/consolidate plists.
  local entry="17 3 * * * $script_path >/dev/null 2>&1"
  {
    printf '%s\n' "$current"
    printf '%s\n' "$_smoke_cron_marker"
    printf '%s\n' "$entry"
  } | crontab -
  info "Installed smoke cron entry: $entry"
}

uninstall_smoke_cron() {
  local current
  current=$(crontab -l 2>/dev/null || true)
  if ! printf '%s' "$current" | grep -qF "$_smoke_cron_marker"; then
    info "Smoke cron not installed (nothing to remove)"
    return 0
  fi
  # Codex review PR #43 round 4 P2: drop the marker line + ONE entry
  # line that immediately follows. Previously set skip=2 after matching
  # the marker, but `next` had already consumed it — so awk also dropped
  # an unrelated user cron line after the B12 entry.
  printf '%s\n' "$current" \
    | awk -v marker="$_smoke_cron_marker" '
        skip > 0     { skip--; next }
        $0 == marker { skip = 1; next }
                     { print }
      ' \
    | crontab -
  info "Removed smoke cron entry"
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
  # Shared hook helper (b12_async_fork, b12_sync_watchdog, b12_should_skip_trivial)
  # sourced by memory-retrieval / memory-proactive-surface / memory-checkpoint /
  # memory-working-context / memory-feedback as of v11.34 / P-SPEED.
  if [ -f "$HOOK_SOURCE/_b12_common.sh" ]; then
    cp "$HOOK_SOURCE/_b12_common.sh" "$HOOK_DEST/"
    chmod +x "$HOOK_DEST/_b12_common.sh"
    count=$((count + 1))
  fi
  # Copy Codex notify hook if present
  if [ -f "$HOOK_SOURCE/b12-codex-notify.sh" ]; then
    cp "$HOOK_SOURCE/b12-codex-notify.sh" "$HOOK_DEST/"
    chmod +x "$HOOK_DEST/b12-codex-notify.sh"
    count=$((count + 1))
  fi
  # Codex spillover helper sourced by memory-codex-session-start.sh.
  # Codex review on PR #41 round 3 caught: the memory-*.sh glob above
  # does not match _b12_codex_spillover.sh (no `memory-` prefix), so
  # SessionStart's fallback `EMIT="$BODY"` shipped the raw payload and
  # bypassed the ~9.2KB direct-emit safeguard for issue #22861.
  if [ -f "$HOOK_SOURCE/_b12_codex_spillover.sh" ]; then
    cp "$HOOK_SOURCE/_b12_codex_spillover.sh" "$HOOK_DEST/"
    chmod +x "$HOOK_DEST/_b12_codex_spillover.sh"
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
  for f in "$SCRIPT_SOURCE"/*.py "$SCRIPT_SOURCE"/*.json; do
    [ -f "$f" ] || continue
    local base=$(basename "$f")
    case " $EXCLUDE " in *" $base "*) continue ;; esac
    cp "$f" "$SCRIPT_DEST/"
    count=$((count + 1))
  done
  # Smoke harness — bash script, not matched by the *.py glob above.
  if [ -f "$SCRIPT_SOURCE/b12_smoke.sh" ]; then
    cp "$SCRIPT_SOURCE/b12_smoke.sh" "$SCRIPT_DEST/"
    chmod +x "$SCRIPT_DEST/b12_smoke.sh"
    count=$((count + 1))
  fi
  # Make MCP server executable
  if [ -f "$SCRIPT_DEST/b12_mcp_server.py" ]; then
    chmod +x "$SCRIPT_DEST/b12_mcp_server.py"
  fi
  if [ "$count" -gt 0 ]; then
    info "Copied $count support scripts to $SCRIPT_DEST"
  fi

  # Copy ML models (classifier head etc.)
  local MODEL_SOURCE="$SCRIPT_DIR/models"
  local MODEL_DEST="$HOME/.B12/models"
  if [ -d "$MODEL_SOURCE" ]; then
    mkdir -p "$MODEL_DEST"
    local mcount=0
    for f in "$MODEL_SOURCE"/*.pkl "$MODEL_SOURCE"/*.onnx; do
      [ -f "$f" ] || continue
      cp "$f" "$MODEL_DEST/"
      mcount=$((mcount + 1))
    done
    if [ "$mcount" -gt 0 ]; then
      info "Copied $mcount ML models to $MODEL_DEST"
    fi
  fi
}

# ─────────────────────────────────────────────
# Step 3b: Update launchd plists (macOS only)
# Migrates ~/.claude/hooks/ → ~/.B12/hooks/
# and ~/.claude/memory-logs/ → ~/.B12/memory-logs/
# ─────────────────────────────────────────────
update_launchd_plists() {
  [ "$(uname)" = "Darwin" ] || return 0

  local LAUNCH_DIR="$HOME/Library/LaunchAgents"
  [ -d "$LAUNCH_DIR" ] || return 0

  local updated=0
  for plist in "$LAUNCH_DIR"/com.b12.*.plist; do
    [ -f "$plist" ] || continue
    local changed=false

    # Check for old ~/.claude/hooks/ paths
    if grep -q "$HOME/.claude/hooks/" "$plist" 2>/dev/null; then
      sed -i '' "s|$HOME/.claude/hooks/|$HOME/.B12/hooks/|g" "$plist"
      changed=true
    fi

    # Check for old ~/.claude/memory-logs/ paths
    if grep -q "$HOME/.claude/memory-logs/" "$plist" 2>/dev/null; then
      sed -i '' "s|$HOME/.claude/memory-logs/|$HOME/.B12/memory-logs/|g" "$plist"
      changed=true
    fi

    if $changed; then
      # Reload the plist so launchd picks up new paths
      launchctl unload "$plist" 2>/dev/null || true
      launchctl load "$plist" 2>/dev/null || true
      updated=$((updated + 1))
    fi
  done

  if [ "$updated" -gt 0 ]; then
    info "Updated $updated launchd plist(s): ~/.claude/ → ~/.B12/ paths"
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

  echo "Installing dependencies (mcp, sentence-transformers, sqlite-vec, fsrs)..."
  "$VENV_PYTHON" -m pip install --quiet mcp sentence-transformers sqlite-vec fsrs || error "pip install failed"
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
        "MCP_EMBEDDING_MODEL": "BAAI/bge-m3",
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

  # Run Ebbinghaus migration (adds strength, last_accessed_at, valid_until columns)
  python3 "$SCRIPT_DIR/scripts/migrate_ebbinghaus.py" "$DB_PATH" 2>/dev/null || true
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
MCP_EMBEDDING_MODEL = "BAAI/bge-m3"
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
# Codex CLI: [hooks.state] trusted-hash pre-pin — DEFERRED.
#
# Round 0 fix #1 in Plan §2 originally proposed pre-pinning SHA-256
# entries into ~/.codex/config.toml [hooks.state] so the first session
# would skip Codex's StartupHooksReview prompt-trust flow. Codex review
# on PR #41 round 1 (2026-05-18) flagged two correctness gaps:
#
#  • P1 (cosmetic): the initial key shape used the hook script path,
#    but Codex's runtime keys hook entries by `<source_path>:
#    <event_label>:<group_index>:<handler_index>` (codex-rs/hooks/src/
#    lib.rs:91 hook_key()).
#  • P1 (substantive): the initial value used a raw file-body SHA-256,
#    but Codex's `trusted_hash` value is `sha256:<hex>` over the
#    canonical JSON of the NormalizedHookIdentity TOML
#    (codex-rs/hooks/src/engine/discovery.rs:543 + codex-rs/config/src/
#    fingerprint.rs:37 version_for_toml).
#
# Reproducing version_for_toml's canonical JSON serialization in
# Python without bit-for-bit drift is risky — any divergence yields
# `HookTrustStatus::Modified`, which is exactly the StartupHooksReview
# friction the pin was meant to skip. Until B12 has a Rust-side
# fixture test that pins the canonical JSON shape (so installer
# changes can't silently break trust matching), this function is a
# no-op that logs the deferral so users know to expect a one-time
# trust prompt per hook on first session.
#
# Tracked in plan doc CX-followup section.
# ─────────────────────────────────────────────
inject_codex_hooks_state() {
  local CONFIG_TOML="$HOME/.codex/config.toml"
  [ -f "$CONFIG_TOML" ] || return 0

  local pending=0
  for f in "$HOOK_DEST"/memory-codex-*.sh; do
    [ -f "$f" ] || continue
    pending=$((pending + 1))
  done

  if [ "$pending" -eq 0 ]; then
    return 0
  fi

  info "Codex hooks deployed ($pending file(s)). First-session trust prompt"
  info "  is expected — Round 0 trust-hash pre-pin is deferred to a"
  info "  follow-up PR (canonical-JSON fixture needed). After accepting"
  info "  the StartupHooksReview prompts, trust is cached in"
  info "  $CONFIG_TOML [hooks.state]."
}

# ─────────────────────────────────────────────
# Codex CLI: merge memory-codex-*.sh into ~/.codex/hooks.json.
# CX1 — registers SessionStart, UserPromptSubmit, Stop. CX2 will extend
# this block with PreToolUse / PostToolUse / PreCompact entries.
#
# Idempotent: any existing B12-managed entry (identifiable by the
# memory-codex- substring in the command) is removed before re-insert.
# Non-B12 entries (e.g., user's own Superset notify hook) are preserved
# verbatim. Plan §2 / CX-C4.
# ─────────────────────────────────────────────
register_codex_hooks_json() {
  local CODEX_DIR="$HOME/.codex"
  local HOOKS_JSON="$CODEX_DIR/hooks.json"
  [ -d "$CODEX_DIR" ] || { warn "Codex directory not found at $CODEX_DIR — skipping hooks.json"; return 1; }

  # Verify deployed hooks exist; without them the json entries point at
  # missing files and Codex's StartupHooksReview will flag them.
  # CX2 expands the required set to include PreToolUse/PostToolUse/
  # PreCompact scripts.
  local missing=0
  for h in memory-codex-session-start.sh memory-codex-prompt-submit.sh \
           memory-codex-stop.sh memory-codex-pre-tool.sh \
           memory-codex-post-tool.sh memory-codex-pre-compact.sh; do
    if [ ! -x "$HOOK_DEST/$h" ]; then
      warn "Codex hook missing at $HOOK_DEST/$h — run copy_hooks first"
      missing=$((missing + 1))
    fi
  done
  [ "$missing" -gt 0 ] && return 1

  python3 - "$HOOKS_JSON" "$HOOK_DEST" << 'PYEOF'
import json, os, sys

hooks_path = sys.argv[1]
hook_dest = sys.argv[2]

# Load or initialize. Codex tolerates a missing file but is strict on
# malformed JSON — we treat a parse failure as "start fresh" so an
# install does not wedge on a stale half-written config.
try:
    with open(hooks_path, 'r') as fh:
        data = json.load(fh)
except (FileNotFoundError, json.JSONDecodeError):
    data = {}

if not isinstance(data, dict):
    data = {}
data.setdefault('hooks', {})

# Event → (script-name, matcher, timeout_sec). The keys are exactly
# what codex-rs/hooks/src/lib.rs:18 HOOK_EVENT_NAMES declares. CX2
# added PreToolUse/PostToolUse/PreCompact entries.
#
# Timeout policy (CLAUDE.md "hook timeout >= watchdog + 5s"):
#   - SessionStart, UserPromptSubmit, Stop, PreToolUse, PostToolUse →
#     20s; their work is bounded (DB read, prompt regex, telemetry log).
#   - PreCompact → 30s; the delegated memory-precompact.sh runs a 25s
#     watchdog timer of its own, so the outer Codex timeout must be
#     watchdog+5 = 30s to avoid Codex killing PreCompact early
#     (Codex review PR #42 round 2 P1).
plan = [
    ('SessionStart',     'memory-codex-session-start.sh', None,                              20),
    ('UserPromptSubmit', 'memory-codex-prompt-submit.sh', None,                              20),
    ('Stop',             'memory-codex-stop.sh',          None,                              20),
    # PreToolUse matcher targets B12's MCP store tool. mcp_* handlers
    # opt into pre_tool_use_payload (codex-rs/core/src/tools/handlers/
    # mcp.rs:173), so this matcher will fire reliably.
    ('PreToolUse',       'memory-codex-pre-tool.sh',      'mcp__B12__memory_store',          20),
    # PostToolUse matcher targets file-modification tools that opt in.
    # `cloud_exec`/`cloud_apply` are NOT tool handlers (they're CLI
    # subcommands under `codex cloud`); the CLI↔App bridge is captured
    # via rollout-scrape in codex_session_end.py instead.
    ('PostToolUse',      'memory-codex-post-tool.sh',     'shell|apply_patch|unified_exec',  20),
    ('PreCompact',       'memory-codex-pre-compact.sh',   None,                              30),
]

def is_b12_entry(entry):
    """Identify a hook entry that B12 previously inserted."""
    if not isinstance(entry, dict):
        return False
    for sub in entry.get('hooks', []):
        if isinstance(sub, dict) and 'memory-codex-' in str(sub.get('command', '')):
            return True
    return False

for event_name, script, matcher, timeout_sec in plan:
    arr = data['hooks'].get(event_name, [])
    if not isinstance(arr, list):
        arr = []
    # Drop any prior B12 entry; preserve everything else verbatim.
    arr = [e for e in arr if not is_b12_entry(e)]
    entry = {
        'hooks': [
            {
                'type': 'command',
                # Codex hook timeouts are in SECONDS (Codex review on PR
                # #41 round 1, 2026-05-18 — initial value 20000 turned
                # into a ~5.5h hang per stuck hook). Claude Code uses
                # milliseconds for the same field name; do not copy a
                # value across without a unit check.
                'command': os.path.join(hook_dest, script),
                'timeout': timeout_sec,
            }
        ]
    }
    # Matcher syntax is per Codex docs: top-level "matcher" key on the
    # group object, only meaningful for events in HOOK_EVENT_NAMES_WITH_
    # MATCHERS (codex-rs/hooks/src/lib.rs:34).
    if matcher is not None:
        entry['matcher'] = matcher
    arr.append(entry)
    data['hooks'][event_name] = arr

with open(hooks_path, 'w') as fh:
    json.dump(data, fh, indent=2)
    fh.write('\n')
PYEOF

  info "Registered 3 B12 hook(s) in $HOOKS_JSON (SessionStart, UserPromptSubmit, Stop)"
}

# ─────────────────────────────────────────────
# Codex CLI: warn if live Codex sessions are running. Round 0 fix #9 /
# Plan §2. Issue #21160 (2026-05-05) — editing hooks.json or config.toml
# during a live session silent-disables ALL hooks for the rest of that
# session. B12's documented workflow (`./install.sh --all` while sessions
# are live) WILL silently break Codex unless we tell the user.
# ─────────────────────────────────────────────
warn_live_codex_sessions() {
  # `pgrep -f` matches against full command line; covers both
  # `codex` (interactive) and `codex exec ...` (one-shot).
  local pids
  pids=$(pgrep -f '(^|/)codex( |$|-)' 2>/dev/null | tr '\n' ' ')
  if [ -n "$pids" ]; then
    warn "Live Codex process(es) detected (PIDs:$pids)"
    warn "  Issue #21160: editing hooks.json / config.toml while a session is"
    warn "  live silent-disables ALL hooks for the rest of that session."
    warn "  Restart any open Codex sessions to pick up the new hook config."
  fi
}

# ─────────────────────────────────────────────
# Codex CLI: install B12 skill
# ─────────────────────────────────────────────
install_codex_skill() {
  local CODEX_DIR="$HOME/.codex"
  local SKILL_SRC="$SCRIPT_DIR/skills/b12-memory"
  local SKILL_DEST="$CODEX_DIR/skills/b12-memory"
  local LEGACY_DEST="$CODEX_DIR/skills/b12"

  if [ ! -d "$CODEX_DIR" ]; then
    return
  fi

  if [ ! -f "$SKILL_SRC/SKILL.md" ]; then
    warn "B12 skill template not found at $SKILL_SRC/SKILL.md"
    return
  fi

  # Legacy cleanup: prior installer versions wrote the B12 skill (with
  # `name: b12-memory`) to ~/.codex/skills/b12/. Without this cleanup,
  # Codex sees two SKILL.md files declaring the same name and may
  # silently pick the older one — defeating the collision fix.
  #
  # Two-tier fingerprint:
  #   (a) BYTE-IDENTICAL to the current install source → safe to delete.
  #       Covers users who installed the latest skill at the legacy
  #       path manually or via a future re-run.
  #   (b) Declares `name: b12-memory` AND contains the canonical B12
  #       header "B12 Memory System" → installer-generated (matches
  #       all prior installer revisions that shipped this skill).
  #       Safe to delete even when content differs, because both
  #       markers together are extremely unlikely in a user-authored
  #       skill.
  #
  # Files that match (a) or (b) → removed. Files that declare only
  # `name: b12-memory` (without the B12 header) → preserved with a
  # warn() so the user inspects manually. Codex review on PR #18
  # rounds 2-3 walked this tradeoff: byte-identical alone misses the
  # upgrade case from prior installer revisions, name-only deletes
  # user content.
  if [ -f "$LEGACY_DEST/SKILL.md" ]; then
    if cmp -s "$LEGACY_DEST/SKILL.md" "$SKILL_SRC/SKILL.md"; then
      rm -f "$LEGACY_DEST/SKILL.md"
      rmdir "$LEGACY_DEST" 2>/dev/null || true
      info "Removed legacy skill at $LEGACY_DEST (byte-identical to current install)"
    elif grep -q '^name: b12-memory$' "$LEGACY_DEST/SKILL.md" 2>/dev/null \
         && grep -q 'B12 Memory System' "$LEGACY_DEST/SKILL.md" 2>/dev/null; then
      rm -f "$LEGACY_DEST/SKILL.md"
      rmdir "$LEGACY_DEST" 2>/dev/null || true
      info "Removed legacy skill at $LEGACY_DEST (B12-installer fingerprint matched)"
    elif grep -q '^name: b12-memory$' "$LEGACY_DEST/SKILL.md" 2>/dev/null; then
      warn "Legacy skill at $LEGACY_DEST/SKILL.md has \`name: b12-memory\` but does"
      warn "not match the B12 installer fingerprint. Inspect manually — if it's a"
      warn "user-authored skill, rename its frontmatter; if it's stale installer"
      warn "output, delete the file."
    fi
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

  # CX1+CX2 hooks registered? Expected 6: SessionStart, UserPromptSubmit,
  # Stop, PreToolUse, PostToolUse, PreCompact.
  local HOOKS_JSON="$HOME/.codex/hooks.json"
  if [ -f "$HOOKS_JSON" ]; then
    local registered
    registered=$(python3 - "$HOOKS_JSON" << 'PYEOF' 2>/dev/null
import json, sys
try:
    data = json.load(open(sys.argv[1]))
except Exception:
    print(0); sys.exit(0)
count = 0
for evt in ('SessionStart', 'UserPromptSubmit', 'Stop', 'PreToolUse', 'PostToolUse', 'PreCompact'):
    for entry in data.get('hooks', {}).get(evt, []):
        for sub in entry.get('hooks', []):
            if 'memory-codex-' in str(sub.get('command', '')):
                count += 1
                break
print(count)
PYEOF
)
    if [ "$registered" = "6" ]; then
      info "Verify: 6 B12 Codex hooks registered in $HOOKS_JSON"
    else
      warn "Verify: expected 6 Codex hook entries in $HOOKS_JSON, found ${registered:-0}"
      errors=$((errors + 1))
    fi
  fi

  # Check B12 skill installed (canonical path is skills/b12-memory/ since the
  # b12-memory name-collision cleanup; older installs may still have
  # ~/.codex/skills/b12/ on disk — harmless, will be ignored)
  if [ -f "$HOME/.codex/skills/b12-memory/SKILL.md" ]; then
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
        'MCP_EMBEDDING_MODEL': 'BAAI/bge-m3',
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
# Gemini CLI: install B12 hook adapters
# ─────────────────────────────────────────────
install_gemini_hooks() {
  local GEMINI_DIR="$HOME/.gemini"
  local SETTINGS_JSON="$GEMINI_DIR/settings.json"
  local GEMINI_HOOK_DEST="$HOOK_DEST/gemini"
  local GEMINI_HOOK_SRC="$HOOK_SOURCE/gemini"

  if [ ! -d "$GEMINI_DIR" ]; then
    warn "Gemini directory not found at $GEMINI_DIR — skipping hook installation"
    return 1
  fi

  if [ ! -d "$GEMINI_HOOK_SRC" ]; then
    warn "Gemini hook adapters not found at $GEMINI_HOOK_SRC"
    return 1
  fi

  # Copy adapter scripts to shared B12 hooks location
  mkdir -p "$GEMINI_HOOK_DEST"
  cp "$GEMINI_HOOK_SRC"/b12-gemini-*.sh "$GEMINI_HOOK_DEST/"
  chmod +x "$GEMINI_HOOK_DEST"/b12-gemini-*.sh
  info "Gemini hook adapters copied to $GEMINI_HOOK_DEST"

  # Register hooks in ~/.gemini/settings.json
  if [ ! -f "$SETTINGS_JSON" ]; then
    echo '{}' > "$SETTINGS_JSON"
  fi

  if ! python3 - "$SETTINGS_JSON" "$GEMINI_HOOK_DEST" << 'PYEOF'
import sys, json

settings_path = sys.argv[1]
hook_dir = sys.argv[2]

with open(settings_path, 'r') as f:
    try:
        settings = json.load(f)
    except json.JSONDecodeError:
        settings = {}

if 'hooks' not in settings:
    settings['hooks'] = {}

# SessionStart hook
settings['hooks']['SessionStart'] = [{
    "hooks": [{
        "name": "b12-session-start",
        "type": "command",
        "command": f"{hook_dir}/b12-gemini-session-start.sh",
        "timeout": 20000,
        "description": "B12 memory system — inject session context"
    }]
}]

# SessionEnd hook
settings['hooks']['SessionEnd'] = [{
    "hooks": [{
        "name": "b12-session-end",
        "type": "command",
        "command": f"{hook_dir}/b12-gemini-session-end.sh",
        "timeout": 35000,
        "description": "B12 memory system — save session summary"
    }]
}]

# AfterTool hook (memory retrieval on built-in tool calls)
settings['hooks']['AfterTool'] = [{
    "matcher": "read_file|list_directory|run_shell_command|write_file|search_files",
    "hooks": [{
        "name": "b12-tool-retrieval",
        "type": "command",
        "command": f"{hook_dir}/b12-gemini-tool-call.sh",
        "timeout": 10000,
        "description": "B12 memory system — contextual memory retrieval"
    }]
}]

with open(settings_path, 'w') as f:
    json.dump(settings, f, indent=2)
    f.write('\n')

PYEOF
  then
    error "Failed to register B12 hooks in $SETTINGS_JSON"
  fi

  info "B12 hooks registered in $SETTINGS_JSON"
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

  if python3 -c "import json; d=json.load(open('$SETTINGS_JSON')); assert 'SessionStart' in d.get('hooks', {})" 2>/dev/null; then
    info "Verify: B12 hooks registered in $SETTINGS_JSON"
  else
    warn "Verify: B12 hooks NOT found in $SETTINGS_JSON (run with --gemini to install)"
    errors=$((errors + 1))
  fi

  if [ -f "$HOME/.B12/hooks/gemini/b12-gemini-session-start.sh" ]; then
    info "Verify: Gemini hook adapters installed"
  else
    warn "Verify: Gemini hook adapters NOT found"
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
        "MCP_EMBEDDING_MODEL": "BAAI/bge-m3",
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
        'MCP_EMBEDDING_MODEL': 'BAAI/bge-m3',
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
        "MCP_EMBEDDING_MODEL": "BAAI/bge-m3",
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
        'MCP_EMBEDDING_MODEL': 'BAAI/bge-m3',
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
        "MCP_EMBEDDING_MODEL": "BAAI/bge-m3",
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
# Continue.dev: MCP config injection. Continue uses ~/.continue/ as the
# shared config root across CLI ('cn'), VS Code extension, and JetBrains
# plugin — one write covers all three surfaces. MCP servers live in
# ~/.continue/mcpServers/*.yaml (preferred per docs) or ~/.continue/
# mcp.json (older shape).
# ─────────────────────────────────────────────
inject_continue_mcp_config() {
  local CONTINUE_DIR="$HOME/.continue"
  local MCP_DIR="$CONTINUE_DIR/mcpServers"
  local MCP_FILE="$MCP_DIR/b12.yaml"
  local TEMPLATE="$SCRIPT_DIR/config/continue-mcp-template.yaml"
  local SERVER_SCRIPT="$SCRIPT_DEST/b12_mcp_server.py"

  if [ ! -d "$CONTINUE_DIR" ]; then
    warn "Continue.dev not found at $CONTINUE_DIR — install Continue (CLI 'cn' or extension) first"
    return 1
  fi
  if [ ! -x "$VENV_PYTHON" ]; then
    warn "Venv Python not found at $VENV_PYTHON"
    warn "Run with --full to create the venv first: ./install.sh --full --continue"
    return 1
  fi
  if [ ! -f "$SERVER_SCRIPT" ]; then
    warn "MCP server script not found at $SERVER_SCRIPT — run standard install first"
    return 1
  fi
  if [ ! -f "$TEMPLATE" ]; then
    warn "Continue MCP template not found at $TEMPLATE"
    return 1
  fi

  mkdir -p "$MCP_DIR"
  sed -e "s|__VENV_PYTHON__|$VENV_PYTHON|g" \
      -e "s|__SCRIPT_PATH__|$SERVER_SCRIPT|g" \
      "$TEMPLATE" > "$MCP_FILE"
  info "B12 MCP server configured at $MCP_FILE"
  echo "     command: $VENV_PYTHON"
  echo "     script:  $SERVER_SCRIPT"
}

# ─────────────────────────────────────────────
# Continue.dev: install behavioral rules into ~/.continue/rules/.
# ─────────────────────────────────────────────
inject_continue_rules() {
  local TEMPLATE="$SCRIPT_DIR/config/continue-instructions-template.md"
  local RULES_DIR="$HOME/.continue/rules"
  if [ ! -f "$TEMPLATE" ]; then
    warn "Continue rules template not found at $TEMPLATE"
    return 1
  fi
  mkdir -p "$RULES_DIR"
  cp "$TEMPLATE" "$RULES_DIR/b12-memory.md"
  info "B12 rules installed to $RULES_DIR/b12-memory.md"
}

# ─────────────────────────────────────────────
# Continue.dev: verify
# ─────────────────────────────────────────────
verify_continue() {
  local MCP_FILE="$HOME/.continue/mcpServers/b12.yaml"
  local RULES="$HOME/.continue/rules/b12-memory.md"
  local errors=0
  if [ -f "$MCP_FILE" ] && grep -q '^name: B12' "$MCP_FILE" 2>/dev/null; then
    info "Verify: B12 MCP server configured for Continue.dev"
  else
    warn "Verify: B12 NOT found at $MCP_FILE"
    errors=$((errors + 1))
  fi
  if [ -f "$RULES" ]; then
    info "Verify: B12 rules present at $RULES"
  else
    warn "Verify: B12 rules NOT found at $RULES"
    errors=$((errors + 1))
  fi
  return "$errors"
}

# ─────────────────────────────────────────────
# Cline: deploy lifecycle hook shims to ~/Documents/Cline/Hooks/.
# Codex review PR #47 P1: global Cline hooks live at
# ~/Documents/Cline/Hooks/, NOT ~/.cline/hooks/ (verified against
# cline/cline:.clinerules/hooks/README.md). Hook files must have NO
# extension and start with a shebang line. Each shim delegates to the
# matching B12 hook and translates `additionalContext` ↔ Cline's
# `contextModification` (camelCase JSON wire key, NOT proto's snake_case).
# Ships: TaskStart, UserPromptSubmit, PreCompact (passive placeholder
# until Cline finalizes PreCompact wire shape upstream).
# Deferred to a focused follow-up: TaskComplete (needs Cline transcript
# adapter for ~/Library/.../tasks/<id>/api_conversation_history.json).
# ─────────────────────────────────────────────
inject_cline_hooks() {
  local TEMPLATE_DIR="$SCRIPT_DIR/config/cline-hooks"
  local CLINE_HOOK_DIR="$HOME/Documents/Cline/Hooks"
  if [ ! -d "$TEMPLATE_DIR" ]; then
    warn "Cline hooks template dir not found at $TEMPLATE_DIR"
    return 1
  fi
  mkdir -p "$CLINE_HOOK_DIR"
  local n=0
  for shim in "$TEMPLATE_DIR"/TaskStart \
              "$TEMPLATE_DIR"/UserPromptSubmit \
              "$TEMPLATE_DIR"/PreCompact; do
    [ -f "$shim" ] || continue
    cp "$shim" "$CLINE_HOOK_DIR/"
    chmod +x "$CLINE_HOOK_DIR/$(basename "$shim")"
    n=$((n + 1))
  done
  info "Deployed $n Cline hook shims to $CLINE_HOOK_DIR"
  info "Enable Cline hooks: VSCode → Cline settings → Feature Settings → check 'Enable Hooks'"
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
        'MCP_EMBEDDING_MODEL': 'BAAI/bge-m3',
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

- `mcp__B12__memory_session_context` — Get session start context (project memories, last summary, instructions)
- `mcp__B12__memory_search` — Find past memories by query, tags, or semantic similarity
- `mcp__B12__memory_store` — Save decisions, patterns, errors, preferences
- `mcp__B12__memory_update` — Update existing memory metadata or tags
- `mcp__B12__memory_quality` — Check memory quality or system health

## Usage Pattern

1. At session start: call `mcp__B12__memory_session_context(project_name="<project>")` first
2. Search for additional project context as needed
3. During work: store important findings as you go
4. Before session end: store a session summary

## Tagging

Always tag memories with:
- `proj:{directory_name}` — project scope
- `user:{username}` — user scope
- Type: `architecture`, `decision`, `pattern`, `gotcha`, `preference`, `progress`
SKILLEOF

  info "B12 skill installed to $SKILL_DEST/SKILL.md"
}

# ─────────────────────────────────────────────
# OpenCode: Build TypeScript Plugin to single JS file
# ─────────────────────────────────────────────
build_opencode_plugin() {
  local PLUGIN_SRC="$SCRIPT_DIR/plugins/opencode"

  # Check bun is installed
  if ! command -v bun >/dev/null 2>&1; then
    warn "Bun not found — OpenCode plugin requires Bun to build"
    warn "Install with: curl -fsSL https://bun.sh/install | bash"
    return 1
  fi

  # Verify plugin source exists
  if [ ! -f "$PLUGIN_SRC/src/index.ts" ]; then
    warn "Plugin source not found at $PLUGIN_SRC/src/index.ts"
    return 1
  fi

  # Build to dist/index.js (single bundled file)
  info "Building OpenCode plugin with Bun..."
  (
    cd "$PLUGIN_SRC" || exit 1
    bun build src/index.ts --outdir dist --target bun --external better-sqlite3 2>&1 | tail -10
  )

  if [ ! -f "$PLUGIN_SRC/dist/index.js" ]; then
    warn "Bun build failed — dist/index.js not created"
    return 1
  fi

  info "OpenCode plugin built successfully"
  return 0
}

# ─────────────────────────────────────────────
# OpenCode: Deploy compiled plugin (index.js) + inject into config
# ─────────────────────────────────────────────
deploy_opencode_plugin() {
  local PLUGIN_SRC="$SCRIPT_DIR/plugins/opencode"
  local PLUGIN_DEST="$HOME/.config/opencode/plugins/b12"
  local COMPILED_JS="$PLUGIN_SRC/dist/index.js"

  # Verify compiled JS exists (build_opencode_plugin must run first)
  if [ ! -f "$COMPILED_JS" ]; then
    warn "Compiled plugin not found at $COMPILED_JS — run build first"
    return 1
  fi

  # Clean destination and old b12-memory plugin (avoid confusion)
  rm -rf "$PLUGIN_DEST" 2>/dev/null || true
  rm -rf "$HOME/.config/opencode/plugins/b12-memory" 2>/dev/null || true
  mkdir -p "$PLUGIN_DEST"

  # Copy compiled index.js (OpenCode loads this directly)
  cp "$COMPILED_JS" "$PLUGIN_DEST/index.js"

  # Copy package.json for dependency info (optional, OpenCode uses bundled)
  cp "$PLUGIN_SRC/package.json" "$PLUGIN_DEST/package.json" 2>/dev/null || true

  info "B12 OpenCode plugin deployed to $PLUGIN_DEST/index.js"
  return 0
}

# ─────────────────────────────────────────────
# OpenCode: Inject plugin path into opencode.json
# ─────────────────────────────────────────────
inject_opencode_plugin_config() {
  local OPENCODE_DIR="$HOME/.config/opencode"
  local CONFIG_JSON="$OPENCODE_DIR/opencode.json"
  local PLUGIN_PATH="./plugins/b12"

  if [ ! -d "$OPENCODE_DIR" ]; then
    warn "OpenCode config directory not found at $OPENCODE_DIR"
    return 1
  fi

  if [ ! -f "$CONFIG_JSON" ]; then
    echo '{}' > "$CONFIG_JSON"
    info "Created $CONFIG_JSON"
  fi

  if ! python3 - "$CONFIG_JSON" "$PLUGIN_PATH" << 'PYEOF'
import sys, json

config_path = sys.argv[1]
plugin_path = sys.argv[2]

with open(config_path, 'r') as f:
    content = f.read().strip()
    if not content:
        content = '{}'
    config = json.loads(content)

if 'plugin' not in config:
    config['plugin'] = []

# Remove old b12-memory or b12 entries
config['plugin'] = [p for p in config['plugin'] if 'b12-memory' not in p and p != './plugins/b12' and p != 'b12']

# Add our plugin
if plugin_path not in config['plugin']:
    config['plugin'].append(plugin_path)

with open(config_path, 'w') as f:
    json.dump(config, f, indent=2)
    f.write('\n')

print(f"Added {plugin_path} to plugin list")
PYEOF
  then
    warn "Failed to update plugin config in $CONFIG_JSON"
    return 1
  fi

  info "B12 OpenCode plugin registered in $CONFIG_JSON"
  return 0
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

  # Plugin deploy check (compiled index.js + registered in config)
  if [ -f "$HOME/.config/opencode/plugins/b12/index.js" ]; then
    info "Verify: B12 OpenCode plugin compiled at ~/.config/opencode/plugins/b12/index.js"
  else
    warn "Verify: B12 OpenCode plugin NOT found at ~/.config/opencode/plugins/b12/index.js"
    errors=$((errors + 1))
  fi

  if [ -f "$CONFIG_JSON" ] && grep -q 'plugins/b12' "$CONFIG_JSON" 2>/dev/null; then
    info "Verify: B12 OpenCode plugin registered in $CONFIG_JSON"
  else
    warn "Verify: B12 OpenCode plugin NOT registered in $CONFIG_JSON"
    errors=$((errors + 1))
  fi

  return $errors
}

# ═════════════════════════════════════════════
# Grok CLI
# ═════════════════════════════════════════════

# ─────────────────────────────────────────────
# Grok: Inject MCP server into ~/.grok/config.toml
# ─────────────────────────────────────────────
inject_grok_mcp_config() {
  local GROK_CONFIG_DIR="$HOME/.grok"
  local CONFIG_TOML="$GROK_CONFIG_DIR/config.toml"
  local SERVER_SCRIPT="$SCRIPT_DEST/b12_mcp_server.py"

  if [ ! -d "$GROK_CONFIG_DIR" ]; then
    warn "Grok config directory not found at $GROK_CONFIG_DIR — creating it"
    mkdir -p "$GROK_CONFIG_DIR"
  fi

  if [ ! -x "$VENV_PYTHON" ]; then
    warn "Venv Python not found at $VENV_PYTHON"
    warn "Run with --full to create the venv first: ./install.sh --full --grok"
    return 1
  fi
  if [ ! -f "$SERVER_SCRIPT" ]; then
    warn "MCP server script not found at $SERVER_SCRIPT — run standard install first"
    return 1
  fi

  if [ ! -f "$CONFIG_TOML" ]; then
    touch "$CONFIG_TOML"
    info "Created $CONFIG_TOML"
  fi

  # Use line-based TOML injection (no external toml module dependency)
  python3 - "$CONFIG_TOML" "$VENV_PYTHON" "$SERVER_SCRIPT" << 'PYEOF'
import sys, os

config_path = sys.argv[1]
venv_python = sys.argv[2]
server_script = sys.argv[3]

with open(config_path, 'r') as f:
    lines = f.readlines()

# Remove any existing [mcp_servers.B12] section (and its subkeys)
filtered = []
in_b12_section = False
for line in lines:
    stripped = line.strip()
    if stripped.startswith('[') and not stripped.startswith('[['):
        table_name = stripped.split(']')[0].lstrip('[').strip()
        if table_name == 'mcp_servers.B12' or table_name.startswith('mcp_servers.B12.'):
            in_b12_section = True
            continue
        else:
            in_b12_section = False
    if in_b12_section:
        continue
    filtered.append(line)

content = ''.join(filtered).rstrip() + '\n'

b12_block = f'''
[mcp_servers.B12]
command = "{venv_python}"
args = ["{server_script}"]
enabled = true
startup_timeout_sec = 20
tool_timeout_sec = 180

[mcp_servers.B12.env]
B12_DATA_DIR = "{os.path.expanduser('~/.B12')}"
'''

content += b12_block

with open(config_path, 'w') as f:
    f.write(content)

print("Added B12 MCP server to ~/.grok/config.toml")
PYEOF

  if [ $? -eq 0 ]; then
    info "B12 MCP server registered in $CONFIG_TOML"
  else
    warn "Failed to update $CONFIG_TOML"
    return 1
  fi

  return 0
}

# ─────────────────────────────────────────────
# Grok: Deploy plugin and skills to ~/.grok/
# ─────────────────────────────────────────────
deploy_grok_plugin() {
  local PLUGIN_SRC="$SCRIPT_DIR/.grok/plugins-available/b12"
  local PLUGIN_DEST="$HOME/.grok/plugins/b12"

  if [ ! -d "$PLUGIN_SRC" ]; then
    warn "Grok plugin source not found at $PLUGIN_SRC (expected in plugins-available/)"
    return 1
  fi

  rm -rf "$PLUGIN_DEST" 2>/dev/null || true
  mkdir -p "$PLUGIN_DEST"
  cp -r "$PLUGIN_SRC"/* "$PLUGIN_DEST"/ 2>/dev/null || true

  info "B12 Grok plugin deployed to $PLUGIN_DEST (from plugins-available/)"
  return 0
}

# ─────────────────────────────────────────────
# Grok: Install b12-memory skill into ~/.grok/skills/
# ─────────────────────────────────────────────
install_grok_skill() {
  local SKILL_SRC="$SCRIPT_DIR/.grok/skills/b12-memory"
  local SKILL_DEST="$HOME/.grok/skills/b12-memory"

  if [ ! -f "$SKILL_SRC/SKILL.md" ]; then
    warn "Grok skill source not found at $SKILL_SRC/SKILL.md"
    return 1
  fi

  mkdir -p "$SKILL_DEST"
  cp "$SKILL_SRC/SKILL.md" "$SKILL_DEST/SKILL.md"

  info "B12 Grok skill installed to $SKILL_DEST/SKILL.md"
  return 0
}

# ─────────────────────────────────────────────
# Grok: Inject B12 instructions into AGENTS.md (project level)
# ─────────────────────────────────────────────
inject_grok_agents() {
  local TARGET="$SCRIPT_DIR/AGENTS.md"
  local TEMPLATE="$SCRIPT_DIR/config/grok-instructions-template.md"

  if [ ! -f "$TARGET" ]; then
    warn "AGENTS.md not found at $TARGET — skipping Grok instructions injection"
    return 0
  fi

  if [ ! -f "$TEMPLATE" ]; then
    warn "Grok instructions template not found at $TEMPLATE"
    return 1
  fi

  # Avoid duplicate injection
  if grep -q 'B12-MEMORY-START' "$TARGET" 2>/dev/null; then
    info "B12 instructions already present in AGENTS.md"
    return 0
  fi

  {
    echo ""
    cat "$TEMPLATE"
  } >> "$TARGET"

  info "Added B12 Grok instructions to AGENTS.md"
  return 0
}

# ─────────────────────────────────────────────
# Grok: Verify installation
# ─────────────────────────────────────────────
verify_grok() {
  local errors=0
  local CONFIG_TOML="$HOME/.grok/config.toml"
  local PLUGIN_DIR="$HOME/.grok/plugins/b12"
  local SKILL_DIR="$HOME/.grok/skills/b12-memory"

  echo ""
  info "Grok CLI Verification:"

  if [ -f "$CONFIG_TOML" ] && grep -q '^\[mcp_servers\.B12\]' "$CONFIG_TOML" 2>/dev/null; then
    info "  ✓ B12 MCP server configured in $CONFIG_TOML"
  else
    warn "  ✗ B12 MCP server NOT found in $CONFIG_TOML"
    errors=$((errors + 1))
  fi

  if [ -d "$PLUGIN_DIR" ]; then
    info "  ✓ B12 plugin directory exists at $PLUGIN_DIR"
  else
    warn "  ✗ B12 plugin directory NOT found at $PLUGIN_DIR"
    errors=$((errors + 1))
  fi

  if [ -f "$SKILL_DIR/SKILL.md" ]; then
    info "  ✓ b12-memory skill installed at $SKILL_DIR"
  else
    warn "  ✗ b12-memory skill NOT found at $SKILL_DIR"
    errors=$((errors + 1))
  fi

  echo ""
  if [ $errors -eq 0 ]; then
    info "Grok CLI integration looks good. Run 'grok inspect' to verify."
  else
    warn "Some Grok verification checks failed. You may need to run 'grok' and trust the plugin/hooks manually."
  fi

  return $errors
}

# ─────────────────────────────────────────────
# B12 shared MCP daemon (v11.22.0+)
# ─────────────────────────────────────────────
# Renders config/com.b12.mcp.daemon.plist into ~/Library/LaunchAgents/ with
# user-specific paths substituted, then launchctl-loads it. The b12_mcp_server.py
# stdio proxy auto-detects the daemon at /tmp/b12-mcp-<UID>.sock; if absent it
# falls back to legacy in-process behaviour so non-Claude-Code consumers
# (Codex, Gemini, Kimi, OpenCode, Grok) see no change.
install_mcp_daemon() {
  if [ "$(uname)" != "Darwin" ]; then
    warn "MCP daemon (launchd) is macOS-only; skipping on $(uname)."
    return 0
  fi

  local TEMPLATE="$SCRIPT_DIR/config/com.b12.mcp.daemon.plist"
  if [ ! -f "$TEMPLATE" ]; then
    warn "Daemon plist template not found: $TEMPLATE"
    return 1
  fi
  if [ ! -x "$VENV_PYTHON" ]; then
    warn "B12 venv Python not found at $VENV_PYTHON — run './install.sh --full' first."
    return 1
  fi

  local LAUNCH_DIR="$HOME/Library/LaunchAgents"
  local PLIST_DEST="$LAUNCH_DIR/com.b12.mcp.daemon.plist"
  local DAEMON_PY="$SCRIPT_DEST/b12_mcp_daemon.py"
  local DATA_DIR="${B12_DATA_DIR:-$HOME/.B12}"

  if [ ! -f "$DAEMON_PY" ]; then
    warn "b12_mcp_daemon.py not deployed yet. Run copy_scripts first."
    return 1
  fi

  mkdir -p "$LAUNCH_DIR"

  # Unload prior daemon if present (idempotent)
  if launchctl list 2>/dev/null | grep -q "com.b12.mcp.daemon"; then
    launchctl unload "$PLIST_DEST" 2>/dev/null || true
  fi

  # Render the plist with absolute paths substituted in
  sed \
    -e "s|B12_HOME_PYTHON|$VENV_PYTHON|g" \
    -e "s|B12_HOME_DAEMON|$DAEMON_PY|g" \
    -e "s|B12_HOME_DATA_DIR|$DATA_DIR|g" \
    "$TEMPLATE" > "$PLIST_DEST"

  launchctl load "$PLIST_DEST"

  # Brief wait for the socket to appear (cold start ~5-10s)
  local SOCK="/tmp/b12-mcp-$(id -u).sock"
  local i=0
  while [ $i -lt 20 ] && [ ! -S "$SOCK" ]; do
    sleep 0.5
    i=$((i + 1))
  done

  if [ -S "$SOCK" ]; then
    info "MCP daemon loaded — listening on $SOCK"
    info "  Logs: tail -f /tmp/b12-mcp-daemon.err.log"
    info "        tail -f $DATA_DIR/memory-logs/mcp-daemon.log"
  else
    warn "MCP daemon plist loaded but socket did not appear within 10s."
    warn "  Inspect: launchctl list | grep b12.mcp"
    warn "  Inspect: tail /tmp/b12-mcp-daemon.err.log"
  fi
}

uninstall_mcp_daemon() {
  if [ "$(uname)" != "Darwin" ]; then
    return 0
  fi
  local PLIST_DEST="$HOME/Library/LaunchAgents/com.b12.mcp.daemon.plist"
  if [ -f "$PLIST_DEST" ]; then
    launchctl unload "$PLIST_DEST" 2>/dev/null || true
    rm -f "$PLIST_DEST"
    info "MCP daemon unloaded and plist removed."
  else
    info "MCP daemon was not installed; nothing to remove."
  fi
  # Best-effort socket cleanup (daemon should have done this on SIGTERM)
  rm -f "/tmp/b12-mcp-$(id -u).sock" "/tmp/b12-mcp-$(id -u).pid" 2>/dev/null || true
}

# ═════════════════════════════════════════════
# Main
# ═════════════════════════════════════════════

echo "B12 Memory System Installer (v11.53.0 — multi-platform)"
echo "─────────────────────────────────"

# Full setup: create venv first
if $FULL_SETUP; then
  setup_venv
  echo ""
fi

# Always create dirs and copy files (Claude Code hooks + scripts)
create_dirs
seed_user_config
copy_hooks
copy_scripts
update_launchd_plists

# Install B12 behavioral skill to Claude Code (enables /b12-memory command)
install_claude_skill() {
  local SKILL_SRC="$SCRIPT_DIR/skills/b12-memory"
  for dir in "$HOME"/.claude*; do
    [ -d "$dir" ] || continue
    local SKILL_DEST="$dir/skills/b12-memory"
    mkdir -p "$SKILL_DEST"
    cp "$SKILL_SRC/SKILL.md" "$SKILL_DEST/SKILL.md"
  done
  info "B12 skill installed to Claude Code skill directories"
}
install_claude_skill

# Install b12 CLI to PATH
install_cli() {
  local CLI_SRC="$SCRIPT_DIR/scripts/b12"
  local CLI_PY="$SCRIPT_DIR/scripts/b12_cli.py"
  local CLI_DEST="$HOME/.local/bin/b12"
  if [ -f "$CLI_SRC" ] && [ -f "$CLI_PY" ]; then
    mkdir -p "$HOME/.local/bin"
    # Copy wrapper and CLI script to a stable location
    cp "$CLI_SRC" "$SCRIPT_DEST/b12"
    cp "$CLI_PY" "$SCRIPT_DEST/b12_cli.py"
    chmod +x "$SCRIPT_DEST/b12"
    # Symlink from PATH-accessible location
    ln -sf "$SCRIPT_DEST/b12" "$CLI_DEST"
    info "b12 CLI installed to $CLI_DEST"
    if ! echo "$PATH" | grep -q "$HOME/.local/bin"; then
      warn "Add to PATH: export PATH=\"\$HOME/.local/bin:\$PATH\""
    fi
  fi
}
install_cli

# Verify MCP package
if [ -x "$VENV_PYTHON" ]; then
  if "$VENV_PYTHON" -c "import mcp" 2>/dev/null; then
    info "MCP Python package available (via b12-venv)"
  else
    warn "MCP Python package not found in b12-venv"
    warn "Run: $VENV_PYTHON -m pip install mcp sentence-transformers sqlite-vec fsrs"
  fi
else
  if ! $FULL_SETUP; then
    warn "B12 venv not found. Run with --full for automatic setup, or manually:"
    echo "       python3 -m venv $VENV_PATH"
    echo "       $VENV_PYTHON -m pip install mcp sentence-transformers sqlite-vec fsrs"
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

# Round 0 fix #9 — warn early if Codex is live. `--all` overwrites
# ~/.B12/hooks/ which is the shared path Codex hook entries point at;
# silent issue #21160 applies even when the user didn't pass --codex.
if [ -d "$HOME/.codex" ] && ($INSTALL_ALL || $INSTALL_CODEX || $FIX_DRIFT); then
  warn_live_codex_sessions
fi

# Phase MX (R10) — surface drift on non-Claude platform CLIs.
# `--all` covers `~/.claude*` only by design. The flags
# `--kimi`/`--codex`/`--gemini`/`--cursor`/`--windsurf`/`--opencode`/
# `--grok` are explicit, but users routinely forget to chain them with
# `--all`. This loop prints a one-line hint per detected platform dir
# whose mcp config lacks B12 — non-fatal, exit code 0.
#
# When `--fix-drift` is also passed, the loop calls each platform's
# inject_*_mcp_config() to auto-register B12. Opt-in only — default
# behavior remains "warn only" so users who detected drift on a
# system they don't want B12 on can ignore the hint.
if $INSTALL_ALL; then
  for plat in codex gemini kimi cursor windsurf opencode grok; do
    case "$plat" in
      codex)    plat_dir="$HOME/.codex"     ; plat_cfg="$HOME/.codex/config.toml"                  ; plat_inject=inject_codex_mcp_config ;;
      gemini)   plat_dir="$HOME/.gemini"    ; plat_cfg="$HOME/.gemini/settings.json"               ; plat_inject=inject_gemini_mcp_config ;;
      kimi)     plat_dir="$HOME/.kimi"      ; plat_cfg="$HOME/.kimi/mcp.json"                      ; plat_inject=inject_kimi_mcp_config ;;
      cursor)   plat_dir="$HOME/.cursor"    ; plat_cfg="$HOME/.cursor/mcp.json"                    ; plat_inject=inject_cursor_mcp_config ;;
      windsurf) plat_dir="$HOME/.codeium/windsurf" ; plat_cfg="$HOME/.codeium/windsurf/mcp_config.json" ; plat_inject=inject_windsurf_mcp_config ;;
      opencode) plat_dir="$HOME/.config/opencode" ; plat_cfg="$HOME/.config/opencode/opencode.json"  ; plat_inject=inject_opencode_mcp_config ;;
      grok)     plat_dir="$HOME/.grok"      ; plat_cfg="$HOME/.grok/config.toml"                   ; plat_inject=inject_grok_mcp_config ;;
    esac
    [ -d "$plat_dir" ] || continue
    # Already registered? Skip.
    if [ -f "$plat_cfg" ] && grep -q '"B12"\|\[mcp_servers\.B12\]\|B12 =' "$plat_cfg" 2>/dev/null; then
      continue
    fi
    if $FIX_DRIFT; then
      echo "[B12] fix-drift: registering B12 for detected $plat_dir/ ..." >&2
      # The inject_* functions return non-zero for "directory not found"
      # warnings which we already gated on -d above; suppress here to
      # keep --all from aborting on a single drift fix.
      "$plat_inject" || true
    else
      echo "[B12] hint: $plat_dir/ exists but $plat_cfg has no B12 entry. Run \`./install.sh --$plat\` to register (or re-run with \`--all --fix-drift\` to auto-register all detected platforms)." >&2
    fi
  done
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
  warn_live_codex_sessions
  inject_codex_mcp_config
  inject_codex_agents
  install_codex_skill
  register_codex_hooks_json
  inject_codex_hooks_state
  echo ""
fi

# Gemini CLI setup
if $INSTALL_GEMINI; then
  echo ""
  echo "── Gemini CLI Setup ─────────────"
  inject_gemini_mcp_config
  inject_gemini_instructions
  install_gemini_hooks
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
  inject_cline_hooks
  echo ""
fi

# OpenCode setup
if $INSTALL_OPENCODE; then
  echo ""
  echo "── OpenCode Setup ───────────────"
  inject_opencode_mcp_config
  inject_opencode_agents
  install_opencode_skill
  build_opencode_plugin
  deploy_opencode_plugin
  inject_opencode_plugin_config
  echo ""
fi

# Continue.dev setup (CLI + VS Code + JetBrains via shared ~/.continue/)
if $INSTALL_CONTINUE; then
  echo ""
  echo "── Continue.dev Setup ───────────"
  inject_continue_mcp_config
  inject_continue_rules
  echo ""
  info "Continue.dev: hooks are Claude Code-compatible. To wire B12 hooks for"
  info "  PreToolUse/PostToolUse/SessionStart/SessionEnd, add the matching"
  info "  entries from ~/.B12/hooks/ to your ~/.continue/config.yaml under"
  info "  'hooks:' (Continue uses the same JSON-on-stdin contract as Claude)."
fi

# Grok setup
if $INSTALL_GROK; then
  echo ""
  echo "── Grok CLI Setup ───────────────"
  inject_grok_mcp_config
  deploy_grok_plugin
  install_grok_skill
  inject_grok_agents
  echo ""
  info "Grok CLI: After restart, run 'grok inspect' to verify B12 plugin and skill are loaded."
  info "         If plugin shows as 'disabled', open Grok and trust it via Ctrl+L → Plugins."
fi

# MCP daemon (v11.22.0+) — explicit opt-in via --daemon / --daemon-uninstall
if $UNINSTALL_DAEMON; then
  echo ""
  echo "── B12 MCP Daemon Uninstall ─────"
  uninstall_mcp_daemon
  echo ""
fi
if $INSTALL_DAEMON; then
  echo ""
  echo "── B12 MCP Daemon Setup ─────────"
  install_mcp_daemon
  echo ""
fi

# 24h smoke cron (Plan §C13) — explicit opt-in via --smoke-cron /
# --smoke-cron-uninstall. Reversible by definition: edits the user's
# crontab only, no launchctl writes (avoids STOP-AND-ASK trigger).
if $UNINSTALL_SMOKE_CRON; then
  echo ""
  echo "── B12 Smoke Cron Uninstall ─────"
  uninstall_smoke_cron
  echo ""
fi
if $INSTALL_SMOKE_CRON; then
  echo ""
  echo "── B12 Smoke Cron Setup ─────────"
  install_smoke_cron
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

if $INSTALL_CONTINUE; then
  echo ""
  echo "── Continue.dev Verification ────"
  verify_continue
  VERIFY_RESULT=$((VERIFY_RESULT + $?))
fi

if $INSTALL_GROK; then
  echo ""
  echo "── Grok CLI Verification ────────"
  verify_grok
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
$INSTALL_GROK && PLATFORMS_INSTALLED="$PLATFORMS_INSTALLED Grok"

if [ $VERIFY_RESULT -eq 0 ]; then
  if [ -n "$PLATFORMS_INSTALLED" ]; then
    info "Installation complete! Restart your AI tools ($PLATFORMS_INSTALLED) to activate B12."
  else
    info "Installation complete! Restart Claude Code to activate B12."
  fi
else
  warn "Installation complete with $VERIFY_RESULT warning(s). See above."
fi

# Show helpful tips
ANY_PLATFORM=false
$INSTALL_CODEX || $INSTALL_GEMINI || $INSTALL_VSCODE || $INSTALL_CURSOR || $INSTALL_KIMI || $INSTALL_WINDSURF || $INSTALL_CLINE || $INSTALL_OPENCODE || $INSTALL_GROK && ANY_PLATFORM=true

if ! $FULL_SETUP && ! $ANY_PLATFORM; then
  echo ""
  echo "Tip: Run './install.sh --full' for automatic venv + MCP config setup."
  echo "     Flags: --codex --gemini --vscode --cursor --kimi --windsurf --cline --opencode --grok"
fi

echo ""
if [ -n "$PLATFORMS_INSTALLED" ]; then
  echo "Next: Restart your tools ($PLATFORMS_INSTALLED), then run the appropriate verification command (e.g. grok inspect for Grok)."
else
  echo "Next: Restart Claude Code, then run /mcp to verify B12 is connected."
fi
