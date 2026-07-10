#!/bin/bash
# B12 Release Readiness Check
# Run before publishing: ./check.sh
# Exit 0 = all clear, Exit 1 = issues found

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

ISSUES=0
WARNINGS=0

fail() { echo -e "${RED}[FAIL]${NC} $1"; ISSUES=$((ISSUES + 1)); }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; WARNINGS=$((WARNINGS + 1)); }
pass() { echo -e "${GREEN}[PASS]${NC} $1"; }

echo "═══════════════════════════════════════"
echo "  B12 Release Readiness Check"
echo "═══════════════════════════════════════"
echo ""

# ── 1. PII / Personal Data ──────────────────

echo "── PII & Personal Data ──"

# Hardcoded /Users/ paths (exclude examples, comments, and this script)
_USERS_HITS=$(grep -rn "/Users/" --include="*.sh" --include="*.py" --include="*.json" --include="*.toml" --include="*.plist" --exclude-dir=".claude" --exclude="check.sh" . 2>/dev/null | grep -v ".git/" | grep -v "/Users/you/" | grep -v "^.*:#" || true)
if [ -n "$_USERS_HITS" ]; then
  fail "Hardcoded /Users/ paths found:"
  echo "$_USERS_HITS" | head -5
else
  pass "No hardcoded /Users/ paths"
fi

# Email addresses
if grep -rn "@gmail\|@hotmail\|@outlook\|@yahoo\|@anthropic" --include="*.sh" --include="*.py" --include="*.md" --include="*.json" --include="*.toml" . 2>/dev/null | grep -v ".git/\|\.claude/worktrees/" | grep -q .; then
  fail "Email addresses found:"
  grep -rn "@gmail\|@hotmail\|@outlook\|@yahoo\|@anthropic" --include="*.sh" --include="*.py" --include="*.md" --include="*.json" --include="*.toml" . 2>/dev/null | grep -v ".git/\|\.claude/worktrees/" | head -5
else
  pass "No email addresses in code"
fi

# API keys / secrets patterns
if grep -rn "sk-[a-zA-Z0-9]\{20,\}" --include="*.sh" --include="*.py" --include="*.json" --include="*.toml" . 2>/dev/null | grep -v ".git/\|\.claude/worktrees/" | grep -q .; then
  fail "Possible API key found (sk-... pattern)"
else
  pass "No API key patterns"
fi

# Stale dates in project files (hardcoded dates that will go stale)
if grep -rn "Today's date is\|currentDate" --include="*.md" . 2>/dev/null | grep -v ".git/\|\.claude/worktrees/" | grep -v "CHANGELOG.md" | grep -q .; then
  fail "Hardcoded date found (will go stale for cloners):"
  grep -rn "Today's date is\|currentDate" --include="*.md" . 2>/dev/null | grep -v ".git/\|\.claude/worktrees/" | grep -v "CHANGELOG.md" | head -5
else
  pass "No hardcoded dates in project files"
fi

echo ""

# ── 2. Code Quality ─────────────────────────

echo "── Code Quality ──"

