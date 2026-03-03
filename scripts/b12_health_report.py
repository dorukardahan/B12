#!/usr/bin/env python3
"""B12 Health Report — Comprehensive weekly report combining quality audit,
feedback digest, and session log data into a single health overview.

Usage:
    python3 scripts/b12_health_report.py
    python3 scripts/b12_health_report.py --format json
    python3 scripts/b12_health_report.py --db-path /path/to/sqlite_vec.db
    python3 scripts/b12_health_report.py --output-dir /path/to/output
"""

import argparse
import json
import os
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

# ── DB path resolution ──────────────────────────────────────────────

def _get_db_path_fallback() -> str:
    """Platform-aware DB path (inline fallback if shared_patterns unavailable)."""
    home = os.path.expanduser("~")
    if sys.platform == "darwin":
        return os.path.join(home, "Library", "Application Support",
                            "mcp-memory", "sqlite_vec.db")
    elif sys.platform == "win32":
        return os.path.join(home, "AppData", "Local",
                            "mcp-memory", "sqlite_vec.db")
    else:
        return os.path.join(home, ".local", "share",
                            "mcp-memory", "sqlite_vec.db")


def _resolve_db_path(cli_path: str | None) -> str:
    if cli_path:
        return cli_path
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
        from shared_patterns import get_db_path
        return get_db_path()
    except ImportError:
        return _get_db_path_fallback()


# ── JSONL helpers ───────────────────────────────────────────────────

