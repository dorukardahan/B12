#!/usr/bin/env python3
"""Dry-run-first cleanup of duplicate live session summaries."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

from shared_patterns import get_db_path
from write_time_merge import select_session_summary_canonical

_NONE = "(none)"
_CHUNK = 500

def _identity(raw):
    try:
        metadata = json.loads(raw or "{}")
    except (json.JSONDecodeError, TypeError):
        return None, _NONE
    if not isinstance(metadata, dict):
        return None, _NONE
    sid = metadata.get("session_id")
    sid = sid.strip() if isinstance(sid, str) else ""
    platform = metadata.get("platform")
    platform = platform.strip() if isinstance(platform, str) else ""
    return (sid or None), (platform or _NONE)

def _plan(conn: sqlite3.Connection) -> dict:
    groups, platforms, no_sid = {}, {}, 0
    rows = conn.execute(
        "SELECT id, metadata FROM memories WHERE memory_type='session_summary' "
        "AND deleted_at IS NULL ORDER BY id").fetchall()
    for row_id, metadata in rows:
        sid, platform = _identity(metadata)
        if sid is None:
            no_sid += 1
            continue
        groups.setdefault(sid, []).append((int(row_id), platform))
        stat = platforms.setdefault(platform, {"rows": 0, "sessions": set(), "dupes": set(), "remove": 0})
        stat["rows"] += 1
        stat["sessions"].add(sid)

    plans = []
    for sid in sorted(groups):
        members = groups[sid]
        if len(members) < 2:
            continue
        chosen = select_session_summary_canonical(conn, session_id=sid, content_hash=None)
        keep = int(chosen[0]) if chosen else -1
        member_ids = {row_id for row_id, _ in members}
        if keep not in member_ids:
            raise RuntimeError(f"canonical row for session {sid!r} is outside its exact-ID group")
        remove = [row_id for row_id, _ in members if row_id != keep]
        for row_id, platform in members:
            platforms[platform]["dupes"].add(sid)
            if row_id in remove:
                platforms[platform]["remove"] += 1
        plans.append({"sid": sid, "platforms": sorted({p for _, p in members}),
                      "keep": keep, "remove": remove})
    return {"plans": plans, "platforms": platforms, "no_sid": no_sid, "remove_count": sum(len(p["remove"]) for p in plans)}

def deduplicate(conn: sqlite3.Connection, *, execute: bool = False, now=None) -> dict:
    if execute and conn.in_transaction:
        raise RuntimeError("execute requires a connection without an active transaction")
    owns_txn = not conn.in_transaction
    if owns_txn:
        conn.execute("BEGIN IMMEDIATE" if execute else "BEGIN")
    try:
        report = _plan(conn)
        if execute:
            ids = [row_id for plan in report["plans"] for row_id in plan["remove"]]
            stamp = time.time() if now is None else now
            changed = 0
            for offset in range(0, len(ids), _CHUNK):
                batch = ids[offset:offset + _CHUNK]
                marks = ",".join("?" for _ in batch)
                changed += conn.execute(
                    f"UPDATE memories SET deleted_at=? "
                    f"WHERE deleted_at IS NULL AND id IN ({marks})",
                    [stamp, *batch],
                ).rowcount
            if changed != report["remove_count"] or _plan(conn)["remove_count"]:
                raise RuntimeError("dedupe postcondition failed; transaction rolled back")
        if owns_txn:
            conn.execute("COMMIT")
        return report
    except BaseException:
        if owns_txn and conn.in_transaction:
            conn.execute("ROLLBACK")
        raise

def render(report: dict, *, execute: bool) -> str:
    lines = ["B12 session-summary dedupe",
             "Mode: EXECUTE" if execute else "Mode: DRY-RUN (no changes)",
             "Session plans:"]
    if report["plans"]:
        for plan in report["plans"]:
            lines.append(
                f"  sid={plan['sid']} platforms={','.join(plan['platforms'])} "
                f"keep={plan['keep']} remove={','.join(map(str, plan['remove']))}"
            )
    else:
        lines.append("  none")
    lines.append("Platform totals:")
    for platform in sorted(report["platforms"]):
        stat = report["platforms"][platform]
        lines.append(
            f"  {platform}: rows={stat['rows']} sessions={len(stat['sessions'])} "
            f"duplicate_sessions={len(stat['dupes'])} "
            f"keep={stat['rows'] - stat['remove']} remove={stat['remove']}"
        )
    lines.append(f"No-session-id live rows: {report['no_sid']} (untouched)")
    count, sessions = report["remove_count"], len(report["plans"])
    if not count:
        lines.append("No duplicate live session summaries found; database unchanged.")
    elif execute:
        lines.append(f"Soft-deleted {count} rows across {sessions} sessions.")
    else:
        lines.extend((f"Would soft-delete {count} rows across {sessions} sessions.", "Re-run with --execute to apply."))
    return "\n".join(lines) + "\n"

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="apply the reported soft deletes")
    parser.add_argument("--db-path", default=get_db_path(), help="override sqlite_vec.db path")
    args = parser.parse_args(argv)
    path = Path(args.db_path).expanduser().resolve()
    if not path.is_file():
        parser.error(f"database not found: {path}")
    uri = path.as_uri() + "?mode=ro"
    conn = sqlite3.connect(str(path) if args.execute else uri, uri=not args.execute, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    if not args.execute:
        conn.execute("PRAGMA query_only=ON")
    try:
        report = deduplicate(conn, execute=args.execute)
    finally:
        conn.close()
    sys.stdout.write(render(report, execute=args.execute))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
