# B12 Project Instructions

This is a memory system project for Claude Code CLI.

## Project structure
- `hooks/` — Shell/Python scripts that fire at Claude Code lifecycle events (v7)
- `scripts/` — Support modules (write_time_merge, Ebbinghaus decay, migration, patch applier)
- `config/` — Template configuration files for Claude Code settings + launchd plists
- `templates/` — User-facing template files (user profile, etc.)
- `docs/` — Architecture documentation and setup guide

## Development rules
- Hook scripts must be POSIX-compatible shell (bash)
- All hooks must exit 0 for success (non-zero blocks actions)
- Hooks must complete within their timeout (SessionStart: 20s, PreCompact: 30s, SessionEnd: 35s)
- Hook timeouts MUST be >= watchdog timer + 5s (timeout = watchdog + 5)
- Never include personal data, API keys, or file paths in the repo
- Use placeholder paths (`/path/to/...`) in templates
- Use `$HOME` or `~` instead of hardcoded user directories
- Test hooks manually before committing: `echo '{}' | ./hooks/script.sh`

## Key technical constraints
- PreCompact hooks cannot inject context — they can only run side effects
- SessionStart hooks CAN inject context via `additionalContext` in JSON output
- The memory MCP server runs as a child process spawned by Claude Code
- Hook scripts receive JSON on stdin and must output valid JSON on stdout
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

The `install.sh` script copies hook files to `~/.claude/hooks/` (the shared
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
