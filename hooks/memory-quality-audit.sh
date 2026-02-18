#!/bin/bash
# B12 Memory System - Weekly Quality Audit
# Evaluates all memories for quality, flags low-quality entries
# Runs via launchd: com.b12.memory-quality-audit (Wednesday 3:00 AM)
#
# Flags:
#   --quiet     Suppress console output (for launchd)
#   --fix       Auto-remediate: clean stale embeddings, regen orphans,
#               flag short memories, clean orphaned graph edges
#
# Output: ~/.claude/memory-logs/quality-audit-{date}.md

set -e

QUIET=false
FIX=false
for arg in "$@"; do
  case "$arg" in
    --quiet) QUIET=true ;;
    --fix)   FIX=true ;;
  esac
done

VENV_PYTHON="$HOME/.local/pipx/venvs/mcp-memory-service/bin/python3"
LOG_DIR="$HOME/.claude/memory-logs"
mkdir -p "$LOG_DIR"

DATE=$(date -u +%Y-%m-%d)
REPORT_FILE="$LOG_DIR/quality-audit-${DATE}.md"

if [ ! -x "$VENV_PYTHON" ]; then
  [ "$QUIET" = false ] && echo "ERROR: venv Python not found at $VENV_PYTHON" >&2
  exit 1
fi

$VENV_PYTHON - "$REPORT_FILE" "$QUIET" "$FIX" << 'PYEOF'
import sys, os, json, sqlite3, warnings, socket as _sock, base64
warnings.filterwarnings('ignore')

report_file = sys.argv[1]
quiet = sys.argv[2] == "true" if len(sys.argv) > 2 else False
fix_mode = sys.argv[3] == "true" if len(sys.argv) > 3 else False
DB_PATH = os.path.expanduser("~/Library/Application Support/mcp-memory/sqlite_vec.db")

if not os.path.exists(DB_PATH):
    print("ERROR: Database not found")
    sys.exit(1)

try:
    import sqlite_vec
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
except Exception as e:
    print(f"ERROR: Cannot connect to DB: {e}")
    sys.exit(1)

from datetime import datetime, timezone
from collections import Counter

now = datetime.now(timezone.utc)
lines = [f"# Memory Quality Audit — {now.strftime('%Y-%m-%d %H:%M UTC')}\n"]
if fix_mode:
    lines.append("**Mode: Auto-remediation enabled (--fix)**\n")

# ── Daemon helper for orphan embedding regeneration ──────────
_DAEMON_SOCK = f"/tmp/b12-embed-{os.getuid()}.sock"
_DAEMON_PID = f"/tmp/b12-embed-{os.getuid()}.pid"

def daemon_alive():
    if not os.path.exists(_DAEMON_SOCK) or not os.path.exists(_DAEMON_PID):
        return False
    try:
        pid = int(open(_DAEMON_PID).read().strip())
        os.kill(pid, 0)
        return True
    except (ValueError, ProcessLookupError, PermissionError, FileNotFoundError):
        return False

def daemon_encode(texts):
    """Encode texts via daemon, return list of bytes or None."""
    try:
        s = _sock.socket(_sock.AF_UNIX, _sock.SOCK_STREAM)
        s.settimeout(30)
        s.connect(_DAEMON_SOCK)
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

fix_actions = []  # Track what --fix did

# Overall stats
total = conn.execute("SELECT COUNT(*) FROM memories WHERE deleted_at IS NULL").fetchone()[0]
deleted = conn.execute("SELECT COUNT(*) FROM memories WHERE deleted_at IS NOT NULL").fetchone()[0]
embeddings = conn.execute("SELECT COUNT(*) FROM memory_embeddings").fetchone()[0]
graph_edges = conn.execute("SELECT COUNT(*) FROM memory_graph").fetchone()[0]

lines.append(f"## Overview")
lines.append(f"- **Active memories**: {total}")
lines.append(f"- **Deleted (tombstones)**: {deleted}")
lines.append(f"- **Embeddings**: {embeddings}")
lines.append(f"- **Graph edges**: {graph_edges}")
lines.append(f"- **Embedding coverage**: {embeddings/total*100:.0f}%" if total > 0 else "- **Embedding coverage**: N/A")
lines.append("")

# Memory type distribution
types = conn.execute("SELECT memory_type, COUNT(*) FROM memories WHERE deleted_at IS NULL GROUP BY memory_type ORDER BY COUNT(*) DESC").fetchall()
lines.append("## Type Distribution")
for t, c in types:
    lines.append(f"- {t}: {c}")
lines.append("")

# Project distribution
rows = conn.execute("SELECT tags FROM memories WHERE deleted_at IS NULL").fetchall()
projects = Counter()
for (tags,) in rows:
    if not tags:
        continue
    for tag in tags.split(','):
        tag = tag.strip()
        if tag.startswith('proj:'):
            projects[tag[5:]] += 1
