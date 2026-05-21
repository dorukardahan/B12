import os
import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import dashboard_server


TOKEN = "test-token"


def _init_db(path: Path, *, graph: bool = True) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content_hash TEXT UNIQUE NOT NULL,
            content TEXT NOT NULL,
            memory_type TEXT,
            tags TEXT,
            metadata TEXT,
            strength REAL DEFAULT 1.0,
            created_at REAL,
            updated_at REAL,
            created_at_iso TEXT,
            updated_at_iso TEXT,
            last_accessed_at REAL,
            deleted_at REAL,
            valid_until TEXT
        );
        """
    )
    if graph:
        conn.execute(
            """
            CREATE TABLE memory_graph (
                source_hash TEXT,
                target_hash TEXT,
                relationship_type TEXT,
                similarity REAL
            )
            """
        )
    conn.commit()
    conn.close()


def _insert_memory(path: Path, content_hash: str, content: str, *,
                   created_at: int = 100, updated_at: int = 100,
                   deleted_at=None) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        INSERT INTO memories
        (content_hash, content, memory_type, tags, metadata, strength,
         created_at, updated_at, created_at_iso, updated_at_iso,
         last_accessed_at, deleted_at, valid_until)
        VALUES (?, ?, 'fact', 'proj:alpha', '{}', 1.0, ?, ?, '', '', NULL, ?, NULL)
        """,
        (content_hash, content, created_at, updated_at, deleted_at),
    )
    conn.commit()
    conn.close()


def _client(db_path: Path, *, auth: bool = True):
    app, token = dashboard_server.create_app(str(db_path), token=TOKEN)
    assert token == TOKEN
    app.testing = False
    client = app.test_client()
    if auth:
        client.environ_base["HTTP_AUTHORIZATION"] = f"Bearer {TOKEN}"
    return client


def _content_for(path: Path, content_hash: str) -> str:
    conn = sqlite3.connect(path)
    try:
        return conn.execute(
            "SELECT content FROM memories WHERE content_hash = ?",
            (content_hash,),
        ).fetchone()[0]
    finally:
        conn.close()


def test_negative_limit_uses_default_cap(tmp_path):
    db_path = tmp_path / "memory.sqlite"
    _init_db(db_path)
    for idx in range(60):
        _insert_memory(db_path, f"{idx:064x}", f"memory {idx}", created_at=idx)

    response = _client(db_path).get(f"/api/memories?limit=-1")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["limit"] == dashboard_server.DEFAULT_LIMIT
    assert len(payload["memories"]) == dashboard_server.DEFAULT_LIMIT


def test_index_route_serves_dashboard_and_requires_token(tmp_path, monkeypatch):
    db_path = tmp_path / "memory.sqlite"
    _init_db(db_path)
    scripts_dir = tmp_path / "scripts"
    dashboard_dir = tmp_path / "dashboard"
    scripts_dir.mkdir()
    dashboard_dir.mkdir()
    (dashboard_dir / "dashboard.html").write_text("<html>dashboard</html>")
    monkeypatch.setattr(dashboard_server, "SCRIPT_DIR", str(scripts_dir))
    client = _client(db_path, auth=False)

    missing_token = client.get("/")
    ok = client.get(f"/?token={TOKEN}")

    assert missing_token.status_code == 401
    assert ok.status_code == 200
    assert ok.mimetype == "text/html"
    assert b"dashboard" in ok.data
    assert "b12_dashboard_token=" in ok.headers.get("Set-Cookie", "")


@pytest.mark.parametrize(
    ("method", "path", "json_body"),
    [
        ("get", "/api/memories", None),
        ("get", "/api/stats", None),
        ("get", "/api/graph", None),
        ("get", "/dashboard/dashboard.js", None),
        ("post", "/api/memories/{hash}", {"tags": "proj:blocked"}),
        ("delete", "/api/memories/{hash}", None),
    ],
)
def test_protected_routes_reject_missing_and_wrong_tokens(tmp_path, method, path, json_body):
    db_path = tmp_path / "memory.sqlite"
    _init_db(db_path)
    content_hash = "1" * 64
    _insert_memory(db_path, content_hash, "protected")
    client = _client(db_path, auth=False)
    route = path.format(hash=content_hash)

    missing = getattr(client, method)(route, json=json_body)
    wrong = getattr(client, method)(
        route,
        json=json_body,
        headers={"Authorization": "Bearer wrong-token"},
    )

    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT content, tags, deleted_at FROM memories WHERE content_hash = ?",
            (content_hash,),
        ).fetchone()
    finally:
        conn.close()
    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert row == ("protected", "proj:alpha", None)


