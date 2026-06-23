#!/bin/bash
# B12 Memory System - PreCompact Hook (v2 — Priority-Weighted Extraction)
# Extracts and scores content before compaction, keeps only high-value items
# Fires on: auto, manual
#
# v2 changes:
# - Priority-weighted scoring (decisions > learnings > errors > files > general)
# - Token budget: keeps only top items within ~2000 tokens
# - Setup/scope context preserved through compaction

set -o pipefail 2>/dev/null || true

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
B12_BASE="${B12_DATA_DIR:-$HOME/.B12}"

PROJECT_NAME=$(basename "$CWD" 2>/dev/null || echo "unknown")
STAGING_DIR="$B12_BASE/memory-staging"
mkdir -p "$STAGING_DIR"

if [ -f "$TRANSCRIPT_PATH" ]; then
  python3 - "$TRANSCRIPT_PATH" "$PROJECT_NAME" "$SESSION_ID" "$STAGING_DIR" "$CWD" << 'PYEOF'
import sys, json, os, re

# Import shared patterns (DRY — same patterns used in session-end.sh)
# B12_HOOK_DIR controls code location; B12_DATA_DIR controls data only
_hook_dir = os.environ.get('B12_HOOK_DIR', os.path.expanduser('~/.B12/hooks'))
sys.path.insert(0, os.path.join(_hook_dir, 'scripts'))
from shared_patterns import (DECISION_RE, ERROR_RE, LEARNING_RE, PREFERENCE_RE,
                             summary_filter)

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

                                # Layer 0: Skip session summary recitations (v12.2)
                                if summary_filter(text[:2000]):
                                    continue

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
    log_dir = os.path.join(os.environ.get('B12_DATA_DIR', os.path.expanduser('~/.B12')), 'memory-logs')
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

# Scrub secrets BEFORE the staging file persists them — precompact-*.txt is
# re-injected on the next SessionStart(compact), so a raw pasted secret here
# would otherwise resurface in context. (scripts/ already on sys.path above.)
try:
    from b12_pii_scrubber import scrub as _pii_scrub
    summary = _pii_scrub(summary)
except ImportError:
    pass

# Write staging file
stage_file = os.path.join(staging_dir, f"precompact-{session_id}.txt")
with open(stage_file, 'w') as f:
    f.write(summary)

PYEOF
fi

# ═══════════════════════════════════════════════════════════════
# DIRECT SQLITE STORE — high-value items survive compaction (v12)
# Only stores items with priority >= 8 (decision, error_fix, learning, preference)
# Uses embed daemon for embeddings; gracefully skips if unavailable.
# ═══════════════════════════════════════════════════════════════

VENV_PYTHON="$HOME/.local/b12-venv/bin/python3"
_UID=$(id -u 2>/dev/null || echo $$)
EMBED_SOCK="/tmp/b12-embed-${_UID}.sock"

if [ -f "$STAGING_DIR/precompact-${SESSION_ID}.txt" ] && [ -x "$VENV_PYTHON" ]; then
  "$VENV_PYTHON" - "$STAGING_DIR/precompact-${SESSION_ID}.txt" "$PROJECT_NAME" "$CWD" "$EMBED_SOCK" << 'PCPYEOF'
import sys, os, json, hashlib, sqlite3, socket as _sock, base64

staging_file = sys.argv[1]
project_name = sys.argv[2]
cwd = sys.argv[3]
daemon_sock = sys.argv[4]

# Read staging file and extract high-value items (priority >= 8)
high_value = []
try:
    with open(staging_file, 'r') as f:
        in_work = False
        for line in f:
            line = line.rstrip('\n')
            if line.startswith('RECENT WORK:'):
                in_work = True
                continue
            if in_work and line.startswith('  ['):
                # Parse: "  [category] text"
                bracket_end = line.index(']', 3)
                category = line[3:bracket_end]
                text = line[bracket_end+2:]
                # Priority mapping (must match precompact scoring)
                priority_map = {'decision': 10, 'error_fix': 9, 'learning': 8, 'preference': 8}
                priority = priority_map.get(category, 0)
                if priority >= 8 and len(text) > 30:
                    high_value.append((category, text))
            elif in_work and not line.startswith('  '):
                in_work = False
except Exception:
    sys.exit(0)

if not high_value:
    sys.exit(0)

# Limit to 5 items
high_value = high_value[:5]

