import importlib
import json
import multiprocessing as mp
import os
import pickle
import sqlite3
import subprocess
import sys
import tempfile
import types
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import surfacing_engine
import embed_daemon
import b12_long_session
import b12_token_budget
import write_time_merge


def _surfacing_rate_limit_process(state_path: str, queue) -> None:
    try:
        allowed = surfacing_engine.check_rate_limit(state_path, True)
        queue.put((True, allowed, None))
    except Exception as exc:
        queue.put((False, False, repr(exc)))


@pytest.fixture
def train_classifier():
    return importlib.import_module("train_classifier")


@pytest.fixture
def train_classifier_head_pkl():
    return importlib.import_module("train_classifier_head_pkl")


def test_surface_increments_fresh_rate_limit_state(tmp_path):
    state_path = tmp_path / "surfacing-state.json"

    result = surfacing_engine.surface(
        "topic",
        "database migration",
        db_path=str(tmp_path / "missing.sqlite"),
        state_path=str(state_path),
    )

    assert result.surfaced is False
    state = json.loads(state_path.read_text())
    assert state["tool_calls_since"] == 1


def test_surface_counts_each_allowed_attempt_once(tmp_path, monkeypatch):
    state_path = tmp_path / "surfacing-state.json"
    state_path.write_text(json.dumps({
        "last_surfaced_at": 0,
        "tool_calls_since": surfacing_engine.RATE_LIMIT_TOOL_CALLS - 1,
        "surfaced_ids": [],
    }))
    monkeypatch.setattr(
        surfacing_engine,
        "_daemon_search",
        lambda *args, **kwargs: [{"id": 1, "score": 0.1}],
    )

    result = surfacing_engine.surface(
        "topic",
        "database migration",
        db_path=str(tmp_path / "missing.sqlite"),
        state_path=str(state_path),
    )

    state = json.loads(state_path.read_text())
    assert result.surfaced is False
    assert state["tool_calls_since"] == surfacing_engine.RATE_LIMIT_TOOL_CALLS


