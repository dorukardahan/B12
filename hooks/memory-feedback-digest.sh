#!/bin/bash
# B12 Memory System - Feedback Digest Generator (v2 — Retrieval Feedback)
# Parses feedback.jsonl and generates feedback-digest.md
# Designed to run weekly (via launchd or manually)
#
# Output: ~/.B12/memory-logs/feedback-digest.md
# SessionStart v4 loads the "## Alerts" section from this file
#
# Usage:
#   ./memory-feedback-digest.sh              # Generate digest
#   ./memory-feedback-digest.sh --quiet      # No stdout output

B12_BASE="${B12_DATA_DIR:-$HOME/.B12}"
FEEDBACK_FILE="$B12_BASE/memory-logs/feedback.jsonl"
DIGEST_FILE="$B12_BASE/memory-logs/feedback-digest.md"
QUIET=false

if [ "$1" = "--quiet" ]; then
  QUIET=true
fi

if [ ! -f "$FEEDBACK_FILE" ]; then
  [ "$QUIET" = false ] && echo "No feedback file found at $FEEDBACK_FILE"
  exit 0
fi

LINE_COUNT=$(wc -l < "$FEEDBACK_FILE" 2>/dev/null | tr -d ' ')
if [ "$LINE_COUNT" -lt 5 ]; then
  [ "$QUIET" = false ] && echo "Not enough data yet ($LINE_COUNT entries). Need at least 5."
  exit 0
fi

python3 - "$FEEDBACK_FILE" "$DIGEST_FILE" "$QUIET" << 'PYEOF'
import sys, json, os
from datetime import datetime, timezone, timedelta
from collections import defaultdict

feedback_file = sys.argv[1]
digest_file = sys.argv[2]
quiet = sys.argv[3] == "true"

# Parse feedback entries
entries = []
with open(feedback_file, 'r') as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue

if not entries:
    if not quiet:
        print("No valid entries in feedback file")
    sys.exit(0)

# Time windows
now = datetime.now(timezone.utc)
week_ago = now - timedelta(days=7)
month_ago = now - timedelta(days=30)

def parse_ts(ts_value):
    try:
        if isinstance(ts_value, (int, float)):
            return datetime.fromtimestamp(ts_value, tz=timezone.utc)
        return datetime.fromisoformat(ts_value.replace('Z', '+00:00'))
    except (ValueError, TypeError, AttributeError, OSError):
        return None

# Categorize entries by time window
recent = []  # last 7 days
monthly = []  # last 30 days
for e in entries:
    ts = parse_ts(e.get('ts', ''))
    if ts:
        if ts >= week_ago:
            recent.append(e)
        if ts >= month_ago:
            monthly.append(e)

# Use monthly for stats (more representative), recent for alerts
stats_entries = monthly if monthly else entries[-200:]

# ═══════════════════════════════════════════════════
# METRICS
# ═══════════════════════════════════════════════════

total = len(stats_entries)
stores = [e for e in stats_entries if e.get('action') == 'store']
searches = [e for e in stats_entries if e.get('action') == 'search']
updates = [e for e in stats_entries if e.get('action') == 'update']
quality_checks = [e for e in stats_entries if e.get('action') == 'quality']

# Store quality
stores_with_metadata = sum(1 for s in stores if s.get('has_metadata'))
stores_with_tags = sum(1 for s in stores if s.get('has_tags'))
stores_with_proj_tag = sum(1 for s in stores if s.get('has_proj_tag'))
stores_with_scope = sum(1 for s in stores if s.get('has_scope'))

# Search quality
empty_searches = sum(1 for s in searches if s.get('empty_result'))

# Search pattern analysis (v2 — retrieval feedback)
search_result_counts = [s.get('result_count', 0) for s in searches if 'result_count' in s]
avg_result_count = sum(search_result_counts) / len(search_result_counts) if search_result_counts else 0

# Search refinement: multiple searches in same session = query refinement
session_search_counts = defaultdict(int)
session_search_queries = defaultdict(list)
for s in searches:
    sid = s.get('session', '')
    if sid:
        session_search_counts[sid] += 1
        qt = s.get('query_text', '')
        if qt:
            session_search_queries[sid].append(qt)

