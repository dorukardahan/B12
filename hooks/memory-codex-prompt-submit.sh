#!/bin/bash
# B12 Codex CLI — UserPromptSubmit hook (Plan §2 CX1).
#
# Phase D agent 1 finding 2 — Codex's `goal` tool handler is in the
# silent set for PreToolUse, so /goal-awareness MUST detect on the
# user prompt string itself rather than on tool fire. We regex the
# prompt body and stamp ~/.B12/state/active-codex-goal-<sid>.txt so
# SessionStart + Stop in this session see the active goal.
#
# Wire input shape: codex-rs/hooks/src/schema.rs:433
#   {session_id, turn_id, transcript_path, cwd, hook_event_name,
#    model, permission_mode, prompt}
#
# Output: empty (no JSON) — we never block the prompt and we never
# inject context here (SessionStart already covered priming). Pure
# side-effect hook.
#
# Fail-open guard outer wrapper.

{
  set -o pipefail 2>/dev/null || true

  B12_BASE="${B12_DATA_DIR:-$HOME/.B12}"
  STATE_DIR="$B12_BASE/state"
  LOG_DIR="$B12_BASE/memory-logs"
  ERR_LOG="$LOG_DIR/codex-hook-errors.log"
  mkdir -p "$STATE_DIR" "$LOG_DIR" 2>/dev/null || true

  INPUT=""
  if [ ! -t 0 ]; then
    INPUT=$(cat)
  fi

  SID=$(printf '%s' "$INPUT" | jq -r '.session_id // empty' 2>/dev/null)
  PROMPT=$(printf '%s' "$INPUT" | jq -r '.prompt // empty' 2>/dev/null)
  [ -z "$SID" ] && exit 0
  [ -z "$PROMPT" ] && exit 0

  ACTIVE_GOAL_FILE="$STATE_DIR/active-codex-goal-${SID}.txt"

  # Codex review PR #41 round 4 — match ONLY against the first line so
  # that a multi-line prompt that merely quotes `/goal` doesn't clobber
  # goal state. The /goal command must occupy the first line; any later
  # `/goal` token is treated as quoted text.
  FIRST_LINE=$(printf '%s' "$PROMPT" | sed -n '1p')

  # /goal clear (or done|reset) → remove the state file. Codex's `/goal`
  # subcommands use space-separated args; match the first word after
  # /goal to avoid a clear-args body that mentions "clear" later in the
  # body being misclassified.
  CLEAR_RE='^[[:space:]]*/goal[[:space:]]+(clear|done|reset|cancel)[[:space:]]*$'
  if printf '%s' "$FIRST_LINE" | grep -Eqi "$CLEAR_RE"; then
    rm -f "$ACTIVE_GOAL_FILE" 2>/dev/null || true
    exit 0
  fi

  # /goal <body...> → write the rest of the prompt (everything after the
  # verb on the first line, plus subsequent lines as continuation) to
  # the state file so SessionStart in a forked or resumed session can
  # re-surface it.
  START_RE='^[[:space:]]*/goal[[:space:]]+'
  if printf '%s' "$FIRST_LINE" | grep -Eqi "$START_RE"; then
    # Strip /goal verb from the first line, keep subsequent lines as-is.
    HEAD_BODY=$(printf '%s' "$FIRST_LINE" | sed -E 's|^[[:space:]]*/goal[[:space:]]+||')
    TAIL=$(printf '%s' "$PROMPT" | sed -n '2,$p')
    if [ -n "$TAIL" ]; then
      printf '%s\n%s\n' "$HEAD_BODY" "$TAIL" > "$ACTIVE_GOAL_FILE" 2>/dev/null || true
    else
      printf '%s\n' "$HEAD_BODY" > "$ACTIVE_GOAL_FILE" 2>/dev/null || true
    fi
  fi

} 2>>"${ERR_LOG:-/dev/null}" || true
exit 0
