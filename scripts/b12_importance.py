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

# ── Phase-2 signal lexicons / patterns (EN + TR; 9 more langs in PR-2b) ──

# Explicit "save this" cues. Distinct from _REMEMBER_TOKENS — fires only when
# no legacy remember token matched (guarded in score_with_breakdown), floors
# at MEMORABLE.
_CUE_TOKENS: tuple[str, ...] = (
    "save this", "save it", "store this", "pin this", "bookmark",
    "mark this", "take note", "make a note",
    # Turkish
    "kaydet", "sakla", "saklayalım", "işaretle", "kaydedelim", "not düş",
)

# Commitment / obligation modals. Fire only when no legacy decision token
# matched (guarded). Multi-word / apostrophe tokens are substring-matched;
# single bare words use word boundaries.
_COMMIT_TOKENS: tuple[str, ...] = (
    "must", "will", "i'll", "we'll", "have to", "need to",
    "committing to", "going to",
    # Turkish obligation markers
    "zorunda", "zorunlu", "mecbur", "gerek", "lazım", "lazim",
    "şart", "sart", "yapacağım", "yapacagim", "edeceğiz", "edecegiz",
)
# Turkish "-malı/-meli" obligation suffix.
_COMMIT_TR_SUFFIX: re.Pattern[str] = re.compile(r"\b\w+(?:malı|meli|mali|meli)\b")
# Negated modals cancel the commitment signal (conservative: any negated
# modal in the content suppresses it — a rare both-modal sentence is acceptable
# loss for v1; documented).
_NEG_MODAL: re.Pattern[str] = re.compile(
    r"\b(?:won't|wont|will not|must not|mustn't|cannot|can't|shouldn't|"
    r"not going to|no need to)\b"
)

# Deadline / date. The legacy _FACT_PATTERNS already cover plain years and
# numeric dates; this adds ISO dates and the *relative* deadline keyword
# surface (future-oriented only — "ago"/"yesterday" intentionally excluded).
_DEADLINE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),                       # ISO date
    re.compile(r"\b\d{1,2}[./]\d{1,2}[./]\d{2,4}\b"),           # D.M.Y / D/M/Y
)
_DEADLINE_TOKENS: tuple[str, ...] = (
    "deadline", "due ", "due:", "due date", "expires", "expiry", "by end of",
    "no later than", "until ", "till ",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    # Turkish
    "son tarih", "vade", "teslim", "kadar", "bitiş tarihi", "bitis tarihi",
    "pazartesi", "salı", "sali", "çarşamba", "carsamba", "perşembe", "persembe",
    "cuma", "cumartesi", "pazar",
)

# Person mention — @handle or email local-part ONLY in Phase 2. The noisy
# capitalized-word + relationship-verb heuristic is deferred until the
# corpus audit (PR-2c) shows it matters.
_PERSON_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?<![\w.])@[A-Za-z0-9_]{2,}\b"),               # @handle
    re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"),                # email
)

# Numeric value — fires only when a magnitude/number co-occurs with a
# context word (prevents incidental digits flooring at FACT).
_NUMERIC_VALUE: re.Pattern[str] = re.compile(
    r"[\$€₺¥£]\s?\d|\b\d+(?:[.,]\d+)?\s?[kKmMbB]\b|\b\d{2,}\b|\b\d+\s?%",
)
_NUMERIC_CONTEXT: tuple[str, ...] = (
    "cost", "budget", "price", "revenue", "salary", "users", "count",
    "amount", "total", "fee", "rate", "percent",
    # Turkish
    "bütçe", "butce", "maliyet", "fiyat", "gelir", "maaş", "maas",
    "kullanıcı", "kullanici", "adet", "tutar", "oran", "ücret", "ucret",
)
_NUMERIC_MAX_LEN: int = 100_000  # skip numeric scan on very large content
_MAX_SCAN_LEN: int = 20_000      # cap regex scan window (O(n^2) backtracking guard)

# Identifiers. SHA = 7–64 hex; domains require a host/path shape (not a bare
# TLD); POSIX absolute paths. Floors at FACT.
_IDENTIFIER_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bPR#\d+\b", re.IGNORECASE),                   # PR#123
    re.compile(r"#\d{1,6}\b"),                                  # #123 issue/PR
    re.compile(r"\b[0-9a-f]{7,64}\b"),                          # git SHA
    re.compile(r"\b[\w.-]+\.[a-z]{2,}/\S+"),                    # host/path
    re.compile(r"(?:^|\s)(/\w[\w/.-]+)"),                       # POSIX abs path
)

