#!/usr/bin/env python3
"""Read-only identity and retention audit for live B12 session summaries."""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
import hashlib
import json
import secrets
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Iterator

from shared_patterns import (
    get_db_path,
    is_usable_identity_dimension,
    is_usable_session_id,
)

_NONE = "(none)"
_LEGACY_PREFIX_LENGTH = 12
_OPENCODE_TRUNCATED_ID_EXTRACTORS = frozenset(
    {"session_end_plugin", "macro_verbs"}
)
_PUBLIC_TAG_PREFIXES = {
    "importance",
    "kind",
    "platform",
    "proj",
    "session",
    "source",
    "topic",
    "type",
}
_CATEGORIES = (
    "bound",
    "intentionally_unbound",
    "recoverable_legacy",
    "ambiguous_legacy",
)


def _file_fingerprint(path: Path) -> tuple[int, int, str] | None:
    try:
        stat = path.stat()
        if not path.is_file():
            return None
        with path.open("rb") as handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
    except FileNotFoundError:
        return None
    return stat.st_size, stat.st_mtime_ns, digest


def _source_fingerprints(path: Path) -> dict[str, tuple[int, int, str] | None]:
    return {
        suffix or "db": _file_fingerprint(Path(f"{path}{suffix}"))
        for suffix in ("", "-wal", "-shm", "-journal")
    }


def _rollback_journal_is_active(
    path: Path, fingerprint: tuple[int, int, str] | None
) -> bool:
    """Treat zero-length/zero-header PERSIST journals as inactive."""
    if fingerprint is None or fingerprint[0] == 0:
        return False
    try:
        with Path(f"{path}-journal").open("rb") as handle:
            header = handle.read(8)
    except FileNotFoundError:
        return False
    return any(header)


def _matches_source(
    copied: Path, source_fingerprint: tuple[int, int, str] | None
) -> bool:
    copied_fingerprint = _file_fingerprint(copied)
    if copied_fingerprint is None or source_fingerprint is None:
        return copied_fingerprint is source_fingerprint
    return (
        copied_fingerprint[0] == source_fingerprint[0]
        and copied_fingerprint[2] == source_fingerprint[2]
    )


@contextmanager
def _stable_snapshot(path: Path) -> Iterator[Path]:
    """Copy a stable DB+WAL image without opening or locking the source files."""
    for _attempt in range(5):
        before = _source_fingerprints(path)
        if _rollback_journal_is_active(path, before["-journal"]):
            raise RuntimeError("active rollback journal detected; retry after the writer exits")
        if before["-wal"] is not None and before["-shm"] is None:
            raise RuntimeError("WAL exists without SHM commit-boundary state; retry later")
        with tempfile.TemporaryDirectory(prefix="b12-summary-audit-") as temp_dir:
            snapshot = Path(temp_dir) / "audit.db"
            source_wal = Path(f"{path}-wal")
            source_shm = Path(f"{path}-shm")
            snapshot_wal = Path(f"{snapshot}-wal")
            snapshot_shm = Path(f"{snapshot}-shm")
            try:
                shutil.copyfile(path, snapshot)
                snapshot.chmod(0o600)
                if before["-wal"] is not None:
                    shutil.copyfile(source_wal, snapshot_wal)
                    snapshot_wal.chmod(0o600)
                    shutil.copyfile(source_shm, snapshot_shm)
                    snapshot_shm.chmod(0o600)
            except FileNotFoundError:
                continue
            after = _source_fingerprints(path)
            copies_match = _matches_source(snapshot, before["db"])
            if before["-wal"] is not None:
                copies_match = copies_match and _matches_source(
                    snapshot_wal, before["-wal"]
                )
                copies_match = copies_match and _matches_source(
                    snapshot_shm, before["-shm"]
                )
            if before != after or not copies_match:
                continue
            yield snapshot
            return
    raise RuntimeError("database changed during five snapshot attempts; retry when quieter")


def _metadata(raw: object) -> tuple[dict[str, Any], bool]:
    if raw is None or raw == "":
        return {}, True
    if not isinstance(raw, str):
        return {}, False
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}, False
    if not isinstance(value, dict):
        return {}, False
    return value, True


def _identifier(value: object) -> str | None:
    return value if is_usable_identity_dimension(value) else None


def _session_identifier(value: object) -> str | None:
    return value if is_usable_session_id(value) else None


def _tags(raw: object) -> list[str]:
    if not isinstance(raw, str):
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def _tag_value(tags: list[str], prefix: str) -> str | None:
    values = [tag[len(prefix):] for tag in tags if tag.startswith(prefix)]
    values = [value for value in values if _identifier(value)]
    return values[0] if values else None


