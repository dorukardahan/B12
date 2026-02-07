# B12 Architecture

## Design principles

1. **Local-first**: All data stays on the user's machine. No cloud dependencies.
2. **Zero manual effort**: The user never needs to say "save this" or "search memory."
3. **Low overhead**: Hooks are fast shell scripts. Memory search adds minimal latency.
4. **Cross-project**: A single database serves all projects. Tags enable filtering.
5. **Recoverable**: PreCompact hook preserves context before it's lost to compaction.

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

[SessionStart] ─── startup/resume ──> Inject memory instructions
       |                                + project context
       v
[Claude works] ─── uses memory MCP ──> Stores/retrieves silently
       |            tools as needed
       v
[PreCompact] ──── auto/manual ──────> Stage transcript summary
       |                               to temp file
       v
[SessionStart] ─── compact ─────────> Recover staged summary
       |                               + instruct Claude to store
       v
[SessionEnd] ──── any reason ───────> Log session metadata
                                       + cleanup staging files
```

## Hook design details

### SessionStart hook

**Purpose**: Make Claude aware of the memory system and prime it with project context.

On `startup` or `resume`:
- Returns `additionalContext` JSON telling Claude about available memory tools
- Includes current project name and path for context tagging
- Instructions are designed to make Claude proactive about saving/retrieving

On `compact`:
- Checks `~/.claude/memory-staging/` for pre-compaction summaries
- If found: injects the summary and tells Claude to store key parts permanently
- If not found: tells Claude to search memory for relevant context

**Why this approach**: The SessionStart hook's `additionalContext` is injected into Claude's system context, making it as reliable as CLAUDE.md instructions but dynamic and session-aware.

### PreCompact hook

**Purpose**: Capture the most recent context before it's lost to compaction.

Process:
1. Reads the last 100 lines of the transcript JSONL file
2. Extracts text content from assistant messages (the actual work output)
3. Takes the last 5 meaningful messages (truncated to 500 chars each)
4. Writes to `~/.claude/memory-staging/precompact-{session_id}.txt`
5. Cleans up staging files older than 1 hour

**Why staging files**: PreCompact hooks cannot inject context back into Claude (they're side-effect-only). The staging file is a bridge: PreCompact writes it, the next SessionStart(compact) reads it.

### SessionEnd hook

**Purpose**: Analytics and cleanup.

Process:
1. Removes any remaining staging files for this session
2. Appends session metadata (project, reason, timestamp) to `sessions.jsonl`
3. Rotates the log file when it exceeds 1000 entries

**Why logging**: Session logs enable future analytics — which projects are most active, how sessions end (clean vs. interrupted), average session duration patterns.

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

## Limitations and future work

### Current limitations (score: 9.5/10)

1. **No usage pattern learning** (0.5 point gap): The system doesn't track which memories are frequently accessed vs. never used. Future: PostToolUse hook on memory tools to log access patterns.

2. **English-optimized embeddings** (0.5 point gap): MiniLM-L6-v2 works well for English technical content but is suboptimal for mixed-language content (e.g., Turkish + English). Future: Upgrade to Nomic Embed v2 or a multilingual model.

### Planned improvements

- **PostToolUse feedback loop**: Track memory search results (found/not found) to improve capture strategy
- **Decay-based archiving**: Automatically archive memories that haven't been accessed in 90+ days
- **Memory consolidation**: Merge similar memories into stronger, deduplicated entries
- **Dashboard**: Web UI for browsing and managing the memory graph
- **0G integration**: Decentralized storage + TEE-based embedding for privacy-preserving cloud memory
