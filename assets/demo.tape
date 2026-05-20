# B12 demo — VHS script (high-fidelity sim render).
#
# Records a faithful Claude Code v2.1.x TUI walkthrough whose retrieval pill
# count is LIVE — the sim shells out to the real `~/.B12/hooks/memory-retrieval.sh`
# against an isolated demo DB at /tmp/b12-demo-home/Library/Application
# Support/mcp-memory/sqlite_vec.db (seeded with 5 public-facing memories from
# README and CLAUDE.md).  The shell uses `gum spin` for the thinking spinner
# and a BG_GRAY-styled input box that captures VHS-typed input via `read -r`.
#
# This is a SIM, not a live Claude Code session — sub-process keychain auth
# refuses to resolve against any HOME-redirected oauthAccount, and using the
# real HOME would expose the unrelated real memory DB.  Live attempts and the
# fallback rationale are documented in docs/demo.md §Live-session blocker.
#
# Render:  vhs assets/demo.tape -o assets/demo.gif   (vhs ≥ 0.10 on PATH)
# Pre-stage: docs/demo.md §Setup spells out /tmp/b12-demo-sim.sh + seeded DB.

Output assets/demo.gif

Set Shell "bash"
Set Theme "Dracula"
Set FontSize 16
Set Width 1200
Set Height 700
Set TypingSpeed 50ms
Set PlaybackSpeed 1.0
Set Padding 24
Set WindowBar Colorful
Set WindowBarSize 40

# ── Hidden: isolated env + load the sim shell function ──
Hide
Set TypingSpeed 0ms
Type "export HOME=/tmp/b12-demo-home"
Enter
Type "export B12_DATA_DIR=/tmp/b12-demo-record"
Enter
Type "export PS1='demo $ '"
Enter
Type "cd /tmp/b12-demo-work"
Enter
Type "source /tmp/b12-demo-sim.sh"
Enter
Type "clear"
Enter
Set TypingSpeed 50ms
Show
Sleep 500ms

# 1. Launch the sim (renders banner, awaits input)
Type "claude"
Enter
Sleep 3500ms

# 2. Turkish recall question — triggers LIVE B12 retrieval against /tmp DB
Type "B12 nasıl çalışıyor? MCP server nerede tanımlı?"
Sleep 600ms
Enter

# 3. Spinner + live pill count + memory-cited Turkish response
Sleep 9500ms

# 4. /mcp — confirms the B12 MCP entry
Type "/mcp"
Sleep 400ms
Enter
Sleep 3500ms

# 5. /exit
Type "/exit"
Sleep 400ms
Enter
Sleep 1000ms
