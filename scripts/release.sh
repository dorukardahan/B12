#!/bin/bash
# B12 release helper — codifies the manual hand-curated release ritual
# (docs/releasing.md) into one command, so the AI agent working in this repo
# can detect when a release is due and cut it in a single shot AFTER the owner
# approves. NOT a CI/auto-publish toolchain — the owner still gates tag timing
# and the agent hand-curates (AI-quality) the CHANGELOG section.
#
# Usage:
#   scripts/release.sh --check                 # is a release due? prints unreleased
#                                               # commit count + suggested bump.
#   scripts/release.sh <X.Y.Z> <notes-file>    # cut the release: sync the 6 version
#                                               # touchpoints, prepend the curated
#                                               # CHANGELOG section, commit, tag,
#                                               # GitHub release.
#   scripts/release.sh --dry-run <X.Y.Z> <f>   # do everything locally except
#                                               # commit/tag/push/release.
#
# The agent supplies <X.Y.Z> and a <notes-file> it HAND-CURATED (grouped
# Added/Changed/Fixed/Internal, user-facing, PII-clean). This script only does
# the mechanical, error-prone parts.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

LAST_TAG="$(git describe --tags --abbrev=0 2>/dev/null || echo '')"

_suggest_bump() {
  # MINOR if any feat(...) lands; else PATCH. MAJOR is never auto-suggested
  # (breaking changes are an explicit owner call). Mirrors docs/releasing.md.
  local range="$1"
  if git log "$range" --format='%s' 2>/dev/null | grep -qiE '^feat(\(|!|:)|BREAKING'; then
    echo minor
  else
    echo patch
  fi
}

_next_version() {
  local bump="$1" base="${LAST_TAG#v}"
  base="${base:-0.0.0}"
  IFS=. read -r MA MI PA <<EOF
$base
EOF
  case "$bump" in
    major) echo "$((MA+1)).0.0" ;;
    minor) echo "$MA.$((MI+1)).0" ;;
    *)     echo "$MA.$MI.$((PA+1))" ;;
  esac
}

if [ "${1:-}" = "--check" ]; then
  if [ -z "$LAST_TAG" ]; then echo "No tags yet — first release."; exit 0; fi
  RANGE="$LAST_TAG..HEAD"
  TOTAL=$(git rev-list --count "$RANGE")
  if [ "$TOTAL" -eq 0 ]; then echo "Up to date — nothing unreleased since $LAST_TAG."; exit 0; fi
  # `|| true` inside the group: grep exits 1 when every commit is a dep-bump
  # (no matches), which would otherwise trip `set -o pipefail`.
  USERFACING=$(git log --oneline "$RANGE" | { grep -viE 'bump |deps\)|deps-dev\)|chore\(deps' || true; } | wc -l | tr -d ' ')
  BUMP=$(_suggest_bump "$RANGE"); NEXT=$(_next_version "$BUMP")
  echo "── Release check ──────────────────────────────────────"
  echo "  Last release : $LAST_TAG"
  echo "  Unreleased   : $TOTAL commits ($USERFACING user-facing, dep-bumps excluded)"
  echo "  Suggested    : v$NEXT  (bump: $BUMP)"
  echo "  Commits:"
  git log --oneline "$RANGE" | sed 's/^/    /'
  echo "────────────────────────────────────────────────────────"
  echo "If a release is warranted: curate notes, then run"
  echo "  scripts/release.sh $NEXT <notes-file>"
  exit 0
fi

DRY=0
if [ "${1:-}" = "--dry-run" ]; then DRY=1; shift; fi

NEW="${1:-}"; NOTES="${2:-}"
if [ -z "$NEW" ] || [ -z "$NOTES" ]; then
  echo "usage: scripts/release.sh [--dry-run] <X.Y.Z> <notes-file>   |   scripts/release.sh --check" >&2
  exit 2
fi
case "$NEW" in v*) NEW="${NEW#v}";; esac
echo "$NEW" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+$' || { echo "ERROR: version must be X.Y.Z (got '$NEW')" >&2; exit 2; }
[ -f "$NOTES" ] || { echo "ERROR: notes file not found: $NOTES" >&2; exit 2; }
[ -s "$NOTES" ] || { echo "ERROR: notes file is empty: $NOTES" >&2; exit 2; }
[ "$(git rev-parse --abbrev-ref HEAD)" = "main" ] || { echo "ERROR: not on main (on $(git rev-parse --abbrev-ref HEAD))" >&2; exit 2; }
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  echo "ERROR: working tree has uncommitted tracked changes — commit/stash first." >&2; exit 2
fi
if git rev-parse "v$NEW" >/dev/null 2>&1; then echo "ERROR: tag v$NEW already exists." >&2; exit 2; fi