def test_surfacing_rate_limit_updates_are_serialized(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    state_path = tmp_path / "surfacing-state.json"
    monkeypatch.setattr(surfacing_engine, "RATE_LIMIT_TOOL_CALLS", 1000)
    monkeypatch.setattr(surfacing_engine, "RATE_LIMIT_COOLDOWN", 0)

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [
            executor.submit(surfacing_engine.check_rate_limit, str(state_path), True)
            for _ in range(25)
        ]
        for future in futures:
            future.result()

    state = json.loads(state_path.read_text())
    assert state["tool_calls_since"] == 25
    assert not list(tmp_path.glob("*.tmp"))


def test_surfacing_rate_limit_updates_are_serialized_across_processes(tmp_path, monkeypatch):
    state_path = tmp_path / "surfacing-state.json"
    monkeypatch.setattr(surfacing_engine, "RATE_LIMIT_TOOL_CALLS", 1000)
    monkeypatch.setattr(surfacing_engine, "RATE_LIMIT_COOLDOWN", 0)
    queue = mp.Queue()
    processes = [
        mp.Process(target=_surfacing_rate_limit_process, args=(str(state_path), queue))
        for _ in range(8)
    ]

    for proc in processes:
        proc.start()
    for proc in processes:
        proc.join(10)

    results = [queue.get(timeout=2) for _ in processes]
    assert all(proc.exitcode == 0 for proc in processes)
    assert all(ok for ok, _allowed, _err in results)
    state = json.loads(state_path.read_text())
    assert state["tool_calls_since"] == 8
    assert not list(tmp_path.glob("*.tmp"))


def test_zero_strength_memory_is_filtered(tmp_path, monkeypatch):
    state_path = tmp_path / "surfacing-state.json"
    state_path.write_text(json.dumps({
        "last_surfaced_at": 0,
        "tool_calls_since": surfacing_engine.RATE_LIMIT_TOOL_CALLS - 1,
        "surfaced_ids": [],
    }))
    monkeypatch.setattr(
        surfacing_engine,
        "_daemon_search",
        lambda *args, **kwargs: [{"id": 1, "score": 0.99, "content": "candidate"}],
    )
    monkeypatch.setattr(
        surfacing_engine,
        "_get_memory_infos_batch",
        lambda db_path, ids: {
            1: {
                "strength": 0.0,
                "created_at": 0,
                "deleted_at": None,
                "valid_until": None,
                "content": "candidate",
                "memory_type": "fact",
                "tags": "",
            }
        },
    )

    result = surfacing_engine.surface(
        "topic",
        "database migration",
        db_path=str(tmp_path / "missing.sqlite"),
        state_path=str(state_path),
    )

    assert result.surfaced is False
    assert result.reason == "No memories passed filters"


def test_sqlite_datetime_valid_until_is_not_expired_before_same_day_deadline():
    checker = getattr(surfacing_engine, "_is_expired", None)
    assert callable(checker)

    assert checker(
        "2026-05-21 23:59:59",
        now=1_779_346_800,  # 2026-05-21 10:00:00 UTC
    ) is False


def test_full_trainer_honors_data_dir_and_corpus_env(monkeypatch, tmp_path, train_classifier):
    original_model_dir = train_classifier.MODEL_DIR

    with monkeypatch.context() as env:
        env.setenv("B12_DATA_DIR", str(tmp_path / "data"))
        env.setenv("B12_CORPUS_PATH", str(tmp_path / "custom-corpus.json"))

        reloaded = importlib.reload(train_classifier)

        assert reloaded.DATA_PATH == tmp_path / "custom-corpus.json"
        assert reloaded.MODEL_DIR == tmp_path / "data" / "models" / "setfit-memory-classifier"
        assert reloaded.ONNX_DIR == tmp_path / "data" / "models" / "setfit-memory-classifier-onnx"

    restored = importlib.reload(train_classifier)
    assert restored.MODEL_DIR == original_model_dir


def test_trainer_corpus_env_does_not_evaluate_tempdir_default(monkeypatch, tmp_path):
    sys.modules.pop("train_classifier", None)
    sys.modules.pop("train_classifier_head_pkl", None)
    with monkeypatch.context() as env:
        env.setenv("B12_CORPUS_PATH", str(tmp_path / "corpus.json"))
        env.setattr(tempfile, "gettempdir", lambda: (_ for _ in ()).throw(AssertionError("tempdir evaluated")))
        reloaded = importlib.import_module("train_classifier")
        assert reloaded.DATA_PATH == tmp_path / "corpus.json"

    sys.modules.pop("train_classifier_head_pkl", None)
    with monkeypatch.context() as env:
        env.setenv("B12_CORPUS_PATH", str(tmp_path / "head-corpus.json"))
        env.setattr(
            tempfile,
            "gettempdir",
            lambda: (_ for _ in ()).throw(AssertionError("tempdir evaluated")),
        )
        reloaded_head = importlib.import_module("train_classifier_head_pkl")
        assert reloaded_head.DATA_PATH == tmp_path / "head-corpus.json"

    sys.modules.pop("train_classifier", None)
    sys.modules.pop("train_classifier_head_pkl", None)


def test_manual_onnx_fallback_fails_fast_with_clear_contract(tmp_path, train_classifier):
    with pytest.raises(RuntimeError, match="native SetFit ONNX export"):
        train_classifier._manual_onnx_export(object(), tmp_path / "model.onnx")


def test_trainer_rejects_empty_train_or_test_split(tmp_path, train_classifier):
    corpus = tmp_path / "corpus.json"
    corpus.write_text(json.dumps([
        {"content_preview": "only test", "proposed_label": "decision", "split": "test"}
    ]))

    with pytest.raises(SystemExit):
        train_classifier.load_data(corpus)


def test_trainer_rejects_incomplete_train_label_coverage(tmp_path, train_classifier):
    corpus = tmp_path / "corpus.json"
    corpus.write_text(json.dumps([
        {"content_preview": "a", "proposed_label": "decision", "split": "train"},
        {"content_preview": "b", "proposed_label": "error_fix", "split": "train"},
        {"content_preview": "c", "proposed_label": "decision", "split": "test"},
    ]))

    with pytest.raises(SystemExit):
        train_classifier.load_data(corpus)


def test_trainer_rejects_non_object_or_empty_content_items(tmp_path, train_classifier):
    non_object = tmp_path / "non-object.json"
    non_object.write_text(json.dumps([42]))

    with pytest.raises(SystemExit):
        train_classifier.load_data(non_object)

    empty_content = tmp_path / "empty-content.json"
    empty_content.write_text(json.dumps([
        {"content_preview": "", "proposed_label": "decision", "split": "train"},
    ]))

    with pytest.raises(SystemExit):
        train_classifier.load_data(empty_content)


def test_trainer_reports_malformed_corpus_json(tmp_path, capsys, train_classifier):
    corpus = tmp_path / "bad.json"
    corpus.write_text("{not-json")

    with pytest.raises(SystemExit):
        train_classifier.load_data(corpus)

    assert "Could not parse data file" in capsys.readouterr().out


def test_head_pickle_trainer_reuses_corpus_validation_before_model_load(
    tmp_path,
    monkeypatch,
    train_classifier_head_pkl,
):
    corpus = tmp_path / "corpus.json"
    corpus.write_text(json.dumps([
        {"content_preview": "a", "proposed_label": "decision", "split": "train"},
        {"content_preview": "b", "proposed_label": "decision", "split": "test"},
        {"content_preview": "bad", "proposed_label": "decision", "split": "trian"},
    ]))
    monkeypatch.setattr(train_classifier_head_pkl, "DATA_PATH", corpus)
    monkeypatch.setitem(
        sys.modules,
        "sentence_transformers",
        types.SimpleNamespace(
            SentenceTransformer=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("model loaded")),
        ),
    )

    with pytest.raises(SystemExit):
        train_classifier_head_pkl.main()