def _read_jsonl(path: str) -> list[dict]:
    """Read a JSONL file, skipping invalid lines."""
    entries = []
    if not os.path.isfile(path):
        return entries
    with open(path, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def _parse_ts(ts_value) -> datetime | None:
    try:
        if isinstance(ts_value, (int, float)):
            return datetime.fromtimestamp(ts_value, tz=timezone.utc)
        if isinstance(ts_value, str):
            return datetime.fromisoformat(ts_value.replace("Z", "+00:00"))
    except (ValueError, TypeError, AttributeError, OSError):
        pass
    return None


def _percentile(sorted_list: list, pct: float):
    """Return the pct-th percentile from a sorted list."""
    if not sorted_list:
        return 0
    idx = min(int(len(sorted_list) * pct), len(sorted_list) - 1)
    return sorted_list[idx]


# ── Report sections ─────────────────────────────────────────────────

def _section_db_metrics(conn: sqlite3.Connection) -> dict:
    """Section 2: Database metrics."""
    active = conn.execute(
        "SELECT COUNT(*) FROM memories WHERE deleted_at IS NULL"
    ).fetchone()[0]
    deleted = conn.execute(
        "SELECT COUNT(*) FROM memories WHERE deleted_at IS NOT NULL"
    ).fetchone()[0]
    # memory_embeddings is a vec0 virtual table — requires sqlite_vec extension.
    # Fall back to 0 if the extension is not loaded.
    try:
        embeddings = conn.execute(
            "SELECT COUNT(*) FROM memory_embeddings"
        ).fetchone()[0]
    except sqlite3.OperationalError:
        embeddings = 0

    try:
        edges = conn.execute(
            "SELECT COUNT(*) FROM memory_graph"
        ).fetchone()[0]
    except sqlite3.OperationalError:
        edges = 0

    # Type distribution
    types_raw = conn.execute(
        "SELECT memory_type, COUNT(*) FROM memories "
        "WHERE deleted_at IS NULL GROUP BY memory_type ORDER BY COUNT(*) DESC"
    ).fetchall()
    type_dist = {t: c for t, c in types_raw}

    # Edge type distribution
    try:
        edge_types_raw = conn.execute(
            "SELECT relationship_type, COUNT(*) FROM memory_graph "
            "GROUP BY relationship_type"
        ).fetchall()
        edge_dist = {t: c for t, c in edge_types_raw}
    except sqlite3.OperationalError:
        edge_dist = {}

    embedding_coverage = (embeddings / active * 100) if active > 0 else 0.0

    return {
        "active": active,
        "deleted": deleted,
        "embeddings": embeddings,
        "edges": edges,
        "embedding_coverage_pct": round(embedding_coverage, 1),
        "type_distribution": type_dist,
        "edge_distribution": edge_dist,
    }


def _section_growth_trends(log_dir: str) -> dict:
    """Section 3: 4-week growth trends from growth-history.jsonl."""
    growth_file = os.path.join(log_dir, "growth-history.jsonl")
    entries = _read_jsonl(growth_file)

    # Take last 4 entries (one per audit week)
    recent = entries[-4:] if len(entries) >= 4 else entries
    weekly_new = [e.get("this_week", 0) for e in recent]
    totals = [e.get("total", 0) for e in recent]
    dates = [e.get("date", "?") for e in recent]

    avg_weekly = sum(weekly_new) / len(weekly_new) if weekly_new else 0
    latest_total = totals[-1] if totals else 0
    projected_6mo = latest_total + int(avg_weekly * 26)

    return {
        "weeks": [
            {"date": d, "total": t, "new": n}
            for d, t, n in zip(dates, totals, weekly_new)
        ],
        "avg_weekly_new": round(avg_weekly, 1),
        "projected_6mo_total": projected_6mo,
    }


def _section_retrieval_perf(log_dir: str) -> dict:
    """Section 4: Retrieval latency p50/p95/p99 from feedback.jsonl."""
    feedback_file = os.path.join(log_dir, "feedback.jsonl")
    entries = _read_jsonl(feedback_file)

    now = datetime.now(timezone.utc)
    week_ago = now - timedelta(days=7)

    # Filter hook_retrieval entries from last 7 days
    latencies = []
    for e in entries:
        if e.get("type") != "hook_retrieval":
            continue
        ts = _parse_ts(e.get("ts", 0))
        if ts and ts >= week_ago:
            lat = e.get("latency_ms", 0)
            if lat > 0:
                latencies.append(lat)

    latencies.sort()
    return {
        "samples": len(latencies),
        "p50_ms": _percentile(latencies, 0.50),
        "p95_ms": _percentile(latencies, 0.95),
        "p99_ms": _percentile(latencies, 0.99),
    }


def _section_retrieval_quality(log_dir: str) -> dict:
    """Section 5: Retrieval quality — usefulness proxy.

    If a hook_retrieval with results is followed by a manual search within
    120s in the same session, the retrieval was likely insufficient.
    """
    feedback_file = os.path.join(log_dir, "feedback.jsonl")
    entries = _read_jsonl(feedback_file)

    now = datetime.now(timezone.utc)
    week_ago = now - timedelta(days=7)

    session_events: dict[str, list] = defaultdict(list)
    for e in entries:
        ts = _parse_ts(e.get("ts", 0))
        if not ts or ts < week_ago:
            continue
        etype = e.get("type", e.get("action", ""))
        sid = e.get("session", e.get("project", ""))
        ts_epoch = ts.timestamp()
        if etype == "hook_retrieval" and e.get("result_count", 0) > 0:
            session_events[sid].append(("retrieval", ts_epoch))
        elif etype in ("search",) or (etype == "hook_retrieval" and e.get("action") == "search"):
            session_events[sid].append(("search", ts_epoch))

    sufficient = 0
    insufficient = 0
    for sid, events in session_events.items():
        events.sort(key=lambda x: x[1])
        for i, (et, ets) in enumerate(events):
            if et != "retrieval":
                continue
            followed = False
            for j in range(i + 1, len(events)):
                nt, nts = events[j]
                if nts - ets > 120:
                    break
                if nt == "search":
                    followed = True
                    break
            if followed:
                insufficient += 1
            else:
                sufficient += 1

    total_assessed = sufficient + insufficient
    sufficiency_pct = (sufficient / total_assessed * 100) if total_assessed > 0 else 100.0

    # Also compute empty search rate from all searches this week
    searches_total = 0
    searches_empty = 0
    for e in entries:
        if e.get("action") != "search":
            continue
        ts = _parse_ts(e.get("ts", 0))
        if ts and ts >= week_ago:
            searches_total += 1
            if e.get("empty_result"):
                searches_empty += 1
    empty_search_pct = (searches_empty / searches_total * 100) if searches_total > 0 else 0.0

    return {
        "assessed_retrievals": total_assessed,
        "sufficient": sufficient,
        "insufficient": insufficient,
        "sufficiency_pct": round(sufficiency_pct, 1),
        "searches_total": searches_total,
        "searches_empty": searches_empty,
        "empty_search_pct": round(empty_search_pct, 1),
    }


def _section_lifecycle(conn: sqlite3.Connection) -> dict:
    """Section 6: Memory lifecycle — new/decayed/dormant/deleted this week."""
    now = datetime.now(timezone.utc)
    week_ago_ts = (now - timedelta(days=7)).timestamp()

    new_count = conn.execute(
        "SELECT COUNT(*) FROM memories "
        "WHERE created_at >= ? AND deleted_at IS NULL",
        (week_ago_ts,),
    ).fetchone()[0]

    # Decayed: strength < 1.0 and last_accessed < week_ago
    decayed = conn.execute(
        "SELECT COUNT(*) FROM memories "
        "WHERE deleted_at IS NULL AND COALESCE(strength, 1.0) < 1.0 "
        "AND COALESCE(last_accessed_at, created_at) < ?",
        (week_ago_ts,),
    ).fetchone()[0]

    # Dormant: valid_until is set and in the past, not deleted
    dormant = conn.execute(
        "SELECT COUNT(*) FROM memories "
        "WHERE deleted_at IS NULL AND valid_until IS NOT NULL "
        "AND valid_until <= datetime('now')"
    ).fetchone()[0]

    # Deleted this week
    deleted_this_week = conn.execute(
        "SELECT COUNT(*) FROM memories "
        "WHERE deleted_at IS NOT NULL AND deleted_at >= ?",
        (week_ago_ts,),
    ).fetchone()[0]

    return {
        "new_this_week": new_count,
        "decayed": decayed,
        "dormant": dormant,
        "deleted_this_week": deleted_this_week,
    }


def _section_top_issues(conn: sqlite3.Connection) -> dict:
    """Section 7: Top issues — duplicates, stale memories, orphaned edges."""
    now = datetime.now(timezone.utc)

    # Duplicate groups (same first 100 chars)
    dupes = conn.execute(
        "SELECT substr(content, 1, 100) AS prefix, COUNT(*) AS cnt "
        "FROM memories WHERE deleted_at IS NULL GROUP BY prefix HAVING cnt > 1"
    ).fetchall()
    dup_groups = [{"preview": d[0][:60], "count": d[1]} for d in dupes[:10]]

    # Stale memories (>90 days without update)
    stale_threshold = now.timestamp() - (90 * 86400)
    stale_count = conn.execute(
        "SELECT COUNT(*) FROM memories "
        "WHERE updated_at < ? AND deleted_at IS NULL",
        (stale_threshold,),
    ).fetchone()[0]
    active_count = conn.execute(
        "SELECT COUNT(*) FROM memories WHERE deleted_at IS NULL"
    ).fetchone()[0]

    # Orphaned graph edges
    try:
        orphaned_edges = conn.execute(
            "SELECT COUNT(*) FROM memory_graph "
            "WHERE source_hash NOT IN (SELECT content_hash FROM memories WHERE deleted_at IS NULL) "
            "OR target_hash NOT IN (SELECT content_hash FROM memories WHERE deleted_at IS NULL)"
        ).fetchone()[0]
        total_edges = conn.execute("SELECT COUNT(*) FROM memory_graph").fetchone()[0]
    except sqlite3.OperationalError:
        orphaned_edges = 0
        total_edges = 0

    return {
        "duplicate_groups": dup_groups,
        "duplicate_group_count": len(dupes),
        "stale_count": stale_count,
        "stale_pct": round(stale_count / active_count * 100, 1) if active_count > 0 else 0.0,
        "orphaned_edges": orphaned_edges,
        "orphan_pct": round(orphaned_edges / max(total_edges, 1) * 100, 1),
    }


def _compute_health_score(
    issues: dict,
    retrieval_perf: dict,
    retrieval_quality: dict,
) -> int:
    """Health score 0-100.

    Formula: 100
      - stale_pct * 30
      - orphan_pct * 20
      - dup_pct * 20
      - (latency_p95 > 500 ? 15 : 0)
      - (empty_search_pct > 40 ? 15 : 0)
    """
    score = 100.0

    # Stale penalty: scale stale_pct (0-100) into 0-30 range
    stale_pct = issues.get("stale_pct", 0) / 100.0  # normalize to 0-1
    score -= stale_pct * 30

    # Orphan penalty: scale orphan_pct (0-100) into 0-20 range
    orphan_pct = issues.get("orphan_pct", 0) / 100.0
    score -= orphan_pct * 20

    # Duplicate penalty: use ratio of dup groups to total (approximate)
    dup_count = issues.get("duplicate_group_count", 0)
    # Cap at 20 groups for max penalty
    dup_pct = min(dup_count / 20.0, 1.0)
    score -= dup_pct * 20

    # Latency penalty
    if retrieval_perf.get("p95_ms", 0) > 500:
        score -= 15

    # Empty search penalty
    if retrieval_quality.get("empty_search_pct", 0) > 40:
        score -= 15

    return max(0, min(100, int(round(score))))


def _section_recommendations(
    health_score: int,
    db_metrics: dict,
    issues: dict,
    retrieval_perf: dict,
    retrieval_quality: dict,
    lifecycle: dict,
) -> list[str]:
    """Section 8: Actionable recommendations based on metrics."""
    recs = []

    if issues.get("stale_pct", 0) > 30:
        recs.append(
            f"Review stale memories: {issues['stale_count']} memories have not been "
            "updated in 90+ days. Run quality audit with --fix to flag dormant entries."
        )

    if issues.get("orphaned_edges", 0) > 10:
        recs.append(
            f"Clean {issues['orphaned_edges']} orphaned graph edges. "
            "Run: hooks/memory-quality-audit.sh --fix"
        )

    if issues.get("duplicate_group_count", 0) > 5:
        recs.append(
            f"Investigate {issues['duplicate_group_count']} duplicate content groups. "
            "Consider consolidating with the consolidation engine."
        )

    if retrieval_perf.get("p95_ms", 0) > 500:
        recs.append(
            f"Retrieval latency p95={retrieval_perf['p95_ms']}ms exceeds 500ms target. "
            "Check if embed daemon is running and responsive."
        )

    if retrieval_quality.get("empty_search_pct", 0) > 40:
        recs.append(
            f"Empty search rate is {retrieval_quality['empty_search_pct']}%. "
            "Consider storing more memories or improving search queries."
        )

    if retrieval_quality.get("sufficiency_pct", 100) < 60:
        recs.append(
            f"Retrieval sufficiency is {retrieval_quality['sufficiency_pct']}%. "
            "Users frequently search manually after auto-retrieval. "
            "Review keyword extraction and embedding quality."
        )

    if db_metrics.get("embedding_coverage_pct", 100) < 90:
        recs.append(
            f"Embedding coverage is {db_metrics['embedding_coverage_pct']}%. "
            "Run embedding backfill to generate missing embeddings."
        )

    if lifecycle.get("dormant", 0) > 20:
        recs.append(
            f"{lifecycle['dormant']} dormant memories detected. "
            "Review and either refresh or archive them."
        )

    if not recs:
        recs.append("All metrics look healthy. No action required.")

    return recs


# ── Output formatters ───────────────────────────────────────────────

def _format_markdown(report: dict) -> str:
    """Render the full report as Markdown."""
    lines: list[str] = []
    ts = report["generated_at"]
    score = report["health_score"]

    # Section 1: Executive Summary
    lines.append(f"# B12 Health Report — {ts}")
    lines.append("")
    db = report["db_metrics"]
    lines.append(f"**Health Score: {score}/100** | "
                 f"Active memories: {db['active']} | "
                 f"Embedding coverage: {db['embedding_coverage_pct']}%")
    lines.append("")

    # Section 2: Database Metrics
    lines.append("## Database Metrics")
    lines.append(f"- Active memories: {db['active']}")
    lines.append(f"- Deleted (tombstones): {db['deleted']}")
    lines.append(f"- Embeddings: {db['embeddings']}")
    lines.append(f"- Graph edges: {db['edges']}")
    lines.append(f"- Embedding coverage: {db['embedding_coverage_pct']}%")
    if db["type_distribution"]:
        lines.append("- Type distribution:")
        for t, c in sorted(db["type_distribution"].items(), key=lambda x: -x[1]):
            lines.append(f"  - {t}: {c}")
    if db["edge_distribution"]:
        lines.append("- Edge types:")
        for t, c in sorted(db["edge_distribution"].items(), key=lambda x: -x[1]):
            lines.append(f"  - {t}: {c}")
    lines.append("")

    # Section 3: Growth Trends
    growth = report["growth_trends"]
    lines.append("## Growth Trends (4 weeks)")
    if growth["weeks"]:
        for w in growth["weeks"]:
            lines.append(f"- {w['date']}: total={w['total']}, new=+{w['new']}")
        lines.append(f"- Average: +{growth['avg_weekly_new']}/week")
        lines.append(f"- Projected 6-month total: ~{growth['projected_6mo_total']}")
    else:
        lines.append("- No growth history data available yet.")
    lines.append("")

    # Section 4: Retrieval Performance
    perf = report["retrieval_perf"]
    lines.append("## Retrieval Performance (last 7 days)")
    if perf["samples"] > 0:
        lines.append(f"- Samples: {perf['samples']}")
        lines.append(f"- p50: {perf['p50_ms']}ms")
        lines.append(f"- p95: {perf['p95_ms']}ms")
        lines.append(f"- p99: {perf['p99_ms']}ms")
        if perf["p95_ms"] > 500:
            lines.append("- **[WARN]** p95 exceeds 500ms target")
    else:
        lines.append("- No retrieval latency data available.")
    lines.append("")

    # Section 5: Retrieval Quality
    qual = report["retrieval_quality"]
    lines.append("## Retrieval Quality")
    if qual["assessed_retrievals"] > 0:
        lines.append(f"- Assessed retrievals: {qual['assessed_retrievals']}")
        lines.append(f"- Sufficient (no follow-up search): {qual['sufficient']} ({qual['sufficiency_pct']}%)")
        lines.append(f"- Insufficient (manual search within 120s): {qual['insufficient']}")
        if qual["sufficiency_pct"] < 60:
            lines.append("- **[WARN]** Low sufficiency — retrieval quality needs improvement")
    else:
        lines.append("- No retrieval quality data available.")
    if qual["searches_total"] > 0:
        lines.append(f"- Searches this week: {qual['searches_total']} ({qual['searches_empty']} empty, {qual['empty_search_pct']}%)")
        if qual["empty_search_pct"] > 40:
            lines.append("- **[WARN]** High empty search rate")
    lines.append("")

    # Section 6: Memory Lifecycle
    lc = report["lifecycle"]
    lines.append("## Memory Lifecycle (this week)")
    lines.append(f"- New: +{lc['new_this_week']}")
    lines.append(f"- Decayed (strength < 1.0, not accessed): {lc['decayed']}")
    lines.append(f"- Dormant (expired valid_until): {lc['dormant']}")
    lines.append(f"- Deleted: {lc['deleted_this_week']}")
    lines.append("")

    # Section 7: Top Issues
    issues = report["top_issues"]
    lines.append("## Top Issues")
    if issues["duplicate_group_count"] > 0:
        lines.append(f"### Duplicate Groups ({issues['duplicate_group_count']})")
        for dg in issues["duplicate_groups"]:
            lines.append(f"- `{dg['preview']}...` ({dg['count']} copies)")
    if issues["stale_count"] > 0:
        lines.append(f"### Stale Memories (>90 days): {issues['stale_count']} ({issues['stale_pct']}%)")
    if issues["orphaned_edges"] > 0:
        lines.append(f"### Orphaned Graph Edges: {issues['orphaned_edges']} ({issues['orphan_pct']}%)")
    if issues["duplicate_group_count"] == 0 and issues["stale_count"] == 0 and issues["orphaned_edges"] == 0:
        lines.append("No issues detected.")
    lines.append("")

    # Section 8: Recommendations
    lines.append("## Recommendations")
    for rec in report["recommendations"]:
        lines.append(f"- {rec}")
    lines.append("")

    return "\n".join(lines)


# ── Main ────────────────────────────────────────────────────────────

def generate_report(db_path: str, output_dir: str, fmt: str) -> dict:
    """Generate the full health report and write to output_dir."""
    log_dir = os.path.join(
        os.environ.get("B12_DATA_DIR", os.path.expanduser("~/.B12")),
        "memory-logs",
    )

    # Connect to DB
    if not os.path.isfile(db_path):
        print(f"ERROR: Database not found at {db_path}", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(db_path, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")

    # Check that memories table exists (required); embeddings/graph are optional
    # (memory_embeddings is a vec0 virtual table, memory_graph may not exist yet)
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table', 'virtual table')"
    ).fetchall()}
    if "memories" not in tables:
        print("ERROR: Required table 'memories' not found in database", file=sys.stderr)
        conn.close()
        sys.exit(1)

    now = datetime.now(timezone.utc)

    # Build all sections
    db_metrics = _section_db_metrics(conn)
    growth_trends = _section_growth_trends(log_dir)
    retrieval_perf = _section_retrieval_perf(log_dir)
    retrieval_quality = _section_retrieval_quality(log_dir)
    lifecycle = _section_lifecycle(conn)
    top_issues = _section_top_issues(conn)

    health_score = _compute_health_score(top_issues, retrieval_perf, retrieval_quality)

    recommendations = _section_recommendations(
        health_score, db_metrics, top_issues,
        retrieval_perf, retrieval_quality, lifecycle,
    )

    conn.close()

    report = {
        "generated_at": now.strftime("%Y-%m-%d %H:%M UTC"),
        "health_score": health_score,
        "db_metrics": db_metrics,
        "growth_trends": growth_trends,
        "retrieval_perf": retrieval_perf,
        "retrieval_quality": retrieval_quality,
        "lifecycle": lifecycle,
        "top_issues": top_issues,
        "recommendations": recommendations,
    }

    # Write output
    os.makedirs(output_dir, exist_ok=True)
    date_str = now.strftime("%Y-%m-%d")

    if fmt == "json":
        out_path = os.path.join(output_dir, f"health-report-{date_str}.json")
        with open(out_path, "w") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
    else:
        out_path = os.path.join(output_dir, f"health-report-{date_str}.md")
        with open(out_path, "w") as f:
            f.write(_format_markdown(report))

    print(f"Health report written to: {out_path}")
    print(f"Health Score: {health_score}/100")

    return report


def main():
    parser = argparse.ArgumentParser(
        description="B12 Health Report — comprehensive weekly memory system report",
    )
    parser.add_argument(
        "--db-path",
        default=None,
        help="Path to SQLite database (default: auto-detect via shared_patterns)",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Directory for report output (default: ~/.B12/memory-logs/)",
    )
    parser.add_argument(
        "--format",
        choices=["md", "json"],
        default="md",
        help="Output format: md (Markdown) or json (default: md)",
    )
    args = parser.parse_args()

    db_path = _resolve_db_path(args.db_path)
    output_dir = args.output_dir or os.path.join(
        os.environ.get("B12_DATA_DIR", os.path.expanduser("~/.B12")),
        "memory-logs",
    )

    generate_report(db_path, output_dir, args.format)


if __name__ == "__main__":
    main()
