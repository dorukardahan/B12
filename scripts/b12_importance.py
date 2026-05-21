#!/usr/bin/env python3
"""
B12 ingest-time importance scoring.

Pure-Python heuristics that turn a raw memory content string into a single
importance value in [0.0, 0.95]. Designed to be called from the write-side
path (write_time_merge.merge_or_insert) before a row is INSERTed, so the
`importance` field on the memories table reflects the writer's intent
rather than depending on a downstream re-scoring pass.

Five named importance bands:

    IMPORTANCE_TRIVIAL    = 0.30  chit-chat, confirmations, micro-progress
    IMPORTANCE_BASELINE   = 0.50  default for plain content
    IMPORTANCE_FACT       = 0.70  structured fact density (dates, URLs, prices)
    IMPORTANCE_DECISION   = 0.75  decision-marker words present
    IMPORTANCE_MEMORABLE  = 0.90  explicit "remember this" tokens (EN + TR)

The bands sit on the upper side of the [0.5, 1.0] interval B12's recall
scorer uses — anything >= 0.7 should rank well above baseline content
when relevance is similar.

Tokens are case-insensitive; Turkish characters (ı, ş, ç, ö, ü, ğ) survive
the lower() call when the runtime is UTF-8 (Python 3 default).

Patterned after AytuncYildizli/B12 PR 24 (3534d0d, feat(scoring):
ingest-time importance heuristics) but slimmed to B12's single-agent
storage and Turkish-first token list. The fact-pattern regexes
(date / price / URL / email / phone) are the cross-portable surface.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# ── Importance bands ────────────────────────────────────────────────

IMPORTANCE_TRIVIAL: float = 0.30
IMPORTANCE_BASELINE: float = 0.50
IMPORTANCE_FACT: float = 0.70
IMPORTANCE_DECISION: float = 0.75
IMPORTANCE_MEMORABLE: float = 0.90
IMPORTANCE_CAP: float = 0.95

# ── Token lists ─────────────────────────────────────────────────────

# Explicit "remember this" tokens. Each match flips the score to
# IMPORTANCE_MEMORABLE. English + Turkish. Lowercased; matched with
# token boundaries so short Turkish phrases such as "not al" do not
# match ordinary English like "not allowed".
_REMEMBER_TOKENS: tuple[str, ...] = (
    "remember this", "remember that", "don't forget", "do not forget",
    "important note", "note this", "for the record", "keep in mind",
    # Turkish
    "hatırla", "unutma", "unutmayalım", "unutmayın",
    "kayda geç", "not al", "not alalım",
    "şunu unutmayalım", "şunu not edelim", "bunu hatırla",
    "lütfen not", "lütfen kaydet", "kaydetmiştik",
)

# Decision-marker words. Each match (in isolation, with word boundaries)
# floors the score at IMPORTANCE_DECISION.
_DECISION_TOKENS: tuple[str, ...] = (
    "decided", "decision", "agreed", "agreement",
    "going with", "chose", "settled on", "final call",
    # Turkish
    "karar verdik", "kararlaştırdık", "karar verildi",
    "anlaştık", "şuna karar", "konsensüs",
)

# Trivial tokens. When the only signal is one of these (alone or with
# fewer than 4 non-token chars besides), the score floors at TRIVIAL.
# Used as full-content match, not substring.
_TRIVIAL_EXACTS: frozenset[str] = frozenset({
    "ok", "okay", "yes", "no", "yep", "nope", "sure", "done",
    "evet", "hayır", "tamam", "tamamdır", "olur", "oldu",
    "anladım", "anlaşıldı", "süper", "harika", "teşekkür", "teşekkürler",
    "thanks", "thx",
})

# Structured-fact regex patterns. Each compiled pattern that matches
# at least once contributes 1 to the fact-density count. When count >= 1
# the score floors at IMPORTANCE_FACT (unless DECISION or MEMORABLE
# fired first — higher wins).
_FACT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(19|20|21)\d{2}\b"),                                       # year
    re.compile(r"\b\d{1,2}[/.\-]\d{1,2}(?:[/.\-]\d{2,4})?\b"),                 # short date
    re.compile(r"[\$€₺¥£]\s?\d+|\b\d+(?:[.,]\d+)?\s?(?:usd|tl|eur|gbp)\b",
               re.IGNORECASE),                                                # price
    re.compile(r"https?://\S+"),                                              # URL
    re.compile(r"\S+@\S+\.\S+"),                                              # email
    re.compile(r"\+\d{10,}|\b\d{3,4}[\s-]?\d{3,4}[\s-]?\d{2,4}\b"),            # phone-ish
)


# ── Result type ────────────────────────────────────────────────────


@dataclass(frozen=True)
class ImportanceBreakdown:
    """Per-signal breakdown returned by score_with_breakdown."""

    band: str        # name of the final band ("memorable" / "decision" / ...)
    score: float     # final importance in [0.0, IMPORTANCE_CAP]
    remember_hit: bool
    decision_hit: bool
    fact_hits: int


# ── Public API ────────────────────────────────────────────────────


def score(content: str | None) -> float:
    """Compute the ingest-time importance for a memory content string.

    Returns a float in [0.0, IMPORTANCE_CAP=0.95]. Empty / None / whitespace-only
    content returns IMPORTANCE_TRIVIAL.
    """
    return score_with_breakdown(content).score


def score_with_breakdown(content: str | None) -> ImportanceBreakdown:
    """Same as score() but also returns which signals fired."""
    if content is None:
        return ImportanceBreakdown("trivial", IMPORTANCE_TRIVIAL, False, False, 0)

    stripped = content.strip()
    if not stripped:
        return ImportanceBreakdown("trivial", IMPORTANCE_TRIVIAL, False, False, 0)

    lower = stripped.lower()

    # Exact-match trivial check (single word like "ok", "tamam")
    if lower in _TRIVIAL_EXACTS:
        return ImportanceBreakdown("trivial", IMPORTANCE_TRIVIAL, False, False, 0)

    remember_hit = any(_phrase_match(lower, tok) for tok in _REMEMBER_TOKENS)
    decision_hit = any(
        _word_match(lower, tok) for tok in _DECISION_TOKENS
    )

    fact_hits = 0
    for pattern in _FACT_PATTERNS:
        if pattern.search(stripped):
            fact_hits += 1

    # Pick the highest band that fired.
    if remember_hit:
        return ImportanceBreakdown(
            "memorable", IMPORTANCE_MEMORABLE, True, decision_hit, fact_hits
        )
    if decision_hit:
        return ImportanceBreakdown(
            "decision", IMPORTANCE_DECISION, False, True, fact_hits
        )
    if fact_hits >= 1:
        return ImportanceBreakdown(
            "fact", IMPORTANCE_FACT, False, False, fact_hits
        )

    return ImportanceBreakdown(
        "baseline", IMPORTANCE_BASELINE, False, False, 0
    )


# ── Helpers ───────────────────────────────────────────────────────


def _word_match(haystack: str, token: str) -> bool:
    """Match a token against a haystack with word boundaries.

    Used for decision tokens so that "decided" doesn't match "undecided"
    by accident. For multi-word tokens like "karar verdik" we fall back
    to plain substring (multi-word phrases have no ambiguity).
    """
    if " " in token:
        return token in haystack
    pattern = r"\b" + re.escape(token) + r"\b"
    return bool(re.search(pattern, haystack))


def _phrase_match(haystack: str, token: str) -> bool:
    pattern = r"(?<!\w)" + re.escape(token) + r"(?!\w)"
    return bool(re.search(pattern, haystack))


# ── CLI smoke-test ─────────────────────────────────────────────────


def _selftest() -> int:
    """Embedded smoke-test. Run via `python3 scripts/b12_importance.py --self-test`."""
    cases: list[tuple[str, float, str]] = [
        ("", IMPORTANCE_TRIVIAL, "empty"),
        ("ok", IMPORTANCE_TRIVIAL, "trivial-en"),
        ("tamam", IMPORTANCE_TRIVIAL, "trivial-tr"),
        ("just some plain text", IMPORTANCE_BASELINE, "baseline"),
        ("we decided to ship on Friday", IMPORTANCE_DECISION, "decision-en"),
        ("şuna karar verdik: cron 60s", IMPORTANCE_DECISION, "decision-tr"),
        ("contact me at foo@example.com or +905551234567", IMPORTANCE_FACT, "fact-multi"),
        ("see https://github.com/dorukardahan/B12", IMPORTANCE_FACT, "fact-url"),
        ("remember this: never amend a merged commit", IMPORTANCE_MEMORABLE, "remember-en"),
        ("lütfen not al: $0G launchı Mayıs", IMPORTANCE_MEMORABLE, "remember-tr"),
        ("hatırla, geçen sefer aynı hatayı yapmıştık", IMPORTANCE_MEMORABLE, "remember-tr-2"),
    ]
    failed = 0
    for content, expected, label in cases:
        out = score_with_breakdown(content)
        status = "OK"
        if abs(out.score - expected) > 1e-6:
            failed += 1
            status = f"FAIL (got {out.score}, expected {expected})"
        print(f"  [{status}] {label:18s}  band={out.band:10s}  score={out.score:.2f}  facts={out.fact_hits}  remember={out.remember_hit}  decision={out.decision_hit}")
    print()
    if failed:
        print(f"FAILED: {failed} / {len(cases)} cases")
        return 1
    print(f"PASSED: {len(cases)} / {len(cases)} cases")
    return 0


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--self-test":
        sys.exit(_selftest())
    if len(sys.argv) > 1:
        result = score_with_breakdown(" ".join(sys.argv[1:]))
        print(f"band={result.band} score={result.score:.2f} facts={result.fact_hits} "
              f"remember={result.remember_hit} decision={result.decision_hit}")
    else:
        print("usage: b12_importance.py [--self-test | content...]")
