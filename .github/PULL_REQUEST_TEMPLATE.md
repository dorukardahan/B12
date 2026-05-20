<!--
Thanks for the PR! Fill in the sections below. PRs without a test plan
will not be merged. See CONTRIBUTING.md for the full validation script.
-->

## Summary

<!-- One paragraph: what changed and why. -->

## Test plan

- [ ] `bash -n hooks/memory-*.sh` passes
- [ ] `python3 -c "import ast; ast.parse(open('<changed_file>.py').read())"` passes for every changed `.py`
- [ ] `echo '{}' | ~/.B12/hooks/memory-retrieval.sh` exits 0
- [ ] Net diff under 300 lines (`gh pr view <N> --json additions,deletions`)
- [ ] (if hooks changed) `./install.sh --all` redeploys cleanly
- [ ] (if scripts/ changed) Self-test or smoke run noted in description

## Docs sync

- [ ] `README.md` updated (feature added/removed, platform table change, etc.)
- [ ] `docs/architecture.md` updated (new module, new ingestion path, etc.)
- [ ] `CHANGELOG.md` entry added for user-facing change
- [ ] No personal paths (`/Users/...`) committed; templated via markers if needed

## Anti-Claude trailer check

- [ ] No `Co-Authored-By: ... claude ...` or `... anthropic ...` trailers in any commit
- [ ] No "Generated with Claude Code" footer in this PR description

## Dependencies

- [ ] No new pip/npm deps, OR Mini Shai-Hulud research linked in description