# Secret / credential shapes. On match the importance boost is SKIPPED and
# secret_suspected is flagged — the value is NEVER stored or logged (public-repo
# secrets rule). Checked before identifiers so a key never floors at FACT.
_DANGEROUS_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(?:sk|pk)_(?:live|test)_[A-Za-z0-9]{8,}"),   # stripe-style
    re.compile(r"\bghp_[A-Za-z0-9]{16,}"),                      # github PAT
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),              # slack token
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),               # aws key id
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),          # PEM private key
    re.compile(r"\bey[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),  # JWT
    re.compile(r"\b(?:api[_-]?key|secret|token|password|passwd)\s*[:=]\s*\S{12,}",
               re.IGNORECASE),                                  # key=value secret
)


# ── Result type ────────────────────────────────────────────────────


@dataclass(frozen=True)
class ImportanceBreakdown:
    """Per-signal breakdown returned by score_with_breakdown.

    The first five fields are the original (pre-Phase-2) contract; every
    construction site passes them positionally. The Phase-2 signal fields are
    APPENDED with defaults so those 5-positional calls keep working unchanged.
    None of these fields store the matched content — only booleans / the final
    band — so a breakdown's repr can never echo a detected secret value.
    """

    band: str        # name of the final band ("memorable" / "decision" / ...)
    score: float     # final importance in [0.0, IMPORTANCE_CAP]
    remember_hit: bool
    decision_hit: bool
    fact_hits: int
    # ── Phase-2 signal fields (appended; default False/"en"/0) ──────────
    commitment_hit: bool = False   # modal/obligation verb (guarded by decision)
    deadline_hit: bool = False     # ISO/relative date or deadline keyword
    person_hit: bool = False       # @handle or email local-part
    cue_hit: bool = False          # explicit "save/store this" (guarded by remember)
    numeric_hit: bool = False      # number + context word (cost/budget/users/...)
    identifier_hit: bool = False   # PR#/SHA/host-path/abs-path
    lang_detected: str = "en"      # populated in PR-2b; "en" placeholder for now
    secret_suspected: bool = False  # api-key/token shape seen; boost skipped, never logged


# ── Public API ────────────────────────────────────────────────────


def score(content: str | None) -> float:
    """Compute the ingest-time importance for a memory content string.

    Returns a float in [0.0, IMPORTANCE_CAP=0.95]. Empty / None / whitespace-only
    content returns IMPORTANCE_TRIVIAL.
    """
    return score_with_breakdown(content).score


def score_with_breakdown(content: str | None) -> ImportanceBreakdown:
    """Same as score() but also returns which signals fired.

    Band resolution is max-wins (the highest band any signal fires takes the
    score); the result is clamped to [0, IMPORTANCE_CAP] so the read-path RET-3
    dual-scale normalization is never perturbed. Phase-2 signals are GUARDED:
    the new MEMORABLE/DECISION-equivalent detectors fire only when the legacy
    remember/decision tokens did not, so breakdown booleans stay truthful and
    nothing is double-counted.
    """
    if content is None:
        return ImportanceBreakdown("trivial", IMPORTANCE_TRIVIAL, False, False, 0)

    stripped = content.strip()
    if not stripped:
        return ImportanceBreakdown("trivial", IMPORTANCE_TRIVIAL, False, False, 0)

    # Bound the regex scan: several patterns here and in the legacy fact set
    # (e.g. the email `\S+@\S+\.\S+`) backtrack O(n^2) on long no-match runs.
    # Real memories are short and importance signals appear early, so scanning
    # a prefix is both safe and a hard guard against pathological content.
    scan = stripped[:_MAX_SCAN_LEN]
    lower = scan.lower()

    # Exact-match trivial check (single word like "ok", "tamam")
    if lower in _TRIVIAL_EXACTS:
        return ImportanceBreakdown("trivial", IMPORTANCE_TRIVIAL, False, False, 0)

    # ── Legacy signals (unchanged; now on the bounded scan window) ──
    remember_hit = any(_phrase_match(lower, tok) for tok in _REMEMBER_TOKENS)
    decision_hit = any(_word_match(lower, tok) for tok in _DECISION_TOKENS)
    fact_hits = sum(1 for p in _FACT_PATTERNS if p.search(scan))

    # ── Phase-2 signals (guarded so they never double-count) ────────
    cue_hit = (not remember_hit) and _detect_cue(lower)
    commitment_hit = (not decision_hit) and _detect_commitment(lower)
    deadline_hit = _detect_deadline(lower)
    person_hit = _detect_person(scan)
    numeric_hit = _detect_numeric(lower)
    identifier_hit, secret_suspected = _detect_identifiers(scan)

    def _build(band: str, value: float) -> ImportanceBreakdown:
        return ImportanceBreakdown(
            band, min(value, IMPORTANCE_CAP), remember_hit, decision_hit, fact_hits,
            commitment_hit=commitment_hit, deadline_hit=deadline_hit,
            person_hit=person_hit, cue_hit=cue_hit, numeric_hit=numeric_hit,
            identifier_hit=identifier_hit, lang_detected="en",
            secret_suspected=secret_suspected,
        )

    # Pick the highest band that fired (max-wins).
    if remember_hit or cue_hit:
        return _build("memorable", IMPORTANCE_MEMORABLE)
    if decision_hit or commitment_hit:
        return _build("decision", IMPORTANCE_DECISION)
    if deadline_hit or fact_hits >= 1 or person_hit or numeric_hit or identifier_hit:
        return _build("fact", IMPORTANCE_FACT)
    return _build("baseline", IMPORTANCE_BASELINE)


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


