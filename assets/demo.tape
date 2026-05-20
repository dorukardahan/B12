# B12 demo — VHS script (Ink-based Claude Code TUI sim).
#
# Records a high-fidelity simulation of a B12-augmented Claude Code session
# rendered by React + Ink (the same framework Claude Code itself uses).
# The banner, spinner, tool call rendering, response formatting, and /mcp
# layout match Claude Code v2.1.x pixel-for-pixel.  The retrieval pill count
# is live — `liveCount()` in /tmp/b12-demo-app/index.js shells out to
# sqlite3 against the isolated demo DB at /tmp/b12-demo-home/Library/...
#
# This is a SIM, not a live Claude Code session — sub-process keychain auth
# refuses to resolve in any HOME-redirected demo HOME (`claude auth status`
# returns loggedIn=false even with full real oauthAccount copied over);
# using the real HOME would surface the maintainer's real memory DB.  See
# docs/demo.md §Live-session blocker for the full trace.
#
# Render:  vhs assets/demo.tape -o assets/demo.gif   (vhs ≥ 0.10 on PATH)
# Pre-stage: docs/demo.md §Setup — seeds the DB and writes the Ink app.

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

# ── Hidden: isolated env + alias `claude` to the Ink demo app ──
Hide
Set TypingSpeed 0ms
Type "export B12_DATA_DIR=/tmp/b12-demo-record"
Enter
Type "export PS1='demo $ '"
Enter
Type "cd /tmp/b12-demo-work"
Enter
Type "alias claude='node /tmp/b12-demo-app/index.js'"
Enter
Type "clear"
Enter
Set TypingSpeed 50ms
Show
Sleep 600ms

# 1. Launch — the Ink app renders the banner, auto-types the Turkish
#    question, fires a Memory tool call, shows the live retrieval pill,
#    walks through the 3-point response, runs /mcp, types /exit.
Type "claude"
Enter
Sleep 17000ms
