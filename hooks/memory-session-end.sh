#!/bin/bash
# B12 Memory System - SessionEnd Hook (v4 — Contextual Extraction + Scope Metadata)
# Extracts decisions, errors/fixes, preferences, learnings with contextual regex + scoring
# Produces: project summary, global summary, executive summary (5-line), rolling history
# Fires on: clear, logout, prompt_input_exit, other
#
# v4 changes:
# - Contextual multi-word regex patterns (reduce noise ~80%)
# - Scoring filter for extraction quality
# - Setup/scope metadata in summaries
# - Executive summary (5-line) for next session's compact loading
# - B12_DATA_DIR support for multi-setup

INPUT=$(cat)
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // "unknown"')
REASON=$(echo "$INPUT" | jq -r '.reason // "other"')
CWD=$(echo "$INPUT" | jq -r '.cwd // ""')
TRANSCRIPT_PATH=$(echo "$INPUT" | jq -r '.transcript_path // ""')

# Central data directory — override with B12_DATA_DIR env var for custom setups
B12_BASE="${B12_DATA_DIR:-$HOME/.claude}"

PROJECT_NAME=$(basename "$CWD" 2>/dev/null || echo "unknown")
SUMMARY_DIR="$B12_BASE/memory-summaries"
STAGING_DIR="$B12_BASE/memory-staging"
LOG_DIR="$B12_BASE/memory-logs"
mkdir -p "$SUMMARY_DIR" "$LOG_DIR"

# Setup detection
if [[ "$B12_BASE" == *".claude-x"* ]] || [[ "$CWD" == *"/0G"* ]] || [[ "$CWD" == *"/0g"* ]]; then
  SETUP_CONTEXT="work"
else
  SETUP_CONTEXT="personal"
fi

# Clean up staging files for this session
rm -f "$STAGING_DIR/precompact-${SESSION_ID}.txt" 2>/dev/null

# Extract structured session summary from transcript
if [ -f "$TRANSCRIPT_PATH" ]; then
  python3 - "$TRANSCRIPT_PATH" "$PROJECT_NAME" "$SESSION_ID" "$SUMMARY_DIR" "$CWD" "$SETUP_CONTEXT" << 'PYEOF'
import sys, json, os, re
from datetime import datetime, timezone

transcript_path = sys.argv[1]
project_name = sys.argv[2]
session_id = sys.argv[3]
summary_dir = sys.argv[4]
cwd = sys.argv[5]
setup_context = sys.argv[6]

user_messages = []
assistant_messages = []
tools_used = set()
files_modified = set()
memory_stores = 0
memory_searches = 0

# ═══════════════════════════════════════════════════════════════
# v4 CONTEXTUAL PATTERNS — require structural context, not just keywords
# ═══════════════════════════════════════════════════════════════

DECISION_RE = re.compile(
    r'(?i)(?:'
    r'(?:decided|chose|going with|selected|opted for|switched to|went with)\s+.{5,80}(?:instead of|over|rather than|because)'
    r'|(?:will use|using)\s+\S+\s+(?:instead of|rather than|for)\s+'
    r'|(?:the (?:approach|solution|decision) is to)\s+'
    r'|(?:decided|chose|going with|selected|opted for|switched to|went with)\s+\S+.{10,}'
    r')'
)

ERROR_RE = re.compile(
    r'(?i)(?:'
    r'(?:fixed|resolved|solved|workaround[: ])\s+.{5,60}(?:error|bug|issue|crash|failure)'
    r'|(?:error|bug|issue|crash)\s+.{0,40}(?:was caused by|because|due to|fixed by|resolved by)'
    r'|(?:root cause|the fix|the solution)\s*(?:is|was|:)\s+'
    r')'
)

PREFERENCE_RE = re.compile(
    r'(?i)(?:'
    r'(?:(?:user|doruk)\s+(?:prefers?|wants?|asked for|(?:does ?\x27?n.?t|never)\s+(?:want|like|use)))'
    r'|(?:always use|never use|convention is|style preference|workflow:)'
    r'|\[user\]\s+'
    r')'
)