# Platform-aware DB path
home = os.path.expanduser("~")
if sys.platform == "darwin":
    db_path = os.path.join(home, "Library", "Application Support", "mcp-memory", "sqlite_vec.db")
elif sys.platform == "win32":
    db_path = os.path.join(home, "AppData", "Local", "mcp-memory", "sqlite_vec.db")
else:
    db_path = os.path.join(home, ".local", "share", "mcp-memory", "sqlite_vec.db")

if not os.path.exists(db_path):
    sys.exit(0)

# Try daemon for embeddings
def daemon_encode(texts):
    try:
        if not os.path.exists(daemon_sock):
            return None
        s = _sock.socket(_sock.AF_UNIX, _sock.SOCK_STREAM)
        s.settimeout(10)
        s.connect(daemon_sock)
        req = json.dumps({'op': 'encode_batch', 'texts': texts}) + '\n'
        s.sendall(req.encode())
        data = b''
        while True:
            chunk = s.recv(1048576)
            if not chunk:
                break
            data += chunk
            if b'\n' in data:
                break
        s.close()
        resp = json.loads(data.decode().strip())
        if resp.get('ok'):
            return [base64.b64decode(e) for e in resp['embeddings']]
    except Exception:
        pass
    return None

from datetime import datetime, timezone
now = datetime.now(timezone.utc)

# PII / secret scrub before hash+embed+store (parity with write_time_merge & session-end).
# Honors B12_DISABLE_PII_SCRUB=1 (handled inside scrub()).
_hook_dir = os.environ.get('B12_HOOK_DIR', os.path.expanduser('~/.B12/hooks'))
sys.path.insert(0, os.path.join(_hook_dir, 'scripts'))
try:
    from b12_pii_scrubber import scrub as _pii_scrub
except ImportError:
    def _pii_scrub(_s):
        return _s
texts = [f"[{cat}] {_pii_scrub(text)}" for cat, text in high_value]

# Get embeddings (skip entirely if daemon unavailable)
embeddings = daemon_encode(texts)
if embeddings is None:
    sys.exit(0)  # No daemon = skip permanent store, staging file still exists

try:
    conn = sqlite3.connect(db_path, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")

    # Load sqlite_vec for embedding storage
    try:
        import sqlite_vec
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
    except Exception:
        conn.close()
        sys.exit(0)

    stored = 0
    for i, (category, text) in enumerate(high_value):
        prefixed = texts[i]
        m_hash = hashlib.sha256(prefixed.strip().lower().encode('utf-8')).hexdigest()

        # Skip duplicates
        if conn.execute("SELECT 1 FROM memories WHERE content_hash = ?", (m_hash,)).fetchone():
            continue

        m_tags = f"proj:{project_name},precompact-save,{category},{now.strftime('%Y-%m')}"
        # Single chokepoint: PreCompact only persists items that survived the
        # priority filter, so 0.70 is passed as the supplied floor. finalize_importance
        # caps a credential-bearing line at baseline (overriding the floor, never
        # amplified) and otherwise returns the strongest of {content score,
        # memory_type floor, 0.70}.
        try:
            import b12_importance as _b12imp
            imp_val = _b12imp.finalize_importance(prefixed, supplied=0.70, memory_type=category)
        except Exception:
            imp_val = 1.5   # prior behavior if the scorer is unavailable
        m_metadata = json.dumps({
            "project": project_name,
            "type": category,
            "importance_score": imp_val,
            "source": "precompact",
            "extraction_method": "precompact_v12"
        })

        conn.execute("""
            INSERT INTO memories (content, content_hash, tags, memory_type, metadata,
                                  created_at, updated_at, created_at_iso, updated_at_iso)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (prefixed, m_hash, m_tags, category, m_metadata,
              now.timestamp(), now.timestamp(), now.isoformat(), now.isoformat()))

        row_id = conn.execute("SELECT id FROM memories WHERE content_hash = ?", (m_hash,)).fetchone()
        if row_id and i < len(embeddings):
            try:
                conn.execute("INSERT INTO memory_embeddings (rowid, content_embedding) VALUES (?, ?)",
                             (row_id[0], embeddings[i]))
            except Exception:
                pass
        stored += 1

    if stored > 0:
        conn.commit()
    conn.close()
except Exception:
    pass  # Never block compaction

PCPYEOF
fi

# Clean up old staging files (older than 2 hours)
find "$STAGING_DIR" -name "precompact-*.txt" -mmin +120 -delete 2>/dev/null

exit 0
