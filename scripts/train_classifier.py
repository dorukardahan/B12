#!/usr/bin/env python3
"""B12 Memory Type Classifier — SetFit train + ONNX export.

Run:  ~/.local/b12-venv/bin/python3 scripts/train_classifier.py [--dry-run|--predict]
"""
import argparse, json, sys
from contextlib import contextmanager
from collections import Counter
from pathlib import Path
import os
import shutil
import tempfile
try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

# Fix: SetFit 1.1.3 imports default_logdir removed in transformers 5.x
try:
    import transformers.training_args as _ta
    if not hasattr(_ta, 'default_logdir'):
        import os as _os, datetime as _dt
        _ta.default_logdir = lambda: _os.path.join('runs', _dt.datetime.now().strftime('%b%d_%H-%M-%S'))
except ImportError:
    pass

LABELS = ["decision", "error_fix", "learning", "preference", "observation", "knowledge", "session_summary"]


def get_data_path() -> Path:
    corpus_path = os.environ.get("B12_CORPUS_PATH")
    if corpus_path:
        return Path(corpus_path)
    return Path(tempfile.gettempdir()) / "b12-setfit-candidates.json"


DATA_PATH = get_data_path()
_DATA_DIR = Path(os.environ.get("B12_DATA_DIR", str(Path.home() / ".B12")))
MODEL_DIR = _DATA_DIR / "models" / "setfit-memory-classifier"
ONNX_DIR = _DATA_DIR / "models" / "setfit-memory-classifier-onnx"
BASE_MODEL = "BAAI/bge-m3"
SAMPLES_PER_LABEL = 8
BATCH_SIZE = 16
NUM_EPOCHS = (1, 10)  # (embedding_epochs, classifier_epochs)


def ensure_setfit(export_onnx: bool = True):
    """Fail fast if trainer dependencies are missing."""
    missing = []
    packages = ["setfit"]
    if export_onnx:
        packages.extend(["onnx", "onnxruntime", "skl2onnx"])
    for pkg in packages:
        try:
            __import__(pkg)
        except ImportError:
            missing.append(pkg)
    if missing:
        raise RuntimeError(
            "Missing trainer dependencies: "
            + ", ".join(missing)
            + ". Install the trainer extra/dependencies explicitly before running."
        )


def load_data(path: Path):
    """Load and validate labeled JSON data. Returns (train_items, test_items)."""
    if not path.exists():
        print(f"[error] Data file not found: {path}")
        print("  Generate it first, then re-run this script.")
        sys.exit(1)

    try:
        with open(path) as f:
            items = json.load(f)
    except OSError as exc:
        print(f"[error] Could not read data file {path}: {exc}")
        sys.exit(1)
    except json.JSONDecodeError as exc:
        print(f"[error] Could not parse data file {path}: {exc}")
        sys.exit(1)

    if not isinstance(items, list) or not items:
        print("[error] Data file must be a non-empty JSON array.")
        sys.exit(1)

    required = {"content_preview", "proposed_label", "split"}
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            print(f"[error] Item {i} must be a JSON object.")
            sys.exit(1)
        missing = required - set(item.keys())
        if missing:
            print(f"[error] Item {i} missing fields: {missing}")
            sys.exit(1)
        if not isinstance(item["content_preview"], str) or not item["content_preview"].strip():
            print(f"[error] Item {i} content_preview must be a non-empty string.")
            sys.exit(1)
        if item["proposed_label"] not in LABELS:
            print(f"[error] Item {i} has unknown label '{item['proposed_label']}'. Valid: {LABELS}")
            sys.exit(1)
        if item["split"] not in ("train", "test"):
            print(f"[error] Item {i} has unknown split '{item['split']}'. Must be 'train' or 'test'.")
            sys.exit(1)

    train = [it for it in items if it["split"] == "train"]
    test = [it for it in items if it["split"] == "test"]
    if not train:
        print("[error] Corpus must include at least one train item.")
        sys.exit(1)
    if not test:
        print("[error] Corpus must include at least one test item.")
        sys.exit(1)
    train_labels = {it["proposed_label"] for it in train}
    if len(train_labels) < 2:
        print("[error] Corpus train split must include at least two labels.")
        sys.exit(1)
    missing_train_labels = [label for label in LABELS if label not in train_labels]
    if missing_train_labels:
        print(f"[error] Corpus train split is missing labels: {missing_train_labels}")
        sys.exit(1)
    return train, test


def print_data_summary(train, test):
    """Print per-class counts and warnings."""
    train_counts = Counter(it["proposed_label"] for it in train)
    test_counts = Counter(it["proposed_label"] for it in test)

    print(f"\n{'Label':<20} {'Train':>6} {'Test':>6}")
    print("-" * 34)
    for label in LABELS:
        tr, te = train_counts.get(label, 0), test_counts.get(label, 0)
        warn = " ** LOW" if tr < SAMPLES_PER_LABEL else ""
        print(f"{label:<20} {tr:>6} {te:>6}{warn}")
    print(f"{'TOTAL':<20} {len(train):>6} {len(test):>6}\n")

    low = [l for l in LABELS if train_counts.get(l, 0) < SAMPLES_PER_LABEL]
    if low:
        print(f"[warn] <{SAMPLES_PER_LABEL} train samples: {low} — accuracy may suffer\n")
    missing = [l for l in LABELS if train_counts.get(l, 0) == 0]
    if missing:
        print(f"[warn] ZERO train samples: {missing} — these classes cannot be learned\n")