def _tag_shape(tags: list[str]) -> str:
    parts: set[str] = set()
    for tag in tags:
        if not tag:
            continue
        if ":" not in tag:
            parts.add("(bare)")
            continue
        prefix = tag.split(":", 1)[0]
        parts.add(prefix if prefix in _PUBLIC_TAG_PREFIXES else "(other_namespaced)")
    return ",".join(sorted(parts)) if parts else _NONE


def _dimension_value(
    metadata: dict[str, Any], tags: list[str], key: str, tag_prefix: str
) -> str | None:
    return _identifier(metadata.get(key)) or _tag_value(tags, tag_prefix)


def _producer_value(metadata: dict[str, Any], tags: list[str]) -> str | None:
    for key in ("producer", "source", "extraction_method"):
        value = _identifier(metadata.get(key))
        if value:
            return value
    return _tag_value(tags, "source:")


def _dimension_label(kind: str, value: str | None, report_key: bytes) -> str:
    """Return a report-local grouping label without exposing the raw value."""
    if not value:
        return _NONE
    digest = hashlib.blake2s(
        value.encode("utf-8"), key=report_key, digest_size=6
    ).hexdigest()
    return f"{kind}#{digest}"


def _age_bucket(value: object, *, now: float) -> str:
    if not isinstance(value, (int, float, str)):
        return "unknown"
    try:
        age_days = max(0.0, (now - float(value)) / 86_400)
    except (TypeError, ValueError):
        return "unknown"
    if age_days < 30:
        return "under_30_days"
    if age_days < 90:
        return "30_to_89_days"
    return "90_days_or_more"


def _recovery_candidate(
    metadata: dict[str, Any],
    tags: list[str],
    full_ids_by_prefix: dict[str, set[str]],
) -> tuple[str | None, str | None, bool]:
    candidates: list[tuple[str, str]] = []
    source_session = _session_identifier(metadata.get("source_session"))
    if source_session:
        candidates.append((source_session, "metadata.source_session"))
    for tag in tags:
        if not tag.startswith("session:"):
            continue
        value = _session_identifier(tag[len("session:"):])
        if value:
            candidates.append((value, "tag.session"))

    values = {value for value, _ in candidates}
    if not values:
        return None, None, False
    if len(values) != 1:
        return None, None, True
    candidate = next(iter(values))
    if (
        len(candidate) == _LEGACY_PREFIX_LENGTH
        and len(full_ids_by_prefix.get(candidate, set())) > 1
    ):
        return None, None, True
    source = next(source for value, source in candidates if value == candidate)
    return candidate, source, True


def _full_ids_by_prefix(conn: sqlite3.Connection) -> dict[str, set[str]]:
    """Index IDs from all recovery surfaces, memory types, and deletion states."""
    prefixes: dict[str, set[str]] = {}

    def _index(session_id: str | None, *, allow_exact_prefix: bool) -> None:
        if not session_id:
            return
        minimum = _LEGACY_PREFIX_LENGTH if allow_exact_prefix else _LEGACY_PREFIX_LENGTH + 1
        if len(session_id) < minimum:
            return
        prefixes.setdefault(
            session_id[:_LEGACY_PREFIX_LENGTH], set()
        ).add(session_id)

    for memory_type, raw_metadata, raw_tags in conn.execute(
        "SELECT memory_type, metadata, tags FROM memories"
    ):
        metadata, _ = _metadata(raw_metadata)
        # A canonical metadata.session_id is already a bound full identity, even
        # when its legitimate value happens to be exactly 12 characters. The two
        # OpenCode SessionEnd extraction paths are a documented exception: they put
        # sessionId.slice(0, 12) on non-summary rows, so those producer fingerprints
        # are legacy prefixes rather than full-ID owners. Recovery surfaces at exact
        # 12 characters are likewise prefixes; indexing either kind would make every
        # unique prefix collide with itself plus its one matching full identity.
        metadata_session_id = _session_identifier(metadata.get("session_id"))
        known_opencode_prefix = bool(
            metadata_session_id
            and len(metadata_session_id) == _LEGACY_PREFIX_LENGTH
            and memory_type != "session_summary"
            and metadata.get("source") == "session_end"
            and metadata.get("extraction_method")
            in _OPENCODE_TRUNCATED_ID_EXTRACTORS
        )
        _index(
            metadata_session_id,
            allow_exact_prefix=not known_opencode_prefix,
        )
        _index(
            _session_identifier(metadata.get("source_session")),
            allow_exact_prefix=False,
        )
        for tag in _tags(raw_tags):
            if tag.startswith("session:"):
                _index(
                    _session_identifier(tag[len("session:"):]),
                    allow_exact_prefix=False,
                )
    return prefixes


