# B12 Codex CLI Port — Implementation Plan

> **Purpose**: This file is the source of truth for porting B12 to Codex CLI.
> Any session (Claude Code or Codex) can pick up from any step.
> Check off steps as completed. Do NOT delete completed steps — mark them.

**Date**: 2026-02-25
**Status**: Layer 1 complete, Layer 2 complete (pending E2E user test)
**Branch**: `feat/codex-support`

---

## Architecture Mapping

| B12 Concept | Claude Code | Codex CLI | Port Strategy |
|---|---|---|---|
| MCP server | `~/.claude.json` (JSON) | `~/.codex/config.toml` (TOML) | Config template + installer |
| Context injection | `CLAUDE.md` | `AGENTS.md` | Write Codex-specific B12 instructions |
| Skills | `~/.claude/skills/` | `~/.agents/skills/` or `~/.codex/skills/` | Create B12 Codex skill |
| Lifecycle hooks | 6 events in `settings.json` | 1 notify hook only | AGENTS.md instructions + notify |
| Session transcript | `~/.claude/projects/*/` JSONL | `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` | Adapter for different format |
| User prompt history | (in transcript) | `~/.codex/history.jsonl` (user msgs only) | Not needed if rollout exists |
| Config dir env var | `B12_DATA_DIR` | `CODEX_HOME` (Codex native) | Detect platform at runtime |
| Settings merge | `settings.json` hooks key | `config.toml` notify key | TOML injection |

---

## Codex Rollout JSONL Format (Verified)

Location: `~/.codex/sessions/YYYY/MM/DD/rollout-{datetime}-{session_id}.jsonl`

```
Line types:
  {type: "session_meta",  payload: {id, timestamp, cwd, originator, cli_version, source, model_provider}}
  {type: "response_item", payload: {type: "message", role: "user"|"developer"|"assistant", content: [{type: "input_text"|"output_text", text: "..."}]}}
  {type: "response_item", payload: {type: "function_call", name: "shell"|"apply_patch"|"mcp__*", arguments: "..."}}
  {type: "response_item", payload: {type: "function_call_output", output: "..."}}
  {type: "event_msg",     payload: {type: "task_started"|"user_message"|...}}
```

Mapping to Claude Code transcript:
- Claude `type: "human"` → Codex `response_item, role: "user"`
- Claude `type: "assistant"` → Codex `response_item, role: "assistant"`
- Claude `tool_use` block → Codex `response_item, type: "function_call"`
- Claude `tool_result` block → Codex `response_item, type: "function_call_output"`
- Claude tool names: `Edit`, `Write`, `Read` → Codex: `shell`, `apply_patch`, `mcp__*`

---

## What Works As-Is (Zero Changes)

| Component | Why | File |
|---|---|---|
| `embed_daemon.py` | Pure Unix socket server, no platform coupling | `scripts/embed_daemon.py` |
| `shared_patterns.py` | Pure regex, no platform coupling | `scripts/shared_patterns.py` |
| `write_time_merge.py` | Pure SQLite + daemon socket | `scripts/write_time_merge.py` |
| `b12_mcp_server.py` | MCP protocol — Codex supports MCP | `scripts/b12_mcp_server.py` |
| `graph_enrich.py` | Pure SQLite + daemon | `scripts/graph_enrich.py` |
| SQLite database | Same DB file, same schema | `~/Library/.../sqlite_vec.db` |

---

## Layer 1: MCP + AGENTS.md (Immediate Value)

### Goal
Codex CLI can use B12's 4 memory tools via MCP. AGENTS.md tells GPT to use them.
Aynı SQLite DB — Claude Code'daki memory'ler Codex'te aranabilir ve tersi.

### Step 1.1: Codex Config Template
- [x] Create `config/codex-config-template.toml`

