"""Build silver-label corpus from live DB for PR15 classifier retrain.

Per plan: corpus generation is the TeamCreate cross-validation step.
For session 1, use typed memories from the live DB as silver labels.
The classifier trained on this corpus replaces the broken 384-dim
pkl; the bge-m3 1024-dim retrain is the actual fix.
"""
import json
import os
import sqlite3
from collections import Counter
from pathlib import Path

# Maps live-DB memory_type to canonical LABELS in train_classifier.py
LABEL_MAP = {
    "decision": "decision",
    "error_fix": "error_fix",
    "bugfix": "error_fix",
    "learning": "learning",
    "gotcha": "learning",
    "preference": "preference",
    "feedback": "preference",
    "observation": "observation",
    "progress": "observation",
    "knowledge": "knowledge",
    "architecture": "knowledge",
    "pattern": "knowledge",
    "audit": "knowledge",
    "infra": "knowledge",
    "content_decision": "knowledge",
    "handoff": "session_summary",
    "session_summary": "session_summary",
}

# Codex review PR #70 round 2 P2: use the cross-platform DB-path
# resolver instead of the macOS-only hardcoded path. B12_DB_PATH
# overrides for non-default installs.
import sys as _sys
_sys.path.insert(0, os.path.dirname(__file__))
try:
    from shared_patterns import get_db_path as _get_db_path
    _default_db = _get_db_path()
except ImportError:
    if _sys.platform == "darwin":
        _default_db = os.path.expanduser(
            "~/Library/Application Support/mcp-memory/sqlite_vec.db")
    elif _sys.platform == "win32":
        _default_db = os.path.expanduser(
            "~/AppData/Local/mcp-memory/sqlite_vec.db")
    else:
        _default_db = os.path.expanduser(
            "~/.local/share/mcp-memory/sqlite_vec.db")
DB_PATH = os.environ.get("B12_DB_PATH", _default_db)
# Codex review PR #70 round 6 P2: /tmp is POSIX-only. Use tempfile.gettempdir()
# (TMPDIR / TEMP / TMP-aware) so the corpus path works on Windows too.
import tempfile as _tempfile
OUT_PATH = Path(os.environ.get(
    "B12_CORPUS_PATH",
    str(Path(_tempfile.gettempdir()) / "b12-setfit-candidates.json"),
))

conn = sqlite3.connect(DB_PATH)
rows = conn.execute(
    "SELECT id, memory_type, content FROM memories "
    "WHERE deleted_at IS NULL AND length(content) BETWEEN 50 AND 2000"
).fetchall()
conn.close()

items = []
for mem_id, mtype, content in rows:
    label = LABEL_MAP.get(mtype)
    if label is None:
        continue
    items.append({
        "memory_id": mem_id,
        "content_preview": content[:1000],
        "proposed_label": label,
    })

# 80/20 deterministic split by id (stable seed)
items.sort(key=lambda x: x["memory_id"])
for i, it in enumerate(items):
    it["split"] = "train" if i % 5 != 4 else "test"

OUT_PATH.write_text(json.dumps(items, indent=2))

counts = Counter(it["proposed_label"] for it in items)
print(f"Wrote {len(items)} items → {OUT_PATH}")
print(f"By label: {dict(counts)}")
print(f"Train: {sum(1 for it in items if it['split']=='train')}")
print(f"Test:  {sum(1 for it in items if it['split']=='test')}")
