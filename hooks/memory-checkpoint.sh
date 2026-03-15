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

# ── Self-timeout watchdog (3s — this hook MUST be fast) ──────
( sleep 3 && kill -TERM $$ 2>/dev/null ) &
_WATCHDOG=$!
trap "kill $_WATCHDOG 2>/dev/null; wait $_WATCHDOG 2>/dev/null" EXIT

INPUT=$(cat)
TOOL_NAME=$(echo "$INPUT" | jq -r '.tool_name // ""')
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // ""')

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

# Cleanup stale session files (older than 24 hours)
find "$CHECKPOINT_DIR" -name '.call-counter-*' -mtime +1 -delete 2>/dev/null || true
find "$CHECKPOINT_DIR" -name '.buffer-*' -mtime +1 -delete 2>/dev/null || true
find "$CHECKPOINT_DIR" -name '.last-flush-*' -mtime +1 -delete 2>/dev/null || true

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

# Not time yet? Exit early (every 15 calls or 600 seconds)
if [ "$COUNT" -lt 15 ] && [ "$ELAPSED" -lt 600 ]; then
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

python3 - "$SCAN_TEXT" "$BUFFER_FILE" "$B12_SCRIPTS" "$PROJECT_NAME" "$NOW" "$LAST_FLUSH" << 'PYEOF'
import sys
import os
import json

scan_text = sys.argv[1]
buffer_file = sys.argv[2]
scripts_dir = sys.argv[3]
project_name = sys.argv[4]
now = int(sys.argv[5])
last_flush_file = sys.argv[6]

# Import shared patterns
sys.path.insert(0, scripts_dir)
try:
    from shared_patterns import (
        DECISION_RE, ERROR_RE, LEARNING_RE, PREFERENCE_RE,
        TOOL_PREF_RE, ARCH_RE, WORKFLOW_RE, CORRECTION_RE,
        IMPLICIT_DECISION_RE, REASON_RE, BLOCKER_RE,
        DB_PATH, content_hash, validate_metadata
    )
except ImportError:
    # shared_patterns not available — skip silently
    sys.exit(0)

# ── Scan for patterns ────────────────────────────────────────
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
            "content": f"[{category.title()}] {text}",
            "category": category,
            "score": base_score,
            "hash": h,
        })

if not matches:
    sys.exit(0)

# ── Append to buffer ─────────────────────────────────────────
with open(buffer_file, "a") as f:
    for m in matches:
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
    for item in buffer_items:
        if item["hash"] in existing_hashes:
            continue

        tags = f"proj:{project_name},checkpoint,{item['category']}"
        metadata = validate_metadata({
            "type": item["category"],
            "source": "checkpoint",
            "importance_score": 0.7,
            "project": project_name,
            "content_hash": item["hash"],
        })

        try:
            conn.execute(
                """INSERT INTO memories (content, metadata, tags, created_at, updated_at)
                   VALUES (?, ?, ?, datetime('now'), datetime('now'))""",
                (item["content"], metadata, tags)
            )
            inserted += 1
            existing_hashes.add(item["hash"])
        except sqlite3.IntegrityError:
            pass

    if inserted > 0:
        conn.commit()

    conn.close()
except (sqlite3.OperationalError, sqlite3.DatabaseError):
    # DB locked or unavailable — skip silently
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

echo '{}'
exit 0
