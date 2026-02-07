# B12 Project Instructions

This is a memory system project for Claude Code CLI.

## Project structure
- `hooks/` — Shell scripts that fire at Claude Code lifecycle events
- `config/` — Template configuration files for Claude Code settings
- `docs/` — Architecture documentation and setup guide

## Development rules
- Hook scripts must be POSIX-compatible shell (bash)
- All hooks must exit 0 for success (non-zero blocks actions)
- Hooks must complete within their timeout (default: 10-15 seconds)
- Never include personal data, API keys, or file paths in the repo
- Use placeholder paths (`/path/to/...`) in templates
- Test hooks manually before committing: `echo '{}' | ./hooks/script.sh`

## Key technical constraints
- PreCompact hooks cannot inject context — they can only run side effects
- SessionStart hooks CAN inject context via `additionalContext` in JSON output
- The memory MCP server runs as a child process spawned by Claude Code
- Hook scripts receive JSON on stdin and must output valid JSON on stdout