def test_full_trainer_publish_rolls_back_both_artifact_dirs_on_second_failure(
    tmp_path,
    monkeypatch,
    train_classifier,
):
    old_model = tmp_path / "model"
    old_onnx = tmp_path / "onnx"
    new_model = tmp_path / "new-model"
    new_onnx = tmp_path / "new-onnx"
    for path, marker in (
        (old_model, "old-model"),
        (old_onnx, "old-onnx"),
        (new_model, "new-model"),
        (new_onnx, "new-onnx"),
    ):
        path.mkdir()
        (path / "marker.txt").write_text(marker)

    original_replace = train_classifier.os.replace

    def flaky_replace(src, dest):
        src_path = Path(src)
        if dest == old_onnx and ".onnx.publish-" in src_path.name:
            raise OSError("publish failed")
        return original_replace(src, dest)

    monkeypatch.setattr(train_classifier.os, "replace", flaky_replace)

    with pytest.raises(OSError):
        train_classifier._publish_artifact_dirs([(new_model, old_model), (new_onnx, old_onnx)])

    assert (old_model / "marker.txt").read_text() == "old-model"
    assert (old_onnx / "marker.txt").read_text() == "old-onnx"


def test_full_trainer_publish_uses_artifact_lock(tmp_path, monkeypatch, train_classifier):
    model = tmp_path / "new-model"
    onnx = tmp_path / "new-onnx"
    dest_model = tmp_path / "model"
    dest_onnx = tmp_path / "onnx"
    for path in (model, onnx):
        path.mkdir()
        (path / "marker.txt").write_text(path.name)
    seen = []

    class DummyLock:
        def __init__(self, lock_dir):
            self.lock_dir = lock_dir

        def __enter__(self):
            seen.append(self.lock_dir)

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(train_classifier, "_artifact_publish_lock", lambda lock_dir: DummyLock(lock_dir))

    train_classifier._publish_artifact_dirs([(model, dest_model), (onnx, dest_onnx)])

    assert seen == [tmp_path]


