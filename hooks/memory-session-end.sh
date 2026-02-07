#!/bin/bash
# B12 Memory System - SessionEnd Hook (v3 — Structured Extraction)
# Extracts decisions, errors/fixes, preferences, learnings from transcript
# Fires on: clear, logout, prompt_input_exit, other
#
# Install: Copy to ~/.claude/hooks/ and chmod +x

INPUT=$(cat)
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // "unknown"')
REASON=$(echo "$INPUT" | jq -r '.reason // "other"')
CWD=$(echo "$INPUT" | jq -r '.cwd // ""')
TRANSCRIPT_PATH=$(echo "$INPUT" | jq -r '.transcript_path // ""')

PROJECT_NAME=$(basename "$CWD" 2>/dev/null || echo "unknown")
SUMMARY_DIR="$HOME/.claude/memory-summaries"
STAGING_DIR="$HOME/.claude/memory-staging"
LOG_DIR="$HOME/.claude/memory-logs"
mkdir -p "$SUMMARY_DIR" "$LOG_DIR"

# Clean up staging files for this session
rm -f "$STAGING_DIR/precompact-${SESSION_ID}.txt" 2>/dev/null

# Extract structured session summary from transcript
if [ -f "$TRANSCRIPT_PATH" ]; then
  python3 - "$TRANSCRIPT_PATH" "$PROJECT_NAME" "$SESSION_ID" "$SUMMARY_DIR" "$CWD" << 'PYEOF'
import sys, json, os, re
from datetime import datetime, timezone

transcript_path = sys.argv[1]
project_name = sys.argv[2]
session_id = sys.argv[3]
summary_dir = sys.argv[4]
cwd = sys.argv[5]

user_messages = []
assistant_messages = []
tools_used = set()
files_modified = set()
memory_stores = 0
memory_searches = 0

# Pattern definitions for structured extraction
DECISION_RE = re.compile(
    r'(?i)\b(decided|chose|going with|will use|selected|opted for|switched to|'
    r'picking|went with|using .+ instead of)\b'
)
ERROR_RE = re.compile(
    r'(?i)\b(error|bug|fix(?:ed)?|broke|crash|fail(?:ed|ure)?|issue|problem|'
    r'resolved|workaround|root cause|debugging)\b'
)
PREFERENCE_RE = re.compile(
    r'(?i)\b(prefer|don.t like|always use|never use|hate|love|want to|'
    r'convention|style preference|workflow)\b'
)
LEARNING_RE = re.compile(
    r'(?i)\b(learned|discovered|turns out|insight|TIL|didn.t know|realized|'
    r'gotcha|pitfall|caveat|trick|tip|important to note)\b'
)

decisions = []
errors_fixes = []
preferences = []
learnings = []

try:
    with open(transcript_path, 'r') as f:
        for line in f:
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
                                    assistant_messages.append(text[:500])

                                    # Pattern matching on assistant text
                                    snippet = text[:400]
                                    if DECISION_RE.search(snippet):
                                        decisions.append(snippet[:200])
                                    if ERROR_RE.search(snippet):
                                        errors_fixes.append(snippet[:200])
                                    if PREFERENCE_RE.search(snippet):
                                        preferences.append(snippet[:200])
                                    if LEARNING_RE.search(snippet):
                                        learnings.append(snippet[:200])

                                elif block.get('type') == 'tool_use':
                                    tool_name = block.get('name', '')
                                    tools_used.add(tool_name)
                                    inp = block.get('input', {})
                                    if tool_name in ('Edit', 'Write') and 'file_path' in inp:
                                        files_modified.add(inp['file_path'])
                                    if tool_name == 'mcp__memory__memory_store':
                                        memory_stores += 1
                                    elif tool_name == 'mcp__memory__memory_search':
                                        memory_searches += 1

                # Also check for tool_use in user messages (for pattern matching on user preferences)
                if msg_type == 'human':
                    content = obj.get('message', {}).get('content', '')
                    text = content if isinstance(content, str) else ''
                    if isinstance(content, list):
                        text = ' '.join(b.get('text', '') for b in content if isinstance(b, dict))
                    if text and PREFERENCE_RE.search(text[:300]):
                        preferences.append(f"[user] {text[:200]}")

            except (json.JSONDecodeError, KeyError, TypeError):
                continue
