#!/usr/bin/env python3
"""B12 SessionEnd Hook for Grok (thin adapter).

Fires when a Grok session ends. Scans the transcript, extracts decisions /
gotchas / learnings / preferences (+ a short progress note) via the shared B12
core, and stores them. Exits 0 on every path.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _b12_grok_core import (  # noqa: E402
    CORE_OK,
    IMPORT_ERROR,
    extract_decisions,
    extract_gotchas,
    extract_learnings,
    extract_preferences,
    store_items,
)


def main():
    try:
        event = json.load(sys.stdin)
    except Exception:
        event = {}
    if not isinstance(event, dict):
        event = {}

    session_id = event.get("session_id", "")
    if not isinstance(session_id, str):
        session_id = ""
    cwd = event.get("cwd", os.getcwd())
    if not isinstance(cwd, str) or not cwd:
        cwd = os.getcwd()
    print(f"[b12-session-end] SessionEnd | session={session_id[:12]}", file=sys.stderr)
    if not session_id:
        sys.exit(0)
    if not CORE_OK:
        print(f"[b12-session-end] shared core unavailable: {IMPORT_ERROR}", file=sys.stderr)
        sys.exit(0)

    chat_history = (
        Path.home() / ".grok" / "sessions" / cwd.replace("/", "%2F") / session_id / "chat_history.jsonl"
    )
    if not chat_history.exists():
        print("[b12-session-end] no transcript; skipping", file=sys.stderr)
        sys.exit(0)

    user_messages = []
    assistant_messages = []
    try:
        with open(chat_history, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    entry = json.loads(line.strip())
                except Exception:
                    continue
                if not isinstance(entry, dict):
                    continue
                etype = entry.get("type")
                if etype == "user":
                    content = entry.get("content", [])
                    if isinstance(content, list):
                        text = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
                    else:
                        text = str(content)
                    if text.strip():
                        user_messages.append(text.strip()[:400])
                elif etype == "assistant":
                    content = entry.get("content", "")
                    if isinstance(content, str) and len(content.strip()) > 40:
                        assistant_messages.append(content.strip()[:1000])
    except Exception:
        sys.exit(0)

    project = os.path.basename(cwd.rstrip("/")) or "unknown"
    base_tags = [f"proj:{project}", "source:grok", "hook:session-end"]
    meta = {"source": "grok-session-end", "session": session_id[:12]}

    items = []
    for msg in assistant_messages[-15:]:
        for d in extract_decisions(msg):
            items.append(("decision", f"[Decision] {d}", base_tags + ["type:decision"], meta))
        for g in extract_gotchas(msg):
            items.append(("error_fix", f"[Gotcha] {g}", base_tags + ["type:gotcha"], meta))
        for ln in extract_learnings(msg):
            items.append(("learning", f"[Learning] {ln}", base_tags + ["type:learning"], meta))
        for p in extract_preferences(msg):
            items.append(("preference", f"[Preference] {p}", base_tags + ["type:preference"], meta))
    for msg in user_messages[-8:]:
        for p in extract_preferences(msg):
            items.append(("preference", f"[Preference] {p}", base_tags + ["type:preference"], meta))

    stored = store_items(items)
    print(f"[b12-session-end] stored {stored} of {len(items)} candidates", file=sys.stderr)
    sys.exit(0)


if __name__ == "__main__":
    main()
