#!/usr/bin/env python3
"""
B12 SessionEnd Hook for Grok (Production Thin Adapter)

Grok session bittiğinde tetiklenir.
- Tüm oturumu tarar
- Karar, gotcha, learning, progress gibi önemli noktaları çıkarır
- B12'ye kalıcı olarak kaydeder
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
    from shared_patterns import extract_decisions, extract_gotchas, extract_learnings, extract_preferences
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

    print(f"[b12-session-end] Session ended: {session_id[:12]}...", file=sys.stderr)

    if not session_id:
        sys.exit(0)

    encoded_cwd = cwd.replace("/", "%2F")
    session_dir = Path.home() / ".grok" / "sessions" / encoded_cwd / session_id
    chat_history = session_dir / "chat_history.jsonl"

    if not chat_history.exists():
        print("[b12-session-end] No transcript found.", file=sys.stderr)
        sys.exit(0)

    if not SHARED_CORE_OK:
        print(f"[b12-session-end] Shared core import failed: {IMPORT_ERROR}", file=sys.stderr)
        sys.exit(0)

    # === Transcript'i oku ===
    user_messages = []
    assistant_messages = []

    with open(chat_history, "r", encoding="utf-8") as f:
        for line in f:
            try:
                entry = json.loads(line.strip())
                if entry.get("type") == "user":
                    content = entry.get("content", [])
                    if isinstance(content, list):
                        text = " ".join([c.get("text", "") for c in content if isinstance(c, dict)])
                    else:
                        text = str(content)
                    if text.strip():
                        user_messages.append(text.strip()[:400])
                elif entry.get("type") == "assistant":
                    content = entry.get("content", "")
                    if isinstance(content, str) and len(content.strip()) > 40:
                        assistant_messages.append(content.strip()[:1000])
            except Exception:
                continue

    print(f"[b12-session-end] Parsed → {len(user_messages)} user, {len(assistant_messages)} assistant messages.", file=sys.stderr)

    # === Extraction ===
    all_decisions = []
    all_gotchas = []
    all_learnings = []
    all_preferences = []

    # Son 15 assistant mesajını derinlemesine tara
    for msg in assistant_messages[-15:]:
        all_decisions.extend(extract_decisions(msg))
        all_gotchas.extend(extract_gotchas(msg))
        all_learnings.extend(extract_learnings(msg))
        all_preferences.extend(extract_preferences(msg))

    # Kullanıcı mesajlarından da tercih yakala
    for msg in user_messages[-8:]:
        all_preferences.extend(extract_preferences(msg))

    print(f"[b12-session-end] Extracted → Decisions: {len(all_decisions)}, Gotchas: {len(all_gotchas)}, Learnings: {len(all_learnings)}, Preferences: {len(all_preferences)}", file=sys.stderr)

    # === B12'ye kaydet ===
    base_tags = ["proj:B12", "source:grok", "hook:session-end"]

    stored = 0

    for item in all_decisions:
        try:
            merge_or_insert(f"[Decision] {item}", tags=base_tags + ["type:decision"], metadata={"source": "grok-session-end"})
            stored += 1
        except Exception as e:
            print(f"[b12-session-end] Store error (decision): {e}", file=sys.stderr)

    for item in all_gotchas:
        try:
            merge_or_insert(f"[Gotcha] {item}", tags=base_tags + ["type:gotcha"], metadata={"source": "grok-session-end"})
            stored += 1
        except Exception as e:
            print(f"[b12-session-end] Store error (gotcha): {e}", file=sys.stderr)

    for item in all_learnings:
        try:
            merge_or_insert(f"[Learning] {item}", tags=base_tags + ["type:learning"], metadata={"source": "grok-session-end"})
            stored += 1
        except Exception as e:
            print(f"[b12-session-end] Store error (learning): {e}", file=sys.stderr)

    for item in all_preferences:
        try:
            merge_or_insert(f"[Preference] {item}", tags=base_tags + ["type:preference"], metadata={"source": "grok-session-end"})
            stored += 1
        except Exception as e:
            print(f"[b12-session-end] Store error (preference): {e}", file=sys.stderr)

    # Oturum özeti (progress)
    if len(assistant_messages) > 5:
        try:
            summary = f"Session summary: {len(user_messages)} user messages, {len(assistant_messages)} assistant messages. Key topics discussed."
            merge_or_insert(f"[Progress] {summary}", tags=base_tags + ["type:progress"], metadata={"source": "grok-session-end", "session": session_id})
            stored += 1
        except Exception:
            pass

    print(f"[b12-session-end] SessionEnd completed. Total stored: {stored}", file=sys.stderr)
    sys.exit(0)


if __name__ == "__main__":
    main()