#!/bin/bash
# B12 shared hook helpers (sourced, not exec'd).
#
# Lives in hooks/ next to the rest of the hook scripts. Adopted in v11.34 /
# P-SPEED so every hook can pick up the same 200ms sync cap, the same
# trivial-call skip whitelist, and the same async-fork shorthand without
# copy-pasting bash plumbing into 14 different scripts.
#
# Idempotent — guarded by _B12_COMMON_LOADED so multiple `source` calls in a
# hook chain don't redefine helpers or re-arm watchdogs.

if [ -n "$_B12_COMMON_LOADED" ]; then
  return 0 2>/dev/null
fi
_B12_COMMON_LOADED=1

# Canonical hook code path. R10 pitfall: hooks must NEVER assume
# `$SCRIPT_DIR` is set; the env var coming in from hooks.json is the only
# stable anchor.
_B12_HOOK_DIR="${B12_HOOK_DIR:-$HOME/.B12/hooks}"
_B12_DATA_DIR="${B12_DATA_DIR:-$HOME/.B12}"

# ── b12_sync_watchdog SECONDS ─────────────────────────────────
# Best-effort client-side soft cap. **Important caveat:** bash queues
# trapped signals (USR1, TERM, INT, …) while the shell is waiting on a
# foreground command — `cmd1 | cmd2` pipes, `$(...)` command substitutions,
# `wait` on a single child. So if `nc -U`, `sqlite3`, or a Python heredoc
# runs longer than the cap, the handler does not fire until that subprocess
# returns. In practice the cap therefore bounds the script's TAIL work
# (FSRS update, graph join, output build) rather than the entire run;
# the worst-case ceiling is driven by the subprocess timeouts (currently
# `nc -w 3`, Python `signal.alarm(3)`). This is acceptable because the
# slow paths have their own internal caps; the watchdog catches
# pathological cases where everything completes but the script tail
# itself is slow. (Codex PR #24 review flagged the signal-deferral
# limitation — see also docs/B12_proactive_recall_plan_2026-05-18.md S3.)
b12_sync_watchdog() {
  local _t="${1:-0.2}"
  local _label="${2:-sync_cap}"
  # Background sleeper sends USR1 to the parent script when the cap fires.
  ( sleep "$_t" && kill -USR1 $$ 2>/dev/null ) &
  _B12_WD_PID=$!
  trap "_b12_sync_cap_handler '$_label' '$_t'" USR1
  trap "kill $_B12_WD_PID 2>/dev/null; wait $_B12_WD_PID 2>/dev/null" EXIT
}

_b12_sync_cap_handler() {
  local _label="$1"
  local _t="$2"
  # Race-safe fallback emit: only write `{}` if the script has NOT already
  # written its real output. Without the guard, USR1 delivered between the
  # caller's `printf` and `exit 0` would append `{}` to valid hook JSON,
  # producing the kind of garbage Claude Code drops silently. Callers that
  # are about to emit output should set _B12_OUTPUT_EMITTED=1 immediately
  # before (or call `b12_mark_output_emitted`).
  if [ "${_B12_OUTPUT_EMITTED:-0}" != "1" ]; then
    echo '{}'
  fi
  # Best-effort log line — never block on a slow filesystem.
  local _logdir="$_B12_DATA_DIR/memory-logs"
  [ -d "$_logdir" ] || mkdir -p "$_logdir" 2>/dev/null
  echo "{\"ts\":$(date +%s),\"event\":\"sync_cap_hit\",\"label\":\"${_label}\",\"timeout_s\":${_t},\"output_already_emitted\":${_B12_OUTPUT_EMITTED:-0}}" \
    >> "$_logdir/sync-cap-hits.jsonl" 2>/dev/null
  exit 0
}

# Mark stdout as "already received the script's primary output" so a late-
# firing sync-cap watchdog won't append a `{}` and corrupt the JSON.
# Safe to call multiple times; safe to call when no watchdog is armed.
b12_mark_output_emitted() {
  _B12_OUTPUT_EMITTED=1
}

# ── b12_async_fork CMD... ─────────────────────────────────────
# Async-by-default wrapper: runs CMD in a backgrounded subshell, redirects
# stdout+stderr to a per-day log, and disowns so a parent shell exit does
# not kill the child. Used by memory-checkpoint / -working-context /
# -feedback (S1) and by the FSRS-update fragment in memory-retrieval.sh.
b12_async_fork() {
  local _logdir="$_B12_DATA_DIR/memory-logs"
  [ -d "$_logdir" ] || mkdir -p "$_logdir" 2>/dev/null
  local _log="$_logdir/async-hooks-$(date +%Y-%m-%d).log"
  ( "$@" ) >>"$_log" 2>&1 < /dev/null &
  disown
}

# ── b12_should_skip_trivial TOOL_NAME TOOL_INPUT_JSON ─────────
# S4 trivial-call whitelist. Returns 0 (skip the hook) for cheap
# operations where surfacing memory is more cost than value. Caller
# supplies the JSON-encoded tool_input so we don't re-shell-out to jq.
b12_should_skip_trivial() {
  local _tool="$1"
  local _input="$2"
  case "$_tool" in
    Read)
      # <1KB Reads (config dotfiles, small text). The JSON payload has
      # no size field, but we can heuristically skip well-known cheap
      # file types AND tiny files when offset/limit is small.
      local _fp
      _fp=$(echo "$_input" | jq -r '.file_path // ""' 2>/dev/null)
      [ -z "$_fp" ] && return 1
      # Skip very small files outright.
      if [ -f "$_fp" ]; then
        local _sz
        _sz=$(wc -c < "$_fp" 2>/dev/null | tr -d ' ')
        if [ -n "$_sz" ] && [ "$_sz" -lt 1024 ]; then
          return 0
        fi
      fi
      # Skip lockfiles and tiny config sentinels regardless of size.
      case "$(basename "$_fp")" in
        .gitignore|.gitkeep|.envrc|.editorconfig|*.lock)
          return 0
          ;;
      esac
      ;;
    Bash)
      # Trivial built-ins: pwd, ls, cd, echo, which, env (no flags).
      local _cmd
      _cmd=$(echo "$_input" | jq -r '.command // ""' 2>/dev/null)
      case "$_cmd" in
        pwd|"ls"|"ls "*|"cd "*|"echo "*|"which "*|"env"|"date"|"hostname"|"whoami")
          return 0
          ;;
      esac
      ;;
  esac
  return 1
}
