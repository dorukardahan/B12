#!/usr/bin/env python3
"""
B12 LLM extraction — prompt templates, schema validation, transcript normalization.

Two surfaces:

  SYSTEM_PROMPT
      Memory Researcher persona. Bilingual (EN+TR). The transcript's
      language drives the output language; mixed transcripts default to
      whichever language has more characters.

  validate_extraction(line: str) -> dict | None
      Parses one JSONL line from the LLM, enforces the closed-set
      `type` field, clamps `importance` to one of the four bands, and
      truncates content to <= 600 chars with a "(truncated)" suffix.
      Returns None for malformed input.

  normalize_transcript(transcript_path, *, cap_chars) -> str
      Delegates to scripts/transcript_adapter.parse() (the canonical
      Claude/Codex unified parser) and emits a flat
        USER: ...
        ASSISTANT: ...
      stream. Last-N window after filtering, capped at `cap_chars`.
      Tool-result content is truncated to the first 200 chars per call.

The design doc (docs/B12_llm_extraction_design.md) refers to this
module as the place that "delegates to existing
scripts/b12_event_canonicalize.py" — that file does not exist in the
repo; the actual canonicalizer is transcript_adapter.parse(). The
delegation target was wrong in the design; the behavior described
(unified flat-text output) is what we implement here against
transcript_adapter.
"""

from __future__ import annotations

import json
import math
import os
import sys
from typing import Any

# ── Constants ───────────────────────────────────────────────────────

_TYPE_VALUES = {"decision", "learning", "gotcha", "preference", "fact"}
_IMPORTANCE_BANDS = (0.50, 0.70, 0.75, 0.90)
_CONTENT_MAX = 600
_REASON_MAX = 100
_TOOL_RESULT_HEAD = 200


SYSTEM_PROMPT = """You are the B12 Memory Researcher.

Your job: read a conversation transcript between a developer and an AI coding
assistant, and extract durable memories worth keeping for future sessions.

Extract ONLY:
- decisions ("we settled on X because Y")
- learnings ("we discovered that X behaves like Y under Z")
- gotchas ("X failed when Y; the trick was Z")
- preferences ("the developer prefers X over Y in this project")
- facts ("the production DB is at HOST; the API rate limit is N/min")

Skip:
- in-progress speculation ("maybe we should try X")
- tool output transcripts (file contents, command output)
- step-by-step task narration
- restatements of code that's visible in the transcript

For each extracted memory, output a JSON object on its own line:
{"type": "decision|learning|gotcha|preference|fact",
 "content": "<= 600 chars, self-contained, no references to 'we' / 'I' without context",
 "importance": 0.50 | 0.70 | 0.75 | 0.90,
 "reason": "<= 100 chars, why this is worth remembering"}

Output max 10 memories. If nothing notable, output nothing.

Language: match the transcript's language. If the transcript is mostly
Turkish, output content in Turkish. If mixed, prefer the more common one.
"""


# ── Validation ──────────────────────────────────────────────────────


def _clamp_importance(value: Any) -> float:
    """Snap an arbitrary numeric input to the nearest allowed band.

    Non-finite inputs (NaN, ±Inf) fall back to the baseline band 0.70
    rather than the first list element (which would silently degrade
    importance to 0.50). The midpoint policy is "favor the lower band
    on exact ties" — `min()` with a key returns the first element that
    achieves the minimum, and the bands tuple is sorted ascending.
    """
    try:
        v = float(value)
    except (TypeError, ValueError):
        return _IMPORTANCE_BANDS[1]
    if not math.isfinite(v):
        return _IMPORTANCE_BANDS[1]
    return min(_IMPORTANCE_BANDS, key=lambda b: abs(b - v))


def _truncate(text: str, limit: int, *, suffix: str = "…(truncated)") -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    keep = max(0, limit - len(suffix))
    return text[:keep] + suffix


def validate_extraction(line: str) -> dict | None:
    """Parse one JSONL line into a normalized extraction dict, or None.

    Closed-set enforcement on `type`. Importance clamped to one of four
    bands. Content truncated at 600 chars with `…(truncated)` suffix.
    Returns None on malformed JSON, missing fields, or out-of-set type.
    """
    if not line or not line.strip():
        return None
    try:
        obj = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None

    t = obj.get("type")
    if t not in _TYPE_VALUES:
        return None

    content = obj.get("content")
    if not isinstance(content, str) or not content.strip():
        return None
    content = _truncate(content, _CONTENT_MAX)

    importance = _clamp_importance(obj.get("importance"))

    reason = obj.get("reason") or ""
    if not isinstance(reason, str):
        reason = ""
    reason = _truncate(reason, _REASON_MAX, suffix="…")

    return {
        "type": t,
        "content": content,
        "importance": importance,
        "reason": reason,
    }


