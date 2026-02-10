# B12 Project Instructions

This is a memory system project for Claude Code CLI.

## Project structure
- `hooks/` — Shell/Python scripts that fire at Claude Code lifecycle events (v7)
- `scripts/` — Support modules (write-time merge, Ebbinghaus decay, migration, patch applier)
- `config/` — Template configuration files for Claude Code settings + launchd plists
- `templates/` — User-facing template files (user profile, etc.)
- `docs/` — Architecture documentation and setup guide

## Development rules
- Hook scripts must be POSIX-compatible shell (bash)
- All hooks must exit 0 for success (non-zero blocks actions)
- Hooks must complete within their timeout (SessionStart: 10s, PreCompact: 15s, SessionEnd: 15s)
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

## Shareability rules
- This repo is designed to be shared publicly
- NO personal information (names, usernames, specific paths like /Users/yourname/)
- NO API keys or secrets
- Use generic examples and placeholders
- User profile template should be empty/generic — users fill in their own info