def test_unauthorized_dashboard_requests_are_rate_limited(tmp_path):
    db_path = tmp_path / "memory.sqlite"
    _init_db(db_path)
    client = _client(db_path, auth=False)

    responses = [client.get("/?token=wrong") for _ in range(dashboard_server.RATE_LIMIT_MAX + 1)]

    assert all(response.status_code == 401 for response in responses[:-1])
    assert responses[-1].status_code == 429


def test_index_route_reports_missing_dashboard_html(tmp_path, monkeypatch):
    db_path = tmp_path / "memory.sqlite"
    _init_db(db_path)
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    monkeypatch.setattr(dashboard_server, "SCRIPT_DIR", str(scripts_dir))

    response = _client(db_path, auth=False).get(f"/?token={TOKEN}")

    assert response.status_code == 404
    payload = response.get_json()
    assert payload == {"error": "Dashboard not found"}
    assert str(scripts_dir) not in response.get_data(as_text=True)


def test_api_routes_do_not_accept_query_string_token(tmp_path):
    db_path = tmp_path / "memory.sqlite"
    _init_db(db_path)

    response = _client(db_path, auth=False).get(f"/api/memories?token={TOKEN}")

    assert response.status_code == 401


def test_memory_tag_filter_matches_exact_delimited_tag(tmp_path):
    db_path = tmp_path / "memory.sqlite"
    _init_db(db_path)
    alpha = "9" * 64
    alpha2 = "8" * 64
    _insert_memory(db_path, alpha, "alpha")
    _insert_memory(db_path, alpha2, "alpha2")
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE memories SET tags = 'proj:alpha' WHERE content_hash = ?", (alpha,))
    conn.execute("UPDATE memories SET tags = 'proj:alpha2' WHERE content_hash = ?", (alpha2,))
    conn.commit()
    conn.close()

    exact = _client(db_path).get(f"/api/memories?tags=proj:alpha")
    wildcard = _client(db_path).get(f"/api/memories?tags=%")

    assert [m["content_hash"] for m in exact.get_json()["memories"]] == [alpha]
    assert wildcard.get_json()["memories"] == []


def test_edit_rejects_wildcard_and_ambiguous_prefixes(tmp_path):
    db_path = tmp_path / "memory.sqlite"
    _init_db(db_path)
    first = "a" * 64
    second = "a" * 63 + "b"
    _insert_memory(db_path, first, "first")
    _insert_memory(db_path, second, "second")
    client = _client(db_path)

    wildcard = client.post(
        f"/api/memories/%25",
        json={"content": "clobbered"},
    )
    ambiguous = client.post(
        f"/api/memories/a",
        json={"content": "clobbered"},
    )

    assert wildcard.status_code == 400
    assert ambiguous.status_code == 409
    assert _content_for(db_path, first) == "first"
    assert _content_for(db_path, second) == "second"


def test_delete_rejects_wildcard_prefix_without_mutation(tmp_path):
    db_path = tmp_path / "memory.sqlite"
    _init_db(db_path)
    content_hash = "b" * 64
    _insert_memory(db_path, content_hash, "keep")

    response = _client(db_path).delete(f"/api/memories/%25")

    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT deleted_at FROM memories WHERE content_hash = ?",
            (content_hash,),
        ).fetchone()
    finally:
        conn.close()
    assert response.status_code == 400
    assert row[0] is None


def test_delete_full_hash_soft_deletes_and_hides_memory(tmp_path):
    db_path = tmp_path / "memory.sqlite"
    _init_db(db_path)
    content_hash = "6" * 64
    _insert_memory(db_path, content_hash, "delete me", updated_at=100)

    response = _client(db_path).delete(f"/api/memories/{content_hash}")
    listing = _client(db_path).get(f"/api/memories")

    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT deleted_at, updated_at FROM memories WHERE content_hash = ?",
            (content_hash,),
        ).fetchone()
    finally:
        conn.close()
    assert response.status_code == 200
    assert response.get_json()["status"] == "deleted"
    assert response.get_json()["id"] == content_hash
    assert row[0] is not None
    assert row[1] > 100
    assert listing.get_json()["memories"] == []


