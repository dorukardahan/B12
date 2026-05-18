#!/bin/bash
# B12 Memory System — Tool Failure Hook (v1, 2026-05-18)
# Captures PostToolUseFailure events as high-signal error memories.
#
# Fires on: PostToolUseFailure (Bash, Edit, Write, WebFetch, MCP tool errors)
# Skips: Read/Glob/Grep failures (too noisy — file-not-found dominates)
# Budget: <500ms, async (never blocks Claude's recovery turn)
# Output: empty JSON (side-effect only — appends to checkpoint buffer)
#
# Why: tool failures encode "X did not work because Y" — exactly the
# kind of signal a future session benefits from (same package, same
# missing dep, same broken path). Without this hook B12 only saw the
# *success* PostToolUse stream, never the failure stream, so the most
# decision-relevant tool turns were invisible to memory.
#
# Implementation note: reuses the existing checkpoint buffer + flush
# pipeline (memory-checkpoint.sh handles the batch INSERT). This hook
# only appends to the buffer; the next checkpoint flush picks it up.

_B12_HOOK_DIR="${B12_HOOK_DIR:-$HOME/.B12/hooks}"
# shellcheck disable=SC1091
. "$_B12_HOOK_DIR/_b12_common.sh"

# 3s watchdog — failure logging must never delay the next turn.
( sleep 3 && kill -TERM $$ 2>/dev/null ) &
_WATCHDOG=$!
trap "kill $_WATCHDOG 2>/dev/null; wait $_WATCHDOG 2>/dev/null" EXIT

INPUT=$(cat)
TOOL_NAME=$(echo "$INPUT" | jq -r '.tool_name // ""')
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // ""')
# PostToolUseFailure field name varies across Claude Code versions:
# code.claude.com/docs/en/hooks documents `error_message`, but Codex
# review of PR #38 (2026-05-18) flagged that the actual on-the-wire
# field is `error`. Read both — whichever is non-empty wins — so this
# hook stays correct across versions without a config flip.
ERROR_MSG=$(echo "$INPUT" | jq -r '
  (.error // "") as $a
  | (.error_message // "") as $b
  | if ($a | length) > 0 then $a else $b end
')

# Filter: skip noise sources. Read-misses, glob-no-match, and grep-no-match
# are normal exploration outcomes, not failures worth remembering.
case "$TOOL_NAME" in
  Read|Glob|Grep|""|mcp__B12__*)
    echo '{}'
    exit 0
    ;;
esac

# Filter: skip empty or trivial errors.
if [ -z "$ERROR_MSG" ] || [ "${#ERROR_MSG}" -lt 20 ]; then
  echo '{}'
  exit 0
fi

# Extract tool input one-liner for context (truncated).
TOOL_INPUT_SUMMARY=$(echo "$INPUT" | jq -r '
  .tool_input | if type == "object" then
    (.command // .file_path // .url // .pattern // "")
  elif . == null then "" else . end
' 2>/dev/null | head -c 200 | tr '\n' ' ')
[ "$TOOL_INPUT_SUMMARY" = "null" ] && TOOL_INPUT_SUMMARY=""

B12_BASE="${B12_DATA_DIR:-$HOME/.B12}"
STAGING_DIR="$B12_BASE/memory-staging"
CHECKPOINT_DIR="$STAGING_DIR/checkpoint"
mkdir -p "$CHECKPOINT_DIR" 2>/dev/null

SESSION_ID12="${SESSION_ID:0:12}"
BUFFER_FILE="$CHECKPOINT_DIR/.buffer-${SESSION_ID12}.jsonl"

# Async append — never block. The next memory-checkpoint flush picks
# this up alongside its own regex-scanned matches and dedups via
# content_hash + DB-side check.
#
# Inline `{ … } &; disown` instead of b12_async_fork: that helper
# redirects stdin to /dev/null, which would swallow the PYEOF heredoc
# before python3 saw it. Matches the pattern memory-checkpoint.sh uses.
{
python3 - "$TOOL_NAME" "$ERROR_MSG" "$TOOL_INPUT_SUMMARY" "$BUFFER_FILE" "$_B12_HOOK_DIR/scripts" << 'PYEOF'
import sys, os, json, fcntl

tool_name, error_msg, tool_input_summary, buffer_file, scripts_dir = sys.argv[1:6]

sys.path.insert(0, scripts_dir)
try:
    from shared_patterns import content_hash, summary_filter
except ImportError:
    sys.exit(0)

# Compose a memory line that's self-describing without the original tool
# input. Future retrieval cares about the failure mode + tool, not the
# raw 4 KB of arguments.
content = f"[error] {tool_name} failed: {error_msg.strip()[:400]}"
if tool_input_summary.strip():
    content += f" (context: {tool_input_summary.strip()[:200]})"

# Skip session summary patterns that occasionally surface in error text
# (e.g. an error containing the literal phrase "# Session Summary").
if summary_filter(content):
    sys.exit(0)

h = content_hash(content[:200])

# Serialize buffer writes with the same flock the checkpoint hook uses
# so concurrent failure + checkpoint fires don't tear the JSONL.
lock_path = buffer_file + ".lock"
try:
    lock_fh = open(lock_path, "a+")
    try:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
    except OSError:
        pass
    with open(buffer_file, "a") as f:
        f.write(json.dumps({
            "content": content[:500],
            "category": "error",
            "score": 8,
            "hash": h,
            "source": "tool_failure",
        }, ensure_ascii=False) + "\n")
except OSError:
    pass
PYEOF
} >/dev/null 2>&1 &
disown

echo '{}'
exit 0