lines.append("## Project Distribution")
for proj, c in projects.most_common(20):
    lines.append(f"- {proj}: {c}")
lines.append("")

# Quality checks
lines.append("## Quality Issues")
issues = []

# ── Check 1: Stale embeddings (for deleted/nonexistent memories) ──
stale_embeddings = conn.execute("""
    SELECT COUNT(*) FROM memory_embeddings
    WHERE rowid NOT IN (SELECT id FROM memories WHERE deleted_at IS NULL)
""").fetchone()[0]
if stale_embeddings > 0:
    issues.append(f"### Stale embeddings (orphaned): {stale_embeddings}")
    if fix_mode:
        conn.execute("""
            DELETE FROM memory_embeddings
            WHERE rowid IN (
                SELECT m.id FROM memories m
                WHERE m.deleted_at IS NOT NULL
                  AND m.deleted_at < unixepoch('now') - 604800
            )
            OR rowid NOT IN (SELECT id FROM memories)
        """)
        conn.commit()
        fix_actions.append(f"Deleted {stale_embeddings} stale embeddings")
        issues.append(f"  - **FIXED**: Deleted {stale_embeddings} stale embeddings")
    issues.append("")

# ── Check 2: Memories without embeddings (orphans) ──
orphan_memories = conn.execute("""
    SELECT m.id, m.content_hash, m.memory_type, substr(m.content, 1, 60), m.content
    FROM memories m
    LEFT JOIN memory_embeddings me ON m.id = me.rowid
    WHERE me.rowid IS NULL AND m.deleted_at IS NULL
""").fetchall()
if orphan_memories:
    issues.append(f"### Memories without embeddings ({len(orphan_memories)})")
    for m in orphan_memories[:10]:
        issues.append(f"- ID={m[0]} type={m[2]} preview=`{m[3]}`")
    if fix_mode and daemon_alive():
        texts = [m[4] for m in orphan_memories]
        emb_results = daemon_encode(texts)
        if emb_results and len(emb_results) == len(orphan_memories):
            regen_count = 0
            for i, m in enumerate(orphan_memories):
                try:
                    conn.execute(
                        "INSERT INTO memory_embeddings (rowid, content_embedding) VALUES (?, ?)",
                        (m[0], emb_results[i])
                    )
                    regen_count += 1
                except Exception:
                    pass
            conn.commit()
            fix_actions.append(f"Regenerated {regen_count} orphan embeddings")
            issues.append(f"  - **FIXED**: Regenerated {regen_count}/{len(orphan_memories)} embeddings via daemon")
        else:
            issues.append("  - **SKIP**: Daemon unavailable or encode failed; orphans not fixed")
    elif fix_mode:
        issues.append("  - **SKIP**: Daemon not running; cannot regenerate embeddings")
    issues.append("")

# ── Check 3: Very short memories (<20 chars) ──
short = conn.execute("""
    SELECT id, content_hash, memory_type, length(content), metadata
    FROM memories WHERE length(content) < 20 AND deleted_at IS NULL
""").fetchall()
if short:
    issues.append(f"### Very short memories ({len(short)})")
    for m in short:
        issues.append(f"- ID={m[0]} type={m[2]} length={m[3]}")
    if fix_mode:
        flagged = 0
        for m in short:
            try:
                meta = json.loads(m[4]) if m[4] else {}
            except (json.JSONDecodeError, TypeError):
                meta = {}
            if 'low-quality' not in meta.get('flags', []):
                flags = meta.get('flags', [])
                flags.append('low-quality')
                meta['flags'] = flags
                conn.execute(
                    "UPDATE memories SET metadata = ? WHERE id = ?",
                    (json.dumps(meta, ensure_ascii=False), m[0])
                )
                flagged += 1
        if flagged:
            conn.commit()
            fix_actions.append(f"Flagged {flagged} short memories as low-quality")
            issues.append(f"  - **FIXED**: Flagged {flagged} memories with `low-quality`")
    issues.append("")

# ── Check 4: Duplicate-ish content ──
dupes = conn.execute("""
    SELECT substr(content, 1, 100) as prefix, COUNT(*) as cnt
    FROM memories WHERE deleted_at IS NULL GROUP BY prefix HAVING cnt > 1
""").fetchall()
if dupes:
    issues.append(f"### Potential duplicates ({len(dupes)} groups)")
    for d in dupes[:5]:
        issues.append(f"- `{d[0][:60]}...` ({d[1]} copies)")
    issues.append("")

# ── Check 5: Stale memories (>90 days) ──
stale_threshold = now.timestamp() - (90 * 86400)
stale = conn.execute("SELECT COUNT(*) FROM memories WHERE updated_at < ? AND deleted_at IS NULL", (stale_threshold,)).fetchone()[0]
if stale > 0:
    issues.append(f"### Stale memories (>90 days without update): {stale}")
    issues.append("")