# Sessions with refinement (2+ searches)
refined_sessions = {sid: cnt for sid, cnt in session_search_counts.items() if cnt >= 2}
refinement_rate = len(refined_sessions) / len(session_search_counts) * 100 if session_search_counts else 0

# Repeated queries (exact same query in different sessions = important topic)
all_queries = [s.get('query_text', '') for s in searches if s.get('query_text')]
query_freq = defaultdict(int)
for q in all_queries:
    if q:
        query_freq[q] += 1
repeated_queries = {q: c for q, c in query_freq.items() if c >= 2}

# Retrieval latency percentiles (from hook_retrieval entries)
hook_retrievals = [e for e in recent if e.get('type') == 'hook_retrieval']
latencies = sorted([e['latency_ms'] for e in hook_retrievals if e.get('latency_ms', 0) > 0])
latency_p50 = latency_p95 = latency_p99 = 0
if latencies:
    latency_p50 = latencies[len(latencies) // 2]
    latency_p95 = latencies[int(len(latencies) * 0.95)]
    latency_p99 = latencies[min(int(len(latencies) * 0.99), len(latencies) - 1)]

# Project distribution
projects = defaultdict(int)
for e in stats_entries:
    proj = e.get('project', 'unknown')
    projects[proj] += 1

# ═══════════════════════════════════════════════════
# ALERTS (high-priority issues for SessionStart)
# ═══════════════════════════════════════════════════

alerts = []

# Alert: Low scope compliance
if stores and len(stores) >= 3:
    scope_rate = stores_with_scope / len(stores) * 100
    if scope_rate < 50:
        alerts.append(f"Low scope compliance: {scope_rate:.0f}% of stores have scope metadata. Always include metadata.scope when storing.")

# Alert: Low project tag usage
if stores and len(stores) >= 3:
    proj_rate = stores_with_proj_tag / len(stores) * 100
    if proj_rate < 50:
        alerts.append(f"Low project tagging: {proj_rate:.0f}% of stores have proj: tags. Always include proj:<name> tag.")

# Alert: High empty search rate
if searches and len(searches) >= 5:
    empty_rate = empty_searches / len(searches) * 100
    if empty_rate > 40:
        alerts.append(f"High empty search rate: {empty_rate:.0f}% searches return no results. Try broader queries or check if memories exist.")

# Alert: No quality checks
if len(stores) >= 10 and len(quality_checks) == 0:
    alerts.append("No memory_quality checks used. Run memory_quality periodically to maintain DB health.")

# Alert: No updates
if len(stores) >= 10 and len(updates) == 0:
    alerts.append("No memory_update calls. Update existing memories instead of creating duplicates.")

# Alert: High search refinement rate
if len(session_search_counts) >= 5 and refinement_rate > 60:
    alerts.append(f"High search refinement rate: {refinement_rate:.0f}% of sessions refine queries. Consider improving memory content for common searches.")

# Alert: Very low result counts
if search_result_counts and avg_result_count < 1.5 and len(searches) >= 5:
    alerts.append(f"Low average results per search ({avg_result_count:.1f}). Memory DB may need more entries.")

# Alert: High retrieval latency
if latencies and latency_p95 > 500:
    alerts.append(f"Retrieval latency high: p95={latency_p95}ms (target <500ms). Check if embed daemon is running.")

# ═══════════════════════════════════════════════════
# BUILD DIGEST
# ═══════════════════════════════════════════════════

lines = []
lines.append(f"# Memory Usage Digest")
lines.append(f"Generated: {now.strftime('%Y-%m-%d %H:%M UTC')}")
lines.append(f"Period: last 30 days ({total} operations)")
lines.append("")

# Alerts section (SessionStart v4 reads this)
lines.append("## Alerts")
if alerts:
    for a in alerts:
        lines.append(f"- {a}")
else:
    lines.append("- No issues detected. Memory usage looks healthy.")
lines.append("")

# Stats
lines.append("## Stats")
lines.append(f"- Stores: {len(stores)}")
lines.append(f"- Searches: {len(searches)} ({empty_searches} empty, avg {avg_result_count:.1f} results)")
lines.append(f"- Updates: {len(updates)}")
lines.append(f"- Quality checks: {len(quality_checks)}")
lines.append(f"- Search sessions: {len(session_search_counts)} ({len(refined_sessions)} with refinement, {refinement_rate:.0f}% rate)")
lines.append("")

# Retrieval latency
if latencies:
    lines.append("## Retrieval Latency (last 7 days)")
    lines.append(f"- Samples: {len(latencies)}")
    lines.append(f"- p50: {latency_p50}ms, p95: {latency_p95}ms, p99: {latency_p99}ms")
    if latency_p95 > 500:
        lines.append(f"- **[WARN]** p95 > 500ms — check embed daemon")
    lines.append("")

# Quality metrics
if stores:
    lines.append("## Store Quality")
    lines.append(f"- With metadata: {stores_with_metadata}/{len(stores)} ({stores_with_metadata/len(stores)*100:.0f}%)")
    lines.append(f"- With tags: {stores_with_tags}/{len(stores)} ({stores_with_tags/len(stores)*100:.0f}%)")
    lines.append(f"- With proj: tag: {stores_with_proj_tag}/{len(stores)} ({stores_with_proj_tag/len(stores)*100:.0f}%)")
    lines.append(f"- With scope metadata: {stores_with_scope}/{len(stores)} ({stores_with_scope/len(stores)*100:.0f}%)")
    lines.append("")

# Search patterns (v2)
if searches and (repeated_queries or refined_sessions):
    lines.append("## Search Patterns")
    if repeated_queries:
        lines.append("### Frequently Searched")
        for q, c in sorted(repeated_queries.items(), key=lambda x: -x[1])[:5]:
            lines.append(f"- \"{q[:80]}\" ({c}x)")
    if refined_sessions:
        lines.append(f"### Search Refinement")
        lines.append(f"- {len(refined_sessions)} sessions refined their queries")
        for sid, cnt in sorted(refined_sessions.items(), key=lambda x: -x[1])[:3]:
            queries = session_search_queries.get(sid, [])
            lines.append(f"  - Session {sid[:8]}...: {cnt} searches")
            for q in queries[:3]:
                lines.append(f"    - \"{q[:60]}\"")
    lines.append("")

# Project distribution
if len(projects) > 1:
    lines.append("## Projects")
    for proj, count in sorted(projects.items(), key=lambda x: -x[1])[:10]:
        lines.append(f"- {proj}: {count} ops")
    lines.append("")

# ═══════════════════════════════════════════════════
# RETRIEVAL USEFULNESS PROXY
# If hook_retrieval (result_count>0) is followed by a manual search
# within 120s, the retrieval was likely insufficient.
# ═══════════════════════════════════════════════════

# Build timeline of retrieval and search events per session
_session_events = defaultdict(list)
for e in recent:
    _type = e.get('type', e.get('action', ''))
    _ts = e.get('ts', 0)
    _sid = e.get('session', e.get('project', ''))
    if _type == 'hook_retrieval' and e.get('result_count', 0) > 0:
        _session_events[_sid].append(('retrieval', _ts, e.get('keywords', '')))
    elif _type in ('search', 'hook_retrieval') and e.get('action') == 'search':
        _session_events[_sid].append(('search', _ts, e.get('query_text', '')))

_sufficient = 0
_insufficient = 0
for sid, events in _session_events.items():
    events.sort(key=lambda x: x[1])
    for i, (etype, ets, edata) in enumerate(events):
        if etype != 'retrieval':
            continue
        # Check if followed by manual search within 120s
        followed_by_search = False
        for j in range(i + 1, len(events)):
            ntype, nts, ndata = events[j]
            if nts - ets > 120:
                break
            if ntype == 'search':
                followed_by_search = True
                break
        if followed_by_search:
            _insufficient += 1
        else:
            _sufficient += 1

_total_assessed = _sufficient + _insufficient
if _total_assessed > 0:
    _sufficiency_pct = _sufficient / _total_assessed * 100
    lines.append("## Retrieval Usefulness")
    lines.append(f"- Assessed: {_total_assessed} retrievals with results")
    lines.append(f"- Sufficient (no follow-up search): {_sufficient} ({_sufficiency_pct:.0f}%)")
    lines.append(f"- Insufficient (manual search within 120s): {_insufficient}")
    if _sufficiency_pct < 50 and _total_assessed >= 5:
        lines.append(f"- **[WARN]** Low sufficiency — retrieval quality needs improvement")
    lines.append("")

# ═══════════════════════════════════════════════════
# SELF-IMPROVING RETRIEVAL — strength adjustment
# Memories not retrieved this week get slight decay
# This creates natural selection: useful memories survive, noisy ones fade
# ═══════════════════════════════════════════════════

import sys as _sys
_home = os.path.expanduser("~")
if _sys.platform == "darwin":
    DB_PATH = os.path.join(_home, "Library", "Application Support", "mcp-memory", "sqlite_vec.db")
elif _sys.platform == "win32":
    DB_PATH = os.path.join(_home, "AppData", "Local", "mcp-memory", "sqlite_vec.db")
else:
    DB_PATH = os.path.join(_home, ".local", "share", "mcp-memory", "sqlite_vec.db")
strength_changes = []

if os.path.exists(DB_PATH):
    try:
        import sqlite3
        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")

        week_ago_ts = (now - timedelta(days=7)).timestamp()

        # Decay: memories not accessed in last 7 days lose -0.05 strength (min 0.3)
        decayed = conn.execute("""
            UPDATE memories
            SET strength = MAX(0.3, COALESCE(strength, 1.0) - 0.05)
            WHERE deleted_at IS NULL
              AND valid_until IS NULL
              AND memory_type NOT IN ('session_summary', 'pattern', 'association')
              AND COALESCE(last_accessed_at, created_at) < ?
              AND COALESCE(strength, 1.0) > 0.3
        """, (week_ago_ts,))
        decay_count = decayed.rowcount

        # Dormancy: strength-floor memories not accessed in 60+ days become dormant
        sixty_days_ago_ts = (now - timedelta(days=60)).timestamp()
        dormant = conn.execute("""
            UPDATE memories
            SET valid_until = datetime('now')
            WHERE strength <= 0.3
              AND COALESCE(last_accessed_at, created_at) < ?
              AND valid_until IS NULL
              AND deleted_at IS NULL
              AND memory_type NOT IN ('session_summary', 'pattern', 'association')
        """, (sixty_days_ago_ts,))
        dormant_count = dormant.rowcount

        # Report: memories with extreme strength values
        high_strength = conn.execute("""
            SELECT substr(content_hash, 1, 8), strength, memory_type,
                   substr(content, 1, 60)
            FROM memories
            WHERE deleted_at IS NULL AND strength >= 3.0
            ORDER BY strength DESC LIMIT 5
        """).fetchall()

        low_strength = conn.execute("""
            SELECT substr(content_hash, 1, 8), strength, memory_type,
                   substr(content, 1, 60)
            FROM memories
            WHERE deleted_at IS NULL AND strength <= 0.5 AND strength > 0
            ORDER BY strength ASC LIMIT 5
        """).fetchall()

        conn.commit()
        conn.close()

        lines.append("## Self-Improving Retrieval")
        lines.append(f"- Decayed {decay_count} memories not accessed in 7 days (-0.05 strength)")
        lines.append(f"- Made {dormant_count} memories dormant (strength=0.3, not accessed in 60+ days)")
        if high_strength:
            lines.append("### Frequently Retrieved (high strength)")
            for h, s, t, preview in high_strength:
                lines.append(f"- `{h}` [{t}] strength={s:.1f}: {preview}...")
        if low_strength:
            lines.append("### Fading Memories (low strength — candidates for review)")
            for h, s, t, preview in low_strength:
                lines.append(f"- `{h}` [{t}] strength={s:.2f}: {preview}...")
        lines.append("")

    except Exception as e:
        lines.append(f"## Self-Improving Retrieval")
        lines.append(f"- Error: {e}")
        lines.append("")

digest_text = '\n'.join(lines)

with open(digest_file, 'w') as f:
    f.write(digest_text)

if not quiet:
    print(digest_text)

PYEOF

exit 0
