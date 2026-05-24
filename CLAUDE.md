# B12 Project Instructions

This is a multi-platform persistent memory system for AI coding assistants (Claude Code, Codex, Gemini, VS Code, Cursor, Kimi, Windsurf, Cline, OpenCode).

## For AI assistants: Setting up B12 for a user

If a user gives you this repo and asks you to set it up:

```bash
cd /path/to/B12
chmod +x install.sh
./install.sh --full    # Creates venv, installs deps, deploys hooks, configures MCP
```

Then restart Claude Code and run `/mcp` to verify `B12 · connected`.

**Key facts:**
- The MCP server (`b12_mcp_server.py`) creates all database tables automatically on first run
- The embedding model (~90MB) downloads on first session — one-time only
- `install.sh --full` handles everything including `~/.claude.json` MCP config
- Claude Code does NOT expand `~` in MCP paths — the installer uses absolute paths

## Project structure
- `hooks/` — Shell/Python scripts that fire at Claude Code lifecycle events
- `scripts/` — Support modules (b12_mcp_server, embed_daemon, write_time_merge, Ebbinghaus decay, etc.)
- `config/` — Template configuration files for all supported platforms + launchd plists
- `templates/` — User-facing template files (user profile, etc.)
- `docs/` — Architecture documentation and setup guide
- `benchmarks/` — Retrieval quality evaluation (LoCoMo)

## Development rules
- Hook scripts must be POSIX-compatible shell (bash)
- All hooks must exit 0 for success (non-zero blocks actions)
- Hooks must complete within their timeout (SessionStart: 20s, PreCompact: 30s, SessionEnd: 35s)
- Hook timeouts MUST be >= watchdog timer + 5s (timeout = watchdog + 5)
- Never include personal data, API keys, or file paths in the repo
- Use placeholder paths (`/path/to/...`) in templates
- Use `$HOME` or `~` instead of hardcoded user directories
- Test hooks manually before committing: `echo '{}' | ./hooks/script.sh`

## Documentation sync rule

When making code changes, ALWAYS update the corresponding documentation:
- **New feature/tool** → update README.md (features list, diagram), docs/architecture.md
- **Changed setup steps** → update README.md (Quick Start), docs/setup.md
- **New script/hook** → update README.md (Project Structure table)
- **Breaking change** → add entry to CHANGELOG.md
- **New config option** → update README.md (Configuration section)
- **Version bump** → update install.sh banner, CHANGELOG.md, README.md changelog section

Documentation lives in: `README.md`, `docs/setup.md`, `docs/architecture.md`, `CHANGELOG.md`

## Key technical constraints
- `B12_DATA_DIR` controls data/state paths (summaries, staging, logs). `B12_HOOK_DIR` controls hook code paths (script imports, embed daemon). They are intentionally separate — data can be per-setup while code stays shared.
- SessionStart context injection has a 6000-char hard cap with progressive trimming (pre-fetch → cross-project → feedback → truncation).
- PreCompact hooks cannot inject context — they can only run side effects
- SessionStart hooks CAN inject context via `additionalContext` in JSON output
- The B12 MCP server (`scripts/b12_mcp_server.py`) runs as a child process spawned by the host application (Claude Code, Cursor, etc.)
- The embed daemon (`scripts/embed_daemon.py`) runs as a background process, communicates via Unix socket
- Hook scripts receive JSON on stdin and must output valid JSON on stdout
- `b12_mcp_server.py` creates all tables via `_ensure_schema()` on startup — no external migration needed for fresh installs
- Python heredocs MUST use `python3 -` (dash) to read from stdin when passing arguments:
  ```bash
  # CORRECT:
  python3 - "$ARG" << 'PYEOF'
  import sys; print(sys.argv[1])
  PYEOF

  # WRONG (Python treats $ARG as script filename):
  python3 << 'PYEOF' "$ARG"
  PYEOF
  ```

## Development workflow

This repo is the **source of truth** for all hook files. The workflow:

1. **Edit** hooks/scripts in this directory (`hooks/`, `scripts/`)
2. **Deploy** to your local Claude Code: `./install.sh --all`
3. **Test** the changes (start a Claude session, trigger the hook)
4. **Commit & push** when satisfied

The `install.sh` script copies hook files to `~/.B12/hooks/` (the shared
location all Claude setups reference). Multiple setups (e.g., `.claude`,
`.claude-work`) share the same hooks directory.

### Validation before committing

```bash
# Check bash syntax for all hooks
for f in hooks/memory-*.sh; do bash -n "$f" && echo "OK: $f"; done

# Check Python syntax inside hooks (extracts heredoc blocks)
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

# Test import of support scripts
python3 -c "import sys; sys.path.insert(0, 'scripts'); from write_time_merge import merge_or_insert; print('OK: write_time_merge')"
```

### Python module naming

Python cannot import modules with hyphens (`-`) in filenames. Always use
underscores (`_`). Example: `write_time_merge.py`, NOT `write-time-merge.py`.

## Language support

Hooks support both English and Turkish:
- **Retrieval**: Unicode-aware keyword extraction (handles ı, ş, ç, ö, ü, ğ)
- **SessionEnd**: Decision/error/learning/preference patterns in both languages
- **Scoring**: Quality filters include Turkish keywords

When adding new patterns, always add both English and Turkish variants.

## Shareability rules
- This repo is designed to be shared publicly
- NO personal information (names, usernames, specific paths like /Users/yourname/)
- NO API keys or secrets
- Use generic examples and placeholders
- User profile template should be empty/generic — users fill in their own info

## Release model

B12 ships **manual hand-curated releases** — there is no semantic-release
or any other auto-publish toolchain. (Semantic-release was removed on
2026-05-24; see commit c497c82 for the rationale: not published to npm,
auto-CHANGELOG was bot-noise, dep tree was the source of most npm
vulnerabilities.)

**Do not re-introduce automated release tooling without explicit owner
approval.** That includes: `semantic-release`, `release-please`,
`changesets`, `release-it`, `auto`, or any other tool that bumps
`package.json` / writes CHANGELOG / creates tags from commit messages.
The release ritual lives in `docs/releasing.md` and is intentional.

When asked to "set up releases" or "automate versioning," **stop and
ask** — the manual ritual is the answer for this repo. Suggesting
automation re-adds the exact dep tree we just removed.