# ── Check 6: Importance score distribution ──
importance_dist = conn.execute("""
    SELECT
        CASE
            WHEN json_extract(metadata, '$.importance_score') >= 2.0 THEN 'critical (2.0)'
            WHEN json_extract(metadata, '$.importance_score') >= 1.5 THEN 'important (1.5-2.0)'
            WHEN json_extract(metadata, '$.importance_score') >= 1.0 THEN 'normal (1.0-1.5)'
            ELSE 'low (<1.0 or unset)'
        END as tier,
        COUNT(*)
    FROM memories GROUP BY tier ORDER BY tier
""").fetchall()
if importance_dist:
    issues.append("### Importance Distribution")
    for tier, cnt in importance_dist:
        issues.append(f"- {tier}: {cnt}")
    issues.append("")

# ── Check 7: Orphaned graph edges ──
orphaned_edges = conn.execute("""
    SELECT COUNT(*) FROM memory_graph
    WHERE source_hash NOT IN (SELECT content_hash FROM memories WHERE deleted_at IS NULL)
       OR target_hash NOT IN (SELECT content_hash FROM memories WHERE deleted_at IS NULL)
""").fetchone()[0]
if orphaned_edges > 0:
    issues.append(f"### Orphaned graph edges: {orphaned_edges}")
    if fix_mode:
        conn.execute("""
            DELETE FROM memory_graph
            WHERE source_hash NOT IN (SELECT content_hash FROM memories WHERE deleted_at IS NULL)
               OR target_hash NOT IN (SELECT content_hash FROM memories WHERE deleted_at IS NULL)
        """)
        conn.commit()
        fix_actions.append(f"Deleted {orphaned_edges} orphaned graph edges")
        issues.append(f"  - **FIXED**: Deleted {orphaned_edges} orphaned graph edges")
    issues.append("")

if not issues:
    lines.append("No quality issues found.")
else:
    lines.extend(issues)

# ── Graph health ──
lines.append("## Graph Health")
edge_types = conn.execute("SELECT relationship_type, COUNT(*) FROM memory_graph GROUP BY relationship_type").fetchall()
if edge_types:
    for et, cnt in edge_types:
        lines.append(f"- {et}: {cnt} edges")
    # Contradiction summary
    contradicts_count = sum(cnt for et, cnt in edge_types if et == 'contradicts')
    if contradicts_count > 0:
        lines.append(f"\n### Top Contradictions ({contradicts_count} total)")
        top_conflicts = conn.execute("""
            SELECT mg.source_hash, mg.target_hash, mg.similarity,
                   substr(m1.content, 1, 80), substr(m2.content, 1, 80)
            FROM memory_graph mg
            LEFT JOIN memories m1 ON m1.content_hash = mg.source_hash AND m1.deleted_at IS NULL
            LEFT JOIN memories m2 ON m2.content_hash = mg.target_hash AND m2.deleted_at IS NULL
            WHERE mg.relationship_type = 'contradicts'
            ORDER BY mg.similarity DESC
            LIMIT 5
        """).fetchall()
        for src_h, tgt_h, sim, c1, c2 in top_conflicts:
            c1_preview = (c1 or '?')[:60]
            c2_preview = (c2 or '?')[:60]
            lines.append(f"- [{sim:.2f}] `{c1_preview}` vs `{c2_preview}`")
else:
    lines.append("- No graph edges yet (will populate as sessions accumulate)")
lines.append("")

# ── Fix summary ──
if fix_mode and fix_actions:
    lines.append("## Auto-Remediation Summary")
    for action in fix_actions:
        lines.append(f"- {action}")
    lines.append("")

# ── Summary score ──
lines.append("## Summary")
health_score = 100
if orphan_memories:
    health_score -= len(orphan_memories) * 10
if short:
    health_score -= len(short) * 5
if dupes:
    health_score -= len(dupes) * 15
if stale_embeddings > 0:
    health_score -= min(stale_embeddings, 5) * 5
if orphaned_edges > 0:
    health_score -= min(orphaned_edges, 5) * 3
health_score = max(0, min(100, health_score))
lines.append(f"**Health Score: {health_score}/100**")

report = '\n'.join(lines)
with open(report_file, 'w') as f:
    f.write(report)

if not quiet:
    print(f"Quality audit complete: {report_file}")
    print(f"Health Score: {health_score}/100")
    print(f"Memories: {total}, Embeddings: {embeddings}, Graph Edges: {len(edge_types) and sum(c for _, c in edge_types) or 0}")
    if fix_actions:
        print(f"Fixes applied: {len(fix_actions)}")
        for a in fix_actions:
            print(f"  - {a}")

conn.close()
PYEOF

# Keep only last 12 audit reports
ls -t "$LOG_DIR"/quality-audit-*.md 2>/dev/null | tail -n +13 | xargs rm -f 2>/dev/null

[ "$QUIET" = false ] && echo "Quality audit complete: $REPORT_FILE"
