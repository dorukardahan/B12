# Contributing to B12

Thanks for the interest in B12. Before you open a PR, please read this short
contributor guide — it covers the few non-obvious workflow rules and the
validation steps the repo expects.

## Quick start

```bash
git clone https://github.com/dorukardahan/B12.git
cd B12
./install.sh --full    # creates venv, installs deps, deploys hooks
```

After install, restart your AI tool and run `/mcp` (or equivalent) to verify
`B12 · connected`. Hack on hooks under `hooks/`, scripts under `scripts/`,
then re-run `./install.sh --all` to redeploy the hook files to `~/.B12/hooks/`.

## Required reading

The most important rules live in [`CLAUDE.md`](CLAUDE.md) (project root).
Skim it before opening a PR — it covers:

- Hook timeout invariants (`timeout >= watchdog + 5s`)
- Python module naming (underscores only — no hyphens, importer breaks)
- B12_DATA_DIR vs B12_HOOK_DIR separation
- Bash 3.2 compat constraints (no `mapfile`, no `declare -A`, no `<<<`)
- Mini Shai-Hulud 4-step protocol for any new pip/npm dependency
- Documentation sync rule (code changes → README/architecture/CHANGELOG updates)

## Never commit personal or private data

B12 is public. Do not put personal or private data — yours or anyone else's —
into the repo, and remember this covers **more than file contents**: it applies
equally to commit messages, branch and tag names, PR/issue titles, descriptions,
and comments, and release notes.

Specifically, never include:

- Absolute home paths or usernames — use `/path/to/B12`, `$HOME`, or `~`
- Email addresses — for security reports use the channel in [SECURITY.md](SECURITY.md)
- Private/internal project names or codenames — a bare reference to a *public*
  repo is fine; internal codenames and private-project detail are not
- Session IDs / UUIDs, machine names, LAN IPs, or third-party account names
- Internal working / audit / session notes

**Why we are strict:** once it is pushed, a leak in git history or PR metadata
cannot be cleanly removed. Pull-request refs are immutable to the author and old
commit SHAs stay fetchable, so purging one takes a history rewrite plus a GitHub
Support request. Catching it before you push is the only easy fix.

Quick self-check before pushing (scan the diff, then eyeball your commit
messages and PR title/body for the same patterns):

```bash
git diff origin/main... | grep -nE '/Users/|/home/[a-z]|@[a-z0-9._-]+\.(com|ai|io|org)' \
  && echo "review these before pushing" || echo "diff clean"
```

**Maintainers:** if a history rewrite is ever needed to purge something, do it
as one atomic `git filter-repo` + force-push. Never merge a fix PR and *then*
rewrite — the merged PR's tracking refs get orphaned onto the old commits,
recreating the very reference you were trying to remove.

## Validation (run before every commit)

```bash
# 1. Bash syntax check for all hooks
for f in hooks/memory-*.sh; do bash -n "$f" && echo "OK: $f"; done

# 2. Python syntax inside hook heredocs (catches PYEOF block errors)
python3 -c "
import ast, re, glob
for path in glob.glob('hooks/memory-*.sh'):
    content = open(path).read()
    for m in re.finditer(r\"<< 'PYEOF'(.*?)PYEOF\", content, re.DOTALL):
        try:
            ast.parse(m.group(1))
        except SyntaxError as e:
            print(f'SYNTAX ERROR in {path}: {e}')
            break
    else:
        print(f'OK: {path}')
"

# 3. Python module imports
python3 -c "import sys; sys.path.insert(0, 'scripts'); from write_time_merge import merge_or_insert; print('OK: write_time_merge')"

# 4. Hook smoke test (must exit 0)
echo '{}' | ~/.B12/hooks/memory-retrieval.sh
echo "retrieval exit=$?"
```

The CI workflow at `.github/workflows/ci.yml` runs the same checks on every PR.

## PR conventions

- **Title format**: `feat(scope): short summary` / `fix(scope): ...` / `docs(scope): ...` / `chore(scope): ...`.
  Conventional-commit prefixes are kept for readability + future tooling
  (semantic-release was removed on 2026-05-24 in favor of hand-curated
  release notes; the prefix discipline still helps PR triage).
- **Net diff cap**: 300 lines per PR. Larger changes should be split.
- **No co-authored-by trailers for AI assistants** — Claude, Anthropic, etc.
  Only humans + explicit upstream porters get trailers.
- **PR description** must include a one-line summary, a checked test plan,
  and (if touching hooks) confirmation that the regression gate above passes.
- **Codex review**: PRs are expected to pass `@codex review` cleanly or
  document why P3 findings were deferred.

See `.github/PULL_REQUEST_TEMPLATE.md` for the exact checklist.

## Filing issues

Use the templates under `.github/ISSUE_TEMPLATE/`:

- **Bug report** — for unexpected behavior with steps to reproduce
- **Feature request** — for proposed additions or design changes
- **Config / install help** — for setup questions and platform integration

For security issues, **do not open a public issue** — see [SECURITY.md](SECURITY.md).

## Code of conduct

This project follows the Contributor Covenant — see
[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md). Be excellent to each other.

## Dependencies

B12 honors the global Mini Shai-Hulud rule (post-2026-05-11 npm worm).
Before adding any new pip or npm dependency:

1. Research the package on socket.dev for supply-chain risk score
2. Check recent maintainer transfers + publish date
3. Open the dependency PR with the research in the description
4. Reviewers confirm explicitly before merge

This protects the user's local DB (which may contain pasted secrets) from
install-time code execution by a compromised upstream.
