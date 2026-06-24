#!/bin/bash
# B12 Memory System — Checkpoint Hook (v1)
# Mid-session memory capture via PostToolUse — zero-token, regex-based
#
# Fires on: PostToolUse (all tools, rate-limited)
# Rate limit: every 15 tool calls OR 10 minutes since last checkpoint
# Budget: <500ms
# Output: empty JSON (side-effect only — writes to DB via batch)
#
# Flow:
#   1. Increment call counter, check rate limit
#   2. Extract text from tool_result + tool_input
#   3. Scan with shared_patterns.py regex patterns
#   4. Score ≥ 6 → add to batch buffer
#   5. Buffer ≥ 3 items → batch INSERT to DB
#   Fallback: DB contention or daemon down → skip silently

# Shared helpers (b12_async_fork, b12_sync_watchdog, b12_should_skip_trivial).
_B12_HOOK_DIR="${B12_HOOK_DIR:-$HOME/.B12/hooks}"
# shellcheck disable=SC1091
. "$_B12_HOOK_DIR/_b12_common.sh"

# ── Self-timeout watchdog (3s — this hook MUST be fast) ──────
( sleep 3 && kill -TERM $$ 2>/dev/null ) &
_WATCHDOG=$!
trap "kill $_WATCHDOG 2>/dev/null; wait $_WATCHDOG 2>/dev/null" EXIT

INPUT=$(cat)
TOOL_NAME=$(echo "$INPUT" | jq -r '.tool_name // ""')
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // ""')

# Q5 (P-RECALL) telemetry — session id + log path. (The latency field was
# removed: measuring it required a per-hook `perl -MTime::HiRes` fork on this
# machine's /bin/bash 3.2.57 where $EPOCHREALTIME is empty, and
# checkpoint-telemetry.jsonl has no consumer for the field.)
_Q5_SESSION_ID12="${SESSION_ID:0:12}"
_Q5_TELEMETRY_LOG="${B12_DATA_DIR:-$HOME/.B12}/memory-logs/checkpoint-telemetry.jsonl"

# Skip if essential tools — don't slow down critical paths
case "$TOOL_NAME" in
  mcp__B12__memory_store|mcp__B12__memory_search|mcp__B12__memory_update)
    echo '{}'
    exit 0
    ;;
esac

B12_BASE="${B12_DATA_DIR:-$HOME/.B12}"
STAGING_DIR="$B12_BASE/memory-staging"
CHECKPOINT_DIR="$STAGING_DIR/checkpoint"
mkdir -p "$CHECKPOINT_DIR" 2>/dev/null

COUNTER_FILE="$CHECKPOINT_DIR/.call-counter-${SESSION_ID:0:12}"
BUFFER_FILE="$CHECKPOINT_DIR/.buffer-${SESSION_ID:0:12}.jsonl"
LAST_FLUSH="$CHECKPOINT_DIR/.last-flush-${SESSION_ID:0:12}"

# (Stale-file cleanup runs inside the backgrounded worker below, collapsed into
# a single `find` — see the comment there.)

NOW=$(date +%s)

# ── Rate limit check ─────────────────────────────────────────
# Increment counter
COUNT=0
if [ -f "$COUNTER_FILE" ]; then
  COUNT=$(cat "$COUNTER_FILE" 2>/dev/null || echo 0)
fi
COUNT=$((COUNT + 1))
echo "$COUNT" > "$COUNTER_FILE"

# Check time since last flush
LAST_FLUSH_TIME=0
if [ -f "$LAST_FLUSH" ]; then
  LAST_FLUSH_TIME=$(cat "$LAST_FLUSH" 2>/dev/null || echo 0)
fi
ELAPSED=$(( NOW - LAST_FLUSH_TIME ))

# Not time yet? Exit early (every 15 calls or 600 seconds).
# Q5: log the early-skip so we can tell "no opportunity to capture" apart
# from "captured but found nothing" in the daily checkpoint audit.
if [ "$COUNT" -lt 15 ] && [ "$ELAPSED" -lt 600 ]; then
  echo "{\"ts\":$(date +%s),\"session_id\":\"${_Q5_SESSION_ID12}\",\"tool\":\"${TOOL_NAME}\",\"phase\":\"rate_limit_skip\",\"counter\":${COUNT},\"elapsed_s\":${ELAPSED}}" \
    >> "$_Q5_TELEMETRY_LOG" 2>/dev/null
  echo '{}'
  exit 0
fi

# Reset counter (we're processing now)
echo "0" > "$COUNTER_FILE"

