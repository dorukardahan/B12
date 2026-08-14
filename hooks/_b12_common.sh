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

# Defensive: pipe failures (e.g. `cmd | tee FILE; rc=$?`) silently return the
# last command's exit by default — the global PIPESTATUS rule. Set pipefail
# here so every hook that sources _b12_common.sh inherits the safer default.
# Hooks that don't source this file opt in directly at the top of their own
# script.
set -o pipefail 2>/dev/null || true

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
# limitation — see also the proactive-recall design notes S3.)
b12_sync_watchdog() {
  local _t="${1:-0.2}"
  local _label="${2:-sync_cap}"
  local _parent=$$
  # HARD timeout: when the cap fires, kill the script's direct children (the
  # in-flight slow subprocess bash would otherwise queue the signal behind)
  # FIRST, then raise USR1 so the existing handler emits {} and exits cleanly.
  # Without the explicit kill, bash queues USR1 while waiting on a foreground
  # command — that's the soft-only limitation Codex PR #24 flagged. With the
  # kill, the foreground returns immediately (subprocess died) and the queued
  # signal is delivered at the next bash statement boundary, where the handler
  # fires.
  #
  # Excludes the watchdog subshell itself (BASHPID inside the subshell is
  # different from the script's $$, which is what the subshell inherits).
  #
  # Caveat: also kills any in-flight `b12_async_fork` work that hasn't yet
  # completed (those are children of $$ until the script exits). Async work
  # is best-effort by design (logging, checkpoint flush, FSRS update), so
  # the tradeoff is acceptable: cap firing is rare, and losing not-yet-
  # finished async work beats leaving a stuck synchronous hook on the
  # Read/Edit/Write/Bash hot path.
  (
    sleep "$_t"
    # macOS /bin/bash is 3.2 and does NOT export BASHPID — relying on it
    # to exclude the watchdog subshell from its own pkill would mis-fire
    # the kill against self on bash 3.2 (Codex PR #33 P1). Capture the
    # subshell PID portably via `sh -c 'echo $PPID'` (the inner `sh`'s
    # PPID is the watchdog subshell). Works on bash 3.2 and 4+.
    _wd_self=$(sh -c 'echo $PPID' 2>/dev/null)
    for _p in $(pgrep -P "$_parent" 2>/dev/null); do
      [ "$_p" = "$_wd_self" ] && continue
      [ -n "$BASHPID" ] && [ "$_p" = "$BASHPID" ] && continue
      kill -TERM "$_p" 2>/dev/null
    done
    kill -USR1 "$_parent" 2>/dev/null
  ) &
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

# ── b12_ensure_embed_daemon ───────────────────────────────────
# Non-blocking, fail-soft on-demand start for hooks that need embeddings after
# the SessionStart daemon has exited (idle timeout, RSS guard, Python upgrade).
# The Python daemon's flock remains the authority for singleton ownership, so
# racing hook fires can at worst launch harmless contenders that exit quickly.
b12_ensure_embed_daemon() {
  local _uid _runtime _sock _pidfile _lock _pid _python _script
  _uid=$(id -u 2>/dev/null || echo $$)
  _runtime="${B12_EMBED_RUNTIME_DIR:-/tmp}"
  _sock="$_runtime/b12-embed-${_uid}.sock"
  _pidfile="$_runtime/b12-embed-${_uid}.pid"
  _lock="$_runtime/b12-embed-${_uid}.lock"

  if [ -S "$_sock" ] && [ -f "$_pidfile" ]; then
    _pid=$(cat "$_pidfile" 2>/dev/null | tr -d '[:space:]')
    [ -n "$_pid" ] && kill -0 "$_pid" 2>/dev/null && return 0
  fi
  if [ -f "$_lock" ]; then
    _pid=$(cat "$_lock" 2>/dev/null | tr -d '[:space:]')
    [ -n "$_pid" ] && kill -0 "$_pid" 2>/dev/null && return 0
  fi

  _python="$HOME/.local/b12-venv/bin/python3"
  _script="$_B12_HOOK_DIR/scripts/embed_daemon.py"
  [ -x "$_python" ] && [ -f "$_script" ] || return 1
  # Launch through a short-lived Python parent. The daemon is the launcher's
  # child, so when the launcher exits it is reparented before this function
  # returns. memory-retrieval's hard watchdog kills every DIRECT hook child;
  # plain `cmd & disown` does not reparent and would let that watchdog kill the
  # 3-12s model load. start_new_session also isolates terminal signals.
  "$_python" -c '
import subprocess, sys
subprocess.Popen(
    [sys.argv[1], sys.argv[2]],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    close_fds=True,
    start_new_session=True,
)
' "$_python" "$_script" </dev/null >/dev/null 2>&1
  return 0
}

# ── b12_should_skip_trivial TOOL_NAME TOOL_INPUT_JSON ─────────
# S4 trivial-call whitelist. Returns 0 (skip the hook) for cheap
# operations where surfacing memory is more cost than value. Caller
# ── b12_resolve_db_path ─────────────────────────────────────
# Must match scripts/b12_mcp_server.py:DB_PATH verbatim. The server's
# branch key is `sys.platform` (Python), not `uname -s` (shell). They
# diverge on WSL (uname says Linux, sys.platform=="linux", AppData
# bind-mount is irrelevant) AND on Cygwin/MSYS POSIX-Python installs
# (uname says CYGWIN*, sys.platform=="cygwin", correct path is XDG).
# So we invoke Python directly — venv first, then any python3 on PATH.
# Codex PR #52 round 2 P2 surfaced both divergences.
b12_resolve_db_path() {
  local _py
  for _py in "$HOME/.local/b12-venv/bin/python3" python3 python; do
    if command -v "$_py" >/dev/null 2>&1 || [ -x "$_py" ]; then
      "$_py" -c '
import os, sys
home = os.path.expanduser("~")
if sys.platform == "darwin":
    print(home + "/Library/Application Support/mcp-memory/sqlite_vec.db")
elif sys.platform == "win32":
    print(home + "/AppData/Local/mcp-memory/sqlite_vec.db")
else:
    print(home + "/.local/share/mcp-memory/sqlite_vec.db")
' 2>/dev/null && return 0
    fi
  done
  # No python on PATH — fall back to uname best-effort.
  case "$(uname -s 2>/dev/null)" in
    Darwin) printf '%s\n' "$HOME/Library/Application Support/mcp-memory/sqlite_vec.db" ;;
    *)      printf '%s\n' "$HOME/.local/share/mcp-memory/sqlite_vec.db" ;;
  esac
}

