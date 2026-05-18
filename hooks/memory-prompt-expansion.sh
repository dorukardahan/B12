#!/bin/bash
# B12 Memory System — UserPromptExpansion Hook (v1, 2026-05-18)
# Detects native /goal / /plan / /clear slash command expansions and
# records goal lifecycle events as memories.
#
# Fires on: UserPromptExpansion (every slash-command expansion)
# Output: empty JSON (side-effect only — DOES NOT block or inject context)
# Budget: <300ms
#
# Why: Claude Code v2.1.139 added native /goal — a long-running task
# condition that drives multi-turn work. B12 previously had no
# visibility into goal start / completion / clear events, which are
# the single best moments to checkpoint "what we were trying to do
# and whether we got there". This hook captures:
#   - /goal <condition>    → start, persist active-goal slug to state
#   - /goal clear|stop|off → end, clear the state file
# Other slash commands (/plan, /memory, /clear) are logged in the
# observation jsonl but not memory-stored — they're navigation, not
# decisions.

_B12_HOOK_DIR="${B12_HOOK_DIR:-$HOME/.B12/hooks}"
# shellcheck disable=SC1091
. "$_B12_HOOK_DIR/_b12_common.sh"

( sleep 3 && kill -TERM $$ 2>/dev/null ) &
_WATCHDOG=$!
trap "kill $_WATCHDOG 2>/dev/null; wait $_WATCHDOG 2>/dev/null" EXIT

INPUT=$(cat)
COMMAND_NAME=$(echo "$INPUT" | jq -r '.command_name // ""')
COMMAND_ARGS=$(echo "$INPUT" | jq -r '.command_args // ""')
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // ""')
SESSION_ID12="${SESSION_ID:0:12}"

# Always log the navigation event (helps analyse which commands drive
# recall patterns). `/clear` is a special case below — it also clears
# any active goal per Claude Code docs, so we route through the
# goal-termination path after logging.
case "$COMMAND_NAME" in
  goal|clear|plan|memory|compact|resume|branch|fork)
    _OBS_LOG="${B12_DATA_DIR:-$HOME/.B12}/memory-logs/slash-commands.jsonl"
    _OBS_DIR=$(dirname "$_OBS_LOG")
    [ -d "$_OBS_DIR" ] || mkdir -p "$_OBS_DIR" 2>/dev/null
    echo "{\"ts\":$(date +%s),\"session_id\":\"${SESSION_ID12}\",\"command\":\"${COMMAND_NAME}\",\"args\":$(echo "$COMMAND_ARGS" | jq -Rs .)}" \
      >> "$_OBS_LOG" 2>/dev/null
    ;;
  *)
    # UserPromptExpansion stdout is injected into Claude's prompt
    # context as additionalContext (per hooks reference). Emitting
    # `{}` would inject the literal two-char token sequence; instead
    # exit silently so the hook is a true no-op for non-goal commands.
    exit 0
    ;;
esac

# Per Claude Code /goal docs (v2.1.139): `/clear` also removes any
# active goal. Re-route /clear into the goal-termination path with
# the `clear` terminator token so the existing case statement below
# captures the previously-active goal as a completion memory and
# deletes the state file. Codex PR #39 round 2 P1 catch.
if [ "$COMMAND_NAME" != "goal" ]; then
  if [ "$COMMAND_NAME" = "clear" ]; then
    COMMAND_ARGS="clear"
  else
    # Other navigation commands: log only, do not touch goal state.
    # Silent exit — see no-`{}`-on-stdout note above.
    exit 0
  fi
fi

# Goal-specific handling below.
B12_BASE="${B12_DATA_DIR:-$HOME/.B12}"
B12_STATE_DIR="$B12_BASE/state"
mkdir -p "$B12_STATE_DIR" 2>/dev/null
ACTIVE_GOAL_FILE="$B12_STATE_DIR/active-goal-${SESSION_ID12}.txt"