def audit_session_summaries(
    conn: sqlite3.Connection, *, now: float | None = None
) -> dict[str, Any]:
    """Classify every active summary without returning content or identity values."""
    rows = conn.execute(
        """
        SELECT id, metadata, tags, created_at, updated_at
        FROM memories
        WHERE memory_type = 'session_summary' AND deleted_at IS NULL
        ORDER BY id
        """
    ).fetchall()
    observed_at = time.time() if now is None else float(now)
    full_ids_by_prefix = _full_ids_by_prefix(conn)
    report_key = secrets.token_bytes(32)

    category_counts = Counter({category: 0 for category in _CATEGORIES})
    unbound_rows: list[dict[str, Any]] = []
    for row in rows:
        row_id = int(row[0])
        metadata, metadata_valid = _metadata(row[1])
        tags = _tags(row[2])
        latest_at = row[4] if row[4] is not None else row[3]
        session_id = _session_identifier(metadata.get("session_id")) if metadata_valid else None
        if session_id:
            category_counts["bound"] += 1
            continue

        marker_present = "session_identity" in metadata
        explicitly_unbound = bool(
            metadata_valid
            and metadata.get("session_identity") == "unbound"
            and _identifier(metadata.get("producer"))
            and _identifier(metadata.get("platform"))
        )
        recovery_value, recovery_source, recovery_evidence_present = _recovery_candidate(
            metadata, tags, full_ids_by_prefix
        )

        if not metadata_valid or (marker_present and not explicitly_unbound):
            category = "ambiguous_legacy"
            recovery_source = None
        elif recovery_value:
            category = "recoverable_legacy"
        elif recovery_evidence_present:
            category = "ambiguous_legacy"
            recovery_source = None
        elif explicitly_unbound:
            category = "intentionally_unbound"
            recovery_source = None
        else:
            category = "ambiguous_legacy"
            recovery_source = None

        platform = _dimension_value(metadata, tags, "platform", "platform:")
        producer = _producer_value(metadata, tags)
        project = _dimension_value(metadata, tags, "project", "proj:")
        category_counts[category] += 1
        unbound_rows.append(
            {
                "id": row_id,
                "category": category,
                "platform": _dimension_label("platform", platform, report_key),
                "producer": _dimension_label("producer", producer, report_key),
                "project": _dimension_label("project", project, report_key),
                "tag_shape": _tag_shape(tags),
                "age_bucket": _age_bucket(latest_at, now=observed_at),
                "recovery_source": recovery_source,
            }
        )

    dimensions: dict[str, dict[str, int]] = {}
    for key in (
        "category",
        "platform",
        "producer",
        "project",
        "tag_shape",
        "age_bucket",
    ):
        dimensions[key] = dict(
            sorted(Counter(row[key] for row in unbound_rows).items())
        )

    return {
        "schema_version": "b12.session_summary_identity_audit.v1",
        "mode": "DRY-RUN (no changes)",
        "active_session_summaries": len(rows),
        "unbound_session_summaries": len(unbound_rows),
        "category_counts": dict(sorted(category_counts.items())),
        "dimensions": dimensions,
        "unbound_rows": unbound_rows,
    }


def render(report: dict[str, Any]) -> str:
    counts = report["category_counts"]
    lines = [
        "B12 session-summary identity audit",
        f"Mode: {report['mode']}",
        f"Active session summaries: {report['active_session_summaries']}",
        f"Unbound session summaries: {report['unbound_session_summaries']}",
        "Identity categories:",
    ]
    for category in _CATEGORIES:
        lines.append(f"  {category}: {counts[category]}")
    lines.append(
        "Use --json for payload-free per-row classifications and report-local dimension counts."
    )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog=(
            "Categories: intentionally_unbound has an explicit marker plus producer/platform; "
            "recoverable_legacy has one non-conflicting structured identity candidate; "
            "ambiguous_legacy has malformed metadata, an incomplete marker, no safe candidate, "
            "or conflicting/colliding candidates. This tool is read-only and never migrates "
            "or deletes rows."
        ),
    )
    parser.add_argument(
        "--db-path", default=get_db_path(), help="override sqlite_vec.db path"
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit per-row JSON with report-local keyed dimensions and no content or identity values",
    )
    args = parser.parse_args(argv)

    path = Path(args.db_path).expanduser().resolve()
    if not path.is_file():
        parser.error(f"database not found: {path}")
    try:
        with _stable_snapshot(path) as snapshot:
            conn = sqlite3.connect(
                snapshot.as_uri() + "?mode=ro", uri=True, timeout=30
            )
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA query_only=ON")
            try:
                report = audit_session_summaries(conn)
            finally:
                conn.close()
    except RuntimeError as exc:
        parser.error(str(exc))
    if args.json:
        json.dump(report, sys.stdout, ensure_ascii=False, sort_keys=True)
        sys.stdout.write("\n")
    else:
        sys.stdout.write(render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
