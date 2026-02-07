# B12 Architecture

## Design principles

1. **Local-first**: All data stays on the user's machine. No cloud dependencies.
2. **Zero manual effort**: The user never needs to say "save this" or "search memory."
3. **Low overhead**: Hooks are fast shell scripts. Memory search adds minimal latency.
4. **Cross-project**: A single database serves all projects. Tags enable filtering.
5. **Recoverable**: PreCompact hook preserves context before it's lost to compaction.
6. **Session-aware**: Each session's summary carries forward to the next one.

## System layers

### Layer 1: Native Auto Memory (built-in)

Claude Code's built-in memory system:
- `MEMORY.md` — first 200 lines loaded into every session's system prompt
- Topic files — referenced from MEMORY.md, loaded on demand
- Path: `~/.claude/projects/<project-hash>/memory/`
- Best for: Stable, high-level project knowledge that should persist permanently

### Layer 2: mcp-memory-service (semantic memory)

External MCP server providing semantic search over stored memories:
- **Database**: SQLite-vec (local file)
- **Embeddings**: MiniLM-L6-v2 (ONNX, runs locally, no API)
- **Quality**: Built-in ONNX ranker for relevance scoring
- **Graph**: Association-based memory connections
- **Backup**: Automatic daily backups
- **Location**: `~/Library/Application Support/mcp-memory/` (macOS)
- Best for: Detailed learnings, decisions, patterns that need semantic search

### Layer 3: Smart hooks (automation glue)

Shell scripts that fire at key lifecycle points:

```
Session lifecycle:

[SessionStart] ─── startup/resume ──> Inject: user profile
       |                                + last session summary
       |                                + memory instructions
       v
[Claude works] ─── uses memory MCP ──> Stores/retrieves silently
       |            tools as needed      Updates user-profile.md
       v
[PreCompact] ──── auto/manual ──────> Stage comprehensive transcript
       |                               summary (15 user msgs +
       |                               10 assistant outputs + files)
       v
[SessionStart] ─── compact ─────────> Recover staged summary
       |                               + instruct Claude to store
       v
[SessionEnd] ──── any reason ───────> Extract session summary
                                       + write latest/history
                                       + log metadata + cleanup
```

### Layer 4: Session summaries (continuity bridge)

Per-project markdown files that bridge between sessions:
- `~/.claude/memory-summaries/{project}-latest.md` — last session's summary
- `~/.claude/memory-summaries/{project}-history.md` — rolling last 5 sessions
- Loaded by SessionStart into the next session's context
- Best for: Short-term continuity ("what did we do last time?")

### Layer 5: User profile (persistent identity)

A markdown file in the project memory directory:
- `~/.claude/projects/<project-hash>/memory/user-profile.md`
- Contains: communication style, preferences, work context, learned patterns
- Claude updates it proactively when learning new preferences
- Loaded by SessionStart into every session
- Best for: Personal context that makes Claude feel like a consistent collaborator

## Hook design details

### SessionStart hook (v2)

**Purpose**: Prime Claude with full context — user identity, last session, and memory instructions.

On `startup` or `resume`:
1. Derives the project memory directory from `$CWD` (same hash format as Claude Code)
2. Loads `user-profile.md` if it exists (first 60 lines)
3. Loads `{project}-latest.md` session summary if it exists (first 50 lines)
4. Combines everything into `additionalContext` JSON
5. Includes behavioral instructions for silent memory management

On `compact`:
1. Checks `~/.claude/memory-staging/` for pre-compaction summaries
2. If found: injects the summary + tells Claude to store key parts permanently
3. If not found: falls back to user profile + last session summary

**Why this approach**: The SessionStart hook's `additionalContext` is injected into Claude's system context, making it as reliable as CLAUDE.md instructions but dynamic and session-aware.

### PreCompact hook (v2)

**Purpose**: Capture comprehensive context before it's lost to compaction.

Process:
1. Parses the ENTIRE transcript JSONL file (not just tail)
2. Extracts up to 15 user messages (500 chars each) — captures intent
3. Extracts up to 10 meaningful assistant messages (800 chars each) — captures work done
4. Tracks all files modified via Edit/Write tool calls
5. Writes structured summary to `~/.claude/memory-staging/precompact-{session_id}.txt`
6. Cleans up staging files older than 2 hours

**Why full transcript**: The v1 approach (`tail -100`) missed earlier context in long sessions. v2 parses everything to capture the full picture before compaction wipes it.

**Why staging files**: PreCompact hooks cannot inject context back into Claude (they're side-effect-only). The staging file is a bridge: PreCompact writes it, the next SessionStart(compact) reads it.

### SessionEnd hook (v2)

**Purpose**: Extract a comprehensive session summary for the next session to use.

Process:
1. Parses the transcript JSONL file
2. Extracts: user messages, assistant messages, tools used, files modified
3. Builds a structured markdown summary with sections:
   - What the user asked (first 10 unique requests)
   - Key outputs (last 8 meaningful assistant messages)
   - Files modified (up to 20)
4. Writes to `{project}-latest.md` (overwritten each session)
5. Appends to `{project}-history.md` (rolling last 5 sessions)
6. Logs session metadata to `sessions.jsonl`
7. Cleans up staging files

**Critical syntax note**: The Python heredoc MUST use `python3 -` (dash) to read from stdin:
```bash
# CORRECT:
python3 - "$ARG1" "$ARG2" << 'PYEOF'
import sys
# sys.argv[1] = $ARG1, sys.argv[2] = $ARG2
PYEOF

# WRONG (Python interprets $ARG1 as script filename):
python3 << 'PYEOF' "$ARG1" "$ARG2"
PYEOF
```

## Cross-project memory

All memories are stored in a single SQLite database. Cross-project recall works because:

1. Every stored memory is tagged with the project name
2. At session start, Claude searches with the current project name AND general terms
3. Relevant memories from other projects surface through semantic similarity
4. Claude can explicitly search for cross-project patterns when working on similar problems

## Multi-setup support

For users with multiple Claude Code setups (e.g., personal + work):

- **MCP server**: Configured globally in `~/.claude.json` — one server, one database
- **Hook scripts**: Stored in `~/.claude/hooks/` with absolute paths
- **Hook config**: Must be added to each setup's `settings.json` separately
- **Memory**: Shared database means both setups contribute to and benefit from the same knowledge base
- **Session summaries**: Per-project, so different projects don't overwrite each other
- **User profile**: Per-project directory, but you can symlink or copy across setups

## Limitations and future work

### Current limitations

1. **No usage pattern learning**: The system doesn't track which memories are frequently accessed vs. never used. Future: PostToolUse hook on memory tools to log access patterns.

2. **English-optimized embeddings**: MiniLM-L6-v2 works well for English technical content but is suboptimal for mixed-language content. Future: Upgrade to a multilingual model.

3. **Session summary quality**: The transcript parser extracts raw text content. It doesn't yet understand conversation structure deeply (e.g., distinguishing decisions from discussions).

### Planned improvements

- **PostToolUse feedback loop**: Track memory search results (found/not found) to improve capture strategy
- **Decay-based archiving**: Automatically archive memories that haven't been accessed in 90+ days
- **Memory consolidation**: Merge similar memories into stronger, deduplicated entries
- **Dashboard**: Web UI for browsing and managing the memory graph
- **0G integration**: Decentralized storage + TEE-based embedding for privacy-preserving cloud memory