```toml
# B12 Memory System MCP Server
# Merge into ~/.codex/config.toml
[mcp_servers.B12]
command = "__VENV_PYTHON__"
args = ["__SCRIPT_PATH__"]
enabled = true
startup_timeout_sec = 30

[mcp_servers.B12.env]
MCP_EMBEDDING_MODEL = "paraphrase-multilingual-MiniLM-L12-v2"
MCP_MAX_RESPONSE_CHARS = "40000"
```

Installer replaces `__VENV_PYTHON__` and `__SCRIPT_PATH__` with absolute paths.

### Step 1.2: B12 AGENTS.md Section
- [x] Create `config/codex-agents-template.md`

Content: B12 memory behavioral instructions adapted for Codex.
Key differences from CLAUDE.md version:
- Tool names are `mcp__B12__memory_store` etc. (same as Claude Code — MCP naming is standard)
- No hook-based auto-retrieval — must instruct model to search proactively
- No auto-session-summary — must instruct model to store findings before session ends
- Reference Codex tool names (`shell`, `apply_patch`) not Claude names (`Read`, `Edit`)

### Step 1.3: Update install.sh
- [x] Add `--codex` flag to install.sh
- [x] Detect `~/.codex/config.toml` exists
- [x] Inject B12 MCP server into `config.toml` (TOML format, NOT JSON)
- [x] Append B12 section to `~/.codex/AGENTS.md` (don't overwrite existing content)
- [x] Create B12 data directories (reuse same dirs — shared with Claude Code)

Key installer changes:
```bash
# New flag
--codex    # Install B12 to Codex CLI setup

# New function: inject_codex_mcp_config()
# Reads config/codex-config-template.toml
# Replaces __VENV_PYTHON__ and __SCRIPT_PATH__
# Appends [mcp_servers.B12] section to ~/.codex/config.toml
# Must NOT duplicate if already present (check first)

# New function: inject_codex_agents()
# Reads config/codex-agents-template.md
# Appends to ~/.codex/AGENTS.md (with separator comment)
# Must NOT duplicate if already present (check for marker)
```

### Step 1.4: Verify MCP Connection
- [ ] Run `codex` CLI — **USER MANUAL TEST**
- [ ] Type `/mcp` — should show `B12 · connected` — **USER MANUAL TEST**
- [ ] Test: "search my memories for B12" — should return results from Claude Code sessions — **USER MANUAL TEST**
- [ ] Test: "store a test memory" — should persist to SQLite — **USER MANUAL TEST**

### Step 1.5: Documentation
- [x] Update README.md — add Codex CLI support section
- [x] Update docs/setup.md — add Codex installation steps
- [x] Update CHANGELOG.md — add v10.2 entry

---

## Layer 2: Notify Hook + Codex Skill (Partial Automation)

### Goal
Session-end processing via notify hook. B12 Skill for structured memory workflows.

### Step 2.1: Codex Notify Hook Script
- [x] Create `hooks/b12-codex-notify.sh`

This script is triggered by Codex's `agent-turn-complete` notify hook.
It receives JSON with `type`, `thread-id`, `turn-id`, `input-messages`, `last-assistant-message`.

What it does:
1. Check if this is a "session end" signal (heuristic: last message is short/empty, or explicit /quit)
2. Find the rollout JSONL file for this thread-id
3. Parse it using a Codex-specific transcript adapter
4. Extract decisions, learnings, errors, preferences (reuse `shared_patterns.py`)
5. Generate session summary
6. Store to SQLite via direct queries (same as `memory-session-end.sh`)

Config in `~/.codex/config.toml`:
```toml
notify = ["bash", "-lc", "~/.codex/hooks/b12-codex-notify.sh"]
```

**Challenge**: Codex notify fires on EVERY turn, not just session end.
**Solution**: Debounce — only process if >5 minutes since last run for this session.
Store last-processed timestamp in a state file.

### Step 2.2: Transcript Adapter Module
- [x] Create `scripts/transcript_adapter.py`

```python
class TranscriptAdapter:
    """Unified interface for Claude Code and Codex transcripts."""

    @staticmethod
    def detect_format(path: str) -> str:
        """Returns 'claude' or 'codex' based on first line."""

    def parse(self, path: str) -> list[Message]:
        """Returns normalized messages regardless of source format."""

    class Message:
        role: str           # "user" | "assistant" | "system"
        content: str        # Text content
        tool_uses: list     # [{name, input, output}]
        files_modified: list
        timestamp: str
```

This lets `memory-session-end.sh` and `b12-codex-notify.sh` share the same extraction logic.

### Step 2.3: B12 Codex Skill
- [x] Create `skills/b12/SKILL.md`

```yaml
---
name: b12-memory
description: >
  Use when starting a session, searching for context, or storing important findings.
  Automatically activated at session start. Provides persistent cross-session memory.
---
```

Skill content instructs Codex to:
1. At session start: call `mcp__B12__memory_search` with project name + "recent work"
2. When user asks about past work: search memory with relevant keywords
3. Before session end: store important decisions, learnings, and errors
4. Tag format: `proj:{project}`, `user:{username}`
5. Memory types: architecture, decision, pattern, gotcha, preference, progress

### Step 2.4: Update Installer for Layer 2
- [x] Copy `b12-codex-notify.sh` to `~/.claude/hooks/` (shared hook location)
- [x] Set `notify` in `config.toml` (merge, don't overwrite existing notify)
- [x] Copy B12 skill to `~/.codex/skills/b12/`
- [x] Copy `transcript_adapter.py` + `codex_session_end.py` to `~/.claude/hooks/scripts/` (shared)

### Step 2.5: Test Layer 2
- [x] Transcript adapter tested with both formats (Claude Code + Codex)
- [x] codex_session_end.py tested — memory ID 324 stored with correct schema
- [x] install.sh idempotency verified (no duplicate notify/MCP entries)
- [ ] Start Codex session, verify skill activates — **USER MANUAL TEST**
- [ ] Work on something, end session, verify notify hook fires — **USER MANUAL TEST**
- [ ] Start Claude Code session — search for Codex session's memories — **USER MANUAL TEST**

---

## Layer 3: Future — When Codex Ships Lifecycle Hooks

### Tracking
- GitHub Issue: https://github.com/openai/codex/issues/12190 (governance hooks)
- GitHub PR: https://github.com/openai/codex/pull/9796 (comprehensive hooks — closed)
- GitHub Discussion: https://github.com/openai/codex/discussions/2150

### Pre-Built Adapters (do now, wire later)
- [ ] Create `scripts/hook_adapter.py` — abstract interface for platform-specific hooks

```python
class HookAdapter:
    """Platform-agnostic hook handler."""

    def on_session_start(self, cwd: str, session_id: str) -> str:
        """Returns context string to inject."""

    def on_user_prompt(self, prompt: str, cwd: str) -> str:
        """Returns relevant memories for this prompt."""

    def on_session_end(self, transcript_path: str, cwd: str):
        """Extracts and stores session summary."""

    def on_pre_tool_use(self, tool_name: str, tool_input: dict) -> dict:
        """Returns modified tool input (e.g., auto-tags)."""

    def on_post_tool_use(self, tool_name: str, tool_input: dict, tool_output: str):
        """Side effects after tool use (feedback, working context)."""
```

### Hook Mapping (When Available)
| Codex Hook (Proposed) | Claude Code Hook | B12 Handler |
|---|---|---|
| `pre-command` | `PreToolUse` | Tag enforcement, input validation |
| `post-command` | `PostToolUse` | Feedback logging, working context |
| `on-session-start` | `SessionStart` | Context injection |
| `on-session-end` | `SessionEnd` | Summary extraction |

---

## Risk Register

| # | Risk | Likelihood | Impact | Mitigation | Status |
|---|---|---|---|---|---|
| R1 | Codex sandbox blocks embed daemon socket | Medium | High | Test with `workspace-write` mode, add `/tmp` to writable_roots | Untested |
| R2 | Codex sandbox blocks venv Python access | Low | High | Venv is in `~/.local/` which should be readable; test | Untested |
| R3 | GPT ignores AGENTS.md memory instructions | High | Medium | Reinforce via Skill, add to AGENTS.md twice (top + bottom) | By design |
| R4 | Notify hook fires too often (every turn) | High | Low | Debounce by session + timestamp, skip if <5min since last | Designed |
| R5 | Rollout JSONL format changes between Codex versions | Medium | Medium | Version-check in transcript_adapter.py, fail gracefully | Designed |
| R6 | TOML injection corrupts existing config.toml | Medium | High | Parse TOML properly (python tomllib), don't string-append | Designed |
| R7 | Existing notify hook in config.toml gets overwritten | Medium | High | Chain: wrap existing + B12 in a dispatcher script | Designed |

---

## File Changes Summary

### New Files
| File | Layer | Purpose |
|---|---|---|
| `config/codex-config-template.toml` | 1 | MCP server config for Codex |
| `config/codex-agents-template.md` | 1 | B12 memory instructions for AGENTS.md |
| `hooks/b12-codex-notify.sh` | 2 | Notify hook for session-end processing |
| `scripts/transcript_adapter.py` | 2 | Unified transcript parser (Claude + Codex) |
| `skills/b12/SKILL.md` | 2 | B12 Codex Skill definition |

### Modified Files
| File | Layer | Change |
|---|---|---|
| `install.sh` | 1 | Add `--codex` flag, TOML injection, AGENTS.md merge |
| `README.md` | 1 | Add Codex CLI support section |
| `docs/setup.md` | 1 | Add Codex installation steps |
| `CHANGELOG.md` | 1 | Add v10.2 entry |
| `CLAUDE.md` | 1 | Add Codex support note |

### Unchanged Files
All existing hooks (`memory-*.sh`) and scripts (`b12_mcp_server.py`, `embed_daemon.py`, etc.) remain unchanged. Codex support is additive — nothing breaks for Claude Code.

---

## Execution Order

### Phase A: Layer 1 Implementation (Claude Code session)
```
1. Create config/codex-config-template.toml
2. Create config/codex-agents-template.md
3. Update install.sh (--codex flag)
4. Update docs (README, setup.md, CHANGELOG)
5. Commit: "feat: Codex CLI support — Layer 1 (MCP + AGENTS.md)"
6. Deploy: ./install.sh --codex
7. Test in Codex CLI
```

### Phase B: Layer 2 Implementation (Claude Code or Codex session)
```
1. Analyze rollout JSONL format in detail (more samples)
2. Create scripts/transcript_adapter.py
3. Create hooks/b12-codex-notify.sh
4. Create skills/b12/SKILL.md
5. Update install.sh (Layer 2 components)
6. Commit: "feat: Codex CLI support — Layer 2 (notify hook + skill)"
7. Deploy and test end-to-end
```

### Phase C: Cross-Platform Testing
```
1. Claude Code session → store memory → Codex session → search → found?
2. Codex session → store memory → Claude Code session → search → found?
3. Long Codex session → notify hook → summary stored?
4. Codex session → B12 skill activates → proactive memory search?
```

---

## Acceptance Criteria

### Layer 1 (Minimum Viable)
- [ ] `codex` CLI shows `B12 · connected` in `/mcp`
- [ ] `mcp__B12__memory_search` returns results from existing Claude Code memories
- [ ] `mcp__B12__memory_store` persists to shared SQLite DB
- [ ] AGENTS.md instructs GPT to use memory tools
- [ ] `./install.sh --codex` completes without errors
- [ ] Claude Code installation is NOT affected by Codex changes

### Layer 2 (Automation)
- [ ] Notify hook fires after Codex session
- [ ] Session summary extracted from rollout JSONL
- [ ] Summary stored in SQLite with correct project/session tags
- [ ] B12 Skill provides memory workflow guidance
- [ ] Cross-platform memory sharing verified (Claude ↔ Codex)