# ── Transcript normalization ───────────────────────────────────────


def _resolve_adapter():
    """Locate transcript_adapter regardless of how this module was imported.

    Importable both from the in-repo `scripts/` directory and from the
    deployed `~/.B12/hooks/scripts/` directory.
    """
    try:
        import transcript_adapter  # type: ignore[import-not-found]
        return transcript_adapter
    except ImportError:
        pass
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    try:
        import transcript_adapter  # type: ignore[import-not-found]
        return transcript_adapter
    except ImportError:
        return None


def normalize_transcript(transcript_path: str, *, cap_chars: int = 50000) -> str:
    """Read a JSONL transcript and emit a flat USER:/ASSISTANT: text stream.

    Filters out system messages. Truncates each tool-result excerpt to
    the first 200 chars. Applies a last-N character window so the most
    recent turns survive when the transcript is larger than `cap_chars`.

    Returns "" if the transcript is missing or empty. Never raises.
    """
    if not transcript_path or not os.path.isfile(transcript_path):
        return ""

    adapter = _resolve_adapter()
    if adapter is None:
        # Adapter unavailable — fall back to raw line read.
        try:
            with open(transcript_path, "r", encoding="utf-8", errors="replace") as f:
                raw = f.read()
        except OSError:
            return ""
        return raw[-cap_chars:]

    try:
        info, messages = adapter.parse(transcript_path)
    except Exception:
        return ""

    pieces: list[str] = []
    msg_tool_seen = False
    for msg in messages:
        role = getattr(msg, "role", "") or ""
        content = (getattr(msg, "content", "") or "").strip()
        if role not in ("user", "assistant"):
            continue
        if content:
            label = "USER" if role == "user" else "ASSISTANT"
            pieces.append(f"{label}: {content}")

        # Emit tool_uses even when the message body was empty —
        # assistant turns that contain only tool calls/results are
        # exactly where gotchas and command failures surface. (Codex
        # review on PR #17 round 1 flagged the prior content-first
        # skip.)
        for tu in getattr(msg, "tool_uses", []) or []:
            msg_tool_seen = True
            output = (getattr(tu, "output", "") or "").strip()
            if not output:
                continue
            head = output[:_TOOL_RESULT_HEAD]
            if len(output) > _TOOL_RESULT_HEAD:
                head += "…"
            tool_name = getattr(tu, "name", "tool") or "tool"
            pieces.append(f"[{tool_name}]: {head}")

    # Codex transcripts attach tools to SessionInfo._all_tools rather than
    # to per-message Message.tool_uses. Surface them when no per-message
    # tool_uses were seen, so LLM-extracted gotchas/errors from command
    # output don't get lost.
    if not msg_tool_seen:
        all_tools = getattr(info, "_all_tools", None) or []
        for tu in all_tools:
            output = (getattr(tu, "output", "") or "").strip()
            if not output:
                continue
            head = output[:_TOOL_RESULT_HEAD]
            if len(output) > _TOOL_RESULT_HEAD:
                head += "…"
            tool_name = getattr(tu, "name", "tool") or "tool"
            pieces.append(f"[{tool_name}]: {head}")

    flat = "\n".join(pieces).strip()
    if len(flat) <= cap_chars:
        return flat
    # Last-N window: drop earliest turns.
    return flat[-cap_chars:]


def detect_dominant_language(text: str) -> str:
    """Coarse Turkish-vs-other detector.

    Counts characters unique to Turkish (ı, ş, ç, ö, ü, ğ, İ, Ş, Ç, Ö, Ü, Ğ).
    Returns "tr" if >= 1 such character appears per 200 ascii letters in
    the text; otherwise "other". Cheap, no library dependency.
    """
    if not text:
        return "other"
    tr_chars = sum(1 for c in text if c in "ışçöüğİŞÇÖÜĞı")
    ascii_letters = sum(1 for c in text if c.isascii() and c.isalpha())
    if ascii_letters == 0:
        return "tr" if tr_chars > 0 else "other"
    ratio = tr_chars / ascii_letters
    return "tr" if ratio >= 0.005 else "other"


# ── CLI entry (debug) ──────────────────────────────────────────────


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--prompt":
        sys.stdout.write(SYSTEM_PROMPT)
        sys.exit(0)
    if len(sys.argv) > 2 and sys.argv[1] == "--normalize":
        cap = int(os.environ.get("B12_LLM_TRANSCRIPT_CAP_CHARS", "50000"))
        out = normalize_transcript(sys.argv[2], cap_chars=cap)
        sys.stdout.write(out)
        sys.exit(0)
    sys.stderr.write(
        "usage: b12_llm_prompts.py [--prompt | --normalize TRANSCRIPT_PATH]\n"
    )
    sys.exit(2)