def test_delete_prefix_response_reports_canonical_hash(tmp_path):
    db_path = tmp_path / "memory.sqlite"
    _init_db(db_path)
    content_hash = "7" * 64
    _insert_memory(db_path, content_hash, "delete me")

    response = _client(db_path).delete(f"/api/memories/{content_hash[:16]}")

    assert response.status_code == 200
    assert response.get_json()["id"] == content_hash


def test_edit_requires_json_object_and_scrubs_content(tmp_path):
    db_path = tmp_path / "memory.sqlite"
    _init_db(db_path)
    content_hash = "c" * 64
    _insert_memory(db_path, content_hash, "safe", updated_at=100)
    client = _client(db_path)

    non_object = client.post(
        f"/api/memories/{content_hash}",
        json=["content"],
    )
    edited = client.post(
        f"/api/memories/{content_hash}",
        json={"content": "api_key=plaintext_secret_value_here_long_enough"},
    )

    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT content, updated_at FROM memories WHERE id = 1",
        ).fetchone()
    finally:
        conn.close()
    assert non_object.status_code == 400
    assert edited.status_code == 200
    assert "plaintext_secret_value_here_long_enough" not in row[0]
    assert "[REDACTED:" in row[0]
    assert row[1] > 100


def test_edit_content_recomputes_hash_and_clears_stale_derived_state(tmp_path):
    db_path = tmp_path / "memory.sqlite"
    _init_db(db_path)
    old_hash = "3" * 64
    target_hash = "4" * 64
    _insert_memory(db_path, old_hash, "old")
    _insert_memory(db_path, target_hash, "target")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE memory_embeddings (rowid INTEGER PRIMARY KEY, content_embedding BLOB)")
    conn.execute("INSERT INTO memory_embeddings VALUES (1, X'0001')")
    conn.execute("INSERT INTO memory_graph VALUES (?, ?, 'related', 0.4)", (old_hash, target_hash))
    conn.execute("INSERT INTO memory_graph VALUES (?, ?, 'related', 0.6)", (target_hash, old_hash))
    conn.execute("UPDATE memories SET metadata = ? WHERE id = 1", (json.dumps({"content_hash": old_hash}),))
    conn.commit()
    conn.close()

    new_content = "new canonical content"
    expected_hash = dashboard_server.content_hash(new_content)
    response = _client(db_path).post(
        f"/api/memories/{old_hash}",
        json={"content": new_content},
    )

    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT content_hash, content, metadata FROM memories WHERE id = 1"
        ).fetchone()
        embedding = conn.execute(
            "SELECT COUNT(*) FROM memory_embeddings WHERE rowid = 1"
        ).fetchone()[0]
        graph_rows = conn.execute("SELECT source_hash, target_hash FROM memory_graph ORDER BY similarity").fetchall()
    finally:
        conn.close()
    assert response.status_code == 200
    assert response.get_json()["id"] == expected_hash
    assert row[0] == expected_hash
    assert row[1] == new_content
    assert json.loads(row[2])["content_hash"] == expected_hash
    assert embedding == 0
    assert graph_rows == [(expected_hash, target_hash), (target_hash, expected_hash)]


def test_edit_content_loads_sqlite_vec_for_embedding_cleanup(tmp_path, monkeypatch):
    db_path = tmp_path / "memory.sqlite"
    _init_db(db_path)
    old_hash = "8" * 64
    _insert_memory(db_path, old_hash, "old")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE memory_embeddings (rowid INTEGER PRIMARY KEY, content_embedding BLOB)")
    conn.execute("INSERT INTO memory_embeddings VALUES (1, X'0001')")
    conn.commit()
    conn.close()
    calls = []

    def fake_load(conn):
        calls.append(conn)
        return True, None

    monkeypatch.setattr(dashboard_server, "try_load_sqlite_vec", fake_load)

    response = _client(db_path).post(
        f"/api/memories/{old_hash}",
        json={"content": "sqlite vec cleanup"},
    )

    assert response.status_code == 200
    assert calls


