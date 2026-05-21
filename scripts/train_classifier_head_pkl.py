"""PR15: train daemon-compatible classifier head pickle on bge-m3 1024-dim.

The full SetFit + ONNX pipeline in scripts/train_classifier.py outputs a
SetFit model dir, not the `{head, labels, cv_accuracy}` pickle that
embed_daemon.py:_load_classifier expects. This focused trainer:

  1. Encodes the silver-label corpus with bge-m3 (1024-dim, matches the
     daemon's live EXPECTED_DIM)
  2. Fits a sklearn LogisticRegression head on the 1024-dim embeddings
  3. Writes the daemon-compatible pickle to ~/.B12/models/classifier-head.pkl

This is the minimum surface needed to unblock the daemon. The full
SetFit pipeline (with few-shot oversampling + ONNX export) lands as a
follow-up — the silver-label corpus is large enough (1797 items) that
a vanilla LogReg already outperforms few-shot.
"""
import os
import pickle
import sys
import time
from collections import Counter
from pathlib import Path

import tempfile as _tempfile


def get_data_path() -> Path:
    corpus_path = os.environ.get("B12_CORPUS_PATH")
    if corpus_path:
        return Path(corpus_path)
    return Path(_tempfile.gettempdir()) / "b12-setfit-candidates.json"


DATA_PATH = get_data_path()
# Codex review PR #70 round 1 P1: honor B12_DATA_DIR so custom-data-dir
# installs land the new pickle where embed_daemon actually loads from
# (embed_daemon._CLASSIFIER_PATH reads the same env var).
_DATA_DIR = Path(os.environ.get("B12_DATA_DIR", str(Path.home() / ".B12")))
OUT_PKL = _DATA_DIR / "models" / "classifier-head.pkl"
DAEMON_MODEL = os.environ.get("MCP_EMBEDDING_MODEL", "BAAI/bge-m3")
BASE_MODEL = os.environ.get("B12_TRAIN_MODEL", DAEMON_MODEL)
ALLOW_MODEL_MISMATCH = os.environ.get("B12_TRAIN_ALLOW_MODEL_MISMATCH", "").lower() in {
    "1",
    "true",
    "yes",
}

LABELS = ["decision", "error_fix", "learning", "preference",
          "observation", "knowledge", "session_summary"]


def _write_pickle_atomic(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = None
    try:
        with _tempfile.NamedTemporaryFile("wb", dir=path.parent, delete=False) as f:
            tmp = Path(f.name)
            pickle.dump(payload, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        if tmp is not None:
            try:
                tmp.unlink()
            except OSError:
                pass
        raise


def _assert_model_matches_daemon() -> None:
    if BASE_MODEL == DAEMON_MODEL or ALLOW_MODEL_MISMATCH:
        return
    raise SystemExit(
        "Refusing to publish classifier head: "
        f"B12_TRAIN_MODEL={BASE_MODEL!r} differs from "
        f"MCP_EMBEDDING_MODEL={DAEMON_MODEL!r}. Set "
        "B12_TRAIN_ALLOW_MODEL_MISMATCH=1 only for an explicit offline artifact."
    )


def main():
    from train_classifier import load_data
    train, test = load_data(DATA_PATH)
    _assert_model_matches_daemon()
    print(f"Train: {len(train)}, Test: {len(test)}")
    print(f"Train per label: {dict(Counter(it['proposed_label'] for it in train))}")

    print(f"Loading {BASE_MODEL}...", flush=True)
    t0 = time.time()
    from sentence_transformers import SentenceTransformer
    st = SentenceTransformer(BASE_MODEL)
    print(f"  loaded in {time.time()-t0:.1f}s")

    print("Encoding train + test...", flush=True)
    t0 = time.time()
    X_train = st.encode([it["content_preview"] for it in train],
                         convert_to_numpy=True, normalize_embeddings=True,
                         batch_size=32, show_progress_bar=True)
    X_test = st.encode([it["content_preview"] for it in test],
                        convert_to_numpy=True, normalize_embeddings=True,
                        batch_size=32, show_progress_bar=True)
    print(f"  encoded {len(train) + len(test)} items in {time.time()-t0:.1f}s")
    print(f"  X_train shape: {X_train.shape}")

    # Codex review PR #70 round 5 P2: don't hard-fail on non-1024 dims —
    # the daemon's dim-guard handles the match check at load time. This
    # script's job is to produce a head whose n_features_in_ matches the
    # encoder, regardless of which encoder the user picked.
    print(f"  Embedding dim: {X_train.shape[1]} (will be recorded in pkl payload)")

    y_train = [LABELS.index(it["proposed_label"]) for it in train]
    y_test = [LABELS.index(it["proposed_label"]) for it in test]

    print("Fitting LogReg head...", flush=True)
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import accuracy_score, classification_report
    head = LogisticRegression(max_iter=1000, class_weight="balanced",
                              C=1.0, n_jobs=-1)
    head.fit(X_train, y_train)

    train_acc = accuracy_score(y_train, head.predict(X_train))
    test_acc = accuracy_score(y_test, head.predict(X_test))
    print(f"\nTrain accuracy: {train_acc:.3f}")
    print(f"Test  accuracy: {test_acc:.3f}\n")

    print("Classification report (test):")
    print(classification_report(y_test, head.predict(X_test),
                                 target_names=LABELS,
                                 labels=list(range(len(LABELS))),
                                 zero_division=0))

    payload = {
        "head": head,
        "labels": LABELS,
        "cv_accuracy": float(test_acc),
        "base_model": BASE_MODEL,
        "trained_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "embedding_dim": int(X_train.shape[1]),
        "n_train": len(train),
        "n_test": len(test),
    }
    _write_pickle_atomic(OUT_PKL, payload)
    print(f"Wrote {OUT_PKL} ({OUT_PKL.stat().st_size} bytes)")


if __name__ == "__main__":
    sys.exit(main() or 0)
