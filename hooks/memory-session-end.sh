#!/bin/bash
# B12 Memory System - SessionEnd Hook (v2)
# Extracts comprehensive session summary from transcript and saves it
#
# Fires on: clear, logout, prompt_input_exit, other
# Side effects:
#   - Writes session summary to ~/.claude/memory-summaries/{project}-latest.md
#   - Maintains rolling history in ~/.claude/memory-summaries/{project}-history.md
#   - Appends to ~/.claude/memory-logs/sessions.jsonl
#   - Cleans up staging files
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

# Extract comprehensive session summary from transcript
if [ -f "$TRANSCRIPT_PATH" ]; then
  # IMPORTANT: Use `python3 -` to read script from stdin via heredoc.
  # `python3 << 'EOF' "$ARG"` is WRONG — Python interprets $ARG as the script file.
  python3 - "$TRANSCRIPT_PATH" "$PROJECT_NAME" "$SESSION_ID" "$SUMMARY_DIR" "$CWD" << 'PYEOF'
import sys, json, os
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
                                    assistant_messages.append(block['text'][:500])
                                elif block.get('type') == 'tool_use':
                                    tool_name = block.get('name', '')
                                    tools_used.add(tool_name)
                                    inp = block.get('input', {})
                                    if tool_name in ('Edit', 'Write') and 'file_path' in inp:
                                        files_modified.add(inp['file_path'])
            except (json.JSONDecodeError, KeyError, TypeError):
                continue
except Exception:
    pass

# Build summary
summary_lines = []
summary_lines.append(f"# Session Summary: {project_name}")
summary_lines.append(f"- **Date**: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
summary_lines.append(f"- **Directory**: {cwd}")
summary_lines.append(f"- **Session**: {session_id[:12]}")
summary_lines.append(f"- **User messages**: {len(user_messages)}")
summary_lines.append(f"- **Tools used**: {', '.join(sorted(tools_used)[:15]) if tools_used else 'none'}")
summary_lines.append("")

# User requests (first 10, deduplicated)
if user_messages:
    summary_lines.append("## What the user asked")
    seen = set()
    count = 0
    for msg in user_messages:
        short = msg[:150].strip()
        if short and short not in seen and count < 10:
            summary_lines.append(f"- {short}")
            seen.add(short)
            count += 1
    summary_lines.append("")

# Key assistant outputs (last 8 meaningful messages)
if assistant_messages:
    meaningful = [m for m in assistant_messages if len(m) > 50]
    recent = meaningful[-8:] if meaningful else assistant_messages[-5:]
    summary_lines.append("## Key outputs")
    for msg in recent:
        summary_lines.append(f"- {msg[:300]}")
    summary_lines.append("")

# Files modified
if files_modified:
    summary_lines.append("## Files modified")
    for f in sorted(files_modified)[:20]:
        summary_lines.append(f"- {f}")
    summary_lines.append("")

summary_text = '\n'.join(summary_lines)

# Write latest session summary
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
sessions = sessions[-4:]  # keep last 4
sessions.append(summary_text)

with open(history_file, 'w') as f:
    f.write(separator.join(sessions))

PYEOF
fi

# Log session end
echo "{\"session\":\"$SESSION_ID\",\"project\":\"$PROJECT_NAME\",\"reason\":\"$REASON\",\"time\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}" >> "$LOG_DIR/sessions.jsonl"

# Keep log reasonable (last 1000 entries)
if [ -f "$LOG_DIR/sessions.jsonl" ]; then
  LINE_COUNT=$(wc -l < "$LOG_DIR/sessions.jsonl")
  if [ "$LINE_COUNT" -gt 1000 ]; then
    tail -500 "$LOG_DIR/sessions.jsonl" > "$LOG_DIR/sessions.jsonl.tmp"
    mv "$LOG_DIR/sessions.jsonl.tmp" "$LOG_DIR/sessions.jsonl"
  fi
fi

exit 0