def dry_run():
    """Validate data file and print summary without training."""
    print("[dry-run] Validating data...")
    train, test = load_data(DATA_PATH)
    print_data_summary(train, test)
    print("[dry-run] Data validation passed. Ready for training.")


def predict_mode():
    """Load saved model and classify text from stdin."""
    if not MODEL_DIR.exists():
        print(f"[error] No saved model at {MODEL_DIR}. Train first.")
        sys.exit(1)

    ensure_setfit(export_onnx=False)
    from setfit import SetFitModel

    print("[predict] Loading model...", file=sys.stderr)
    model = SetFitModel.from_pretrained(str(MODEL_DIR))
    text = sys.stdin.read().strip()
    if not text:
        print("[error] No text provided on stdin.")
        sys.exit(1)

    prediction = model.predict([text])[0]
    print(prediction)


def train():
    """Full training pipeline: load data, train SetFit, evaluate, export ONNX."""
    ensure_setfit(export_onnx=True)

    from datasets import Dataset
    from setfit import SetFitModel, Trainer, TrainingArguments
    from sklearn.metrics import accuracy_score, classification_report, confusion_matrix

    # --- Load data ---
    train_items, test_items = load_data(DATA_PATH)
    print_data_summary(train_items, test_items)

    train_ds = Dataset.from_dict({
        "text": [it["content_preview"] for it in train_items],
        "label": [LABELS.index(it["proposed_label"]) for it in train_items],
    })
    test_ds = Dataset.from_dict({
        "text": [it["content_preview"] for it in test_items],
        "label": [LABELS.index(it["proposed_label"]) for it in test_items],
    })

    MODEL_DIR.parent.mkdir(parents=True, exist_ok=True)
    ONNX_DIR.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{MODEL_DIR.name}-", dir=MODEL_DIR.parent) as model_tmp_s, \
            tempfile.TemporaryDirectory(prefix=f".{ONNX_DIR.name}-", dir=ONNX_DIR.parent) as onnx_tmp_s:
        model_tmp = Path(model_tmp_s)
        onnx_tmp = Path(onnx_tmp_s)

        # --- Init model ---
        print(f"[train] Loading base model: {BASE_MODEL}")
        # Build SetFit model from components (avoids config_setfit.json Hub lookup issue)
        from sentence_transformers import SentenceTransformer
        from sklearn.linear_model import LogisticRegression
        st_body = SentenceTransformer(BASE_MODEL)
        model = SetFitModel(model_body=st_body, model_head=LogisticRegression(max_iter=500))
        model.labels = LABELS

        args = TrainingArguments(
            output_dir=str(model_tmp / "checkpoints"),
            batch_size=BATCH_SIZE,
            num_epochs=NUM_EPOCHS,
            sampling_strategy="oversampling",
            seed=42,
        )
        trainer = Trainer(
            model=model,
            args=args,
            train_dataset=train_ds,
            eval_dataset=test_ds,
            metric="accuracy",
            column_mapping={"text": "text", "label": "label"},
        )

        # --- Train ---
        print("[train] Starting SetFit training...")
        trainer.train()

        # --- Evaluate ---
        print("\n[eval] Running evaluation on test split...")
        preds = model.predict([it["content_preview"] for it in test_items])
        true_labels = [LABELS.index(it["proposed_label"]) for it in test_items]

        # preds may be label strings or indices depending on model.labels
        if isinstance(preds[0], str):
            pred_indices = [LABELS.index(p) for p in preds]
        else:
            pred_indices = [int(p) for p in preds]

        acc = accuracy_score(true_labels, pred_indices)
        print(f"\n  Overall accuracy: {acc:.1%}")
        print(f"\n  Classification report:\n")
        print(classification_report(
            true_labels, pred_indices,
            target_names=LABELS, labels=list(range(len(LABELS))), zero_division=0,
        ))

        cm = confusion_matrix(true_labels, pred_indices, labels=list(range(len(LABELS))))
        print("  Confusion matrix (rows=true, cols=predicted):")
        header = "  " + f"{'':>18}" + "".join(f"{l[:6]:>8}" for l in LABELS)
        print(header)
        for i, row in enumerate(cm):
            print(f"  {LABELS[i]:>18}" + "".join(f"{v:>8}" for v in row))

        # --- Save PyTorch model ---
        print(f"\n[save] Saving PyTorch model to staging directory {model_tmp}")
        model.save_pretrained(str(model_tmp))

        # --- Export ONNX ---
        print(f"[onnx] Exporting to staging directory {onnx_tmp}")
        onnx_path = onnx_tmp / "model.onnx"

        try:
            from setfit.exporters.onnx import export_onnx
            export_onnx(
                model_body=model.model_body,
                model_head=model.model_head,
                opset=14,
                output_path=str(onnx_path),
            )
        except Exception as e:
            raise RuntimeError(
                "native SetFit ONNX export failed; manual token-output fallback "
                "is disabled because it is incompatible with the sklearn head"
            ) from e

        _verify_onnx(onnx_path)

        # Save label mapping alongside ONNX
        meta = {"labels": LABELS, "base_model": BASE_MODEL, "accuracy": round(acc, 4)}
        with open(onnx_tmp / "config.json", "w") as f:
            json.dump(meta, f, indent=2)
        _copy_tokenizer(onnx_tmp)

        _publish_artifact_dirs([(model_tmp, MODEL_DIR), (onnx_tmp, ONNX_DIR)])

    print(f"\n[done] Training complete.")
    print(f"  PyTorch model: {MODEL_DIR}")
    print(f"  ONNX model:    {ONNX_DIR / 'model.onnx'}")
    print(f"  Accuracy:      {acc:.1%}")