echo "Cutting release v$NEW (last: ${LAST_TAG:-none})  dry-run=$DRY"

# 1) Sync the six version touchpoints + prepend the curated CHANGELOG section.
python3 - "$NEW" "$NOTES" <<'PY'
import sys, json, re, datetime
NEW, NOTES = sys.argv[1], sys.argv[2]
notes = open(NOTES).read().strip()
today = datetime.date.today().isoformat()  # ok: release-time stamp, run by a human-approved invocation
# CHANGELOG: replace a leading "## Unreleased" block (up to next "## ") with the
# new versioned section; else insert right after the "# Changelog" title.
header = f"## [v{NEW}] — {today}\n\n"
section = header + notes.rstrip() + "\n"
cl = open("CHANGELOG.md").read().splitlines(keepends=True)
out, i, inserted = [], 0, False
# keep the title line(s) until the first "## "
while i < len(cl) and not cl[i].startswith("## "):
    out.append(cl[i]); i += 1
if i < len(cl) and cl[i].startswith("## Unreleased"):
    i += 1
    while i < len(cl) and not cl[i].startswith("## "):  # skip old Unreleased body
        i += 1
out.append(section + "\n")
out.extend(cl[i:])
open("CHANGELOG.md","w").write("".join(out))

def sub(path, pat, repl):
    s = open(path).read(); s2 = re.sub(pat, repl, s, count=1, flags=re.M)
    if s2 == s: raise SystemExit(f"ERROR: version touchpoint not found in {path} ({pat})")
    open(path,"w").write(s2)
sub("pyproject.toml", r'^version = ".*"', f'version = "{NEW}"')
sub("scripts/b12_mcp_server.py", r'^B12_VERSION = ".*"', f'B12_VERSION = "v{NEW}"')
sub("scripts/b12_health.py", r'^VERSION = "[0-9][^"]*"', f'VERSION = "{NEW}"')
sub("install.sh", r'B12 Memory System Installer \(v[0-9.]*', f'B12 Memory System Installer (v{NEW}')
for jf in ("package.json", ".claude-plugin/plugin.json"):
    d = json.load(open(jf)); d["version"] = NEW
    json.dump(d, open(jf,"w"), indent=2); open(jf,"a").write("\n")
print(f"  synced 6 version touchpoints -> {NEW}; prepended CHANGELOG section")
PY

# 2) Sanity: all six touchpoints agree.
echo "  verifying touchpoints..."
for chk in \
  "pyproject.toml:^version = \"$NEW\"" \
  "package.json:\"version\": \"$NEW\"" \
  ".claude-plugin/plugin.json:\"version\": \"$NEW\"" \
  "scripts/b12_mcp_server.py:^B12_VERSION = \"v$NEW\"" \
  "scripts/b12_health.py:^VERSION = \"$NEW\"" \
  "install.sh:Installer [(]v$NEW"; do
  f="${chk%%:*}"; pat="${chk#*:}"
  grep -qE "$pat" "$f" || { echo "ERROR: touchpoint mismatch in $f" >&2; exit 1; }
done
echo "  all 6 touchpoints == $NEW ✓"

if [ "$DRY" -eq 1 ]; then
  echo "DRY-RUN: edits applied locally, NOT committed. Review with 'git diff', then 'git checkout -- .' to undo or re-run without --dry-run."
  exit 0
fi

# 3) Commit, annotated tag, push, GitHub release.
git add CHANGELOG.md README.md pyproject.toml package.json .claude-plugin/plugin.json \
        scripts/b12_mcp_server.py scripts/b12_health.py install.sh 2>/dev/null || true
git add pyproject.toml package.json .claude-plugin/plugin.json scripts/b12_mcp_server.py scripts/b12_health.py install.sh CHANGELOG.md
git commit -m "chore(release): v$NEW"
SUMMARY="$(head -1 "$NOTES" | sed 's/^#* *//')"
git tag -a "v$NEW" -m "v$NEW${SUMMARY:+: $SUMMARY}"
git push origin main
git push origin "v$NEW"
gh release create "v$NEW" --title "v$NEW" --notes-file "$NOTES"
echo "Released v$NEW ✓  https://github.com/dorukardahan/B12/releases/tag/v$NEW"
