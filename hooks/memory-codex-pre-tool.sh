#!/bin/bash
# B12 Codex CLI — PreToolUse hook (Plan §2 CX2).
#
# Ports the Claude Code memory-tag-enforce hook to Codex. Only fires on
# tool names that opt into `pre_tool_use_payload` (codex-rs/core/src/
# tools/registry.rs) — at 2026-05-18 GA those are: shell, unified_exec,
# apply_patch, extension_tools, mcp. We register the matcher against
# `mcp__B12__memory_store` so this hook only sees memory writes.
#
# Cloud bridge note: `cloud_exec` / `cloud_apply` are NOT Codex tool
# names — they are CLI subcommands under `codex cloud` (codex-rs/
# cloud-tasks/). PostToolUse matchers on those names would never fire;
# the CLI↔App bridge is captured via rollout-scrape in
# codex_session_end.py instead (already shipping since PR #41 CX0).
# This trade-off is documented in plan doc CX2 row.
#
# Wire input shape: codex-rs/hooks/src/schema.rs:242 PreToolUseCommandInput
#   {session_id, turn_id, transcript_path, cwd, hook_event_name, model,
#    permission_mode, tool_name, tool_input, tool_use_id}
#
# Output: empty (no decision). This hook is observational + tag-mutation
# only; never blocks tool execution. The legacy notify-style mutation
# of tool_input is not surfaced through Codex's PreToolUse contract —
# Codex review on the Claude Code tag-enforce hook (PR #20) showed that
# returning `{"hookSpecificOutput":{"hookEventName":"PreToolUse",
# "additionalContext":...}}` is the right wire shape if we want to
# nudge the model toward better tags without blocking. CX2 does the
# nudge via a system-level annotation, not by rewriting the call.
#
# Fail-open guard outer wrapper.

{
  set -o pipefail 2>/dev/null || true

  B12_BASE="${B12_DATA_DIR:-$HOME/.B12}"
  LOG_DIR="$B12_BASE/memory-logs"
  ERR_LOG="$LOG_DIR/codex-hook-errors.log"
  mkdir -p "$LOG_DIR" 2>/dev/null || true

  INPUT=""
  if [ ! -t 0 ]; then
    INPUT=$(cat)
  fi

  TOOL_NAME=$(printf '%s' "$INPUT" | jq -r '.tool_name // empty' 2>/dev/null)
  CWD=$(printf '%s' "$INPUT" | jq -r '.cwd // empty' 2>/dev/null)

  # Matcher catch-net: hooks.json matcher is the primary filter, but a
  # registry change could surface unexpected tool names here. Hard-skip
  # everything we don't expect.
  case "$TOOL_NAME" in
    mcp__B12__memory_store|memory_store) ;;
    *) exit 0 ;;
  esac

  PROJECT=$(basename "${CWD:-$PWD}")

  # Compose a quiet additional-context nudge — "remind the model that
  # B12 expects [proj:<name>] in every memory_store call". Whether the
  # model honors it is the model's choice; PreToolUse doesn't rewrite
  # tool_input.
  CTX="📝 B12 memory_store tag policy reminder:
- Required tag: \`proj:${PROJECT}\`
- Recommended additional tags: \`user:<setup>\` (e.g. \`user:personal\`), and one of \`decision\` / \`learning\` / \`gotcha\` / \`preference\` so cross-session retrieval can scope.
- Tag values: comma-separated, no spaces around commas.
"

  python3 - "$CTX" << 'PYEOF' 2>/dev/null
import json, sys
print(json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "additionalContext": sys.argv[1],
    }
}))
PYEOF

} 2>>"${ERR_LOG:-/dev/null}" || true
exit 0
