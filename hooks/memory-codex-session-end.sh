#!/bin/bash
# B12 Codex CLI — session-scoped SessionEnd hook.
# Upstream flushes the rollout before invoking this root-only event, then gives
# hooks at most three seconds. Detach extraction so teardown is never blocked.

set -o pipefail 2>/dev/null || true

B12_BASE="${B12_DATA_DIR:-$HOME/.B12}"
B12_SCRIPTS="${B12_HOOK_DIR:-$HOME/.B12/hooks}/scripts"
LOG_DIR="$B12_BASE/memory-logs"
ERR_LOG="$LOG_DIR/codex-hook-errors.log"
SESS_LOG="$LOG_DIR/codex-session-end.log"
mkdir -p "$LOG_DIR" 2>/dev/null || true

# Permanent redirect avoids an inherited save-fd keeping Codex's pipe open.
exec 2>>"${ERR_LOG:-/dev/null}"

INPUT=""
if [ ! -t 0 ]; then
  INPUT=$(cat)
fi

SID=$(printf '%s' "$INPUT" | jq -r '.session_id // empty' 2>/dev/null)
TRANSCRIPT=$(printf '%s' "$INPUT" | jq -r '.transcript_path // empty' 2>/dev/null)
[ -z "$SID" ] && exit 0

ROLLOUT=""
if [ -n "$TRANSCRIPT" ] && [ -f "$TRANSCRIPT" ]; then
  ROLLOUT="$TRANSCRIPT"
else
  SESS_ROOT="${CODEX_HOME:-$HOME/.codex}/sessions"
  if [ -d "$SESS_ROOT" ]; then
    ROLLOUT=$(find "$SESS_ROOT" -name "rollout-*${SID}*.jsonl" -type f 2>/dev/null | head -1)
  fi
fi

if [ -n "$ROLLOUT" ] && [ -f "$ROLLOUT" ]; then
  VENV_PY="$HOME/.local/b12-venv/bin/python3"
  if [ -x "$VENV_PY" ]; then PY="$VENV_PY"; else PY="python3"; fi
  (
    "$PY" "$B12_SCRIPTS/codex_session_end.py" "$ROLLOUT" || true
  ) </dev/null >>"$SESS_LOG" 2>&1 &
  disown 2>/dev/null || true
fi

exit 0