# ── Extract scannable text ───────────────────────────────────
# Combine tool_input and tool_result for pattern scanning
# Truncate to 4000 chars to stay within budget
SCAN_TEXT=$(echo "$INPUT" | jq -r '
  [
    (.tool_input | if type == "object" then
      (.content // ""),
      (.command // ""),
      (.file_path // ""),
      (.pattern // "")
    else . end),
    (.tool_result // "" | if length > 3000 then .[:3000] else . end)
  ] | map(select(. != null and . != "")) | join("\n")
' 2>/dev/null | head -c 4000)

# Skip if nothing to scan
if [ -z "$SCAN_TEXT" ] || [ ${#SCAN_TEXT} -lt 20 ]; then
  echo '{}'
  exit 0
fi

# ── Pattern matching via Python ──────────────────────────────
B12_SCRIPTS="${B12_HOOK_DIR:-$HOME/.B12/hooks}/scripts"

# Detect project name from CWD
PROJECT_NAME=$(basename "${PWD:-/tmp}")

# S1 (P-SPEED): run the regex + classifier + DB write entirely in
# background. This hook fires on EVERY PostToolUse and emits no
# additionalContext — there is zero reason to block the tool flow on
# the ~50-200ms Python heredoc. The main script falls through to the
# `echo '{}'` immediately, the child process finishes the work,
# disown'd so a parent exit cannot SIGHUP it.
{
# Stale-file cleanup. Collapsed from 4 separate `find` forks (which ran on every
# PostToolUse before the rate-limit early-exit) into ONE `find` inside this
# backgrounded worker, so cleanup never blocks the hot path. The 1-day sweep
# excludes `*.lock` — the fcntl.flock sidecars on the buffer file
# (`.buffer-<sid>.jsonl.lock`) must not be deleted while a concurrent worker
# holds flock on the inode (the next worker would create a new inode under the
# same name and race the writers). Lock sidecars are 0 bytes and get a separate
# 7-day sweep so they don't accumulate forever either. The combined predicate
# deletes the exact same set as the original four (verified).
find "$CHECKPOINT_DIR" \( \
    \( \( -name '.call-counter-*' -o -name '.buffer-*' -o -name '.last-flush-*' \) ! -name '*.lock' -mtime +1 \) \
    -o \( -name '*.lock' -mtime +7 \) \
  \) -delete 2>/dev/null || true

python3 - "$SCAN_TEXT" "$BUFFER_FILE" "$B12_SCRIPTS" "$PROJECT_NAME" "$NOW" "$LAST_FLUSH" "$_Q5_TELEMETRY_LOG" "$_Q5_SESSION_ID12" "$TOOL_NAME" << 'PYEOF'
import sys
import os
import json

scan_text = sys.argv[1]
buffer_file = sys.argv[2]
scripts_dir = sys.argv[3]
project_name = sys.argv[4]
now = int(sys.argv[5])
last_flush_file = sys.argv[6]

# ── Concurrency lock (PR #28 P1) ─────────────────────
# S1 backgrounding means multiple checkpoint hooks can overlap on the
# same per-session BUFFER_FILE + LAST_FLUSH. fcntl.flock on a sidecar
# `.lock` file serializes the critical sections (append-to-buffer,
# read-buffer, batch-insert, remove-buffer, write-last-flush). The
# lock is released when this Python process exits — even on hard kill —
# so a crashed worker won't deadlock the next fire.
import fcntl as _b12_fcntl
_b12_lock_path = buffer_file + ".lock"
_b12_lock_fh = open(_b12_lock_path, "a+")
try:
    _b12_fcntl.flock(_b12_lock_fh.fileno(), _b12_fcntl.LOCK_EX)
except OSError:
    # Locks unsupported (rare — e.g. NFS without flock) — proceed unlocked.
    pass

# ── Q5 (P-RECALL) telemetry plumbing ───────────────────────
# We're inside a backgrounded subshell — write our own JSONL line so the
# parent doesn't need to wait. (Latency tracking was removed: it required a
# perl Time::HiRes fork — $EPOCHREALTIME is empty on bash 3.2.57 — and
# checkpoint-telemetry.jsonl has no consumer for the field.)
import time as _q5_time
_q5_log_path = sys.argv[7] if len(sys.argv) > 7 else ""
_q5_sid12 = sys.argv[8] if len(sys.argv) > 8 else ""
_q5_tool = sys.argv[9] if len(sys.argv) > 9 else ""

def _q5_log(phase, captured=0, dropped_dedup=0, inserted=0, error=None):
    if not _q5_log_path:
        return
    import json as _json
    rec = {
        "ts": int(_q5_time.time()),
        "session_id": _q5_sid12,
        "tool": _q5_tool,
        "phase": phase,
        "captured": int(captured),
        "dropped_dedup": int(dropped_dedup),
        "inserted": int(inserted),
    }
    if error:
        rec["error"] = str(error)[:120]
    try:
        with open(_q5_log_path, "a") as f:
            f.write(_json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass

# Q5 final-line bookkeeping — log no matter how we exit unless explicit phase=flush already wrote.
_q5_final_phase = "scan_only"
_q5_final_stats = {"captured": 0, "dropped_dedup": 0, "inserted": 0}

import atexit as _q5_atexit
def _q5_finalize():
    if _q5_final_phase != "logged":
        _q5_log(_q5_final_phase, **_q5_final_stats)
_q5_atexit.register(_q5_finalize)

# Import shared patterns
sys.path.insert(0, scripts_dir)
try:
    from shared_patterns import (
        DECISION_RE, ERROR_RE, LEARNING_RE, PREFERENCE_RE,
        TOOL_PREF_RE, ARCH_RE, WORKFLOW_RE, CORRECTION_RE,
        IMPLICIT_DECISION_RE, REASON_RE, BLOCKER_RE,
        DB_PATH, content_hash, validate_metadata,
        summary_filter, classify_by_prefix,
    )
except ImportError:
    # shared_patterns not available — skip silently
    _q5_final_phase = "import_fail"
    sys.exit(0)

# Scrub secrets BEFORE buffering: items are appended to .buffer-*.jsonl under
# memory-staging and can sit there across invocations (until >=3 accumulate or
# the flush fires), so redact at capture, not just at the DB flush.
try:
    from b12_pii_scrubber import scrub as _pii_scrub
except ImportError:
    def _pii_scrub(_s):
        return _s

# ── Layer 0: Skip session summary recitations ───────────────
if summary_filter(scan_text):
    sys.exit(0)

# ── Layer 1: Check for [Label] prefix auto-classification ───
prefix_classified = False
prefix_result = classify_by_prefix(scan_text)
if prefix_result:
    h = content_hash(scan_text[:200])
    with open(buffer_file, "a") as f:
        f.write(json.dumps({
            "content": _pii_scrub(scan_text[:300]),
            "category": prefix_result["type"],
            "score": 9,
            "hash": h,
        }, ensure_ascii=False) + "\n")
    prefix_classified = True
    # Skip regex — prefix is deterministic, jump to flush check

# ── Layer 2: Scan for patterns (regex) — skip if prefix handled ─
PATTERNS = [
    (DECISION_RE,          "decision",          8),
    (IMPLICIT_DECISION_RE, "implicit_decision", 7),
    (ERROR_RE,             "error",             8),
    (LEARNING_RE,          "learning",          7),
    (PREFERENCE_RE,        "preference",        9),
    (TOOL_PREF_RE,         "tool_pref",         7),
    (ARCH_RE,              "architecture",       7),
    (WORKFLOW_RE,          "workflow",           6),
    (CORRECTION_RE,        "correction",         8),
    (REASON_RE,            "reasoning",          6),
    (BLOCKER_RE,           "blocker",            8),
]

matches = []
seen_hashes = set()

if not prefix_classified:
    for regex, category, base_score in PATTERNS:
        for m in regex.finditer(scan_text):
            text = m.group(0).strip()
            if len(text) < 15 or len(text) > 500:
                continue
            h = content_hash(text)
            if h in seen_hashes:
                continue
            seen_hashes.add(h)
            matches.append({
                "content": text,
                "category": "general",
                "score": base_score,
                "hash": h,
            })

    if not matches:
        sys.exit(0)

    # ── ML classify via daemon (LogReg head over embeddings) ─────
    import socket as _sock
    _uid = os.getuid() if hasattr(os, 'getuid') else os.getpid()
    _daemon_sock = f"/tmp/b12-embed-{_uid}.sock"

    def _daemon_classify(text):
        """Call daemon classify op. Returns type str or None."""
        try:
            s = _sock.socket(_sock.AF_UNIX, _sock.SOCK_STREAM)
            s.settimeout(2)
            s.connect(_daemon_sock)
            req = json.dumps({"op": "classify", "text": text}) + "\n"
            s.sendall(req.encode())
            data = b""
            while b"\n" not in data:
                chunk = s.recv(4096)
                if not chunk:
                    break
                data += chunk
            s.close()
            resp = json.loads(data.decode().strip())
            if resp.get("ok") and resp.get("type"):
                return resp["type"]
        except Exception:
            pass
        return None

    # Try to classify each match — fallback to "general"
    if os.path.exists(_daemon_sock):
        for m in matches:
            classified = _daemon_classify(m["content"])
            if classified:
                m["category"] = classified

    # ── Append regex matches to buffer ───────────────────────────
    with open(buffer_file, "a") as f:
        for m in matches:
            m["content"] = _pii_scrub(m.get("content", ""))
            f.write(json.dumps(m, ensure_ascii=False) + "\n")

# ── Check buffer size — flush if ≥ 3 items ──────────────────
buffer_items = []
if os.path.exists(buffer_file):
    with open(buffer_file, "r") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    buffer_items.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

if len(buffer_items) < 3:
    sys.exit(0)

# ── Batch INSERT to DB ───────────────────────────────────────
import sqlite3

if not os.path.exists(DB_PATH):
    sys.exit(0)

try:
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")

    # PII / secret scrub before store (parity with write_time_merge & session-end).
    # scripts_dir is already on sys.path (see above); honors B12_DISABLE_PII_SCRUB=1.
    try:
        from b12_pii_scrubber import scrub as _pii_scrub
    except ImportError:
        def _pii_scrub(_s):
            return _s

    try:
        import b12_importance as _b12imp

        def _resolve_importance(_content):
            # Single chokepoint: 0.70 (the prior checkpoint floor) is passed as the
            # supplied value. finalize_importance caps a credential-bearing item at
            # baseline (never floored to 0.70) and otherwise returns the strongest
            # of {content score, 0.70} — bit-identical to the prior
            # max(score, 0.70) since no memory_type floor is applied here.
            return _b12imp.finalize_importance(_content, supplied=0.70)
    except ImportError:
        def _resolve_importance(_content):
            return 0.7   # prior checkpoint floor if the scorer is unavailable

    # Deduplicate against existing memories (by content_hash)
    existing_hashes = set()
    try:
        rows = conn.execute(
            "SELECT content_hash FROM memories WHERE content_hash IS NOT NULL"
        ).fetchall()
        existing_hashes = {r[0] for r in rows if r[0]}
    except Exception:
        pass

    inserted = 0
    dropped_dedup = 0
    for item in buffer_items:
        if item["hash"] in existing_hashes:
            dropped_dedup += 1
            continue

        tags = f"proj:{project_name},checkpoint,{item['category']}"
        # Floor at 0.7 (the prior checkpoint constant): checkpoint only buffers
        # content already flagged high-signal by the regex categories (decision /
        # error / learning / preference / tool_pref / correction / blocker, base
        # score >= 6) or a [Label] prefix, so a category-flagged capture stays at
        # >= 0.7 even when b12_importance's content tokens don't fire (e.g.
        # "user prefers tabs over spaces"). Content heuristics can only RAISE it
        # (a date/decision/"remember" -> up to 0.95). Without the floor, importance
        # now also shortens the aging half-life, so these would age faster than before.
        # Secret cap first (a credential-bearing checkpoint item is held at
        # baseline, never floored to 0.7), then the checkpoint's own high-signal
        # floor for everything else — both via the shared chokepoint.
        importance_score = _resolve_importance(item["content"])
        metadata = validate_metadata({
            "type": item["category"],
            "source": "checkpoint",
            "importance_score": importance_score,
            "project": project_name,
            "content_hash": item["hash"],
        })

        try:
            conn.execute(
                """INSERT INTO memories (content, metadata, tags, created_at, updated_at)
                   VALUES (?, ?, ?, datetime('now'), datetime('now'))""",
                (_pii_scrub(item["content"]), metadata, tags)
            )
            inserted += 1
            existing_hashes.add(item["hash"])
        except sqlite3.IntegrityError:
            dropped_dedup += 1

    if inserted > 0:
        conn.commit()

    conn.close()
    _q5_log("flush", captured=len(buffer_items),
            dropped_dedup=dropped_dedup, inserted=inserted)
    _q5_final_phase = "logged"
except (sqlite3.OperationalError, sqlite3.DatabaseError) as _q5_err:
    # DB locked or unavailable — skip silently
    _q5_log("db_error", captured=len(buffer_items), error=_q5_err)
    _q5_final_phase = "logged"
    pass

# Clear buffer after flush
try:
    os.remove(buffer_file)
except OSError:
    pass

# Update last flush timestamp
with open(last_flush_file, "w") as f:
    f.write(str(now))

PYEOF
} >/dev/null 2>&1 &
disown

echo '{}'
exit 0
