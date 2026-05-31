#!/bin/bash
# B12 Memory System — SubagentStop Hook (v1, 2026-05-18)
# Captures subagent responses (Agent tool, /batch, Explore/Plan/custom
# agents) as candidate memories before they vanish from parent context.
#
# Fires on: SubagentStop (general-purpose, Explore, Plan, custom agents)
# Output: empty JSON (side-effect only)
# Budget: <500ms, async
#
# Why: subagents return long summaries that the parent context window
# absorbs but doesn't always preserve. The summary text is exactly the
# kind of cross-session-useful signal B12 wants. With
# CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1 enabled, this
# fires frequently — every Explore / Plan / general-purpose call.
#
# Implementation: reuses shared_patterns.py + the existing checkpoint
# buffer so dedup, flock, and the eventual flush path are unchanged.

_B12_HOOK_DIR="${B12_HOOK_DIR:-$HOME/.B12/hooks}"
# shellcheck disable=SC1091
. "$_B12_HOOK_DIR/_b12_common.sh"

( sleep 4 && kill -TERM $$ 2>/dev/null ) &
_WATCHDOG=$!
trap "kill $_WATCHDOG 2>/dev/null; wait $_WATCHDOG 2>/dev/null" EXIT

INPUT=$(cat)
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // ""')
AGENT_TYPE=$(echo "$INPUT" | jq -r '.agent_type // ""')
# Codex review of PR #38 (2026-05-18) flagged that the Stop hook's
# on-the-wire field is `last_assistant_message`, not `response` as
# code.claude.com/docs/en/hooks documents. SubagentStop's docs use
# the same `response` name; pre-emptively accept either so this hook
# stays correct if SubagentStop follows the same shift.
RESPONSE=$(echo "$INPUT" | jq -r '
  (.last_assistant_message // "") as $a
  | (.response // "") as $b
  | if ($a | length) > 0 then $a else $b end
')

# Skip empty / trivial responses (canceled subagent, error path).
if [ "${#RESPONSE}" -lt 300 ]; then
  echo '{}'
  exit 0
fi

B12_BASE="${B12_DATA_DIR:-$HOME/.B12}"
STAGING_DIR="$B12_BASE/memory-staging"
CHECKPOINT_DIR="$STAGING_DIR/checkpoint"
mkdir -p "$CHECKPOINT_DIR" 2>/dev/null

SESSION_ID12="${SESSION_ID:0:12}"
BUFFER_FILE="$CHECKPOINT_DIR/.buffer-${SESSION_ID12}.jsonl"

{
python3 - "$RESPONSE" "$AGENT_TYPE" "$BUFFER_FILE" "$_B12_HOOK_DIR/scripts" << 'PYEOF'
import sys, os, json, fcntl

response_text, agent_type, buffer_file, scripts_dir = sys.argv[1:5]

sys.path.insert(0, scripts_dir)
try:
    from shared_patterns import (
        DECISION_RE, ERROR_RE, LEARNING_RE, REASON_RE, BLOCKER_RE,
        content_hash, summary_filter,
    )
except ImportError:
    sys.exit(0)

# Subagent responses are typically structured summaries — skip them
# wholesale if they look like a session recap (Phase X recap, etc.)
# which the session-end pipeline already covers.
if summary_filter(response_text):
    sys.exit(0)

# Keep the tail — subagents almost always put findings / conclusions
# at the end. 6 KB is enough to capture the wrap-up without bloating.
text = response_text[-6000:]

# Subagent type prefix lets future retrieval distinguish "Explore
# said X" from "general-purpose said X" without re-running the agent.
prefix = f"[subagent:{agent_type or 'unknown'}] "

candidates = []
seen_hashes = set()

# Tighter selection than turn-end — subagents emit lots of text but
# only the decision-shaped sentences are worth a memory slot.
PATTERNS = [
    (DECISION_RE,  "decision",   8),
    (LEARNING_RE,  "learning",   7),
    (ERROR_RE,     "error",      8),
    (REASON_RE,    "reasoning",  6),
    (BLOCKER_RE,   "blocker",    8),
]

for regex, category, score in PATTERNS:
    for m in regex.finditer(text):
        snippet = m.group(0).strip()
        if len(snippet) < 30 or len(snippet) > 500:
            continue
        marked = (prefix + snippet)[:500]
        h = content_hash(marked)
        if h in seen_hashes:
            continue
        seen_hashes.add(h)
        candidates.append({
            "content": marked,
            "category": category,
            "score": score,
            "hash": h,
            "source": f"subagent_stop:{agent_type or 'unknown'}",
        })
        if len(candidates) >= 4:
            break
    if len(candidates) >= 4:
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