def test_edit_content_rewrites_graph_with_composite_key_collision(tmp_path):
    db_path = tmp_path / "memory.sqlite"
    _init_db(db_path, graph=False)
    old_hash = "9" * 64
    target_hash = "a" * 64
    new_content = "new collision content"
    expected_hash = dashboard_server.content_hash(new_content)
    _insert_memory(db_path, old_hash, "old")
    _insert_memory(db_path, target_hash, "target")
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE memory_graph (
            source_hash TEXT,
            target_hash TEXT,
            relationship_type TEXT,
            similarity REAL,
            PRIMARY KEY (source_hash, target_hash)
        )
        """
    )
    conn.execute("INSERT INTO memory_graph VALUES (?, ?, 'related', 0.4)", (old_hash, target_hash))
    conn.execute("INSERT INTO memory_graph VALUES (?, ?, 'related', 0.8)", (expected_hash, target_hash))
    conn.commit()
    conn.close()

    response = _client(db_path).post(
        f"/api/memories/{old_hash}",
        json={"content": new_content},
    )

    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("SELECT source_hash, target_hash FROM memory_graph").fetchall()
    finally:
        conn.close()
    assert response.status_code == 200
    assert (old_hash, target_hash) not in rows
    assert rows == [(expected_hash, target_hash)]


def test_edit_content_preserves_self_referential_graph_edge(tmp_path):
    db_path = tmp_path / "memory.sqlite"
    _init_db(db_path, graph=False)
    old_hash = "6" * 64
    new_content = "new self edge content"
    expected_hash = dashboard_server.content_hash(new_content)
    _insert_memory(db_path, old_hash, "old")
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE memory_graph (
            source_hash TEXT,
            target_hash TEXT,
            relationship_type TEXT,
            similarity REAL,
            PRIMARY KEY (source_hash, target_hash)
        )
        """
    )
    conn.execute("INSERT INTO memory_graph VALUES (?, ?, 'related', 1.0)", (old_hash, old_hash))
    conn.commit()
    conn.close()

    response = _client(db_path).post(
        f"/api/memories/{old_hash}",
        json={"content": new_content},
    )

    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("SELECT source_hash, target_hash FROM memory_graph").fetchall()
    finally:
        conn.close()
    assert response.status_code == 200
    assert rows == [(expected_hash, expected_hash)]


def test_edit_content_rolls_back_if_embedding_cleanup_fails(tmp_path):
    db_path = tmp_path / "memory.sqlite"
    _init_db(db_path)
    old_hash = "8" * 64
    _insert_memory(db_path, old_hash, "old", updated_at=100)
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE memory_embeddings (rowid INTEGER PRIMARY KEY, content_embedding BLOB)")
    conn.execute("INSERT INTO memory_embeddings VALUES (1, X'0001')")
    conn.execute(
        """
        CREATE TRIGGER fail_embedding_cleanup
        BEFORE DELETE ON memory_embeddings
        BEGIN
            SELECT RAISE(ABORT, 'cleanup failed');
        END;
        """
    )
    conn.commit()
    conn.close()

    response = _client(db_path).post(
        f"/api/memories/{old_hash}",
        json={"content": "new content"},
    )

    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT content, content_hash FROM memories WHERE id = 1").fetchone()
        embedding_count = conn.execute("SELECT COUNT(*) FROM memory_embeddings WHERE rowid = 1").fetchone()[0]
    finally:
        conn.close()
    assert response.status_code == 500
    assert row == ("old", old_hash)
    assert embedding_count == 1