def _token_in(haystack: str, token: str) -> bool:
    """Substring for multi-word / apostrophe tokens; word boundaries otherwise."""
    if " " in token or "'" in token:
        return token in haystack
    return _word_match(haystack, token)


# ── Phase-2 signal detectors (each returns bool; identifiers returns a pair) ──


def _detect_cue(lower: str) -> bool:
    """Explicit save/store cue → MEMORABLE (guarded by remember in caller)."""
    return any(_token_in(lower, tok) for tok in _CUE_TOKENS)


def _detect_commitment(lower: str) -> bool:
    """Modal/obligation verb → DECISION (guarded by decision in caller).

    A negated modal anywhere in the content cancels the signal (conservative;
    a rare sentence carrying both a negated and an un-negated modal is an
    accepted false-negative for v1).
    """
    if _NEG_MODAL.search(lower):
        return False
    if any(_token_in(lower, tok) for tok in _COMMIT_TOKENS):
        return True
    return bool(_COMMIT_TR_SUFFIX.search(lower))


def _detect_deadline(lower: str) -> bool:
    """ISO/numeric date or a future-oriented deadline keyword → FACT."""
    if any(p.search(lower) for p in _DEADLINE_PATTERNS):
        return True
    return any(_token_in(lower, tok) for tok in _DEADLINE_TOKENS)


def _detect_person(stripped: str) -> bool:
    """@handle or email local-part → FACT (capitalized-word heuristic deferred)."""
    return any(p.search(stripped) for p in _PERSON_PATTERNS)


def _detect_numeric(lower: str) -> bool:
    """Number + context word → FACT. Skipped on very large content."""
    if len(lower) > _NUMERIC_MAX_LEN:
        return False
    if not _NUMERIC_VALUE.search(lower):
        return False
    return any(ctx in lower for ctx in _NUMERIC_CONTEXT)


def _detect_identifiers(stripped: str) -> tuple[bool, bool]:
    """Return (identifier_hit, secret_suspected).

    A credential-shaped token short-circuits: the importance boost is skipped
    (identifier_hit stays False) and secret_suspected is flagged. The matched
    value is never returned, stored, or logged.
    """
    if any(p.search(stripped) for p in _DANGEROUS_PATTERNS):
        return (False, True)
    return (any(p.search(stripped) for p in _IDENTIFIER_PATTERNS), False)


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
        # ── Phase-2 signal cases (EN + TR) ──────────────────────────
        ("save this for later: the prod endpoint", IMPORTANCE_MEMORABLE, "cue-en"),
        ("sakla bunu: prod endpoint", IMPORTANCE_MEMORABLE, "cue-tr"),
        ("I must finish the audit this week", IMPORTANCE_DECISION, "commit-en"),
        ("bunu yapmak zorundayız, gerek var", IMPORTANCE_DECISION, "commit-tr"),
        ("the invoice is due Friday", IMPORTANCE_FACT, "deadline-en"),
        ("son tarih Pazartesi, atlamayalım", IMPORTANCE_FACT, "deadline-tr"),
        ("ping @alice about the rollout", IMPORTANCE_FACT, "person-handle"),
        ("the budget is 50k for 500 users", IMPORTANCE_FACT, "numeric-ctx"),
        ("fixed in PR#123", IMPORTANCE_FACT, "identifier-pr"),
        ("just chatting about the weather", IMPORTANCE_BASELINE, "no-signal"),
        ("sk_live_abc123DEF456ghi789jkl012mno345pqr", IMPORTANCE_BASELINE, "secret-skip"),
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