except Exception:
    pass

# Deduplicate extracted patterns
def dedup(items, max_count=5):
    seen = set()
    result = []
    for item in items:
        short = item[:80]
        if short not in seen and len(result) < max_count:
            result.append(item)
            seen.add(short)
    return result

decisions = dedup(decisions)
errors_fixes = dedup(errors_fixes)
preferences = dedup(preferences)
learnings = dedup(learnings)

# Build summary
now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
lines = []
lines.append(f"# Session Summary: {project_name}")
lines.append(f"- **Date**: {now}")
lines.append(f"- **Directory**: {cwd}")
lines.append(f"- **Session**: {session_id[:12]}")
lines.append(f"- **User messages**: {len(user_messages)}")
lines.append(f"- **Tools used**: {', '.join(sorted(tools_used)[:15]) if tools_used else 'none'}")
lines.append(f"- **Memory ops**: {memory_stores} stores, {memory_searches} searches")
lines.append("")

# Structured sections
if decisions:
    lines.append("## Decisions Made")
    for d in decisions:
        lines.append(f"- {d}")
    lines.append("")

if errors_fixes:
    lines.append("## Errors & Fixes")
    for e in errors_fixes:
        lines.append(f"- {e}")
    lines.append("")

if learnings:
    lines.append("## Key Learnings")
    for l in learnings:
        lines.append(f"- {l}")
    lines.append("")

if preferences:
    lines.append("## User Preferences Observed")
    for p in preferences:
        lines.append(f"- {p}")
    lines.append("")

# User requests (first 8, deduplicated)
if user_messages:
    lines.append("## What the user asked")
    seen = set()
    count = 0
    for msg in user_messages:
        short = msg[:150].strip()
        if short and short not in seen and count < 8:
            lines.append(f"- {short}")
            seen.add(short)
            count += 1
    lines.append("")

# Files modified
if files_modified:
    lines.append("## Files modified")
    for f in sorted(files_modified)[:20]:
        lines.append(f"- {f}")
    lines.append("")

summary_text = '\n'.join(lines)

# Write latest summary
summary_file = os.path.join(summary_dir, f"{project_name}-latest.md")
with open(summary_file, 'w') as f:
    f.write(summary_text)

# Append to rolling history (last 5 sessions)
history_file = os.path.join(summary_dir, f"{project_name}-history.md")
separator = "\n\n---\n\n"

existing = ""
if os.path.exists(history_file):
    with open(history_file, 'r') as f:
        existing = f.read()

sessions = existing.split("---")
sessions = [s.strip() for s in sessions if s.strip()]
sessions = sessions[-4:]
sessions.append(summary_text)

with open(history_file, 'w') as f:
    f.write(separator.join(sessions))

PYEOF
fi

# Log session end
echo "{\"session\":\"$SESSION_ID\",\"project\":\"$PROJECT_NAME\",\"reason\":\"$REASON\",\"time\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}" >> "$LOG_DIR/sessions.jsonl"

# Keep log reasonable
if [ -f "$LOG_DIR/sessions.jsonl" ]; then
  LINE_COUNT=$(wc -l < "$LOG_DIR/sessions.jsonl")
  if [ "$LINE_COUNT" -gt 1000 ]; then
    tail -500 "$LOG_DIR/sessions.jsonl" > "$LOG_DIR/sessions.jsonl.tmp"
    mv "$LOG_DIR/sessions.jsonl.tmp" "$LOG_DIR/sessions.jsonl"
  fi
fi

exit 0
