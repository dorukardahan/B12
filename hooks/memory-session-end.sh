#!/bin/bash
# B12 Memory System - SessionEnd Hook (v5 — MCP Store + Thinking Filter)
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

# ── Self-timeout watchdog ─────────────────────────────────────
# Kills this script if it exceeds max runtime. Prevents orphan processes.
( sleep 30 && kill -TERM $$ 2>/dev/null ) &
_WATCHDOG=$!
trap "kill $_WATCHDOG 2>/dev/null; wait $_WATCHDOG 2>/dev/null" EXIT

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

# Setup detection (set B12_WORK_PATTERN env var to match your work dirs)
_WORK_PAT="${B12_WORK_PATTERN:-}"
if [ -n "$_WORK_PAT" ] && { [[ "$B12_BASE" == *"$_WORK_PAT"* ]] || [[ "$CWD" == *"/$_WORK_PAT"* ]] || [[ "$CWD" == *"/${_WORK_PAT,,}"* ]]; }; then
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

# Import shared patterns (DRY — same patterns used in precompact.sh)
# Note: sys.argv[0] is "-" in heredocs, so use B12_DATA_DIR / known path
_b12_base = os.environ.get('B12_DATA_DIR', os.path.expanduser('~/.claude'))
sys.path.insert(0, os.path.join(_b12_base, 'hooks', 'scripts'))
from shared_patterns import DECISION_RE, ERROR_RE, LEARNING_RE, PREFERENCE_RE

transcript_path = sys.argv[1]
project_name = sys.argv[2]
session_id = sys.argv[3]
summary_dir = sys.argv[4]
cwd = sys.argv[5]
setup_context = sys.argv[6]

# Guard against empty setup_context (causes "user:," in tags)
if not setup_context or setup_context.strip() == '':
    setup_context = 'personal'

user_messages = []
assistant_messages = []
tools_used = set()
files_modified = set()
memory_stores = 0
memory_searches = 0

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
                                    # Strip thinking blocks before processing
                                    text = re.sub(r'<(?:antml:)?thinking>.*?</(?:antml:)?thinking>', '', text, flags=re.DOTALL).strip()
                                    if not text:
                                        continue
                                    assistant_messages.append(text[:500])

                                    # Pattern matching on full assistant text (scan up to 2000 chars)
                                    scan_text = text[:2000]
                                    if DECISION_RE.search(scan_text):
                                        # Extract context around the match
                                        m = DECISION_RE.search(scan_text)
                                        start = max(0, m.start() - 50)
                                        decisions.append(scan_text[start:start+250])
                                    if ERROR_RE.search(scan_text):
                                        m = ERROR_RE.search(scan_text)
                                        start = max(0, m.start() - 50)
                                        errors_fixes.append(scan_text[start:start+250])
                                    if PREFERENCE_RE.search(scan_text):
                                        m = PREFERENCE_RE.search(scan_text)
                                        start = max(0, m.start() - 50)
                                        preferences.append(scan_text[start:start+250])
                                    if LEARNING_RE.search(scan_text):
                                        m = LEARNING_RE.search(scan_text)
                                        start = max(0, m.start() - 50)
                                        learnings.append(scan_text[start:start+250])

                                elif block.get('type') == 'tool_use':
                                    tool_name = block.get('name', '')
                                    tools_used.add(tool_name)
                                    inp = block.get('input', {})
                                    if tool_name in ('Edit', 'Write') and 'file_path' in inp:
                                        files_modified.add(inp['file_path'])
                                    if tool_name in ('mcp__memory__store_memory', 'mcp__memory__memory_store'):
                                        memory_stores += 1
                                    elif tool_name in ('mcp__memory__retrieve_memory', 'mcp__memory__memory_search'):
                                        memory_searches += 1

                # User preference detection
                if msg_type == 'human':
                    content = obj.get('message', {}).get('content', '')
                    text = content if isinstance(content, str) else ''
                    if isinstance(content, list):
                        text = ' '.join(b.get('text', '') for b in content if isinstance(b, dict))
                    if text and PREFERENCE_RE.search(text[:1000]):
                        m = PREFERENCE_RE.search(text[:1000])
                        start = max(0, m.start() - 30)
                        preferences.append(f"[user] {text[start:start+250]}")

            except (json.JSONDecodeError, KeyError, TypeError):
                continue
except Exception as e:
    import traceback
    sys.stderr.write(f"SessionEnd transcript parse error: {e}\n{traceback.format_exc()}\n")

# ═══════════════════════════════════════════════════════════════
# v4 SCORING FILTER — quality gate for extracted items
# ═══════════════════════════════════════════════════════════════

