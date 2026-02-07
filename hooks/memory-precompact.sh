#!/bin/bash
# B12 Memory System - PreCompact Hook (v2)
# Extracts comprehensive context before compaction and stages it for recovery
#
# Fires on: auto, manual
# Side effect: Creates staging file in ~/.claude/memory-staging/
#
# Install: Copy to ~/.claude/hooks/ and chmod +x

INPUT=$(cat)
TRANSCRIPT_PATH=$(echo "$INPUT" | jq -r '.transcript_path // ""')
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // "unknown"')
CWD=$(echo "$INPUT" | jq -r '.cwd // ""')

PROJECT_NAME=$(basename "$CWD" 2>/dev/null || echo "unknown")
STAGING_DIR="$HOME/.claude/memory-staging"
mkdir -p "$STAGING_DIR"

if [ -f "$TRANSCRIPT_PATH" ]; then
  # IMPORTANT: Use `python3 -` to read script from stdin via heredoc.
  # `python3 << 'EOF' "$ARG"` is WRONG — Python interprets $ARG as the script file.
  python3 - "$TRANSCRIPT_PATH" "$PROJECT_NAME" "$SESSION_ID" "$STAGING_DIR" << 'PYEOF'
import sys, json, os

transcript_path = sys.argv[1]
project_name = sys.argv[2]
session_id = sys.argv[3]
staging_dir = sys.argv[4]

user_messages = []
assistant_messages = []
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
                        user_messages.append(content[:500])
                    elif isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict) and block.get('type') == 'text':
                                user_messages.append(block['text'][:500])

                elif msg_type == 'assistant':
                    content = obj.get('message', {}).get('content', [])
                    if isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict):
                                if block.get('type') == 'text':
                                    text = block.get('text', '')
                                    if text.strip():
                                        assistant_messages.append(text[:800])
                                elif block.get('type') == 'tool_use':
                                    inp = block.get('input', {})
                                    name = block.get('name', '')
                                    if name in ('Edit', 'Write') and 'file_path' in inp:
                                        files_modified.add(inp['file_path'])
            except (json.JSONDecodeError, KeyError, TypeError):
                continue
except Exception:
    pass

# Build comprehensive staging summary
lines = []
lines.append(f"Project: {project_name}")
lines.append(f"Session: {session_id[:12]}")
lines.append(f"User messages: {len(user_messages)}")
lines.append("")

# All user requests (captures intent)
lines.append("USER REQUESTS:")
for i, msg in enumerate(user_messages[-15:]):
    lines.append(f"  {i+1}. {msg[:300]}")
lines.append("")

# Recent assistant work (captures what was done)
lines.append("RECENT WORK:")
meaningful = [m for m in assistant_messages if len(m) > 80]
for msg in meaningful[-10:]:
    lines.append(f"  - {msg[:400]}")
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
