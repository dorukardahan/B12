# Releasing B12

B12 ships **manual hand-curated releases**. There is no `semantic-release`
or other auto-publish toolchain — it was removed on 2026-05-24 in favor
of full owner control over CHANGELOG content, tag timing, and release
notes. See commit `c497c82` for the rationale.

## When to cut a release

- After a batch of user-facing changes lands on `main`
- Roughly cadence: monthly, or when a noteworthy feature/fix ships
- **Not** on every commit. Stack 5-15 commits per release.

## The ritual (5 minutes)

### 1. Decide the next version

Follow semver against the previous tag (`git describe --tags --abbrev=0`):

| Change category | Bump |
|---|---|
| Breaking change (DB schema, hook contract, CLI flag removed) | **MAJOR** (`v12.0.0`) |
| New feature, new MCP tool, new hook event support | **MINOR** (`v11.75.0`) |
| Bug fix, doc fix, perf tweak, dep bump | **PATCH** (`v11.74.2`) |

When in doubt, prefer MINOR — B12 isn't on a strict semver contract yet.

### 2. Update the three version touchpoints

Keep these in sync. The audit's "stale version constant" finding came
from forgetting one of them:

```bash
NEW=11.75.0  # without the 'v' prefix

# package.json
python3 -c "import json; d=json.load(open('package.json')); d['version']='$NEW'; json.dump(d, open('package.json','w'), indent=2); print(open('package.json').read())"

# scripts/b12_mcp_server.py — B12_VERSION constant (note the 'v' prefix)
sed -i '' "s/^B12_VERSION = \".*\"/B12_VERSION = \"v$NEW\"/" scripts/b12_mcp_server.py

# .claude-plugin/plugin.json
python3 -c "import json; d=json.load(open('.claude-plugin/plugin.json')); d['version']='$NEW'; json.dump(d, open('.claude-plugin/plugin.json','w'), indent=2); print(open('.claude-plugin/plugin.json').read())"
```

### 3. Hand-curate CHANGELOG.md

Prepend a new section above the existing entries. Keep it
user-facing — group by what someone using B12 will see, not by commit:

```markdown
## [v11.75.0] — 2026-MM-DD

### Added
- Brief sentence about new feature, file:line if helpful

### Changed
- Brief sentence

### Fixed
- Brief sentence about user-visible bug

### Internal
- (Optional) refactors / test additions / CI changes
```

Skim `git log $(git describe --tags --abbrev=0)..HEAD --oneline` for the
commit range to summarize; ignore `chore(deps)` / Dependabot noise
unless it's user-facing.

### 4. Commit + tag + push

```bash
git add CHANGELOG.md package.json scripts/b12_mcp_server.py .claude-plugin/plugin.json
git commit -m "chore(release): v$NEW"
git tag -a "v$NEW" -m "v$NEW: <one-line summary matching CHANGELOG section title>"
git push origin main --tags
```

### 5. Create the GitHub release

```bash
# Extract the new CHANGELOG section into a release-notes file
awk '/^## /{i++; if(i==2) exit} i==1' CHANGELOG.md | tail -n +2 > /tmp/release-notes.md

gh release create "v$NEW" --title "v$NEW" --notes-file /tmp/release-notes.md
```

If the release contains breaking changes, also pass `--latest=false`
or add a "⚠ Breaking changes" callout to the notes top.

## What NOT to do

- **Do not** add `semantic-release`, `release-please`, `changesets`,
  `release-it`, or any auto-bumper. The toolchain was removed on
  2026-05-24 with a 6459-line lockfile cleanup; re-adding it
  reintroduces the same npm dep tree (handlebars / lodash / tar /
  picomatch transitive vulnerabilities).
- **Do not** create lightweight tags (`git tag v11.75.0`) — always use
  annotated tags (`git tag -a v11.75.0 -m "..."`) so `git describe`
  works and the tag carries release context.
- **Do not** force-push tags. If a release went out wrong, cut a new
  patch version with the fix; don't rewrite history that downstream
  clones already pulled.
- **Do not** delete a release on GitHub once it's been live for >24h —
  same reason. Mark superseded with a `[Deprecated]` prefix in the
  release notes instead.

## Pre-release sanity checks (optional, ~30s)

Before tagging, a quick check that the things that should be in sync
actually are:

```bash
# All three version touchpoints match?
grep -E '"version"' package.json .claude-plugin/plugin.json
grep -E '^B12_VERSION' scripts/b12_mcp_server.py
# CHANGELOG top entry matches?
head -3 CHANGELOG.md
# CI is green on current main?
gh run list --branch main --limit 1
```

## Why manual

- **Not published to npm.** semantic-release's primary value (auto-publish
  to a registry) didn't apply — B12 ships via `git clone` + `install.sh`.
- **Solo maintainer.** Conventional-commit-driven automation paid for
  itself in team settings; for one person, hand-curation is faster
  than wrangling commit prefixes.
- **CHANGELOG quality.** Auto-generated CHANGELOG was a bot-wall of every
  commit subject. Hand-curated by feature/area is more useful for users
  reading "what changed in v11.x?"
- **Supply chain.** The toolchain's transitive deps (semantic-release,
  @semantic-release/git, @semantic-release/npm, @semantic-release/exec)
  pulled in ~250 packages including handlebars/lodash/tar — all of which
  had advisories in May 2026. Removing the toolchain removed the surface.

If the project ever publishes to npm, PyPI, or another registry with
real semver consumers, revisit this decision.
