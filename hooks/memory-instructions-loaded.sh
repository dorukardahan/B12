#!/bin/bash
# B12 Memory System — InstructionsLoaded Hook (v1, 2026-05-18)
# Logs every CLAUDE.md / .claude/rules/*.md / MEMORY.md load event to
# a JSONL telemetry trail for later analysis.
#
# Fires on: InstructionsLoaded (any rule/memory file enters context)
# Output: empty JSON (pure observability — cannot inject context per docs)
# Budget: <200ms, sync
#
# Why: Convergent finding from Phase C research (Agent 1 + Agent 3):
# B12 has zero visibility today into which CLAUDE.md and
# `.claude/rules/*.md` files Anthropic actually loaded. After a
# `/compact` the rules can drift (issue #59309), and B12's session
# context becomes incomplete because it can't reason about what
# CLAUDE.md is already covering. Logging every load (file_path,
# memory_type, load_reason, parent_file_path) gives the surfacing
# engine future signal: (a) skip re-injecting memories whose source
# CLAUDE.md just got compacted away, (b) detect rule-glob loads to
# bias retrieval toward that subdirectory's stored memories.
#
# Anthropic is explicitly leaning into observability-only hooks
# (v2.1.143 added an 8-block cap on Stop loops). This hook follows
# the same pattern: subscribe broadly, write asynchronously, never
# block. No additionalContext is emitted because the docs say
# InstructionsLoaded does not support it.
#
# Dedup: Anthropic fires this event up to 3× per file per compact
# (anthropics/claude-code #52176 reproducer). We dedup by
# (file_path, load_reason, session_id, 1-second-bucket) so the log
# captures the load *event* rather than every internal fire.

_B12_HOOK_DIR="${B12_HOOK_DIR:-$HOME/.B12/hooks}"
# shellcheck disable=SC1091
. "$_B12_HOOK_DIR/_b12_common.sh"

( sleep 3 && kill -TERM $$ 2>/dev/null ) &
_WATCHDOG=$!
trap "kill $_WATCHDOG 2>/dev/null; wait $_WATCHDOG 2>/dev/null" EXIT

INPUT=$(cat)
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // ""')
SESSION_ID12="${SESSION_ID:0:12}"
FILE_PATH=$(echo "$INPUT" | jq -r '.file_path // ""')
MEMORY_TYPE=$(echo "$INPUT" | jq -r '.memory_type // ""')
LOAD_REASON=$(echo "$INPUT" | jq -r '.load_reason // ""')
PARENT_PATH=$(echo "$INPUT" | jq -r '.parent_file_path // .trigger_file_path // ""')

# Skip when essential fields are empty (defensive).
if [ -z "$FILE_PATH" ]; then
  echo '{}'
  exit 0
fi

B12_BASE="${B12_DATA_DIR:-$HOME/.B12}"
STATE_DIR="$B12_BASE/state"
LOG_FILE="$B12_BASE/memory-logs/instructions-loaded.jsonl"
DEDUP_FILE="$STATE_DIR/instr-loaded-dedup-${SESSION_ID12}.txt"
mkdir -p "$STATE_DIR" "$B12_BASE/memory-logs" 2>/dev/null

# 1-second bucket dedup — Anthropic's triple-fire all lands in the
# same wallclock second, so bucket-by-second collapses them to one
# log entry without losing distinct re-loads later in the session.
#
# Race-safety (Codex PR #40 round 6 catch): if multiple fires arrive
# concurrently (subprocess parallelism, e.g. nested traversal), each
# could pass the grep-check before any writes the ledger, defeating
# dedup. Use python3's fcntl.flock to serialise the check+write into
# a single critical section. The flock auto-releases when the
# subprocess exits, even on crash, so no deadlock risk.
NOW=$(date +%s)
DEDUP_KEY="${FILE_PATH}|${LOAD_REASON}|${NOW}"

# Python helper: atomic check-then-append under flock. Echoes "DUP"
# when the key was already present (caller exits early), or empty
# string otherwise (caller writes the log line).
_DEDUP_RESULT=$(python3 - "$DEDUP_FILE" "$DEDUP_KEY" << 'PYEOF'
import sys, os, fcntl
dedup_file, key = sys.argv[1], sys.argv[2]
lock = open(dedup_file + ".lock", "a+")
try:
    fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
except OSError:
    # Lock unsupported — best-effort, fall through unlocked.
    pass
keys = []
if os.path.exists(dedup_file):
    try:
        with open(dedup_file) as f:
            keys = [ln.rstrip("\n") for ln in f.readlines()]
    except OSError:
        keys = []
if key in keys:
    print("DUP")
    sys.exit(0)
keys.append(key)
keys = keys[-64:]
try:
    with open(dedup_file + ".tmp", "w") as f:
        f.write("\n".join(keys) + "\n")
    os.replace(dedup_file + ".tmp", dedup_file)
except OSError:
    pass
PYEOF
)
if [ "$_DEDUP_RESULT" = "DUP" ]; then
  echo '{}'
  exit 0
fi

# Append JSONL line. jq -c emits compact one-line output regardless
# of the input's whitespace, so the log stays parseable.
LINE=$(jq -nc \
  --argjson ts "$NOW" \
  --arg sid "$SESSION_ID12" \
  --arg fp "$FILE_PATH" \
  --arg mt "$MEMORY_TYPE" \
  --arg lr "$LOAD_REASON" \
  --arg pp "$PARENT_PATH" \
  '{ts:$ts,session_id:$sid,file_path:$fp,memory_type:$mt,load_reason:$lr,parent:$pp}')
echo "$LINE" >> "$LOG_FILE" 2>/dev/null

echo '{}'
exit 0
