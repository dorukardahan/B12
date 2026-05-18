#!/bin/bash
# B12 Memory System — Proactive Surfacing Hook (v2 — daemon-route)
#
# Surfaces relevant memories when the user reads/edits files or hits errors.
# Fires on: PreToolUse (Read/Edit/Write), PostToolUse (Bash with errors).
#
# v2 changes (2026-05-18, P-FOUNDATION):
# - Daemon-route: removes the Python heredoc that ate ~200-300ms per fire.
#   The daemon's `recall` op replaces both the heredoc import and the
#   semantic search inside surfacing_engine.py for the hot path.
# - T1 per-turn cap: total injected content ≤ ~800 tokens (3200 chars).
# - T2 cumulative cap: skips when session-cumulative B12 I/O ≥ 80K tokens.
# - T3 same-session dedup ledger: never re-surface a memory in one session.
# - Rate limit (1 surface per N tool calls, 60s cooldown) preserved.
#
# Output: JSON additionalContext or empty {} on no-surface / cap-hit / cooldown.
# Performance target: p50 < 150ms.

# ── Self-timeout watchdog ─────────────────────────────────────
( sleep 4 && kill -TERM $$ 2>/dev/null ) &
_WATCHDOG=$!
trap "kill $_WATCHDOG 2>/dev/null; wait $_WATCHDOG 2>/dev/null" EXIT

INPUT=$(cat)
TOOL_NAME=$(echo "$INPUT" | jq -r '.tool_name // ""')
HOOK_EVENT=$(echo "$INPUT" | jq -r '.hook_event_name // ""')
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // ""')
SESSION_ID12="${SESSION_ID:0:12}"

B12_BASE="${B12_DATA_DIR:-$HOME/.B12}"
B12_STATE_DIR="$B12_BASE/state"
SURFACE_STATE="$B12_BASE/surfacing-state.json"
B12_TOK_STATE="$B12_STATE_DIR/session-tok-${SESSION_ID12}.txt"
B12_DEDUP_LEDGER="$B12_STATE_DIR/session-injected-${SESSION_ID12}.txt"
B12_TOK_PER_TURN="${B12_MAX_INJECT_TOKENS:-800}"
B12_TOK_SESSION_MAX="${B12_MAX_SESSION_TOKENS:-80000}"
B12_TOK_PER_TURN_CHARS=$(( B12_TOK_PER_TURN * 4 ))
mkdir -p "$B12_STATE_DIR" 2>/dev/null

# ── Trigger classification ──────────────────────────────────
TRIGGER_TYPE=""
TRIGGER_CONTEXT=""

if [ "$HOOK_EVENT" = "PreToolUse" ]; then
  case "$TOOL_NAME" in
    Read|Edit|Write)
      TRIGGER_TYPE="file"
      TRIGGER_CONTEXT=$(echo "$INPUT" | jq -r '.tool_input.file_path // ""' 2>/dev/null)
      ;;
  esac
elif [ "$HOOK_EVENT" = "PostToolUse" ]; then
  if [ "$TOOL_NAME" = "Bash" ]; then
    TOOL_RESULT=$(echo "$INPUT" | jq -r '.tool_result // ""' 2>/dev/null)
    if echo "$TOOL_RESULT" | grep -qiE '(error|failed|exception|traceback|errno|permission denied|not found|command not found)' 2>/dev/null; then
      TRIGGER_TYPE="error"
      TRIGGER_CONTEXT=$(echo "$TOOL_RESULT" | head -5 | head -c 500)
    fi
  fi
fi

if [ -z "$TRIGGER_TYPE" ] || [ -z "$TRIGGER_CONTEXT" ] || [ "$TRIGGER_CONTEXT" = "null" ]; then
  echo '{}'
  exit 0
fi

# ── T2 pre-check — cumulative session cap ───────────────────
_TOK_USED=0
if [ -f "$B12_TOK_STATE" ]; then
  _TOK_USED=$(cat "$B12_TOK_STATE" 2>/dev/null | tr -cd '0-9')
  [ -z "$_TOK_USED" ] && _TOK_USED=0
fi
if [ "$_TOK_USED" -ge "$B12_TOK_SESSION_MAX" ]; then
  echo '{}'
  exit 0
fi

