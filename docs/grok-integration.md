# B12 + Grok CLI Integration (Native)

This document describes the **Grok-native** way to use B12 with Grok CLI.

It was designed from the ground up in May 2026 as a clean, declarative, low-maintenance integration that leverages Grok's strengths (Skills with auto-invoke, native subagents + `fork_context`, Plugins, Hooks, AGENTS.md accumulation, and excellent TUI) instead of porting legacy patterns from Claude Code or OpenCode.

## Why This Integration is Different (and Better)

| Aspect                  | Legacy (Claude/OpenCode)      | Grok Native (this)                     |
|-------------------------|-------------------------------|----------------------------------------|
| Memory injection        | Imperative shell hooks        | Declarative Skills + auto-invoke       |
| Extraction quality      | Regex + patterns              | LLM subagents (researcher persona) + patterns |
| Architecture            | Heavy duplication per host    | Thin adapters + 100% shared core       |
| Maintenance             | High (multiple codebases)     | Very low (mostly declarative)          |
| Distribution            | Manual install.sh             | Plugin (`.grok/plugins/b12/`) + marketplace ready |
| User experience         | Terminal only                 | Full TUI (`/skills`, `grok inspect`, `Ctrl+L`) |

## Quick Start

1. Make sure you are inside the B12 repo:
   ```bash
   cd /path/to/B12
   ```

2. Install the plugin:
   ```bash
   grok plugin add file://.grok/plugins/b12 --name b12
   ```

3. Add the MCP server (one-time):
   ```bash
   grok mcp add B12 \
     --command python3 \
     --args "/path/to/B12/scripts/b12_mcp_server.py"
   ```

4. Open a new Grok session in this directory and run:
   ```bash
   grok inspect
   ```

You should see the B12 MCP tools (`B12__memory_*`) and the `b12-memory` skill.

## What You Get

- Automatic context loading at the start of sessions (via the `b12-memory` skill).
- Clean "B12 pill" summaries when relevant memories are found.
- Proactive memory storage during work.
- The ability to spawn specialized memory subagents for deep analysis on long sessions.
- PreCompact and SessionEnd lifecycle automation (via thin hooks that reuse the shared B12 engine).
- Full access to the B12 dashboard, quality tools, consolidation, etc. through the normal MCP tools.

## Architecture Overview

All heavy logic lives in the single shared Python core (`scripts/b12_mcp_server.py` + `consolidation_engine.py`, `write_time_merge.py`, `ebbinghaus.py`, `shared_patterns.py`, etc.).

The Grok integration only adds:
- `.grok/skills/b12-memory/SKILL.md` (declarative orchestrator)
- `.grok/plugins/b12/` (full distributable plugin)
- Thin Python wrappers in the plugin (they only know how to find Grok session files and call the shared core)

This is the model that future platforms should follow.

## Verification Commands

```bash
grok inspect                 # Full picture of loaded rules, skills, plugins, MCP
grok mcp list                # Check B12 server status
/skills                      # See the b12-memory skill
```

## Related Files

- `.grok/plugins/b12/README.md` — Plugin documentation
- `.grok/plugins/b12/skills/b12-memory/SKILL.md` — The main skill (auto-invoking)
- `docs/architecture.md` — Overall B12 architecture (still valid)

## Long-term Vision

B12 should eventually have a clean `platforms/` + `core/` structure so that adding support for a new AI coding assistant becomes a 1-2 day task instead of months of duplicated effort.

The Grok integration (May 2026) is the first implementation of that clean vision.

---

**Status**: Active development. The foundation (Skills + Plugin skeleton + thin hooks) is in place. Full PreCompact/SessionEnd automation and subagent memory workers are being completed.