# ── b12_get_db_path ─────────────────────────────────────────
# P3 hook-latency: cache the resolved DB path so high-frequency hooks don't
# spawn python3 (via b12_resolve_db_path) on every single fire. The path only
# changes when the OS / home dir changes, so a short TTL is safe. Honors
# B12_DATA_DIR for the cache location (per the B12_DATA_DIR/B12_HOOK_DIR
# separation contract). Falls back to b12_resolve_db_path on miss/expiry, and
# degrades gracefully to it if the cache cannot be written. TTL kept short
# (60s) so a setup reconfiguration can't serve a stale path for long.
b12_get_db_path() {
  local _cache="$_B12_DATA_DIR/state/db-path.cache"
  local _ttl=60
  if [ -f "$_cache" ]; then
    local _now _mtime _age
    _now=$(date +%s 2>/dev/null)
    # stat mtime: try GNU (-c %Y) FIRST, then BSD/macOS (-f %m). Ordering matters:
    # GNU `stat -f` means --file-system and on Linux writes an FS report to stdout
    # (NOT a clean non-zero failure), so a BSD-first probe would poison _mtime on
    # Linux and break the arithmetic below. The case guard is belt-and-suspenders:
    # force _mtime empty unless it is all-digits, so no stray report can reach the
    # arithmetic regardless of stat flavour. (PR #108 review r3441627762.)
    _mtime=$(stat -c %Y "$_cache" 2>/dev/null || stat -f %m "$_cache" 2>/dev/null)
    case "$_mtime" in ''|*[!0-9]*) _mtime="" ;; esac
    if [ -n "$_now" ] && [ -n "$_mtime" ]; then
      _age=$((_now - _mtime))
      if [ "$_age" -ge 0 ] && [ "$_age" -lt "$_ttl" ]; then
        local _cached
        _cached=$(cat "$_cache" 2>/dev/null)
        if [ -n "$_cached" ]; then
          printf '%s\n' "$_cached"
          return 0
        fi
      fi
    fi
  fi
  # Miss / expiry: resolve fresh (python3), populate cache best-effort, return.
  local _path
  _path=$(b12_resolve_db_path)
  if [ -n "$_path" ]; then
    [ -d "$_B12_DATA_DIR/state" ] || mkdir -p "$_B12_DATA_DIR/state" 2>/dev/null
    printf '%s\n' "$_path" > "$_cache" 2>/dev/null
    printf '%s\n' "$_path"
  fi
}

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