def score_extraction(text, category):
    """Score how likely a text snippet is to be a genuine extraction."""
    score = 0
    text_lower = text.lower()

    if category == 'decision':
        if any(w in text_lower for w in ['instead of', 'over', 'rather than', 'because', 'tradeoff',
                                          'yerine', 'çünkü', 'sebebiyle', 'nedeniyle']):
            score += 2
        if any(w in text_lower for w in ['chose', 'decided', 'selected', 'opted',
                                          'karar', 'seçtik', 'tercih', 'gidelim']):
            score += 1

    elif category == 'error':
        has_problem = any(w in text_lower for w in ['error', 'bug', 'crash', 'fail', 'broke',
                                                     'hata', 'sorun', 'çöktü', 'bozuldu'])
        has_resolution = any(w in text_lower for w in ['fixed', 'resolved', 'solved', 'workaround', 'caused by', 'root cause',
                                                        'düzelttik', 'çözdük', 'giderdik', 'sebebi', 'nedeni'])
        if has_problem and has_resolution:
            score += 3
        elif has_problem:
            score += 0  # Problem without resolution = not useful

    elif category == 'learning':
        if any(w in text_lower for w in ['turns out', 'gotcha', 'pitfall', 'caveat', 'important to note',
                                          'meğer', 'meğerse', 'anlaşılan', 'dikkat', 'önemli']):
            score += 2
        if any(w in text_lower for w in ['because', 'so that', 'çünkü', 'dolayı']):
            score += 1

    elif category == 'preference':
        if any(w in text_lower for w in ['always', 'never', 'prefer', 'convention',
                                          'her zaman', 'asla', 'hiçbir zaman', 'tercih']):
            score += 1
        if any(w in text_lower for w in ['user', '[user]', 'kullanıcı']):
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
errors_fixes = [e for e in dedup(errors_fixes) if score_extraction(e, 'error') >= 1]
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

# What was done (always populated if files were modified or tools used)
key_actions = []
if files_modified:
    key_actions.append(f"Modified {len(files_modified)} files")
if tools_used:
    key_tools = [t for t in sorted(tools_used) if t not in ('Read', 'Glob', 'Grep', 'LS')][:8]
    if key_tools:
        key_actions.append(f"Used: {', '.join(key_tools)}")
if memory_stores > 0:
    key_actions.append(f"Stored {memory_stores} memories")
if key_actions:
    lines.append("## What Was Done")
    for a in key_actions:
        lines.append(f"- {a}")
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
separator = "\n\n<!-- SESSION_BREAK -->\n\n"

existing = ""
if os.path.exists(history_file):
    with open(history_file, 'r') as f:
        existing = f.read()

# Support both old (---) and new separators for backwards compat
if "<!-- SESSION_BREAK -->" in existing:
    sessions = existing.split("<!-- SESSION_BREAK -->")
else:
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
  # Clean up any orphaned embed scripts from previous sessions
  find "$LOG_DIR" -name "embed-*.py" -mmin +30 -delete 2>/dev/null

  # Write embed script to LOG_DIR (not /tmp/ — system cleanup could race)
  # Avoids 9.4s blocking (import: 4.9s + model load: 4.5s + encode: 0.01s)
  EMBED_SCRIPT="$LOG_DIR/embed-${SESSION_ID}.py"
  cat > "$EMBED_SCRIPT" << 'MEMPYEOF'
import sys, os, json, hashlib, sqlite3, warnings, socket as _sock, base64
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

# Store any non-trivial session (removed strict has_insights gate)
# Sessions with structured sections get higher importance
INSIGHT_SECTIONS = ['## Decisions Made', '## Errors & Fixes', '## Key Learnings',
                    '## User Preferences Observed', '## What Was Done']
has_insights = any(s in content for s in INSIGHT_SECTIONS)

content_hash = hashlib.sha256(content.encode()).hexdigest()
DB_PATH = os.path.expanduser("~/Library/Application Support/mcp-memory/sqlite_vec.db")
if not os.path.exists(DB_PATH):
    sys.exit(0)

# ─── Daemon-first embedding (Phase 1) ────────────────────────
# Try embedding daemon before loading SentenceTransformer (~4.5s savings)
_DAEMON_SOCK = f"/tmp/b12-embed-{os.getuid()}.sock"
_DAEMON_PID = f"/tmp/b12-embed-{os.getuid()}.pid"
_USE_DAEMON = False

if os.path.exists(_DAEMON_SOCK) and os.path.exists(_DAEMON_PID):
    try:
        _pid = int(open(_DAEMON_PID).read().strip())
        os.kill(_pid, 0)  # Check if process is alive
        _USE_DAEMON = True
    except (ValueError, ProcessLookupError, PermissionError, FileNotFoundError):
        pass

