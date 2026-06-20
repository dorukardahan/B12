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

## Release trigger — proactive (applies to ANY AI agent working in this repo)

B12 uses **manual hand-curated releases** (no CI auto-publish — see `docs/releasing.md`),
but the owner should NOT have to remember to ask. Whatever assistant you are — Claude
Code, Codex, Gemini, OpenCode, Hermes, OpenClaw, Cursor, etc. — when you finish a batch
of release-worthy work in this repo (e.g. you merged one or more user-facing PRs to
`main`, or made a version-affecting change), do this **at the end of the task, unprompted**:

1. **Detect** — run `scripts/release.sh --check`. It prints the unreleased commit count
   since the last tag, the user-facing subset (dep-bumps excluded), and a suggested
   semver bump.
2. **Prompt** — if a release is warranted, tell the owner explicitly, e.g.
   *"N commits since vX.Y.Z (suggested vNEXT) — cut a release?"* Do NOT cut it silently;
   the owner gates tag timing.
3. **On approval** — hand-curate the CHANGELOG section yourself (grouped
   `### Added / ### Changed / ### Fixed / ### Internal`, user-facing wording, ignore
   dep-bump noise, PII/secret-clean — maintainer quality, NOT raw commit subjects),
   write it to a notes file, then run:
   `scripts/release.sh <X.Y.Z> <notes-file>` — it syncs all 6 version touchpoints,
   prepends the CHANGELOG, commits, annotated-tags, and creates the GitHub release.
   Use `scripts/release.sh --dry-run <X.Y.Z> <notes-file>` first if unsure.

This preserves the manual-release model (owner approves; the agent supplies maintainer-
quality notes) while removing the toil — the owner never has to say "configure the
release/changelog." NOTE: this is the agent's standing job, NOT a re-introduction of an
auto-publish toolchain (semantic-release/release-please/changesets remain out — see the
Release model rule in `CLAUDE.md` and `docs/releasing.md`).

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

## Grok CLI Native Support (2026-05)

When working in Grok CLI, prefer the native integration located in `.grok/plugins/b12/` and `.grok/skills/b12-memory/`.

Key points:
- The primary interface is the `b12-memory` skill (auto-invokes on memory-related prompts via its description).
- Full B12 MCP tools are available as `B12__memory_*`.
- Use Grok's native subagent system (`task` tool + `fork_context=true` + researcher persona) for deep memory extraction/audits on long sessions.
- Lifecycle automation (PreCompact staging, SessionEnd extraction) is provided via thin hooks in the plugin that delegate to the shared core.
- Verify everything with `grok inspect`, `/skills`, and `grok mcp list`.
- All Grok-specific files are additive — they do not modify any existing Claude Code, OpenCode, or other platform code.

For full details see `docs/grok-integration.md`.

<!-- B12-MEMORY-START -->

# B12 Persistent Memory (Grok)

When working in this repository with Grok CLI, the `b12-memory` skill is available.

**Recommended practices:**
- At the start of sessions, let the `b12-memory` skill load relevant context.
- Use `B12__memory_search`, `B12__memory_store`, `B12__memory_surface` etc. via the MCP tools.
- For complex memory tasks on long sessions, use Grok's native subagent system (`task` tool with `fork_context` and researcher persona).

The B12 engine provides semantic search, Ebbinghaus decay, consolidation, and cross-session memory.

<!-- B12-MEMORY-END -->