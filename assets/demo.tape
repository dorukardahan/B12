# B12 demo — VHS script
# Renders a ≤45s overview of B12: install one-liner → /mcp → memory_store → memory_search → cross-tool note.
# Render locally:  vhs assets/demo.tape -o assets/demo.gif

Output assets/demo.gif

Set Shell "bash"
Set Theme "Dracula"
Set FontSize 16
Set Width 1200
Set Height 700
Set TypingSpeed 25ms
Set PlaybackSpeed 1.0
Set Padding 24
Set Margin 8
Set WindowBar Colorful
Set WindowBarSize 40

# 1. Install one-liner
Type "# 1. Install B12 (venv + MCP + hooks, one command)"
Sleep 400ms
Enter
Type "git clone https://github.com/dorukardahan/B12.git && cd B12 && ./install.sh --full"
Sleep 400ms
Enter
Sleep 1500ms

# 2. Restart Claude Code → /mcp
Ctrl+L
Type "# 2. Restart Claude Code, then type /mcp"
Sleep 300ms
Enter
Type "/mcp"
Sleep 400ms
Enter
Type "  ▸ B12 · connected ✓   (tools: memory_store / search / update / quality …)"
Sleep 1500ms
Enter

# 3. Store a memory
Ctrl+L
Type "# 3. Store a memory — hash-deduped at the MCP tool, embedded async"
Sleep 300ms
Enter
Type "memory_store("
Enter
Type "  content='The api/auth.py module uses HS256 JWT with 15-min expiry.',"
Enter
Type "  metadata={'tags':['proj:webshop','area:auth'], 'type':'architecture'}"
Enter
Type ")"
Sleep 300ms
Enter
Type "  ▸ Stored memory (hash: 7c3b9e2a4f81d20a, id: 4218)"
Sleep 1500ms
Enter

# 4. Search across sessions and tools
Ctrl+L
Type "# 4. Search — same DB powers Claude Code, Codex, Cursor, Cline, Zed, …"
Sleep 300ms
Enter
Type "memory_search(query='auth jwt expiry', mode='hybrid', limit=3)"
Sleep 400ms
Enter
Type "  1. api/auth.py uses HS256 JWT with 15-min expiry …   (0.94)"
Enter
Type "  2. refresh-token rotation lives in api/refresh.py …  (0.81)"
Enter
Type "  3. middleware/jwt.py decodes both HS256 and RS256 …  (0.77)"
Sleep 1800ms
Enter

# 5. Final card
Ctrl+L
Type "# One SQLite DB · one MCP server · 13 platforms · local-first."
Sleep 500ms
Enter
Type "# Memory you stored once shows up everywhere you code."
Sleep 2500ms