# Bash syntax check on all hooks
BASH_FAIL=0
for f in hooks/*.sh; do
  [ -f "$f" ] || continue
  if ! bash -n "$f" 2>/dev/null; then
    fail "Bash syntax error: $f"
    BASH_FAIL=1
  fi
done
[ "$BASH_FAIL" -eq 0 ] && pass "All hook scripts pass bash -n"

# Python syntax check on all scripts
PY_FAIL=0
for f in scripts/*.py; do
  [ -f "$f" ] || continue
  if ! python3 -c "import ast; ast.parse(open('$f').read())" 2>/dev/null; then
    fail "Python syntax error: $f"
    PY_FAIL=1
  fi
done
[ "$PY_FAIL" -eq 0 ] && pass "All Python scripts pass ast.parse"

# Bash 4+ features (breaks macOS Bash 3.2)
if grep -rn '\${[A-Za-z_]*,,}\|\${[A-Za-z_]*\^\^}' hooks/*.sh 2>/dev/null | grep -q .; then
  fail "Bash 4+ syntax found (breaks macOS Bash 3.2):"
  grep -rn '\${[A-Za-z_]*,,}\|\${[A-Za-z_]*\^\^}' hooks/*.sh 2>/dev/null | head -5
else
  pass "No Bash 4+ only syntax in hooks"
fi

# Bare os.getuid() without guard (breaks Windows)
if grep -rn "os\.getuid()" scripts/*.py 2>/dev/null | grep -v "hasattr" | grep -q .; then
  warn "Unguarded os.getuid() found (breaks Windows):"
  grep -rn "os\.getuid()" scripts/*.py 2>/dev/null | grep -v "hasattr" | head -5
else
  pass "All os.getuid() calls have hasattr guard"
fi

echo ""

# ── 3. Migration & Paths ────────────────────

echo "── Migration & Paths ──"

# Check for ~/.claude/ in code that should be ~/.B12/
# Exclude: install.sh (legitimately references both), docs explaining Claude Code paths
if grep -rn '~/\.claude/\|\$HOME/\.claude/' --include="*.sh" --include="*.py" --exclude-dir=".claude" . 2>/dev/null | grep -v ".git/" | grep -v "install.sh" | grep -v "check.sh" | grep -v "# " | grep -q .; then
  warn "~/.claude/ references in code (should these be ~/.B12/?):"
  grep -rn '~/\.claude/\|\$HOME/\.claude/' --include="*.sh" --include="*.py" --exclude-dir=".claude" . 2>/dev/null | grep -v ".git/" | grep -v "install.sh" | grep -v "check.sh" | grep -v "# " | head -5
else
  pass "No stale ~/.claude/ paths in hook/script code"
fi

# Hash algorithm consistency
if grep -rn "hashlib\.md5" scripts/*.py 2>/dev/null | grep -q .; then
  fail "MD5 hash found (should be SHA-256 for consistency):"
  grep -rn "hashlib\.md5" scripts/*.py 2>/dev/null | head -5
else
  pass "Hash algorithm consistent (SHA-256)"
fi

# MCP config template consistency across supported tools (#158)
# Every MCP template in config/ must agree on model, response-char budget,
# command and script reference. Catches per-tool drift (e.g. a template
# silently missing MCP_EMBEDDING_MODEL) before release.
if python3 "$SCRIPT_DIR/scripts/validate_mcp_templates.py" --quiet > /dev/null 2>&1; then
  pass "MCP config templates consistent across tools"
else
  fail "MCP config template drift detected:"
  python3 "$SCRIPT_DIR/scripts/validate_mcp_templates.py" --quiet 2>&1 | grep -i 'FAIL' | head -10
fi

echo ""

# ── 4. Repo Hygiene ─────────────────────────

echo "── Repo Hygiene ──"

# Tracked files that shouldn't be
BAD_TRACKED=0
for pattern in "*.pyc" "*.db" "*.env" "*.sqlite"; do
  if git ls-files -- "$pattern" 2>/dev/null | grep -q .; then
    fail "Tracked file that should be gitignored: $pattern"
    BAD_TRACKED=1
  fi
done
[ "$BAD_TRACKED" -eq 0 ] && pass "No bad files tracked in git"

# __pycache__ directories
if git ls-files -- "*__pycache__*" 2>/dev/null | grep -q .; then
  fail "__pycache__ tracked in git"
else
  pass "No __pycache__ in git"
fi

# Internal planning docs
for f in PLAN-*.md TODO-*.md INTERNAL-*.md; do
  if [ -f "$f" ]; then
    warn "Internal doc in repo: $f (remove before public release?)"
  fi
done

# AGENTS.md sync check
if [ -f "AGENTS.md" ] && [ -f "CLAUDE.md" ]; then
  if diff -q AGENTS.md CLAUDE.md > /dev/null 2>&1; then
    pass "AGENTS.md in sync with CLAUDE.md"
  else
    warn "AGENTS.md differs from CLAUDE.md — intentional?"
  fi
elif [ -f "AGENTS.md" ] && [ -L "AGENTS.md" ]; then
  pass "AGENTS.md is symlink to CLAUDE.md"
fi

echo ""

# ── 5. Documentation ────────────────────────

echo "── Documentation ──"

# README exists and has setup instructions
if [ -f "README.md" ]; then
  if grep -q "install.sh" README.md; then
    pass "README.md has install instructions"
  else
    warn "README.md missing install.sh reference"
  fi
else
  fail "README.md missing"
fi

# Template files are generic
if [ -f "templates/user-profile.md" ]; then
  if grep -q "doruk\|/Users/" templates/user-profile.md; then
    fail "User profile template contains personal data"
  else
    pass "User profile template is generic"
  fi
fi

echo ""

# ── Summary ──────────────────────────────────

echo "═══════════════════════════════════════"
if [ "$ISSUES" -eq 0 ] && [ "$WARNINGS" -eq 0 ]; then
  echo -e "  ${GREEN}ALL CLEAR${NC} — ready for public release"
elif [ "$ISSUES" -eq 0 ]; then
  echo -e "  ${YELLOW}$WARNINGS warning(s)${NC}, 0 failures — review warnings"
else
  echo -e "  ${RED}$ISSUES failure(s)${NC}, $WARNINGS warning(s) — fix before release"
fi
echo "═══════════════════════════════════════"

exit "$ISSUES"