# ── Rate limit (preserved from v1) ───────────────────────────
# Tool-call counter + 60s cooldown, both keyed by session via the JSON file.
NOW=$(date +%s)
LAST_AT=0
TOOL_CALLS=0
if [ -f "$SURFACE_STATE" ]; then
  _STATE=$(cat "$SURFACE_STATE" 2>/dev/null)
  if [ -n "$_STATE" ]; then
    LAST_AT=$(echo "$_STATE" | jq -r '.last_surfaced_at // 0' 2>/dev/null)
    TOOL_CALLS=$(echo "$_STATE" | jq -r '.tool_calls // 0' 2>/dev/null)
  fi
fi
TOOL_CALLS=$((TOOL_CALLS + 1))
ELAPSED=$(( NOW - LAST_AT ))
if [ "$TOOL_CALLS" -lt 5 ] || [ "$ELAPSED" -lt 60 ]; then
  # Persist the bumped counter, exit empty.
  echo "{\"last_surfaced_at\":${LAST_AT},\"tool_calls\":${TOOL_CALLS}}" > "${SURFACE_STATE}.tmp" 2>/dev/null && \
    mv "${SURFACE_STATE}.tmp" "$SURFACE_STATE" 2>/dev/null
  echo '{}'
  exit 0
fi

# ── Build query string from trigger context ─────────────────
QUERY=""
case "$TRIGGER_TYPE" in
  file)
    QUERY=$(basename "$TRIGGER_CONTEXT" 2>/dev/null)
    ;;
  error)
    # First non-empty line is usually the actual error line.
    QUERY=$(echo "$TRIGGER_CONTEXT" | grep -m1 -E '\S' | head -c 200)
    ;;
esac

if [ -z "$QUERY" ] || [ "${#QUERY}" -lt 4 ]; then
  echo '{}'
  exit 0
fi

# ── DB path ────────────────────────────────────────────────
if [ "$(uname)" = "Darwin" ]; then
  DB_PATH="$HOME/Library/Application Support/mcp-memory/sqlite_vec.db"
elif [ -d "$HOME/AppData" ]; then
  DB_PATH="$HOME/AppData/Local/mcp-memory/sqlite_vec.db"
else
  DB_PATH="$HOME/.local/share/mcp-memory/sqlite_vec.db"
fi
# Helper: reset the rate-limit counter when an expensive attempt finishes
# (hit or miss) so we don't pay the daemon round-trip on every subsequent fire
# until something actually hits. last_surfaced_at is NOT bumped on a miss —
# only a real surface advances it.
_reset_rate_limit_no_hit() {
  echo "{\"last_surfaced_at\":${LAST_AT},\"tool_calls\":0}" \
    > "${SURFACE_STATE}.tmp" 2>/dev/null && \
    mv "${SURFACE_STATE}.tmp" "$SURFACE_STATE" 2>/dev/null
}

if [ ! -f "$DB_PATH" ]; then
  _reset_rate_limit_no_hit
  echo '{}'
  exit 0
fi

# ── Daemon helpers ──────────────────────────────────────────
_UID=$(id -u 2>/dev/null || echo $$)
EMBED_SOCK="/tmp/b12-embed-${_UID}.sock"
EMBED_PID="/tmp/b12-embed-${_UID}.pid"

daemon_alive() {
  [ -S "$EMBED_SOCK" ] && [ -f "$EMBED_PID" ] && \
    kill -0 "$(cat "$EMBED_PID" 2>/dev/null)" 2>/dev/null
}

daemon_request() {
  printf '%s\n' "$1" | nc -U "$EMBED_SOCK" -w 3 2>/dev/null
}

if ! daemon_alive; then
  # No daemon → no surfacing this turn. Cheaper than firing the cold fallback.
  # Reset the counter — otherwise it stays at threshold and every subsequent
  # fire repeats the expensive prep work even though the daemon is still down.
  _reset_rate_limit_no_hit
  echo '{}'
  exit 0
fi

# ── Load T3 ledger ──────────────────────────────────────────
_DEDUP_IDS=","
if [ -f "$B12_DEDUP_LEDGER" ]; then
  while IFS= read -r _did; do
    _did="${_did//[^0-9]/}"
    [ -n "$_did" ] && _DEDUP_IDS="${_DEDUP_IDS}${_did},"
  done < "$B12_DEDUP_LEDGER"
fi
_SKIP_IDS_JSON=$(echo "$_DEDUP_IDS" | tr ',' '\n' | grep -E '^[0-9]+$' | jq -Rn '[inputs | tonumber]')

# ── Call daemon recall op ───────────────────────────────────
_REQ=$(jq -nc --arg q "$QUERY" --arg db "$DB_PATH" --argjson skip "$_SKIP_IDS_JSON" \
  '{op:"recall",query:$q,db_path:$db,limit:3,threshold:0.55,skip_ids:$skip}')
