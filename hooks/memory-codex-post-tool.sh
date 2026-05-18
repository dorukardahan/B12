#!/bin/bash
# B12 Codex CLI — PostToolUse hook (Plan §2 CX2).
#
# Captures file-modification telemetry. Matchers in hooks.json target
# `shell` and `apply_patch` (the two in-coverage tools that mutate
# files); other tool names get a no-op skip even if the matcher
# expression accidentally widens.
#
# CLI↔App bridge note: the plan called for matchers on `cloud_exec` and
# `cloud_apply` to capture App-spawned cloud-task completions. Inspection
# of codex-rs/core/src/tools/handlers/ at 2026-05-18 confirms there is no
# tool handler by either name — `codex cloud` is a CLI subcommand
# (cloud-tasks/cli.rs), not a tool, so PreToolUse/PostToolUse matchers
# would never fire. CX2 falls back to the rollout-scrape path already
# shipping in scripts/codex_session_end.py: cloud-task rollouts land in
# ~/.codex/sessions/.../rollout-*.jsonl just like local sessions, and
# Stop hook's debounced rollout processor picks them up via the
# `transcript_path` field. Trade-off documented in plan doc CX2 row.
#
# Wire input shape: codex-rs/hooks/src/schema.rs:279 PostToolUseCommandInput
#   {..., tool_name, tool_input, tool_response, tool_use_id}
#
# Output: empty.
#
# Fail-open guard outer wrapper.

{
  set -o pipefail 2>/dev/null || true

  B12_BASE="${B12_DATA_DIR:-$HOME/.B12}"
  LOG_DIR="$B12_BASE/memory-logs"
  ERR_LOG="$LOG_DIR/codex-hook-errors.log"
  TELE_LOG="$LOG_DIR/codex-post-tool.log"
  mkdir -p "$LOG_DIR" 2>/dev/null || true

  INPUT=""
  if [ ! -t 0 ]; then
    INPUT=$(cat)
  fi

  TOOL_NAME=$(printf '%s' "$INPUT" | jq -r '.tool_name // empty' 2>/dev/null)
  SID=$(printf '%s' "$INPUT" | jq -r '.session_id // empty' 2>/dev/null)

  # Tool-name allowlist — defensive guard against matcher widening.
  case "$TOOL_NAME" in
    shell|apply_patch|unified_exec) ;;
    *) exit 0 ;;
  esac

  # Extract a one-line telemetry record. For apply_patch we want the
  # file paths touched; for shell/unified_exec we want the command's
  # first 200 chars (full transcripts live in the rollout file).
  python3 - "$INPUT" "$TELE_LOG" "$SID" "$TOOL_NAME" << 'PYEOF' 2>/dev/null
import json, sys, time
raw, tele_log, sid, tool_name = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
try:
    data = json.loads(raw)
except json.JSONDecodeError:
    sys.exit(0)
ts = int(time.time())
tool_input = data.get("tool_input") or {}
# Normalize a one-line summary per tool family.
if tool_name == "apply_patch":
    patch = ""
    if isinstance(tool_input, dict):
        patch = tool_input.get("input") or tool_input.get("patch") or ""
    # apply_patch payload is freeform text starting with `*** Begin Patch`.
    # Extract the `*** Update File:` / `*** Add File:` / `*** Delete File:`
    # markers which name the affected paths.
    files = []
    for line in (patch or "").splitlines():
        line = line.strip()
        for marker in ("*** Update File:", "*** Add File:", "*** Delete File:"):
            if line.startswith(marker):
                files.append(line[len(marker):].strip())
    summary = "apply_patch files=" + ",".join(sorted(set(files))[:10])
elif tool_name in ("shell", "unified_exec"):
    cmd = ""
    if isinstance(tool_input, dict):
        cmd = tool_input.get("command")
        if isinstance(cmd, list):
            cmd = " ".join(str(c) for c in cmd)
        elif cmd is None:
            cmd = ""
    summary = f"{tool_name} cmd={str(cmd)[:200]}"
else:
    sys.exit(0)
try:
    with open(tele_log, "a", encoding="utf-8") as fh:
        fh.write(f"[{ts}] sid={sid} {summary}\n")
except OSError:
    pass
PYEOF

} 2>>"${ERR_LOG:-/dev/null}" || true
exit 0