def test_edit_content_noop_preserves_existing_embedding(tmp_path):
    db_path = tmp_path / "memory.sqlite"
    _init_db(db_path)
    old_hash = "7" * 64
    _insert_memory(db_path, old_hash, "same content", updated_at=100)
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE memory_embeddings (rowid INTEGER PRIMARY KEY, content_embedding BLOB)")
    conn.execute("INSERT INTO memory_embeddings VALUES (1, X'0001')")
    conn.commit()
    conn.close()

    response = _client(db_path).post(
        f"/api/memories/{old_hash}",
        json={"content": "same content"},
    )

    conn = sqlite3.connect(db_path)
    try:
        embedding_count = conn.execute("SELECT COUNT(*) FROM memory_embeddings WHERE rowid = 1").fetchone()[0]
        row = conn.execute("SELECT content, content_hash FROM memories WHERE id = 1").fetchone()
    finally:
        conn.close()
    assert response.status_code == 200
    assert response.get_json()["id"] == old_hash
    assert embedding_count == 1
    assert row == ("same content", old_hash)


def test_edit_tags_and_memory_type_are_scrubbed(tmp_path):
    db_path = tmp_path / "memory.sqlite"
    _init_db(db_path)
    old_hash = "5" * 64
    _insert_memory(db_path, old_hash, "memory")
    secret = "api_key=plaintext_secret_value_here_long_enough"

    response = _client(db_path).post(
        f"/api/memories/{old_hash}",
        json={"tags": ["proj:demo", secret], "memory_type": f"fact-{secret}"},
    )

    conn = sqlite3.connect(db_path)
    try:
        tags, memory_type = conn.execute("SELECT tags, memory_type FROM memories WHERE id = 1").fetchone()
    finally:
        conn.close()
    assert response.status_code == 200
    assert "plaintext_secret_value_here_long_enough" not in tags
    assert "plaintext_secret_value_here_long_enough" not in memory_type
    assert "[REDACTED:" in tags
    assert "[REDACTED:" in memory_type


def test_delete_rejects_if_hash_changes_after_lookup(tmp_path, monkeypatch):
    db_path = tmp_path / "memory.sqlite"
    _init_db(db_path)
    old_hash = "4" * 64
    new_hash = "5" * 64
    _insert_memory(db_path, old_hash, "memory")
    original_lookup = dashboard_server._lookup_memory_id

    def racing_lookup(conn, memory_id):
        row, error = original_lookup(conn, memory_id)
        if row is not None:
            other = sqlite3.connect(db_path)
            try:
                other.execute("UPDATE memories SET content_hash = ? WHERE id = ?", (new_hash, row["id"]))
                other.commit()
            finally:
                other.close()
        return row, error

    monkeypatch.setattr(dashboard_server, "_lookup_memory_id", racing_lookup)

    response = _client(db_path).delete(f"/api/memories/{old_hash}")

    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT content_hash, deleted_at FROM memories WHERE id = 1").fetchone()
    finally:
        conn.close()
    assert response.status_code == 409
    assert row == (new_hash, None)


def test_edit_rejects_if_hash_changes_after_lookup(tmp_path, monkeypatch):
    db_path = tmp_path / "memory.sqlite"
    _init_db(db_path)
    old_hash = "6" * 64
    new_hash = "7" * 64
    _insert_memory(db_path, old_hash, "memory")
    original_lookup = dashboard_server._lookup_memory_id

    def racing_lookup(conn, memory_id):
        row, error = original_lookup(conn, memory_id)
        if row is not None:
            other = sqlite3.connect(db_path)
            try:
                other.execute("UPDATE memories SET content_hash = ? WHERE id = ?", (new_hash, row["id"]))
                other.commit()
            finally:
                other.close()
        return row, error

    monkeypatch.setattr(dashboard_server, "_lookup_memory_id", racing_lookup)

    response = _client(db_path).post(
        f"/api/memories/{old_hash}",
        json={"tags": ["proj:updated"]},
    )

    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT content_hash, tags FROM memories WHERE id = 1").fetchone()
    finally:
        conn.close()
    assert response.status_code == 409
    assert row == (new_hash, "proj:alpha")


def test_stats_tolerates_missing_graph_table(tmp_path):
    db_path = tmp_path / "memory.sqlite"
    _init_db(db_path, graph=False)
    _insert_memory(db_path, "d" * 64, "memory")

    response = _client(db_path).get(f"/api/stats")

    assert response.status_code == 200
    assert response.get_json()["total_graph_edges"] == 0