LEARNING_RE = re.compile(
    r'(?i)(?:'
    r'(?:turns out|TIL|important to note|gotcha|pitfall|caveat)\s*(?::|that|,)\s+'
    r'|(?:learned|discovered|realized)\s+that\s+'
    r'|(?:the (?:trick|key|insight) (?:is|was))\s+'
    r')'
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

                # User preference detection
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

# ═══════════════════════════════════════════════════════════════
# v4 SCORING FILTER — quality gate for extracted items
# ═══════════════════════════════════════════════════════════════

def score_extraction(text, category):
    """Score how likely a text snippet is to be a genuine extraction."""
    score = 0
    text_lower = text.lower()

    if category == 'decision':
        if any(w in text_lower for w in ['instead of', 'over', 'rather than', 'because', 'tradeoff']):
            score += 2
        if any(w in text_lower for w in ['chose', 'decided', 'selected', 'opted']):
            score += 1

    elif category == 'error':
        has_problem = any(w in text_lower for w in ['error', 'bug', 'crash', 'fail', 'broke'])
        has_resolution = any(w in text_lower for w in ['fixed', 'resolved', 'solved', 'workaround', 'caused by', 'root cause'])
        if has_problem and has_resolution:
            score += 3
        elif has_problem:
            score += 0  # Problem without resolution = not useful

    elif category == 'learning':
        if any(w in text_lower for w in ['turns out', 'gotcha', 'pitfall', 'caveat', 'important to note']):
            score += 2
        if 'because' in text_lower or 'so that' in text_lower:
            score += 1

    elif category == 'preference':
        if any(w in text_lower for w in ['always', 'never', 'prefer', 'convention']):
            score += 1
        if any(w in text_lower for w in ['user', 'doruk', '[user]']):
            score += 2

    # Penalty for very short text
    if len(text) < 40:
        score -= 1

    return score

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

# Apply dedup first, then scoring filter
decisions = [d for d in dedup(decisions) if score_extraction(d, 'decision') >= 1]
errors_fixes = [e for e in dedup(errors_fixes) if score_extraction(e, 'error') >= 2]
learnings = [l for l in dedup(learnings) if score_extraction(l, 'learning') >= 1]
preferences = [p for p in dedup(preferences) if score_extraction(p, 'preference') >= 1]

# ═══════════════════════════════════════════════════════════════
# BUILD FULL SUMMARY
# ═══════════════════════════════════════════════════════════════

now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
lines = []
lines.append(f"# Session Summary: {project_name}")
lines.append(f"- **Date**: {now}")
lines.append(f"- **Directory**: {cwd}")
lines.append(f"- **Setup**: {setup_context}")
lines.append(f"- **Session**: {session_id[:12]}")
lines.append(f"- **User messages**: {len(user_messages)}")
lines.append(f"- **Tools used**: {', '.join(sorted(tools_used)[:15]) if tools_used else 'none'}")
lines.append(f"- **Memory ops**: {memory_stores} stores, {memory_searches} searches")
lines.append(f"- **Scope tags**: proj:{project_name}, user:{setup_context}")
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

# ═══════════════════════════════════════════════════════════════
# EXECUTIVE SUMMARY — compact 5-line version for next session
# ═══════════════════════════════════════════════════════════════

exec_lines = []
exec_lines.append(f"[{now}] {project_name} ({setup_context})")

# What user asked (first 2)
if user_messages:
    for msg in user_messages[:2]:
        exec_lines.append(f"  Asked: {msg[:100]}")

# Key outcomes
if decisions:
    exec_lines.append(f"  Decision: {decisions[0][:120]}")
if errors_fixes:
    exec_lines.append(f"  Fixed: {errors_fixes[0][:120]}")
if learnings:
    exec_lines.append(f"  Learned: {learnings[0][:120]}")

# Files (count only)
if files_modified:
    exec_lines.append(f"  Modified {len(files_modified)} files")

executive_summary = '\n'.join(exec_lines[:6])

# ═══════════════════════════════════════════════════════════════
# WRITE OUTPUT FILES
# ═══════════════════════════════════════════════════════════════

# Full project-specific summary
summary_file = os.path.join(summary_dir, f"{project_name}-latest.md")
with open(summary_file, 'w') as f:
    f.write(summary_text)

# Global latest (most recent session, any project)
global_file = os.path.join(summary_dir, "global-latest.md")
with open(global_file, 'w') as f:
    f.write(summary_text)

# Executive summary (compact — for fast SessionStart loading)
exec_file = os.path.join(summary_dir, f"{project_name}-exec.md")
with open(exec_file, 'w') as f:
    f.write(executive_summary)

global_exec_file = os.path.join(summary_dir, "global-exec.md")
with open(global_exec_file, 'w') as f:
    f.write(executive_summary)

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

# ═══════════════════════════════════════════════════════════════
# STORE SESSION SUMMARY TO MCP MEMORY (v5 addition)
# Uses venv Python with SentenceTransformer + sqlite-vec
# Only stores sessions with meaningful content (decisions/errors/learnings)
# ═══════════════════════════════════════════════════════════════

SUMMARY_FILE="$SUMMARY_DIR/${PROJECT_NAME}-latest.md"
VENV_PYTHON="$HOME/.local/pipx/venvs/mcp-memory-service/bin/python3"

if [ -f "$SUMMARY_FILE" ] && [ -x "$VENV_PYTHON" ]; then
  $VENV_PYTHON - "$SUMMARY_FILE" "$PROJECT_NAME" "$SETUP_CONTEXT" "$SESSION_ID" 2>/dev/null << 'MEMPYEOF'
import sys, os, json, hashlib, sqlite3, warnings
warnings.filterwarnings('ignore')
os.environ['TOKENIZERS_PARALLELISM'] = 'false'

summary_file = sys.argv[1]
project_name = sys.argv[2]
setup_context = sys.argv[3]
session_id = sys.argv[4]

with open(summary_file, 'r') as f:
    content = f.read().strip()

# Skip trivial sessions (too short or no insights)
if len(content) < 100:
    sys.exit(0)

INSIGHT_SECTIONS = ['## Decisions Made', '## Errors & Fixes', '## Key Learnings', '## User Preferences Observed']
has_insights = any(s in content for s in INSIGHT_SECTIONS)
if not has_insights:
    sys.exit(0)

content_hash = hashlib.sha256(content.encode()).hexdigest()
DB_PATH = os.path.expanduser("~/Library/Application Support/mcp-memory/sqlite_vec.db")
if not os.path.exists(DB_PATH):
    sys.exit(0)

try:
    import sqlite_vec
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)

    # Skip if already stored (duplicate hash)
    if conn.execute("SELECT 1 FROM memories WHERE content_hash = ?", (content_hash,)).fetchone():
        conn.close()
        sys.exit(0)

    from sentence_transformers import SentenceTransformer
    import numpy as np
    from datetime import datetime, timezone

    model_name = os.environ.get('MCP_EMBEDDING_MODEL', 'paraphrase-multilingual-MiniLM-L12-v2')
    model = SentenceTransformer(model_name, device='cpu')
    embedding = model.encode([content], convert_to_numpy=True)[0]
    embedding_bytes = embedding.astype(np.float32).tobytes()

    now = datetime.now(timezone.utc)
    tags = f"proj:{project_name},user:{setup_context},session-summary,{now.strftime('%Y-%m')}"

    # Importance scoring based on content richness
    importance = 1.0
    for section in INSIGHT_SECTIONS:
        if section in content:
            importance += 0.25
    importance = min(importance, 2.0)

    metadata = json.dumps({
        "project": project_name,
        "setup": setup_context,
        "scope": "project",
        "type": "session-summary",
        "session_id": session_id[:12],
        "importance_score": importance
    })

    conn.execute("""
        INSERT INTO memories (content, content_hash, tags, memory_type, metadata,
                              created_at, updated_at, created_at_iso, updated_at_iso)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (content, content_hash, tags, 'session_summary', metadata,
          now.timestamp(), now.timestamp(), now.isoformat(), now.isoformat()))

    row_id = conn.execute("SELECT id FROM memories WHERE content_hash = ?", (content_hash,)).fetchone()[0]

    conn.execute("""
        INSERT INTO memory_embeddings (rowid, content_embedding)
        VALUES (?, ?)
    """, (row_id, embedding_bytes))

    conn.commit()
    conn.close()

except Exception:
    pass  # Fail silently — session logging is more important than memory storage

MEMPYEOF
fi

# Log session end
echo "{\"session\":\"$SESSION_ID\",\"project\":\"$PROJECT_NAME\",\"setup\":\"$SETUP_CONTEXT\",\"reason\":\"$REASON\",\"time\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}" >> "$LOG_DIR/sessions.jsonl"

# Keep log reasonable
if [ -f "$LOG_DIR/sessions.jsonl" ]; then
  LINE_COUNT=$(wc -l < "$LOG_DIR/sessions.jsonl")
  if [ "$LINE_COUNT" -gt 1000 ]; then
    tail -500 "$LOG_DIR/sessions.jsonl" > "$LOG_DIR/sessions.jsonl.tmp"
    mv "$LOG_DIR/sessions.jsonl.tmp" "$LOG_DIR/sessions.jsonl"
  fi
fi

exit 0
