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
  Semantic-release reads the prefix.
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
