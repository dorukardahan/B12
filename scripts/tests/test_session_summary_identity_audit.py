from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
SCRIPT = SCRIPTS / "b12_audit_session_summaries.py"
sys.path.insert(0, str(SCRIPTS))
import b12_audit_session_summaries as audit  # noqa: E402


def _db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE memories (
            id INTEGER PRIMARY KEY,
            metadata TEXT,
            tags TEXT,
            memory_type TEXT,
            created_at REAL,
            updated_at REAL,
            deleted_at REAL
        )
        """
    )
    return conn


def _add(
    conn: sqlite3.Connection,
    row_id: int,
    metadata: object,
    tags: str,
    *,
    age_days: int = 5,
    memory_type: str = "session_summary",
    deleted_at: float | None = None,
) -> None:
    raw = metadata if isinstance(metadata, str) else json.dumps(metadata)
    created_at = time.time() - age_days * 86_400
    conn.execute(
        "INSERT INTO memories VALUES (?, ?, ?, ?, ?, ?, ?)",
        (row_id, raw, tags, memory_type, created_at, created_at, deleted_at),
    )


def _sqlite_snapshot(path: Path) -> dict[str, bytes | None]:
    snapshot: dict[str, bytes | None] = {}
    for suffix in ("", "-wal", "-shm", "-journal"):
        candidate = Path(f"{path}{suffix}")
        snapshot[suffix or "db"] = candidate.read_bytes() if candidate.exists() else None
    return snapshot


def test_read_only_audit_classifies_every_active_unbound_summary(tmp_path: Path) -> None:
    assert SCRIPT.is_file(), "read-only session-summary identity audit CLI is missing"
    path = tmp_path / "summaries.db"
    conn = _db(path)
    _add(conn, 1, {"session_id": "stable-session-alpha"}, "proj:alpha")
    _add(conn, 2, {"session_id": "stable-session-beta"}, "proj:alpha")
    _add(
        conn,
        3,
        {
            "session_identity": "unbound",
            "producer": "mcp_session_tracker",
            "platform": "mcp-only",
            "project": "alpha",
        },
        "proj:alpha,type:session_summary,source:mcp,platform:mcp-only",
    )
    _add(
        conn,
        4,
        {"source_session": "legacy-source-full", "platform": "claude"},
        "proj:alpha,session-summary",
        age_days=45,
    )
    _add(
        conn,
        5,
        {"platform": "codex"},
        "proj:beta,session:tag-session-full,session-summary",
        age_days=120,
    )
    _add(
        conn,
        6,
        {"platform": "legacy"},
        "proj:alpha,private-session-id-prefix:value,session-summary",
    )
    _add(
        conn,
        7,
        {"source_session": "source-a"},
        "proj:alpha,session:source-b,session-summary",
    )
    _add(
        conn,
        8,
        {"source_session": "123456789012"},
        "proj:alpha,session-summary",
    )
    _add(
        conn,
        9,
        {
            "session_identity": "unbound",
            "platform": "mcp-only",
            "source_session": "must-not-recover-incomplete-marker",
        },
        "proj:alpha,session-summary",
    )
    _add(
        conn,
        10,
        "{broken",
        "proj:alpha,session:must-not-recover-malformed-metadata,session-summary",
    )
    _add(
        conn,
        11,
        {
            "session_identity": "unbound",
            "producer": "mcp_session_tracker",
            "platform": "mcp-only",
        },
        "proj:deleted,session-summary",
        deleted_at=1.0,
    )
    _add(
        conn,
        12,
        {"session_id": "123456789012-from-non-summary"},
        "proj:other",
        memory_type="decision",
    )
    _add(
        conn,
        13,
        {"session_id": "123456789012-from-deleted-summary"},
        "proj:deleted,session-summary",
        deleted_at=1.0,
    )
    conn.commit()
    conn.close()
    before = _sqlite_snapshot(path)

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--db-path", str(path), "--json"],
        text=True,
        capture_output=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["mode"] == "DRY-RUN (no changes)"
    assert report["active_session_summaries"] == 10
    assert report["unbound_session_summaries"] == 8
    assert report["category_counts"] == {
        "ambiguous_legacy": 5,
        "bound": 2,
        "intentionally_unbound": 1,
        "recoverable_legacy": 2,
    }
    assert [row["id"] for row in report["unbound_rows"]] == list(range(3, 11))
    assert {row["category"] for row in report["unbound_rows"]} == {
        "ambiguous_legacy",
        "intentionally_unbound",
        "recoverable_legacy",
    }
    by_id = {row["id"]: row for row in report["unbound_rows"]}
    assert by_id[3]["category"] == "intentionally_unbound"
    assert by_id[3]["age_bucket"] == "under_30_days"
    assert by_id[3]["tag_shape"] == "platform,proj,source,type"
    assert by_id[3]["platform"].startswith("platform#")
    assert by_id[3]["producer"].startswith("producer#")
    assert by_id[3]["project"].startswith("project#")
    assert by_id[3]["recovery_source"] is None
    assert by_id[4]["category"] == "recoverable_legacy"
    assert by_id[4]["recovery_source"] == "metadata.source_session"
    assert by_id[4]["age_bucket"] == "30_to_89_days"
    assert by_id[5]["category"] == "recoverable_legacy"
    assert by_id[5]["recovery_source"] == "tag.session"
    assert by_id[5]["age_bucket"] == "90_days_or_more"
    assert by_id[7]["category"] == "ambiguous_legacy"
    assert by_id[8]["category"] == "ambiguous_legacy"
    assert by_id[9]["category"] == "ambiguous_legacy"
    assert by_id[9]["recovery_source"] is None
    assert by_id[10]["category"] == "ambiguous_legacy"
    assert by_id[10]["recovery_source"] is None
    assert _sqlite_snapshot(path) == before

    serialized = json.dumps(report, sort_keys=True)
    for private_value in (
        "legacy-source-full",
        "tag-session-full",
        "mcp_session_tracker",
        "mcp-only",
        "alpha",
        "must-not-recover",
        "private-session-id-prefix",
    ):
        assert private_value not in serialized


def test_dimension_labels_are_consistent_only_within_one_report(tmp_path: Path) -> None:
    path = tmp_path / "unlinkable-labels.db"
    conn = _db(path)
    for row_id in (1, 2):
        _add(
            conn,
            row_id,
            {
                "session_identity": "unbound",
                "producer": "low-entropy-producer",
                "platform": "low-entropy-platform",
                "project": "low-entropy-project",
            },
            "session-summary",
        )
    conn.commit()

    first = audit.audit_session_summaries(conn)
    second = audit.audit_session_summaries(conn)
    conn.close()

    for dimension in ("producer", "platform", "project"):
        assert first["unbound_rows"][0][dimension] == first["unbound_rows"][1][dimension]
        assert first["unbound_rows"][0][dimension] != second["unbound_rows"][0][dimension]


def test_exact_twelve_character_bound_id_participates_in_prefix_collision_check(
    tmp_path: Path,
) -> None:
    path = tmp_path / "exact-prefix-collision.db"
    conn = _db(path)
    _add(conn, 1, {"session_id": "123456789012"}, "session-summary")
    _add(
        conn,
        2,
        {"session_id": "123456789012-longer-bound-id"},
        "decision",
        memory_type="decision",
    )
    _add(conn, 3, {"source_session": "123456789012"}, "session-summary")
    conn.commit()

    report = audit.audit_session_summaries(conn)
    conn.close()

    by_id = {row["id"]: row for row in report["unbound_rows"]}
    assert by_id[3]["category"] == "ambiguous_legacy"
    assert by_id[3]["recovery_source"] is None


def test_structured_recovery_evidence_takes_precedence_over_complete_unbound_marker(
    tmp_path: Path,
) -> None:
    path = tmp_path / "marker-recovery-conflict.db"
    conn = _db(path)
    marker = {
        "session_identity": "unbound",
        "producer": "legacy-producer",
        "platform": "legacy-platform",
    }
    _add(conn, 1, {**marker, "source_session": "authoritative-session"}, "session-summary")
    _add(
        conn,
        2,
        {**marker, "source_session": "candidate-a"},
        "session:candidate-b,session-summary",
    )
    conn.commit()

    report = audit.audit_session_summaries(conn)
    conn.close()

    by_id = {row["id"]: row for row in report["unbound_rows"]}
    assert by_id[1]["category"] == "recoverable_legacy"
    assert by_id[1]["recovery_source"] == "metadata.source_session"
    assert by_id[2]["category"] == "ambiguous_legacy"
    assert by_id[2]["recovery_source"] is None


def test_multiple_structured_session_tags_are_ambiguous(tmp_path: Path) -> None:
    path = tmp_path / "multiple-session-tags.db"
    conn = _db(path)
    _add(
        conn,
        1,
        {},
        "session:candidate-a,session:candidate-b,session-summary",
    )
    conn.commit()

    report = audit.audit_session_summaries(conn)
    conn.close()

    assert report["unbound_rows"][0]["category"] == "ambiguous_legacy"
    assert report["unbound_rows"][0]["recovery_source"] is None


def test_inactive_persistent_rollback_journal_does_not_block_snapshot(
    tmp_path: Path,
) -> None:
    path = tmp_path / "persistent-journal.db"
    conn = _db(path)
    _add(
        conn,
        1,
        {"session_identity": "unbound", "producer": "p", "platform": "x"},
        "",
    )
    conn.commit()
    conn.close()
    journal = Path(f"{path}-journal")
    journal.write_bytes(bytes(512))
    before = _sqlite_snapshot(path)

    with audit._stable_snapshot(path) as snapshot:
        snapshot_conn = sqlite3.connect(snapshot.as_uri() + "?mode=ro", uri=True)
        try:
            report = audit.audit_session_summaries(snapshot_conn)
        finally:
            snapshot_conn.close()

    assert report["unbound_session_summaries"] == 1
    assert _sqlite_snapshot(path) == before


def test_nonzero_rollback_journal_header_blocks_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "active-journal.db"
    conn = _db(path)
    conn.commit()
    conn.close()
    Path(f"{path}-journal").write_bytes(b"active!!" + bytes(504))

    try:
        with audit._stable_snapshot(path):
            raise AssertionError("snapshot unexpectedly accepted a nonzero journal header")
    except RuntimeError as exc:
        assert "active rollback journal detected" in str(exc)


def test_wal_audit_reads_committed_rows_without_changing_database_or_sidecars(
    tmp_path: Path,
) -> None:
    path = tmp_path / "wal.db"
    conn = _db(path)
    assert conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    _add(
        conn,
        1,
        {
            "session_identity": "unbound",
            "producer": "mcp_session_tracker",
            "platform": "mcp-only",
        },
        "session-summary",
    )
    conn.commit()

    before = _sqlite_snapshot(path)
    assert before["-wal"] is not None
    assert before["-shm"] is not None
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--db-path", str(path), "--json"],
        text=True,
        capture_output=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["unbound_session_summaries"] == 1
    assert _sqlite_snapshot(path) == before
    conn.close()


def test_snapshot_retries_when_writer_commits_during_bundle_copy(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "concurrent.db"
    writer = _db(path)
    assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    _add(
        writer,
        1,
        {
            "session_identity": "unbound",
            "producer": "mcp_session_tracker",
            "platform": "mcp-only",
        },
        "session-summary",
    )
    writer.commit()

    original_copyfile = audit.shutil.copyfile
    state: dict[str, object] = {"triggered": False, "committed_source": None}

    def copyfile_with_commit(source, destination):
        result = original_copyfile(source, destination)
        if Path(source) == Path(f"{path}-wal") and not state["triggered"]:
            state["triggered"] = True
            _add(
                writer,
                2,
                {
                    "session_identity": "unbound",
                    "producer": "mcp_session_tracker",
                    "platform": "mcp-only",
                },
                "session-summary",
            )
            writer.commit()
            state["committed_source"] = _sqlite_snapshot(path)
        return result

    monkeypatch.setattr(audit.shutil, "copyfile", copyfile_with_commit)
    with audit._stable_snapshot(path) as snapshot:
        snapshot_conn = sqlite3.connect(
            snapshot.as_uri() + "?mode=ro", uri=True, timeout=10
        )
        snapshot_conn.execute("PRAGMA query_only=ON")
        try:
            report = audit.audit_session_summaries(snapshot_conn)
        finally:
            snapshot_conn.close()

    assert state["triggered"] is True
    assert report["active_session_summaries"] == 2
    assert report["unbound_session_summaries"] == 2
    assert _sqlite_snapshot(path) == state["committed_source"]
    writer.close()


def test_snapshot_retries_when_wal_sidecars_disappear_during_fingerprint(
    tmp_path: Path, monkeypatch
) -> None:
    for close_on_call in (1, 2):
        path = tmp_path / f"sidecar-race-{close_on_call}.db"
        writer = _db(path)
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        _add(
            writer,
            1,
            {
                "session_identity": "unbound",
                "producer": "mcp_session_tracker",
                "platform": "mcp-only",
            },
            "session-summary",
        )
        writer.commit()

        source_wal = Path(f"{path}-wal")
        original_fingerprint = audit._file_fingerprint
        calls = 0
        active_writer: sqlite3.Connection | None = writer

        def fingerprint_with_close(target: Path):
            nonlocal calls, active_writer
            if target == source_wal:
                calls += 1
                if calls == close_on_call:
                    assert active_writer is not None
                    active_writer.close()
                    active_writer = None
            return original_fingerprint(target)

        with monkeypatch.context() as scoped_patch:
            scoped_patch.setattr(audit, "_file_fingerprint", fingerprint_with_close)
            with audit._stable_snapshot(path) as snapshot:
                snapshot_conn = sqlite3.connect(
                    snapshot.as_uri() + "?mode=ro", uri=True, timeout=10
                )
                snapshot_conn.execute("PRAGMA query_only=ON")
                try:
                    report = audit.audit_session_summaries(snapshot_conn)
                finally:
                    snapshot_conn.close()

        assert calls >= close_on_call
        assert report["active_session_summaries"] == 1
        assert report["unbound_session_summaries"] == 1
        if active_writer is not None:
            active_writer.close()


def test_help_states_identity_and_retention_safety_contract() -> None:
    assert SCRIPT.is_file(), "read-only session-summary identity audit CLI is missing"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0
    help_text = " ".join(result.stdout.split())
    assert "intentionally_unbound" in help_text
    assert "recoverable_legacy" in help_text
    assert "ambiguous_legacy" in help_text
    assert "never migrates or deletes rows" in help_text
    assert "--execute" not in help_text
