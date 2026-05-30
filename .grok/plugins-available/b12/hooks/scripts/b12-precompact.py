#!/usr/bin/env python3
"""B12 PreCompact Hook for Grok (thin adapter).

Fires on Grok's PreCompact event. Reads the session transcript, extracts
high-value decisions / gotchas / learnings via the shared B12 core, and stores
them. Exits 0 on every path (never blocks compaction).
"""

import json
import os
import sys
from pathlib import Path

# The shared core lives next to this file in the deployed plugin.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _b12_grok_core import (  # noqa: E402
    CORE_OK,
    IMPORT_ERROR,
    extract_decisions,
    extract_gotchas,
    extract_learnings,
    store_items,
)


def main():
    try:
        event = json.load(sys.stdin)
    except Exception:
        event = {}

    session_id = event.get("session_id", "")
    cwd = event.get("cwd", os.getcwd())
    print(f"[b12-precompact] PreCompact | session={session_id[:12]}", file=sys.stderr)
    if not session_id:
        sys.exit(0)
    if not CORE_OK:
        print(f"[b12-precompact] shared core unavailable: {IMPORT_ERROR}", file=sys.stderr)
        sys.exit(0)

    chat_history = (
        Path.home() / ".grok" / "sessions" / cwd.replace("/", "%2F") / session_id / "chat_history.jsonl"
    )
    if not chat_history.exists():
        print("[b12-precompact] no transcript; skipping", file=sys.stderr)
        sys.exit(0)

    assistant_messages = []
    try:
        with open(chat_history, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    entry = json.loads(line.strip())
                except Exception:
                    continue
                if entry.get("type") == "assistant":
                    content = entry.get("content", "")
                    if isinstance(content, str) and len(content.strip()) > 50:
                        assistant_messages.append(content.strip()[:1200])
    except Exception:
        sys.exit(0)

    project = os.path.basename(cwd.rstrip("/")) or "unknown"
    base_tags = [f"proj:{project}", "source:grok", "hook:precompact"]
    meta = {"source": "grok-precompact", "session": session_id[:12]}

    # (memory_type, content, tags, metadata) — memory_type is a canonical label.
    items = []
    for msg in assistant_messages[-12:]:
        for d in extract_decisions(msg):
            items.append(("decision", f"[Decision] {d}", base_tags + ["type:decision"], meta))
        for g in extract_gotchas(msg):
            items.append(("error_fix", f"[Gotcha] {g}", base_tags + ["type:gotcha"], meta))
        for ln in extract_learnings(msg):
            items.append(("learning", f"[Learning] {ln}", base_tags + ["type:learning"], meta))

    stored = store_items(items)
    print(f"[b12-precompact] stored {stored} of {len(items)} candidates", file=sys.stderr)
    sys.exit(0)


if __name__ == "__main__":
    main()
