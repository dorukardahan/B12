#!/bin/bash
# B12 Memory System - PostToolUse Working Memory Tracker (v1)
# Tracks active files and search patterns during conversation
# Persists to working-memory.json for use after compaction
#
# Fires on: Read, Edit, Write, Glob, Grep (PostToolUse)
# Output: empty JSON (side-effect only)
# Performance target: <50ms

INPUT=$(cat)
TOOL_NAME=$(echo "$INPUT" | jq -r '.tool_name // ""')
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // ""')

B12_BASE="${B12_DATA_DIR:-$HOME/.claude}"
STAGING_DIR="$B12_BASE/memory-staging"
WM_FILE="$STAGING_DIR/working-memory.json"
mkdir -p "$STAGING_DIR" 2>/dev/null

NOW=$(date +%s)

# Extract entity from tool input
ENTITY=""
ENTITY_TYPE="file"

case "$TOOL_NAME" in
  Read|Edit|Write)
    ENTITY=$(echo "$INPUT" | jq -r '.tool_input.file_path // ""' 2>/dev/null)
    [ "$TOOL_NAME" = "Edit" ] || [ "$TOOL_NAME" = "Write" ] && ENTITY_TYPE="modified"
    ;;
  Glob)
    ENTITY=$(echo "$INPUT" | jq -r '.tool_input.pattern // ""' 2>/dev/null)
    ENTITY_TYPE="search"
    ;;
  Grep)
    ENTITY=$(echo "$INPUT" | jq -r '.tool_input.pattern // ""' 2>/dev/null)
    ENTITY_TYPE="search"
    ;;
esac

# Skip if no entity extracted
if [ -z "$ENTITY" ] || [ "$ENTITY" = "null" ]; then
  echo '{}'
  exit 0
fi

# For files, use basename (shorter, more meaningful)
if [ "$ENTITY_TYPE" = "file" ] || [ "$ENTITY_TYPE" = "modified" ]; then
  ENTITY=$(basename "$ENTITY" 2>/dev/null || echo "$ENTITY")
fi

# Truncate search patterns
if [ "$ENTITY_TYPE" = "search" ]; then
  ENTITY=$(echo "$ENTITY" | head -c 80)
fi

# Update working memory JSON atomically via Python (jq can't do in-place array dedup easily)
python3 - "$WM_FILE" "$ENTITY" "$ENTITY_TYPE" "$SESSION_ID" "$NOW" << 'PYEOF'
import sys, json, os

wm_file = sys.argv[1]
entity = sys.argv[2]
entity_type = sys.argv[3]
session_id = sys.argv[4]
now = int(sys.argv[5])

# Load or initialize
wm = {"active_files": [], "modified_files": [], "search_patterns": [], "session_id": "", "updated_at": 0}
if os.path.exists(wm_file):
    try:
        with open(wm_file, 'r') as f:
            wm = json.load(f)
    except (json.JSONDecodeError, IOError):
        pass

# Reset if different session
if wm.get("session_id") != session_id:
    wm = {"active_files": [], "modified_files": [], "search_patterns": [], "session_id": session_id, "updated_at": now}

# Add entity to appropriate list (dedup, keep most recent first)
if entity_type == "modified":
    lst = wm.get("modified_files", [])
    if entity in lst:
        lst.remove(entity)
    lst.insert(0, entity)
    wm["modified_files"] = lst[:15]
    # Also track in active_files
    af = wm.get("active_files", [])
    if entity in af:
        af.remove(entity)
    af.insert(0, entity)
    wm["active_files"] = af[:20]
elif entity_type == "file":
    lst = wm.get("active_files", [])
    if entity in lst:
        lst.remove(entity)
    lst.insert(0, entity)
    wm["active_files"] = lst[:20]
elif entity_type == "search":
    lst = wm.get("search_patterns", [])
    if entity in lst:
        lst.remove(entity)
    lst.insert(0, entity)
    wm["search_patterns"] = lst[:10]

wm["updated_at"] = now

# Write atomically (write to tmp then rename)
tmp = wm_file + ".tmp"
with open(tmp, 'w') as f:
    json.dump(wm, f)
os.rename(tmp, wm_file)
PYEOF

echo '{}'
exit 0