def test_stats_counts_only_edges_between_active_memories(tmp_path):
    db_path = tmp_path / "memory.sqlite"
    _init_db(db_path)
    source_hash = "1" * 64
    target_hash = "2" * 64
    _insert_memory(db_path, source_hash, "source")
    _insert_memory(db_path, target_hash, "target")
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO memory_graph VALUES (?, ?, 'related', 0.5)", (source_hash, target_hash))
    conn.commit()
    conn.close()

    _client(db_path).delete(f"/api/memories/{target_hash}")
    response = _client(db_path).get(f"/api/stats")

    assert response.status_code == 200
    assert response.get_json()["total_graph_edges"] == 0


def test_stats_clamps_negative_strengths_to_lowest_bin(tmp_path):
    db_path = tmp_path / "memory.sqlite"
    _init_db(db_path)
    _insert_memory(db_path, "5" * 64, "weak")
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE memories SET strength = -10.0")
    conn.commit()
    conn.close()

    response = _client(db_path).get(f"/api/stats")

    assert response.status_code == 200
    assert response.get_json()["strength_histogram"][0]["count"] == 1


def test_stats_defaults_null_strength_to_one_point_zero_bin(tmp_path):
    db_path = tmp_path / "memory.sqlite"
    _init_db(db_path)
    _insert_memory(db_path, "7" * 64, "default strength")
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE memories SET strength = NULL")
    conn.commit()
    conn.close()

    response = _client(db_path).get(f"/api/stats")

    assert response.status_code == 200
    histogram = response.get_json()["strength_histogram"]
    assert histogram[2]["range"] == "1.0-1.5"
    assert histogram[2]["count"] == 1


def test_graph_preserves_zero_similarity_and_filters_edges_in_sql(tmp_path, monkeypatch):
    db_path = tmp_path / "memory.sqlite"
    _init_db(db_path)
    monkeypatch.setattr(dashboard_server, "MAX_GRAPH_NODES", 2)
    source = "e" * 64
    target = "f" * 64
    outside = "0" * 64
    _insert_memory(db_path, source, "source", created_at=3)
    _insert_memory(db_path, target, "target", created_at=2)
    _insert_memory(db_path, outside, "outside", created_at=1)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO memory_graph VALUES (?, ?, 'related', 0.0)",
        (source, target),
    )
    conn.execute(
        "INSERT INTO memory_graph VALUES (?, ?, 'related', 0.9)",
        (outside, source),
    )
    conn.commit()
    conn.close()

    response = _client(db_path).get(f"/api/graph")

    assert response.status_code == 200
    payload = response.get_json()
    assert [edge["data"]["source"] for edge in payload["edges"]] == [source]
    assert payload["edges"][0]["data"]["weight"] == 0.0


def test_sse_event_collector_reports_edits_and_deletes(tmp_path):
    db_path = tmp_path / "memory.sqlite"
    _init_db(db_path)
    edited = "1" * 64
    deleted = "2" * 64
    _insert_memory(db_path, edited, "edited", created_at=100, updated_at=300)
    _insert_memory(db_path, deleted, "deleted", created_at=100, updated_at=320, deleted_at=320)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        collector = getattr(dashboard_server, "_collect_changed_memory_events", None)
        assert callable(collector)
        events = collector(conn, last_check=200, limit=20)
    finally:
        conn.close()

    by_hash = {event["content_hash"]: event for event in events}
    assert by_hash[edited]["event"] == "updated"
    assert by_hash[deleted]["event"] == "deleted"


def test_dashboard_asset_traversal_does_not_match_sibling_prefix(tmp_path, monkeypatch):
    db_path = tmp_path / "memory.sqlite"
    _init_db(db_path)
    scripts_dir = tmp_path / "scripts"
    dashboard_dir = tmp_path / "dashboard"
    sibling_dir = tmp_path / "dashboard_evil"
    scripts_dir.mkdir()
    dashboard_dir.mkdir()
    sibling_dir.mkdir()
    (dashboard_dir / "dashboard.html").write_text("<html></html>")
    (sibling_dir / "secret.txt").write_text("do not serve")
    monkeypatch.setattr(dashboard_server, "SCRIPT_DIR", str(scripts_dir))

    response = _client(db_path).get(
        f"/dashboard/..%2Fdashboard_evil%2Fsecret.txt"
    )

    assert response.status_code == 403