def _daemon_encode(texts):
    """Encode texts via daemon socket, return list of float32 bytes or None."""
    try:
        s = _sock.socket(_sock.AF_UNIX, _sock.SOCK_STREAM)
        s.settimeout(15)  # generous timeout for batch encoding
        s.connect(_DAEMON_SOCK)
        req = json.dumps({'op': 'encode_batch', 'texts': texts}) + '\n'
        s.sendall(req.encode())
        data = b''
        while True:
            chunk = s.recv(1048576)  # 1MB buffer
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

# Embedding helper: daemon-first with model fallback
_model = [None]  # Lazy-loaded

def encode_texts(texts):
    """Return list of float32 bytes for each text."""
    import numpy as np
    if _USE_DAEMON:
        result = _daemon_encode(texts)
        if result:
            return result
    # Cold fallback: load model locally
    if _model[0] is None:
        from sentence_transformers import SentenceTransformer
        model_name = os.environ.get('MCP_EMBEDDING_MODEL', 'paraphrase-multilingual-MiniLM-L12-v2')
        _model[0] = SentenceTransformer(model_name, device='cpu')
    embeddings = _model[0].encode(texts, convert_to_numpy=True)
    return [emb.astype(np.float32).tobytes() for emb in embeddings]

try:
    import sqlite_vec
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)

    # Skip if already stored (duplicate hash)
    if conn.execute("SELECT 1 FROM memories WHERE content_hash = ?", (content_hash,)).fetchone():
        conn.close()
        sys.exit(0)

    from datetime import datetime, timezone
    import numpy as np

    embedding_bytes = encode_texts([content])[0]

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

    # Link to previous session summary for this project (temporal chain)
    prev = conn.execute("""
        SELECT content_hash FROM memories
        WHERE memory_type = 'session_summary'
          AND tags LIKE ?
          AND content_hash != ?
        ORDER BY created_at DESC LIMIT 1
    """, (f'%proj:{project_name}%', content_hash)).fetchone()

    if prev:
        conn.execute("""
            INSERT OR IGNORE INTO memory_graph
            (source_hash, target_hash, similarity, connection_types, metadata, created_at, relationship_type)
            VALUES (?, ?, 0.5, '["temporal","session_sequence"]', ?, ?, 'follows')
        """, (content_hash, prev[0], json.dumps({"project": project_name}), now.timestamp()))

    # ─── Micro-memory extraction (v5.2) ──────────────────────
    # Store individual decisions/errors/learnings as separate searchable memories
    import re as _re
    MICRO_SECTIONS = {
        '## Decisions Made': ('decision', 1.5),
        '## Decisions': ('decision', 1.5),
        '## Errors & Fixes': ('error_fix', 1.2),
        '## Problems Fixed': ('error_fix', 1.2),
        '## Bugs Fixed': ('error_fix', 1.2),
        '## Key Learnings': ('learning', 1.0),
        '## Learned': ('learning', 1.0),
        '## Takeaways': ('learning', 1.0),
        '## What Was Done': ('progress', 0.8),
    }
    micro_texts = []
    micro_meta = []

    for section_hdr, (mem_type, imp) in MICRO_SECTIONS.items():
        if section_hdr not in content:
            continue
        # Extract bullets from section
        match = _re.search(
            rf'^{_re.escape(section_hdr)}\n(.*?)(?=\n## |\Z)',
            content, _re.MULTILINE | _re.DOTALL
        )
        if not match:
            continue
        raw_bullets = match.group(1).strip().split('\n')
        bullets = []
        for b in raw_bullets:
            b = b.strip()
            if not b.startswith('- '):
                continue
            # Strip markdown bold prefix: "- **label**: text" → "label: text"
            clean = _re.sub(r'^\*\*(.+?)\*\*:\s*', r'\1: ', b.lstrip('- '))
            if not clean:
                clean = b.lstrip('- ')
            if len(clean) > 15:
                bullets.append(clean)

        # Quality filter: reject conversation fragments, keep actionable knowledge
        def is_actionable(text):
            tl = text.lower().strip()
            # Skip conversation fragments and formatting
            SKIP = ['tamam', 'ok,', 'evet', 'hayır', 'anladım', 'güzel', 'merhaba',
                    'hey ', 'hi ', 'şimdi', 'peki', 'hadi', '`★', '─────',
                    'tüm ', 'bekliyoruz', 'haklısın', 'seni duyuyorum']
            if any(tl.startswith(s.lower()) for s in SKIP):
                return False
            if tl.startswith('|') or tl.startswith('#'):
                return False
            # Minimum word count — very short items are usually noise
            if len(tl.split()) < 5:
                return False
            # Require technical/actionable signals
            # Removed overly generic: 'used', 'stored', 'created', 'modified',
            # 'deleted', 'added', 'removed', 'updated' (match noise like
            # "[Progress] Used: Bash, Edit..." and "Stored 9 memories")
            SIGNALS = ['file', 'error', 'bug', 'fix', 'api', 'config', 'hook',
                       'script', 'sql', 'python', 'bash', 'git', 'docker',
                       '.py', '.sh', '.json', '.md', 'command', 'function',
                       'import', 'install', 'deploy', 'test', 'memory', 'tag',
                       'embed', 'query', 'switched', 'decided', 'chose',
                       'configured', 'migrat', 'resolved']
            return any(sig in tl for sig in SIGNALS)

        prefix_map = {'decision': 'Decision', 'error_fix': 'Error Fix',
                      'learning': 'Learning', 'progress': 'Progress'}
        for bullet in bullets[:5]:
            if not is_actionable(bullet):
                continue
            prefixed = f"[{prefix_map[mem_type]}] {bullet}"
            micro_texts.append(prefixed)
            micro_meta.append((mem_type, imp))

    if micro_texts:
        # Try to import write-time merge (graceful degradation if unavailable)
        _USE_MERGE = False
        try:
            sys.path.insert(0, os.path.expanduser("~/.claude/hooks/scripts"))
            from write_time_merge import merge_or_insert
            _USE_MERGE = True
        except ImportError:
            pass

        micro_emb_bytes = encode_texts(micro_texts)
        for i, text in enumerate(micro_texts):
            mem_type, imp = micro_meta[i]
            m_hash = hashlib.sha256(text.encode()).hexdigest()
            m_tags = f"proj:{project_name},user:{setup_context},{mem_type},{now.strftime('%Y-%m')}"
            m_metadata = json.dumps({
                "project": project_name, "setup": setup_context,
                "scope": "project", "type": mem_type,
                "source_session": session_id[:12],
                "importance_score": imp
            })
            m_emb = micro_emb_bytes[i]

            if _USE_MERGE:
                result = merge_or_insert(
                    conn, content=text, content_hash=m_hash,
                    tags=m_tags, memory_type=mem_type,
                    metadata=m_metadata, embedding_bytes=m_emb, now=now,
                    db_path=DB_PATH
                )
                # result.action: "inserted", "merged", or "noop_duplicate"
                # Log contradictions as graph edges (Phase 2)
                if hasattr(result, 'contradictions') and result.contradictions:
                    for c in result.contradictions:
                        conn.execute("""
                            INSERT OR IGNORE INTO memory_graph
                            (source_hash, target_hash, similarity, connection_types,
                             metadata, created_at, relationship_type)
                            VALUES (?, ?, ?, '["nli","store_time"]', ?, ?, 'contradicts')
                        """, (m_hash, c['hash'], c['score'],
                              json.dumps({"detected_by": "session_end_nli",
                                          "snippet": c.get('snippet', '')[:60]}),
                              now.timestamp()))
            else:
                # Fallback: direct INSERT (original behavior)
                if conn.execute("SELECT 1 FROM memories WHERE content_hash = ?", (m_hash,)).fetchone():
                    continue
                conn.execute("""
                    INSERT INTO memories (content, content_hash, tags, memory_type, metadata,
                                          created_at, updated_at, created_at_iso, updated_at_iso)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (text, m_hash, m_tags, mem_type, m_metadata,
                      now.timestamp(), now.timestamp(), now.isoformat(), now.isoformat()))
                m_row = conn.execute("SELECT id FROM memories WHERE content_hash = ?", (m_hash,)).fetchone()[0]
                conn.execute("INSERT INTO memory_embeddings (rowid, content_embedding) VALUES (?, ?)",
                             (m_row, m_emb))

    conn.commit()
    conn.close()

except Exception as e:
    # Log error instead of silent failure
    import traceback
    log_dir = os.path.expanduser("~/.claude/memory-logs")
    os.makedirs(log_dir, exist_ok=True)
    with open(os.path.join(log_dir, "memory-errors.log"), 'a') as ef:
        ef.write(f"[{__import__('datetime').datetime.now().isoformat()}] SessionEnd embed error: {e}\n")
        ef.write(traceback.format_exc() + "\n")

MEMPYEOF
  # Launch in background: subshell runs Python then cleans up temp file + daemon
  (
    "$VENV_PYTHON" "$EMBED_SCRIPT" "$SUMMARY_FILE" "$PROJECT_NAME" "$SETUP_CONTEXT" "$SESSION_ID"
    rm -f "$EMBED_SCRIPT"
    # Shut down embedding daemon (session is ending, no more requests expected)
    _UID=$(id -u)
    _SOCK="/tmp/b12-embed-${_UID}.sock"
    [ -S "$_SOCK" ] && printf '{"op":"shutdown"}\n' | nc -U "$_SOCK" -w 2 2>/dev/null
  ) > /dev/null 2>&1 &
  disown 2>/dev/null
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
