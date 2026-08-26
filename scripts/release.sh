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
#   scripts/release.sh <X.Y.Z> <notes-file>    # cut the release: sync the version
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

_latest_release_tag() {
  # `git describe --tags` accepts any tag name. Limit its candidate set to the
  # repository's strict release convention so maintenance/milestone tags never
  # redefine the release range. The filtered names are safe literal --match
  # patterns because they contain only `v`, digits, and dots.
  local tag
  local -a matches=()
  while IFS= read -r tag; do
    [ -n "$tag" ] && matches+=(--match "$tag")
  done < <(git tag --merged HEAD --list | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' || true)
  [ "${#matches[@]}" -gt 0 ] || return 1
  git describe --tags --abbrev=0 "${matches[@]}" 2>/dev/null
}

LAST_TAG="$(_latest_release_tag || echo '')"

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

_current_version() {
  # The release baseline: the last tag if present, else the installed package
  # version (so a shallow/tagless clone doesn't think it's at 0.0.0).
  if [ -n "$LAST_TAG" ]; then echo "${LAST_TAG#v}"; return; fi
  grep -E '^version = ' pyproject.toml 2>/dev/null | head -1 | sed -E 's/^version = "([^"]+)".*/\1/'
}

if [ "${1:-}" = "--check" ]; then
  if [ -z "$LAST_TAG" ]; then
    # Shallow/tagless checkout — try to pull tags so the range is meaningful.
    git fetch --tags --quiet 2>/dev/null || true
    LAST_TAG="$(_latest_release_tag || echo '')"
  fi
  if [ -z "$LAST_TAG" ]; then
    CUR="$(_current_version)"
    echo "── Release check: INDETERMINATE ────────────────────────"
    echo "  No git tags in this checkout (shallow/tagless clone, or no 'origin'"
    echo "  / no network in this sandbox), so the unreleased range CANNOT be"
    echo "  computed. Current package version: v${CUR:-unknown}."
    echo "  ACTION: run 'git fetch --tags' and re-run --check, or tell the owner"
    echo "  the release state can't be determined here so they can check manually."
    echo "────────────────────────────────────────────────────────"
    exit 0
  fi
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
# Refuse a non-incrementing version (typo / accidental downgrade) BEFORE mutating
# any file. Baseline = last tag, or the package version on a tagless clone.
CUR="$(_current_version)"
if [ -n "$CUR" ]; then
  python3 - "$CUR" "$NEW" <<'PY' || { echo "ERROR: v$NEW is not greater than current v$CUR — refusing to downgrade/repeat." >&2; exit 2; }
import sys
def t(v): return tuple(int(x) for x in v.split("."))
sys.exit(0 if t(sys.argv[2]) > t(sys.argv[1]) else 1)
PY
fi

echo "Cutting release v$NEW (last: ${LAST_TAG:-none})  dry-run=$DRY"

# 1) Sync every version touchpoint + prepend the curated CHANGELOG section.
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
for jf in ("package.json", ".claude-plugin/plugin.json", "plugins/opencode/package.json"):
    d = json.load(open(jf)); d["version"] = NEW
    json.dump(d, open(jf,"w"), indent=2); open(jf,"a").write("\n")

# marketplace.json: bump the PLUGIN ENTRY version (what `/plugin marketplace add`
# users see) — NOT the catalog metadata.version. Was never synced, so it drifted
# ~62 releases behind (audit #15).
try:
    mp = json.load(open(".claude-plugin/marketplace.json"))
    if mp.get("plugins"):
        mp["plugins"][0]["version"] = NEW
    json.dump(mp, open(".claude-plugin/marketplace.json","w"), indent=2)
    open(".claude-plugin/marketplace.json","a").write("\n")
except FileNotFoundError:
    pass

# package-lock.json carries the version in two places (top-level + the root
# package entry). Sync it too if present, so the release commit doesn't leave a
# stale lockfile / dirty `npm install --package-lock-only` diff.
try:
    lock = json.load(open("package-lock.json"))
    lock["version"] = NEW
    if isinstance(lock.get("packages"), dict) and "" in lock["packages"]:
        lock["packages"][""]["version"] = NEW
    json.dump(lock, open("package-lock.json","w"), indent=2); open("package-lock.json","a").write("\n")
except FileNotFoundError:
    pass

# README front-page "## Changelog (recent)" — keep it in sync (AGENTS.md doc-sync
# rule). Prepend a minimal stub entry so the front page never pins to an older
# version; the agent may enrich it with highlights during curation.
try:
    rd = open("README.md").read()
    anchor = "## Changelog (recent)\n"
    if anchor in rd and f"### v{NEW} (" not in rd:
        idx = rd.index(anchor) + len(anchor)
        entry = f"\n### v{NEW} ({today})\n\nSee [CHANGELOG.md](CHANGELOG.md) for the full notes.\n"
        open("README.md","w").write(rd[:idx] + entry + rd[idx:])
        print("  prepended README 'Changelog (recent)' stub entry")
except FileNotFoundError:
    pass
print(f"  synced version touchpoints -> {NEW}; prepended CHANGELOG section")
PY

# 2) Sanity: the canonical validator checks package metadata and the first
# CHANGELOG release header together before any commit, tag, push, or release.
echo "  verifying touchpoints..."
python3 scripts/check_package_versions.py
for chk in \
  "pyproject.toml:^version = \"$NEW\"" \
  "package.json:\"version\": \"$NEW\"" \
  "plugins/opencode/package.json:\"version\": \"$NEW\"" \
  ".claude-plugin/plugin.json:\"version\": \"$NEW\"" \
  "scripts/b12_mcp_server.py:^B12_VERSION = \"v$NEW\"" \
  "scripts/b12_health.py:^VERSION = \"$NEW\"" \
  ".claude-plugin/marketplace.json:\"version\": \"$NEW\"" \
  "install.sh:Installer [(]v$NEW"; do
  f="${chk%%:*}"; pat="${chk#*:}"
  grep -qE "$pat" "$f" || { echo "ERROR: touchpoint mismatch in $f" >&2; exit 1; }
done
# package-lock.json only if present (npm toolchain was removed; it may be absent).
if [ -f package-lock.json ]; then
  grep -qE "\"version\": \"$NEW\"" package-lock.json || { echo "ERROR: package-lock.json not synced to $NEW" >&2; exit 1; }
fi
echo "  all release version touchpoints == $NEW ✓"

if [ "$DRY" -eq 1 ]; then
  echo "DRY-RUN: edits applied locally, NOT committed. Review with 'git diff', then 'git checkout -- .' to undo or re-run without --dry-run."
  exit 0
fi

# 3) Commit, annotated tag, push, GitHub release.
# README.md is staged as a safety net in case the agent pre-edited a changelog
# highlight there; `git add` of an unchanged tracked file is a no-op.
git add CHANGELOG.md README.md pyproject.toml package.json \
        plugins/opencode/package.json \
        .claude-plugin/plugin.json .claude-plugin/marketplace.json \
        scripts/b12_mcp_server.py scripts/b12_health.py install.sh
if [ -f package-lock.json ]; then git add package-lock.json; fi
git commit -m "chore(release): v$NEW"
SUMMARY="$(head -1 "$NOTES" | sed 's/^#* *//')"
git tag -a "v$NEW" -m "v$NEW${SUMMARY:+: $SUMMARY}"
git push origin main
git push origin "v$NEW"
gh release create "v$NEW" --title "v$NEW" --notes-file "$NOTES"
echo "Released v$NEW ✓  https://github.com/dorukardahan/B12/releases/tag/v$NEW"
