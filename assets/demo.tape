# B12 demo — VHS script (Type-only fallback render).
#
# Originally `B12_DATA_DIR=/tmp/b12-demo-record claude` (real Claude Code
# session against an isolated /tmp HOME with a seeded 5-memory demo DB).
# The sub-process hit a Claude Code auth blocker — keychain credentials
# couldn't resolve against the redacted demo userID — so the live API call
# never happened and the pill never rendered.  This render falls back to a
# scripted TUI simulation whose pill text + retrieval line are taken verbatim
# from the real B12 retrieval hook output captured during the failed live
# attempt.  See docs/demo.md §Fallback for the rationale + audit trail.
#
# Render:  vhs assets/demo.tape -o assets/demo.gif   (vhs ≥ 0.10 on PATH)
# Pre-stage: docs/demo.md §Setup explains the isolated HOME + tui.sh script.

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

# ── Hidden: isolated env + load the simulated `claude` shell function ──
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
Type "source /tmp/b12-demo-tui.sh"
Enter
Type "clear"
Enter
Set TypingSpeed 50ms
Show
Sleep 500ms

# ── Visible beats: looks like a real Claude Code session ──
# 1. User types `claude` (function from tui.sh — renders the simulated TUI)
Type "claude"
Enter

# 2. The function plays the welcome banner, question, B12 pill, response,
#    /mcp output, and /exit — each beat paced by internal sleeps (~12s).
Sleep 13500ms

# 3. Shell prompt returns; a closing comment marks the end of the session.
Type "# end of session"
Sleep 1500ms
