#!/usr/bin/env python3
"""
B12 PreCompact Hook for Grok (Production Thin Adapter)

Grok PreCompact eventi tetiklendiğinde çalışır.
- Transcript'i okur (chat_history.jsonl)
- Karar, gotcha, learning gibi yüksek değerli içerikleri çıkarır
- write_time_merge ile B12'ye yazar
"""

import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[5]
SCRIPTS_DIR = REPO_ROOT / "scripts"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

try:
    from shared_patterns import extract_decisions, extract_gotchas, extract_learnings
    from write_time_merge import merge_or_insert
    SHARED_CORE_OK = True
except ImportError as e:
    SHARED_CORE_OK = False
    IMPORT_ERROR = str(e)


def main():
    try:
        event = json.load(sys.stdin)
    except Exception:
        event = {}

    session_id = event.get("session_id", "")
    cwd = event.get("cwd", os.getcwd())

    print(f"[b12-precompact] PreCompact triggered | Session: {session_id[:12]}...", file=sys.stderr)

    if not session_id:
        sys.exit(0)

    encoded_cwd = cwd.replace("/", "%2F")
    session_dir = Path.home() / ".grok" / "sessions" / encoded_cwd / session_id
    chat_history = session_dir / "chat_history.jsonl"

    if not chat_history.exists():
        print("[b12-precompact] No transcript found. Skipping.", file=sys.stderr)
        sys.exit(0)

    if not SHARED_CORE_OK:
        print(f"[b12-precompact] Shared core import failed: {IMPORT_ERROR}", file=sys.stderr)
        sys.exit(0)

    # === Transcript'ten son assistant mesajlarını çıkar ===
    assistant_messages = []
    with open(chat_history, "r", encoding="utf-8") as f:
        for line in f:
            try:
                entry = json.loads(line.strip())
                if entry.get("type") == "assistant":
                    content = entry.get("content", "")
                    if isinstance(content, str) and len(content.strip()) > 50:
                        assistant_messages.append(content.strip()[:1200])
            except Exception:
                continue

    if not assistant_messages:
        print("[b12-precompact] No meaningful assistant messages.", file=sys.stderr)
        sys.exit(0)

    recent_messages = assistant_messages[-12:]  # PreCompact öncesi en kritik kısım

    all_decisions = []
    all_gotchas = []
    all_learnings = []

    for msg in recent_messages:
        all_decisions.extend(extract_decisions(msg))
        all_gotchas.extend(extract_gotchas(msg))
        all_learnings.extend(extract_learnings(msg))

    print(f"[b12-precompact] Extracted → Decisions: {len(all_decisions)}, Gotchas: {len(all_gotchas)}, Learnings: {len(all_learnings)}", file=sys.stderr)

    # === B12'ye kaydet ===
    project = "B12"
    base_tags = ["proj:B12", "source:grok", "hook:precompact"]

    stored = 0

    for decision in all_decisions:
        try:
            merge_or_insert(
                content=f"[Decision] {decision}",
                tags=base_tags + ["type:decision"],
                metadata={"source": "grok-precompact", "session": session_id}
            )
            stored += 1
        except Exception as e:
            print(f"[b12-precompact] Store failed (decision): {e}", file=sys.stderr)

    for gotcha in all_gotchas:
        try:
            merge_or_insert(
                content=f"[Gotcha] {gotcha}",
                tags=base_tags + ["type:gotcha"],
                metadata={"source": "grok-precompact", "session": session_id}
            )
            stored += 1
        except Exception as e:
            print(f"[b12-precompact] Store failed (gotcha): {e}", file=sys.stderr)

    for learning in all_learnings:
        try:
            merge_or_insert(
                content=f"[Learning] {learning}",
                tags=base_tags + ["type:learning"],
                metadata={"source": "grok-precompact", "session": session_id}
            )
            stored += 1
        except Exception as e:
            print(f"[b12-precompact] Store failed (learning): {e}", file=sys.stderr)

    print(f"[b12-precompact] Stored {stored} memories successfully.", file=sys.stderr)
    sys.exit(0)


if __name__ == "__main__":
    main()