# Normalise args: lowercase + trim. /goal accepts these terminators:
#   clear / stop / off / reset / none / cancel
# v2.1.139 spec: bare `/goal` with NO args is a status check, NOT a
# termination — Codex PR #39 P1 caught the original code path deleting
# the active-goal file on every status check. Only the explicit
# terminator tokens end a goal.
_ARGS_LOWER=$(echo "$COMMAND_ARGS" | tr '[:upper:]' '[:lower:]' | head -c 256)
_ARGS_TRIM=$(echo "$_ARGS_LOWER" | awk '{$1=$1; print}')

# Bare /goal = status check. Do not touch state, do not emit memory.
# Silent exit so no `{}` lands in prompt context.
if [ -z "$_ARGS_TRIM" ]; then
  exit 0
fi

case "$_ARGS_TRIM" in
  clear|stop|off|reset|none|cancel)
    # Goal explicitly terminated. Capture the previously-active goal
    # as a completion memory if it existed.
    if [ -f "$ACTIVE_GOAL_FILE" ]; then
      _prev_goal=$(head -c 800 "$ACTIVE_GOAL_FILE" 2>/dev/null)
      rm -f "$ACTIVE_GOAL_FILE" 2>/dev/null
      if [ -n "$_prev_goal" ] && [ "${#_prev_goal}" -ge 20 ]; then
        STAGING_DIR="$B12_BASE/memory-staging/checkpoint"
        mkdir -p "$STAGING_DIR" 2>/dev/null
        BUFFER_FILE="$STAGING_DIR/.buffer-${SESSION_ID12}.jsonl"
        {
python3 - "$_prev_goal" "$BUFFER_FILE" "$_B12_HOOK_DIR/scripts" "$_ARGS_TRIM" << 'PYEOF'
import sys, os, json, fcntl
prev_goal, buffer_file, scripts_dir, args_trim = sys.argv[1:5]
sys.path.insert(0, scripts_dir)
try:
    from shared_patterns import content_hash
except ImportError:
    sys.exit(0)
verb = f"cleared via /goal {args_trim}"
content = f"[goal-end] Goal {verb}: {prev_goal[:600]}"
h = content_hash(content[:200])
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
            "category": "decision",
            "score": 9,
            "hash": h,
            "source": "goal_end",
        }, ensure_ascii=False) + "\n")
except OSError:
    pass
PYEOF
        } >/dev/null 2>&1 &
        disown
      fi
    fi
    ;;
  *)
    # Goal started — persist the condition. Future hooks (retrieval,
    # checkpoint, session-end) can read this file and tag memories
    # with [goal:<short-slug>] for cross-session re-surfacing.
    GOAL_CONDITION=$(echo "$COMMAND_ARGS" | head -c 2000)
    if [ -n "$GOAL_CONDITION" ]; then
      printf '%s' "$GOAL_CONDITION" > "$ACTIVE_GOAL_FILE" 2>/dev/null
      # Stage a "goal-start" memory in the checkpoint buffer.
      STAGING_DIR="$B12_BASE/memory-staging/checkpoint"
      mkdir -p "$STAGING_DIR" 2>/dev/null
      BUFFER_FILE="$STAGING_DIR/.buffer-${SESSION_ID12}.jsonl"
      {
python3 - "$GOAL_CONDITION" "$BUFFER_FILE" "$_B12_HOOK_DIR/scripts" << 'PYEOF'
import sys, os, json, fcntl
goal, buffer_file, scripts_dir = sys.argv[1:4]
sys.path.insert(0, scripts_dir)
try:
    from shared_patterns import content_hash
except ImportError:
    sys.exit(0)
# Trim to first sentence / 400 chars for memory clarity. The full
# condition stays in the state file for tag synthesis.
short = goal.split("\n")[0][:400]
content = f"[goal-start] {short}"
h = content_hash(content[:200])
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
            "category": "decision",
            "score": 9,
            "hash": h,
            "source": "goal_start",
        }, ensure_ascii=False) + "\n")
except OSError:
    pass
PYEOF
      } >/dev/null 2>&1 &
      disown
    fi
    ;;
esac

# Silent exit — see top-of-file note: UserPromptExpansion stdout
# becomes prompt context, so no `{}` placeholder.
exit 0
