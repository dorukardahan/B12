#!/bin/bash
# B12 Memory System - PreCompact Hook (v2 — Priority-Weighted Extraction)
# Extracts and scores content before compaction, keeps only high-value items
# Fires on: auto, manual
#
# v2 changes:
# - Priority-weighted scoring (decisions > learnings > errors > files > general)
# - Token budget: keeps only top items within ~2000 tokens
# - Setup/scope context preserved through compaction

# ── Self-timeout watchdog ─────────────────────────────────────
# Kills this script if it exceeds max runtime. Prevents orphan processes.
( sleep 25 && kill -TERM $$ 2>/dev/null ) &
_WATCHDOG=$!
trap "kill $_WATCHDOG 2>/dev/null; wait $_WATCHDOG 2>/dev/null" EXIT

INPUT=$(cat)
TRANSCRIPT_PATH=$(echo "$INPUT" | jq -r '.transcript_path // ""')
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // "unknown"')
CWD=$(echo "$INPUT" | jq -r '.cwd // ""')

# Central data directory — override with B12_DATA_DIR env var for custom setups
B12_BASE="${B12_DATA_DIR:-$HOME/.claude}"

PROJECT_NAME=$(basename "$CWD" 2>/dev/null || echo "unknown")
STAGING_DIR="$B12_BASE/memory-staging"
mkdir -p "$STAGING_DIR"

if [ -f "$TRANSCRIPT_PATH" ]; then
  python3 - "$TRANSCRIPT_PATH" "$PROJECT_NAME" "$SESSION_ID" "$STAGING_DIR" "$CWD" << 'PYEOF'
import sys, json, os, re

# Import shared patterns (DRY — same patterns used in session-end.sh)
# B12_HOOK_DIR controls code location; B12_DATA_DIR controls data only
_hook_dir = os.environ.get('B12_HOOK_DIR', os.path.expanduser('~/.claude/hooks'))
sys.path.insert(0, os.path.join(_hook_dir, 'scripts'))
from shared_patterns import DECISION_RE, ERROR_RE, LEARNING_RE, PREFERENCE_RE

transcript_path = sys.argv[1]
project_name = sys.argv[2]
session_id = sys.argv[3]
staging_dir = sys.argv[4]
cwd = sys.argv[5]

# Priority weights for content scoring
PRIORITY_WEIGHTS = {
    'decision': 10,
    'error_fix': 9,
    'learning': 8,
    'preference': 8,
    'file_modified': 7,
    'user_request': 6,
    'progress': 5,
    'general_work': 2,
}

# Scored items: [(priority, category, text)]
scored_items = []
user_messages = []
files_modified = set()

try:
    # Optimization: only read last 3000 lines to avoid slow parse on huge transcripts
    import subprocess
    tail_result = subprocess.run(
        ['tail', '-n', '3000', transcript_path],
        capture_output=True, text=True, timeout=10
    )
    tail_lines = tail_result.stdout.splitlines() if tail_result.returncode == 0 else []

    for line in tail_lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            msg_type = obj.get('type', '')

            if msg_type == 'human':
                content = obj.get('message', {}).get('content', '')
                if isinstance(content, str) and content.strip():
                    user_messages.append(content[:300])
                elif isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get('type') == 'text':
                            user_messages.append(block['text'][:300])

            elif msg_type == 'assistant':
                content = obj.get('message', {}).get('content', [])
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict):
                            if block.get('type') == 'text' and block.get('text', '').strip():
                                text = block['text']
                                snippet = text[:400]

                                # Score by pattern match
                                if DECISION_RE.search(snippet):
                                    scored_items.append((PRIORITY_WEIGHTS['decision'], 'decision', snippet[:200]))
                                elif ERROR_RE.search(snippet):
                                    scored_items.append((PRIORITY_WEIGHTS['error_fix'], 'error_fix', snippet[:200]))
                                elif LEARNING_RE.search(snippet):
                                    scored_items.append((PRIORITY_WEIGHTS['learning'], 'learning', snippet[:200]))
                                elif PREFERENCE_RE.search(snippet):
                                    scored_items.append((PRIORITY_WEIGHTS['preference'], 'preference', snippet[:200]))
                                elif len(text) > 100:
                                    scored_items.append((PRIORITY_WEIGHTS['general_work'], 'general', snippet[:200]))

                            elif block.get('type') == 'tool_use':
                                inp = block.get('input', {})
                                name = block.get('name', '')
                                if name in ('Edit', 'Write') and 'file_path' in inp:
                                    files_modified.add(inp['file_path'])
        except (json.JSONDecodeError, KeyError, TypeError):
            continue
except Exception as e:
    import traceback
    log_dir = os.path.expanduser("~/.claude/memory-logs")
    os.makedirs(log_dir, exist_ok=True)
    with open(os.path.join(log_dir, "memory-errors.log"), 'a') as ef:
        ef.write(f"[{__import__('datetime').datetime.now().isoformat()}] PreCompact error: {e}\n")
        ef.write(traceback.format_exc() + "\n")

# Sort by priority (highest first), dedup, take top items within token budget
scored_items.sort(key=lambda x: -x[0])

# Dedup by first 80 chars
seen = set()
unique_items = []
for priority, category, text in scored_items:
    key = text[:80]
    if key not in seen:
        unique_items.append((priority, category, text))
        seen.add(key)

# Token budget: ~2000 tokens ≈ ~8000 chars
CHAR_BUDGET = 8000
lines = []
lines.append(f"Project: {project_name}")
lines.append(f"Session: {session_id[:12]}")
lines.append(f"User messages: {len(user_messages)}")
lines.append("")

# User requests (highest value — captures intent)
lines.append("USER REQUESTS:")
for msg in user_messages[-10:]:
    lines.append(f"  - {msg[:200]}")
lines.append("")

# Scored content (top items by priority)
char_used = sum(len(l) for l in lines)
lines.append("RECENT WORK:")
for priority, category, text in unique_items:
    entry = f"  [{category}] {text[:300]}"
    if char_used + len(entry) > CHAR_BUDGET:
        break
    lines.append(entry)
    char_used += len(entry)
lines.append("")

# Files touched
if files_modified:
    lines.append("FILES MODIFIED:")
    for f in sorted(files_modified)[:20]:
        lines.append(f"  - {f}")

summary = '\n'.join(lines)

# Write staging file
stage_file = os.path.join(staging_dir, f"precompact-{session_id}.txt")
with open(stage_file, 'w') as f:
    f.write(summary)

PYEOF
fi

# Clean up old staging files (older than 2 hours)
find "$STAGING_DIR" -name "precompact-*.txt" -mmin +120 -delete 2>/dev/null

exit 0