def _manual_onnx_export(model, onnx_path: Path):
    """Disabled fallback: token outputs do not match the sklearn head contract."""
    raise RuntimeError(
        "native SetFit ONNX export is required; manual token-output fallback "
        "is incompatible with the sklearn head"
    )


def _verify_onnx(onnx_path: Path):
    """Quick sanity check: load ONNX model with CPUExecutionProvider."""
    try:
        import onnxruntime as ort
        sess = ort.InferenceSession(str(onnx_path), providers=["CPUExecutionProvider"])
        print(f"[onnx] Verified: {len(sess.get_inputs())} inputs, provider=CPU")
    except Exception as e:
        raise RuntimeError(f"ONNX verification failed: {e}") from e


def _replace_dir_after_verify(src: Path, dest: Path) -> None:
    """Replace a model artifact directory only after staging verification passes."""
    backup = None
    if dest.exists():
        backup = dest.with_name(f".{dest.name}.bak-{os.getpid()}")
        if backup.exists():
            shutil.rmtree(backup)
        os.replace(dest, backup)
    try:
        os.replace(src, dest)
    except Exception:
        if backup is not None and backup.exists() and not dest.exists():
            os.replace(backup, dest)
        raise
    else:
        if backup is not None and backup.exists():
            shutil.rmtree(backup)


@contextmanager
def _artifact_publish_lock(lock_dir: Path):
    """Serialize classifier artifact swaps across concurrent training runs."""
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / ".classifier-publish.lock"
    with open(lock_path, "a+") as lock_file:
        if fcntl is not None:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _publish_artifact_dirs(pairs: list[tuple[Path, Path]]) -> None:
    """Publish several artifact dirs as a rollback-safe group."""
    if not pairs:
        return
    backups: list[tuple[Path, Path | None]] = []
    staging_copies: list[Path] = []
    lock_dir = pairs[0][1].parent
    with _artifact_publish_lock(lock_dir):
        try:
            for index, (src, dest) in enumerate(pairs):
                staged = dest.with_name(f".{dest.name}.publish-{os.getpid()}-{index}")
                if staged.exists():
                    shutil.rmtree(staged)
                shutil.copytree(src, staged)
                staging_copies.append(staged)
                backup = None
                if dest.exists():
                    backup = dest.with_name(f".{dest.name}.bak-{os.getpid()}")
                    if backup.exists():
                        shutil.rmtree(backup)
                    os.replace(dest, backup)
                    backups.append((dest, backup))
                os.replace(staged, dest)
                staging_copies.remove(staged)
                if backup is None:
                    backups.append((dest, backup))
        except Exception:
            for staged in staging_copies:
                if staged.exists():
                    shutil.rmtree(staged)
            for dest, backup in reversed(backups):
                if dest.exists():
                    shutil.rmtree(dest)
                if backup is not None and backup.exists():
                    os.replace(backup, dest)
            raise
        else:
            for _, backup in backups:
                if backup is not None and backup.exists():
                    shutil.rmtree(backup)


def _copy_tokenizer(dest: Path):
    """Copy tokenizer files to ONNX dir for standalone inference."""
    try:
        from transformers import AutoTokenizer
        AutoTokenizer.from_pretrained(BASE_MODEL).save_pretrained(str(dest))
        print(f"[onnx] Tokenizer saved to {dest}")
    except Exception as e:
        raise RuntimeError(f"Could not save tokenizer: {e}") from e


def main():
    parser = argparse.ArgumentParser(description="B12 Memory Type Classifier")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Validate data only")
    mode.add_argument("--predict", action="store_true", help="Classify stdin text")
    args = parser.parse_args()

    if args.dry_run:
        dry_run()
    elif args.predict:
        predict_mode()
    else:
        train()


if __name__ == "__main__":
    main()
