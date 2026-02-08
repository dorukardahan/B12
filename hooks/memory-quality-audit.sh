#!/bin/bash
# B12 Memory System - Weekly Quality Audit
# Evaluates all memories for quality, flags low-quality entries
# Runs via launchd: com.b12.memory-quality-audit (Wednesday 3:00 AM)
#
# Output: ~/.claude/memory-logs/quality-audit-{date}.md

set -e

VENV_PYTHON="$HOME/.local/pipx/venvs/mcp-memory-service/bin/python3"
LOG_DIR="$HOME/.claude/memory-logs"
mkdir -p "$LOG_DIR"

DATE=$(date -u +%Y-%m-%d)
REPORT_FILE="$LOG_DIR/quality-audit-${DATE}.md"

if [ ! -x "$VENV_PYTHON" ]; then
  echo "ERROR: venv Python not found at $VENV_PYTHON" >&2
  exit 1
fi

$VENV_PYTHON - "$REPORT_FILE" << 'PYEOF'
import sys, os, json, sqlite3, warnings
warnings.filterwarnings('ignore')

report_file = sys.argv[1]
DB_PATH = os.path.expanduser("~/Library/Application Support/mcp-memory/sqlite_vec.db")

if not os.path.exists(DB_PATH):
    print("ERROR: Database not found")
    sys.exit(1)

try:
    import sqlite_vec
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
except Exception as e:
    print(f"ERROR: Cannot connect to DB: {e}")
    sys.exit(1)

from datetime import datetime, timezone

now = datetime.now(timezone.utc)
lines = [f"# Memory Quality Audit — {now.strftime('%Y-%m-%d %H:%M UTC')}\n"]

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
from collections import Counter
projects = Counter()
for (tags,) in rows:
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

# Check 1: Memories without embeddings
orphan_memories = conn.execute("""
    SELECT m.id, m.content_hash, m.memory_type, substr(m.content, 1, 60)
    FROM memories m
    LEFT JOIN memory_embeddings me ON m.id = me.rowid
    WHERE me.rowid IS NULL
""").fetchall()
if orphan_memories:
    issues.append(f"### Memories without embeddings ({len(orphan_memories)})")
    for m in orphan_memories[:10]:
        issues.append(f"- ID={m[0]} type={m[2]} preview=`{m[3]}`")
    issues.append("")

# Check 2: Very short memories (likely low quality)
short = conn.execute("SELECT id, content_hash, memory_type, length(content) FROM memories WHERE length(content) < 50 AND deleted_at IS NULL").fetchall()
if short:
    issues.append(f"### Very short memories ({len(short)})")
    for m in short:
        issues.append(f"- ID={m[0]} type={m[2]} length={m[3]}")
    issues.append("")

# Check 3: Duplicate-ish content (same first 100 chars)
dupes = conn.execute("""
    SELECT substr(content, 1, 100) as prefix, COUNT(*) as cnt
    FROM memories WHERE deleted_at IS NULL GROUP BY prefix HAVING cnt > 1
""").fetchall()
if dupes:
    issues.append(f"### Potential duplicates ({len(dupes)} groups)")
    for d in dupes[:5]:
        issues.append(f"- `{d[0][:60]}...` ({d[1]} copies)")
    issues.append("")

# Check 4: Stale memories (not accessed in 90+ days)
stale_threshold = now.timestamp() - (90 * 86400)
stale = conn.execute("SELECT COUNT(*) FROM memories WHERE updated_at < ? AND deleted_at IS NULL", (stale_threshold,)).fetchone()[0]
if stale > 0:
    issues.append(f"### Stale memories (>90 days without update): {stale}")
    issues.append("")

# Check 5: Importance score distribution
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

if not issues:
    lines.append("No quality issues found.")
else:
    lines.extend(issues)

# Graph health
lines.append("## Graph Health")
if graph_edges > 0:
    edge_types = conn.execute("SELECT relationship_type, COUNT(*) FROM memory_graph GROUP BY relationship_type").fetchall()
    for et, cnt in edge_types:
        lines.append(f"- {et}: {cnt} edges")
else:
    lines.append("- No graph edges yet (will populate as sessions accumulate)")
lines.append("")

# Summary
lines.append("## Summary")
health_score = 100
if orphan_memories:
    health_score -= len(orphan_memories) * 10
if short:
    health_score -= len(short) * 5
if dupes:
    health_score -= len(dupes) * 15
health_score = max(0, min(100, health_score))
lines.append(f"**Health Score: {health_score}/100**")

report = '\n'.join(lines)
with open(report_file, 'w') as f:
    f.write(report)

print(f"Quality audit complete: {report_file}")
print(f"Health Score: {health_score}/100")
print(f"Memories: {total}, Embeddings: {embeddings}, Graph Edges: {graph_edges}")

conn.close()
PYEOF

# Keep only last 12 audit reports
ls -t "$LOG_DIR"/quality-audit-*.md 2>/dev/null | tail -n +13 | xargs rm -f 2>/dev/null

echo "Quality audit complete: $REPORT_FILE"