_RESP=$(daemon_request "$_REQ")

if ! echo "$_RESP" | jq -e '.ok == true' >/dev/null 2>&1; then
  _reset_rate_limit_no_hit
  echo '{}'
  exit 0
fi

_HIT_COUNT=$(echo "$_RESP" | jq -r '.results | length' 2>/dev/null)
if [ -z "$_HIT_COUNT" ] || [ "$_HIT_COUNT" = "0" ]; then
  _reset_rate_limit_no_hit
  echo '{}'
  exit 0
fi

# ── Format hits (basic format — Q4 4-field upgrade lives in P-RECALL) ───
_DISPLAY=$(echo "$_RESP" | jq -r '.results | map(.display) | .[]')

_HEADER="Proactive surfacing (${TRIGGER_TYPE}: $(echo "$QUERY" | head -c 60)):"
_CAND_CONTEXT=$(printf '%s\n%s\n' "$_HEADER" "$_DISPLAY")

# ── T1 per-turn cap ────────────────────────────────────────
_CAND_LEN=${#_CAND_CONTEXT}
if [ "$_CAND_LEN" -gt "$B12_TOK_PER_TURN_CHARS" ]; then
  _CAND_CONTEXT="${_CAND_CONTEXT:0:$B12_TOK_PER_TURN_CHARS}"$'\n[trimmed: per-turn token cap hit]'
  _CAND_LEN=${#_CAND_CONTEXT}
fi
_CAND_TOK=$(( (_CAND_LEN + 3) / 4 ))

# ── T2 cumulative cap recheck ──────────────────────────────
_WOULD_BE=$(( _TOK_USED + _CAND_TOK ))
if [ "$_WOULD_BE" -gt "$B12_TOK_SESSION_MAX" ]; then
  echo "{\"ts\":$(date +%s),\"session_id\":\"${SESSION_ID12}\",\"reason\":\"would_exceed_cumulative\",\"requested_tokens\":${_CAND_TOK},\"used\":${_TOK_USED},\"ceiling\":${B12_TOK_SESSION_MAX}}" \
    >> "$B12_BASE/memory-logs/token-budget-skips.jsonl" 2>/dev/null
  # Counter reset so we don't keep paying daemon round-trip on every fire
  # after the session budget is exhausted.
  _reset_rate_limit_no_hit
  echo '{}'
  exit 0
fi

# Record budget + dedup state BEFORE returning so a concurrent fire
# doesn't double-bill.
echo "$_WOULD_BE" > "${B12_TOK_STATE}.tmp" 2>/dev/null && mv "${B12_TOK_STATE}.tmp" "$B12_TOK_STATE" 2>/dev/null

_INJECTED_IDS=$(echo "$_RESP" | jq -r '.results | map(.id|tostring) | .[]')
if [ -n "$_INJECTED_IDS" ]; then
  _NEW_LEDGER=$( {
    echo "$_INJECTED_IDS"
    [ -f "$B12_DEDUP_LEDGER" ] && cat "$B12_DEDUP_LEDGER"
  } | awk 'NF && !seen[$0]++' | head -500 )
  echo "$_NEW_LEDGER" > "${B12_DEDUP_LEDGER}.tmp" 2>/dev/null && \
    mv "${B12_DEDUP_LEDGER}.tmp" "$B12_DEDUP_LEDGER" 2>/dev/null
fi

# Reset surface rate-limit state (counter back to 0, mark surfaced at NOW).
echo "{\"last_surfaced_at\":${NOW},\"tool_calls\":0}" > "${SURFACE_STATE}.tmp" 2>/dev/null && \
  mv "${SURFACE_STATE}.tmp" "$SURFACE_STATE" 2>/dev/null

# The hook fires on BOTH PreToolUse (Read/Edit/Write) and PostToolUse (Bash
# with errors). Claude Code's consumer filters injections by hookEventName, so
# echoing the actual event the script saw is required — hardcoding PreToolUse
# silently drops the inject on the error-surfacing path. Default to PreToolUse
# only when the input did not carry an event (defensive — should not happen).
_EVENT_OUT="${HOOK_EVENT:-PreToolUse}"
printf '%s' "$_CAND_CONTEXT" | \
  jq -Rs --arg ev "$_EVENT_OUT" \
    '{hookSpecificOutput:{hookEventName:$ev,additionalContext:.}}'

exit 0
