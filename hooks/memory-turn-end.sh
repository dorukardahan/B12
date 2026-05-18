#!/bin/bash
# B12 Memory System — Stop Hook (v1, 2026-05-18)
# End-of-turn capture: scans Claude's response text for decision /
# learning / error patterns and queues them into the checkpoint buffer.
#
# Fires on: Stop (every assistant turn end)
# Skips: trivial responses (<200 chars), pure tool-call narration
# Budget: <500ms, fully async
# Output: empty JSON — NEVER blocks. v2.1.143 added an 8-block cap on
#         Stop hooks; this hook is strictly side-effect.
#
# Why: PostToolUse sees tool results but not Claude's *commentary*
# around them. The final assistant text per turn is where Claude says
# "I tried X, decided Y, learned Z" — high signal that B12 was missing.
# Reuses the existing checkpoint buffer + flush so no new SQL paths.

_B12_HOOK_DIR="${B12_HOOK_DIR:-$HOME/.B12/hooks}"
# shellcheck disable=SC1091
. "$_B12_HOOK_DIR/_b12_common.sh"

( sleep 4 && kill -TERM $$ 2>/dev/null ) &
_WATCHDOG=$!
trap "kill $_WATCHDOG 2>/dev/null; wait $_WATCHDOG 2>/dev/null" EXIT

INPUT=$(cat)
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // ""')
# Claude Code's Stop payload field name shifted between releases:
# code.claude.com/docs/en/hooks documents `response`, but Codex review
# of PR #38 (2026-05-18) flagged that the actual on-the-wire field is
# `last_assistant_message`. Read both — whichever is non-empty wins —
# so this hook stays correct across versions without a config flip.
RESPONSE=$(echo "$INPUT" | jq -r '
  (.last_assistant_message // "") as $a
  | (.response // "") as $b
  | if ($a | length) > 0 then $a else $b end
')

# Skip trivial / empty responses — nothing useful to scan.
if [ "${#RESPONSE}" -lt 200 ]; then
  echo '{}'
  exit 0
fi

B12_BASE="${B12_DATA_DIR:-$HOME/.B12}"
STAGING_DIR="$B12_BASE/memory-staging"
CHECKPOINT_DIR="$STAGING_DIR/checkpoint"
mkdir -p "$CHECKPOINT_DIR" 2>/dev/null

SESSION_ID12="${SESSION_ID:0:12}"
BUFFER_FILE="$CHECKPOINT_DIR/.buffer-${SESSION_ID12}.jsonl"
PROJECT_NAME=$(basename "${PWD:-/tmp}")

# Async scan + append. Reuses shared_patterns.py — identical scoring
# and dedup semantics to the checkpoint hook, so a decision sentence
# Claude wrote in the response is treated the same as one Claude
# wrote in a tool result.
#
# Inline `{ … } &; disown` instead of b12_async_fork: that helper
# redirects stdin to /dev/null, which would swallow the PYEOF heredoc
# before python3 saw it. Matches the pattern memory-checkpoint.sh uses.
{
python3 - "$RESPONSE" "$BUFFER_FILE" "$_B12_HOOK_DIR/scripts" "$PROJECT_NAME" << 'PYEOF'
import sys, os, json, fcntl

response_text, buffer_file, scripts_dir, project_name = sys.argv[1:5]

sys.path.insert(0, scripts_dir)
try:
    from shared_patterns import (
        DECISION_RE, ERROR_RE, LEARNING_RE, PREFERENCE_RE,
        CORRECTION_RE, REASON_RE, BLOCKER_RE,
        content_hash, summary_filter, classify_by_prefix,
    )
except ImportError:
    sys.exit(0)

# Skip "this is a summary of what I just did" recitations — the
# session-end pipeline already handles those.
if summary_filter(response_text):
    sys.exit(0)

# Truncate to 8 KB — turn responses can be long, but decision signals
# usually live in the final paragraphs, so we keep the tail.
text = response_text[-8000:]

# Layer 1: prefix-classified sentences (e.g. "[Decision] ..." lines).
candidates = []
seen_hashes = set()

prefix_result = classify_by_prefix(text)
if prefix_result:
    snippet = text[:300].strip()
    h = content_hash(snippet[:200])
    candidates.append({
        "content": snippet,
        "category": prefix_result["type"],
        "score": 9,
        "hash": h,
        "source": "turn_end",
    })
    seen_hashes.add(h)

# Layer 2: pattern scan. Tighter score floor than checkpoint hook —
# end-of-turn fires once per turn, so we can afford to be selective.
PATTERNS = [
    (DECISION_RE,   "decision",   8),
    (LEARNING_RE,   "learning",   7),
    (ERROR_RE,      "error",      8),
    (PREFERENCE_RE, "preference", 9),
    (CORRECTION_RE, "correction", 8),
    (REASON_RE,     "reasoning",  6),
    (BLOCKER_RE,    "blocker",    8),
]

for regex, category, score in PATTERNS:
    for m in regex.finditer(text):
        snippet = m.group(0).strip()
        if len(snippet) < 30 or len(snippet) > 500:
            continue
        h = content_hash(snippet)
        if h in seen_hashes:
            continue
        seen_hashes.add(h)
        candidates.append({
            "content": snippet,
            "category": category,
            "score": score,
            "hash": h,
            "source": "turn_end",
        })
        # Cap: a single response shouldn't flood the buffer.
        if len(candidates) >= 5:
            break
    if len(candidates) >= 5:
        break

if not candidates:
    sys.exit(0)

lock_path = buffer_file + ".lock"
try:
    lock_fh = open(lock_path, "a+")
    try:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
    except OSError:
        pass
    with open(buffer_file, "a") as f:
        for c in candidates:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
except OSError:
    pass
PYEOF
} >/dev/null 2>&1 &
disown

echo '{}'
exit 0
