#!/bin/bash
# B12 24h smoke harness (§C13). Drives memory-session-start.sh +
# memory-retrieval.sh against every detected ~/.claude* setup; writes one
# marker line per setup to ~/.B12/memory-logs/smoke-YYYYMMDD.log. Reversible:
# `./install.sh --smoke-cron-uninstall` removes the cron entry.
set -uo pipefail

B12_BASE="${B12_DATA_DIR:-$HOME/.B12}"
HOOK_BASE="${B12_HOOK_DIR:-$B12_BASE/hooks}"
LOG_DIR="$B12_BASE/memory-logs"
mkdir -p "$LOG_DIR" 2>/dev/null || true
LOG_FILE="$LOG_DIR/smoke-$(date +%Y%m%d).log"
RUN_TS=$(date '+%Y-%m-%dT%H:%M:%S%z')

# macOS bash 3.2 has no `timeout`; prefer coreutils, else run bare.
if command -v gtimeout >/dev/null 2>&1; then TIMEOUT_CMD="gtimeout 15"
elif command -v timeout >/dev/null 2>&1; then TIMEOUT_CMD="timeout 15"
else TIMEOUT_CMD=""
fi

drive_hook() {
  local hook_path="$1" stdin_payload="$2" setup_dir="$3" rc
  [ -x "$hook_path" ] || { echo "missing"; return; }
  if [ -n "$TIMEOUT_CMD" ]; then
    (cd "$setup_dir" && printf '%s' "$stdin_payload" | $TIMEOUT_CMD "$hook_path" >/dev/null 2>&1)
  else
    (cd "$setup_dir" && printf '%s' "$stdin_payload" | "$hook_path" >/dev/null 2>&1)
  fi
  rc=$?
  echo "exit=$rc"
}

SESSION_PAYLOAD='{"session_id":"smoke-test-synth","source":"startup","cwd":"'"$HOME"'"}'
RETRIEVAL_PAYLOAD='{"session_id":"smoke-test-synth","prompt":"smoke probe — memory recall sanity check","cwd":"'"$HOME"'"}'

# Dedupe by inode — macOS HFS+/APFS is case-insensitive by default, so two
# setup dirs differing only in case resolve to the same directory.
# Codex review PR #43 round 2 P2: if hooks are missing across ALL detected
# setups, that's a damaged-install signal — fail the smoke. Per-setup
# missing is non-fatal because a user may install B12 only in ~/.claude.
OVERALL=0
SEEN_INODES=""
SUCCESS_COUNT=0
MISSING_COUNT=0
for setup_raw in "$HOME"/.claude "$HOME"/.claude-*; do
  [ -d "$setup_raw" ] || continue
  inode=$(stat -f %i "$setup_raw" 2>/dev/null || stat -c %i "$setup_raw" 2>/dev/null)
  [ -z "$inode" ] && inode="$setup_raw"
  case " $SEEN_INODES " in *" $inode "*) continue ;; esac
  SEEN_INODES="$SEEN_INODES $inode"
  SS_RES=$(drive_hook "$HOOK_BASE/memory-session-start.sh" "$SESSION_PAYLOAD" "$setup_raw")
  RT_RES=$(drive_hook "$HOOK_BASE/memory-retrieval.sh" "$RETRIEVAL_PAYLOAD" "$setup_raw")
  printf '[%s] setup=%s session-start=%s retrieval=%s\n' \
    "$RUN_TS" "$(basename "$setup_raw")" "$SS_RES" "$RT_RES" >>"$LOG_FILE"
  case "$SS_RES$RT_RES" in
    *exit=0*exit=0*) SUCCESS_COUNT=$((SUCCESS_COUNT + 1)) ;;
    *missing*)       MISSING_COUNT=$((MISSING_COUNT + 1)) ;;
    *)               OVERALL=1 ;;
  esac
done
# Damaged-install detection: setups were found but every one returned
# `missing` for at least one hook — install.sh failed to deploy.
if [ "$SUCCESS_COUNT" -eq 0 ] && [ "$MISSING_COUNT" -gt 0 ]; then
  printf '[%s] FAIL: hooks missing in %d setup(s); run ./install.sh --all\n' \
    "$RUN_TS" "$MISSING_COUNT" >>"$LOG_FILE"
  OVERALL=1
fi
exit "$OVERALL"
