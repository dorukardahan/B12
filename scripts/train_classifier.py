#!/usr/bin/env python3
"""B12 Memory Type Classifier — SetFit train + ONNX export.

Run:  ~/.local/b12-venv/bin/python3 scripts/train_classifier.py [--dry-run|--predict]
"""
import argparse, json, pickle, subprocess, sys
from collections import Counter
from pathlib import Path

# Fix: SetFit 1.1.3 imports default_logdir removed in transformers 5.x
try:
    import transformers.training_args as _ta
    if not hasattr(_ta, 'default_logdir'):
        import os as _os, datetime as _dt
        _ta.default_logdir = lambda: _os.path.join('runs', _dt.datetime.now().strftime('%b%d_%H-%M-%S'))
except ImportError:
    pass

LABELS = ["decision", "error_fix", "learning", "preference", "observation", "knowledge", "session_summary"]
DATA_PATH = Path("/tmp/b12-setfit-candidates.json")
MODEL_DIR = Path.home() / ".B12" / "models" / "setfit-memory-classifier"
ONNX_DIR = Path.home() / ".B12" / "models" / "setfit-memory-classifier-onnx"
BASE_MODEL = "BAAI/bge-m3"
SAMPLES_PER_LABEL = 8
BATCH_SIZE = 16
NUM_EPOCHS = (1, 10)  # (embedding_epochs, classifier_epochs)


def ensure_setfit():
    """Install setfit + ONNX export deps if missing."""
    missing = []
    for pkg in ("setfit", "onnx", "skl2onnx"):
        try:
            __import__(pkg)
        except ImportError:
            missing.append(pkg)
    if missing:
        print(f"[setup] Installing missing packages: {', '.join(missing)}")
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "--quiet"] + missing,
            stdout=subprocess.DEVNULL,
        )
        print("[setup] Installation complete.")


def load_data(path: Path):
    """Load and validate labeled JSON data. Returns (train_items, test_items)."""
    if not path.exists():
        print(f"[error] Data file not found: {path}")
        print("  Generate it first, then re-run this script.")
        sys.exit(1)

    with open(path) as f:
        items = json.load(f)

    if not isinstance(items, list) or not items:
        print("[error] Data file must be a non-empty JSON array.")
        sys.exit(1)

    required = {"content_preview", "proposed_label", "split"}
    for i, item in enumerate(items):
        missing = required - set(item.keys())
        if missing:
            print(f"[error] Item {i} missing fields: {missing}")
            sys.exit(1)
        if item["proposed_label"] not in LABELS:
            print(f"[error] Item {i} has unknown label '{item['proposed_label']}'. Valid: {LABELS}")
            sys.exit(1)
        if item["split"] not in ("train", "test"):
            print(f"[error] Item {i} has unknown split '{item['split']}'. Must be 'train' or 'test'.")
            sys.exit(1)

    train = [it for it in items if it["split"] == "train"]
    test = [it for it in items if it["split"] == "test"]
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

    ensure_setfit()
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
    ensure_setfit()

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

    # --- Init model ---
    print(f"[train] Loading base model: {BASE_MODEL}")
    # Build SetFit model from components (avoids config_setfit.json Hub lookup issue)
    from sentence_transformers import SentenceTransformer
    from sklearn.linear_model import LogisticRegression
    st_body = SentenceTransformer(BASE_MODEL)
    model = SetFitModel(model_body=st_body, model_head=LogisticRegression(max_iter=500))
    model.labels = LABELS

    args = TrainingArguments(
        output_dir=str(MODEL_DIR / "checkpoints"),
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
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    print(f"\n[save] Saving PyTorch model to {MODEL_DIR}")
    model.save_pretrained(str(MODEL_DIR))

    # --- Export ONNX ---
    print(f"[onnx] Exporting to {ONNX_DIR}")
    ONNX_DIR.mkdir(parents=True, exist_ok=True)
    onnx_path = ONNX_DIR / "model.onnx"

    try:
        from setfit.exporters.onnx import export_onnx
        export_onnx(
            model_body=model.model_body,
            model_head=model.model_head,
            opset=14,
            output_path=str(onnx_path),
        )
    except Exception as e:
        print(f"[onnx] SetFit native export failed ({e}), using manual export...")
        _manual_onnx_export(model, onnx_path)

    _verify_onnx(onnx_path)

    # Save label mapping alongside ONNX
    meta = {"labels": LABELS, "base_model": BASE_MODEL, "accuracy": round(acc, 4)}
    with open(ONNX_DIR / "config.json", "w") as f:
        json.dump(meta, f, indent=2)
    _copy_tokenizer(ONNX_DIR)

    print(f"\n[done] Training complete.")
    print(f"  PyTorch model: {MODEL_DIR}")
    print(f"  ONNX model:    {onnx_path}")
    print(f"  Accuracy:      {acc:.1%}")


def _manual_onnx_export(model, onnx_path: Path):
    """Fallback: export transformer body to ONNX + pickle the sklearn head."""
    import torch
    from transformers import AutoTokenizer
    tokenizer = AutoTokenizer.from_pretrained(BASE_MODEL)
    body = model.model_body[0].auto_model
    dummy = tokenizer("hello world", return_tensors="pt", padding=True, truncation=True)
    names = list(dummy.keys())
    axes = {k: {0: "batch", 1: "seq"} for k in names} | {"last_hidden_state": {0: "batch", 1: "seq"}}
    torch.onnx.export(body, tuple(dummy.values()), str(onnx_path),
                      input_names=names, output_names=["last_hidden_state"],
                      dynamic_axes=axes, opset_version=14)
    with open(onnx_path.parent / "head.pkl", "wb") as f:
        pickle.dump(model.model_head, f)
    print(f"[onnx] Manual export: body + head.pkl saved")


def _verify_onnx(onnx_path: Path):
    """Quick sanity check: load ONNX model with CPUExecutionProvider."""
    try:
        import onnxruntime as ort
        sess = ort.InferenceSession(str(onnx_path), providers=["CPUExecutionProvider"])
        print(f"[onnx] Verified: {len(sess.get_inputs())} inputs, provider=CPU")
    except Exception as e:
        print(f"[onnx] Verification warning: {e}")


def _copy_tokenizer(dest: Path):
    """Copy tokenizer files to ONNX dir for standalone inference."""
    try:
        from transformers import AutoTokenizer
        AutoTokenizer.from_pretrained(BASE_MODEL).save_pretrained(str(dest))
        print(f"[onnx] Tokenizer saved to {dest}")
    except Exception as e:
        print(f"[onnx] Could not save tokenizer: {e}")


def main():
    parser = argparse.ArgumentParser(description="B12 Memory Type Classifier")
    parser.add_argument("--dry-run", action="store_true", help="Validate data only")
    parser.add_argument("--predict", action="store_true", help="Classify stdin text")
    args = parser.parse_args()

    if args.dry_run:
        dry_run()
    elif args.predict:
        predict_mode()
    else:
        train()


if __name__ == "__main__":
    main()