def test_trainer_dependency_check_does_not_auto_install(monkeypatch, train_classifier):
    calls = []
    real_import = __import__

    def fake_import(name, *args, **kwargs):
        if name == "setfit":
            raise ImportError("missing")
        return real_import(name, *args, **kwargs)

    def fail_process_call(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("install side effect attempted")

    monkeypatch.setattr("builtins.__import__", fake_import)
    monkeypatch.setattr(os, "system", fail_process_call)
    monkeypatch.setattr(subprocess, "run", fail_process_call)
    monkeypatch.setattr(subprocess, "check_call", fail_process_call)
    monkeypatch.setattr(subprocess, "Popen", fail_process_call)

    with pytest.raises(RuntimeError, match="Missing trainer dependencies"):
        train_classifier.ensure_setfit()
    assert calls == []


def test_predict_dependency_check_does_not_require_onnx_export_stack(monkeypatch, train_classifier):
    real_import = __import__

    def fake_import(name, *args, **kwargs):
        if name == "setfit":
            return types.SimpleNamespace()
        if name in {"onnx", "onnxruntime", "skl2onnx"}:
            raise ImportError("export dependency missing")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", fake_import)

    train_classifier.ensure_setfit(export_onnx=False)
    with pytest.raises(RuntimeError, match="onnx"):
        train_classifier.ensure_setfit(export_onnx=True)


def test_trainer_cli_modes_are_mutually_exclusive(monkeypatch, train_classifier):
    monkeypatch.setattr(sys, "argv", ["train_classifier.py", "--dry-run", "--predict"])

    with pytest.raises(SystemExit) as exc:
        train_classifier.main()

    assert exc.value.code == 2


def test_write_time_merge_revives_expired_exact_duplicate(tmp_path, monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE memories (
            id INTEGER PRIMARY KEY,
            content TEXT,
            content_hash TEXT UNIQUE,
            tags TEXT,
            memory_type TEXT,
            metadata TEXT,
            created_at REAL,
            updated_at REAL,
            created_at_iso TEXT,
            updated_at_iso TEXT,
            deleted_at REAL,
            valid_until TEXT
        )
        """
    )
    content_hash = write_time_merge._sha256_hex("same content")
    conn.execute(
        """
        INSERT INTO memories
        (id, content, content_hash, tags, memory_type, metadata, created_at,
         updated_at, created_at_iso, updated_at_iso, deleted_at, valid_until)
        VALUES (1, 'same content', ?, 'old', 'fact', '{}', 0, 0, '', '', NULL, '2026-05-20T00:00:00+00:00')
        """,
        (content_hash,),
    )
    monkeypatch.setattr(write_time_merge, "_ensure_sqlite_vec_loaded", lambda conn: None)
    monkeypatch.setattr(write_time_merge, "_upsert_embedding", lambda *args, **kwargs: None)

    result = write_time_merge.merge_or_insert(
        conn,
        content="same content",
        content_hash=content_hash,
        tags="fresh",
        memory_type="fact",
        metadata={"source": "test"},
        embedding_bytes=b"",
        now=1_779_379_200,
    )

    row = conn.execute("SELECT tags, valid_until, metadata FROM memories WHERE id = 1").fetchone()
    assert result.action == "merged"
    assert result.reason == "revived_expired_duplicate"
    assert row[0] == "fresh"
    assert row[1] is None


def test_valid_until_parser_treats_same_day_iso_expiration_as_inactive():
    now = datetime.fromisoformat("2026-05-21T10:00:00+00:00")

    assert write_time_merge._valid_until_active("2026-05-21T00:00:00+00:00", now) is False
    assert write_time_merge._valid_until_active("2026-05-21T23:59:59+00:00", now) is True


def test_classifier_head_pickle_write_is_atomic(tmp_path, monkeypatch, train_classifier_head_pkl):
    out_path = tmp_path / "classifier-head.pkl"
    original = {"head": "old"}
    out_path.write_bytes(pickle.dumps(original))
    writer = getattr(train_classifier_head_pkl, "_write_pickle_atomic", None)
    assert callable(writer)

    def failing_dump(payload, handle):
        handle.write(b"partial")
        raise RuntimeError("boom")

    monkeypatch.setattr(train_classifier_head_pkl.pickle, "dump", failing_dump)

    with pytest.raises(RuntimeError):
        writer(out_path, {"head": "new"})

    assert pickle.loads(out_path.read_bytes()) == original


def test_classifier_head_trainer_refuses_daemon_model_mismatch(monkeypatch, train_classifier_head_pkl):
    monkeypatch.setattr(train_classifier_head_pkl, "BASE_MODEL", "BAAI/other-model")
    monkeypatch.setattr(train_classifier_head_pkl, "DAEMON_MODEL", "BAAI/bge-m3")
    monkeypatch.setattr(train_classifier_head_pkl, "ALLOW_MODEL_MISMATCH", False)

    with pytest.raises(SystemExit, match="differs from"):
        train_classifier_head_pkl._assert_model_matches_daemon()

    monkeypatch.setattr(train_classifier_head_pkl, "ALLOW_MODEL_MISMATCH", True)
    train_classifier_head_pkl._assert_model_matches_daemon()


def test_embed_daemon_rejects_classifier_head_model_mismatch(tmp_path, monkeypatch):
    classifier_path = tmp_path / "classifier-head.pkl"
    classifier_path.write_bytes(pickle.dumps({
        "base_model": "BAAI/other-model",
        "head": "dummy",
        "labels": ["decision"],
    }))
    monkeypatch.setattr(embed_daemon, "_CLASSIFIER_PATH", str(classifier_path))
    monkeypatch.setattr(embed_daemon, "_CLASSIFIER_AVAILABLE", None)
    monkeypatch.setattr(embed_daemon, "_CLASSIFIER_HEAD", None)
    monkeypatch.setattr(embed_daemon, "MODEL_NAME", "BAAI/bge-m3")
    monkeypatch.setenv("B12_CLASSIFIER_ALLOW_MODEL_MISMATCH", "0")

    assert embed_daemon._load_classifier() is False
    assert embed_daemon._CLASSIFIER_HEAD is None


def test_token_budget_record_inject_serializes_concurrent_updates(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    monkeypatch.setenv("B12_DATA_DIR", str(tmp_path))
    session_id = "concurrent-budget-session"

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [
            executor.submit(b12_token_budget.record_inject, session_id, 1)
            for _ in range(25)
        ]
        for future in futures:
            future.result()

    assert b12_token_budget.cumulative_used(session_id) == 25


def test_long_session_turn_counter_serializes_concurrent_updates(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    monkeypatch.setenv("B12_DATA_DIR", str(tmp_path))
    session_id = "concurrent-turn-session"

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [
            executor.submit(b12_long_session.bump_turn_counter, session_id)
            for _ in range(25)
        ]
        for future in futures:
            future.result()

    assert b12_long_session.read_turn(session_id) == 25
