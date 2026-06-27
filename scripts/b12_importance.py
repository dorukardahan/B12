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

import math
import os
import re
import unicodedata
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
    re.compile(r"https?://\S{1,2048}"),                                       # URL
    re.compile(r"[^\s@]{1,64}@[^\s@]{1,255}\.[^\s@]{1,64}"),                   # email (bounded: linear, no O(n^2))
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
    "must", "i'll", "we'll", "have to", "need to", "committing to",
    # "<subject> will" — bare "will" is also a name ("Will reviewed the patch")
    # and a noun ("free will"), and bare "going to" is usually movement
    # ("going to the store"), so require a subject pronoun to catch the
    # future/commitment sense without the false positives.
    "i will", "we will", "you will", "they will", "he will", "she will",
    "i am going to", "we are going to", "i'm going to", "we're going to",
    # Turkish bare-word markers. zorunda/zorunlu/mecbur/lazım are matched
    # inflection-aware by _COMMIT_TR_WORDS below (so "zorundayız"/"mecburuz"/
    # "lazımdır" fire). gerek/şart stay bare — their derivations are ambiguous
    # ("gerekçe"=justification, "gereksiz"=unnecessary, "şartlar"=conditions).
    "gerek", "şart", "sart", "yapacağım", "yapacagim", "edeceğiz", "edecegiz",
)
# Inflection-aware Turkish obligation stems + common copular endings.
_COMMIT_TR_WORDS: re.Pattern[str] = re.compile(
    r"\b(?:zorunda|zorunlu|mecbur|lazım|lazim)"
    r"(?:y[ıiu]z|y[ıiu]m|sın|sin|sınız|siniz|d[ıiuü]r|d[ıi]|[uü]z)?\b"
)
# Turkish "-malı/-meli" obligation suffix, allowing the common personal
# endings (yapmalı / yapmalıyız / yapmalısın / etmeliyim / gitmeliler ...).
# Endings are enumerated (not a bare \w*) so noun forms that merely contain the
# letters — "maliyet", "normalimiz", "önemli" — do NOT match.
_TR_PERSON_END = r"(?:y[ıiu]z|y[ıiu]m|sın|sin|sınız|siniz|lar|ler|dır|dir|dur|dür)"
_COMMIT_TR_SUFFIX: re.Pattern[str] = re.compile(
    # Verb obligation: stem + -malı/-meli, optionally with a personal ending
    # (yapmalı / yapmalıyız / yapmalısın / etmeliyim / beklemeliyiz). The dotless
    # "ı" / front "e" are distinctive enough to match bare. The ASCII "mali"
    # variant REQUIRES a personal ending, so accusative nouns ("ihtimali",
    # "normali", "kemali") and the word "mali" (financial) do NOT match.
    r"\b\w+(?:(?:malı|meli)" + _TR_PERSON_END + r"?|mali" + _TR_PERSON_END + r")\b"
)
# Turkish negated obligation — an obligation WORD closely followed by "değil"/
# "yok" (local negation), so an unrelated değil/yok elsewhere does not cancel a
# real obligation. Bounded gap → linear.
_TR_NEG_OBLIGATION: re.Pattern[str] = re.compile(
    # \w* after each lets inflected forms match (zorundayız, değiliz, yoktur).
    # değil/yok must follow within the SAME clause (space-separated, optionally
    # after one Turkish particle) — "zorunda da değiliz" / "gerek de yok" negate,
    # but a comma clause break to an unrelated negation does NOT ("zorundayız,
    # risk yok" still commits).
    r"\b(?:zorunda|zorunlu|mecbur|gerek|lazım|lazim|şart|sart)\w*"
    r"(?:\s+(?:da|de|bile|hiç|hic|asla|artık|artik))?"
    r"\s+(?:değil|degil|yok)\w*"
    # negated "be forced to": "zorunda kalma" + a NEGATIVE-tense suffix
    # (kalmayacağız / kalmadık / kalmaz) — NOT the positive infinitive/gerund
    # "kalmak"/"kalmanız".
    r"|\bzorunda\s+kalma(?:yaca[kğg]|d[ıi]|z)\w*"
)
# Negative -mamalı/-memeli obligation infix ("yapmamalıyız" = we must NOT).
_TR_NEG_SUFFIX: re.Pattern[str] = re.compile(r"\b\w*(?:mamalı|memeli|mamali)")
# Negated modals cancel the commitment signal (conservative: any negated
# modal in the content suppresses it — a rare both-modal sentence is acceptable
# loss for v1; documented). Covers EN modal negations AND "don't/doesn't/do not
# have to / need to" (common non-commitments that reverse the meaning).
_NEG_MODAL: re.Pattern[str] = re.compile(
    r"\b(?:won't|wont|will not|must not|mustn't|cannot|can't|shouldn't|"
    r"not going to|no need to|"
    # negated have-to/need-to, allowing up to 3 intervening words/punctuation so
    # "don't really have to" / "do not necessarily need to" / "should not have to"
    # / "do not, however, need to" are all suppressed.
    r"(?:do|does|did|should|would|could)(?:n't| not)(?:[\s,]+\w+){0,3}[\s,]+(?:have|need) to|"
    # negated future: "will/'ll ... not|never" ("we will never", "we'll never",
    # "we will, however, not").
    r"(?:will|'ll)(?:[\s,]+\w+){0,3}[\s,]+(?:not|never))\b"
)
# "<subject> will/'ll see" is a deferral idiom ("we'll see", "we'll see how it
# goes"), NOT a commitment. Matches the deferral shape only — clause-end ("we'll
# see[.]") or a deferral continuation (see how/what/whether/if/about ...) — so a
# LITERAL "see <object>" commitment is preserved ("I'll see you tomorrow", "I will
# see Alice on Monday" stay DECISION). The continuation is consumed up to the next
# CLAUSE punctuation (sentence terminator OR comma), which INCLUDES coordinated
# alternatives inside the same clause ("we'll see whether we will deploy or we will
# rollback") so _detect_commitment's rescan does not re-trigger on a modal INSIDE
# the deferral. A commitment in a SEPARATE clause — after a sentence terminator
# ("...how it goes. We must migrate.") or a comma ("...about it, but we must
# decide") or BEFORE the hedge ("we'll deploy, then we'll see ...") — still fires.
# (Accepted limitation: an UNPUNCTUATED run-on that coordinates a real commitment
# onto a deferral, "we'll see how it goes and we must ship", is treated as part of
# the deferral — a rare, low-stakes band miss.) Audit #11 + Codex review.
_HEDGE_FUTURE: re.Pattern[str] = re.compile(
    r"\b(?:i|we|you|they|he|she)(?:'ll|\s+will)\s+see\b"
    # Consume the deferral continuation up to the next clause separator. Besides
    # sentence terminators and commas, a colon, a newline, or an em/en dash also
    # bounds a clause — excluding them keeps a real commitment on the other side
    # ("we'll see how it goes: we must migrate", "... — we must migrate", or
    # "must migrate" on the next line) out of the hedge.
    # Consume the continuation but STOP before a spaced ASCII hyphen "<space>-"
    # (a clause separator like "... - we must migrate") via the tempered (?!\s-),
    # while a hyphen INSIDE a word ("re-deploy", "well-known") has no preceding
    # space and is consumed normally. Em/en dashes stop via the char class.
    # A comma or spaced dash may precede the wh-word and it's still the deferral
    # ("we'll see, if we need to migrate", "we'll see - how it goes" — Codex PR #140).
    r"(?:[\s,]+(?:[-–—]\s*)?(?:how|what|whether|if|about|when|where|who|whom|whose|which|why)\b"
    # Consume to the next clause boundary, STOPPING before a spaced ASCII hyphen
    # (tempered `(?!\s-)`; an intra-word hyphen like "re-deploy" is consumed) and
    # before a " but " contrast clause (a real commitment, "...goes but we must X",
    # Codex PR #140). Em/en dashes, comma, colon, terminators, parens stop via the
    # char class.
    r"(?:(?!\s-|\s+but\b)[^.!?;,:\n\r–—()\[\]])*)?"
    # A newline directly after the deferral is itself a clause boundary (a multiline
    # memory whose next line is NOT a commitment, "we'll see how it goes\nmaybe
    # later", must still strip the hedge so the bare "we'll" doesn't score DECISION).
    # A spaced ASCII hyphen, " but ", and an opening parenthesis/bracket also bound
    # the clause ("we'll see how it goes (we must migrate)" surfaces the must).
    r"(?=\s*$|[\n\r]|\s+but\b|\s*[-.,!?;:–—()\[\]])"
)

# Deadline / date. The legacy _FACT_PATTERNS already cover plain years and
# numeric dates; this adds ISO dates and the *relative* deadline keyword
# surface (future-oriented only — "ago"/"yesterday" intentionally excluded).
_DEADLINE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),                       # ISO date
    re.compile(r"\b\d{1,2}[./]\d{1,2}[./]\d{2,4}\b"),           # D.M.Y / D/M/Y
    # "due" only in a real deadline context — followed by a temporal word/number
    # ("due tomorrow", "due by 2026-07-01") or as the predicate "is/are due".
    # This excludes the idioms ("due diligence/process/credit"), causal "due to",
    # and negated "not due" that bare "due" used to mis-fire on. ("due Friday"
    # is still caught by the weekday token.)
    re.compile(r"\bdue\s+(?:(?:on|by|before|today|tonight|tomorrow|next|this)\b|\d)"),
    # "due date(s)" as an explicit deadline noun, singular or plural ("due date is
    # Friday", "due dates: Friday and Monday") — the "due <temporal>" form doesn't
    # cover the noun "date(s)" (Codex PR #140).
    re.compile(r"\bdue\s+dates?\b"),
    # "by (the) end of <temporal>" with a real temporal target ("by end of day",
    # "by the end of the week/month/quarter"). A bare "by end of" token over-fired
    # on non-temporal objects ("by end of the book/meeting"), so it's gone — the
    # weekday form is handled by the weekday-context pattern below (Codex PR #140).
    re.compile(
        r"\bby\s+(?:the\s+)?end\s+of\s+(?:the\s+)?"
        r"(?:day|week|month|year|quarter|sprint|today|tomorrow|business\s+day|eod|cob)\b"
    ),
    # predicate "is/are due" only when "due" ends the clause (end / punctuation)
    # or is followed by a temporal word/number — so "the report is due[.]" /
    # "is due tomorrow" fire, but the idioms "is due diligence/process/credit"
    # and the causal "is due to" do not.
    re.compile(
        r"\b(?:is|are|was|were|it'?s|they'?re)\s+due"
        r"(?=\s*$|\s*[.,!?;:)]|\s+(?:on|by|before|today|tonight|tomorrow|next|this)\b|\s+\d)"
    ),
    # A weekday is a deadline ONLY in a deadline context — a deadline preposition
    # before it ("by Friday", "before Monday", "due Tuesday", "until next Friday").
    # A BARE weekday is not a deadline ("we met last Monday", "standup every
    # Tuesday", "Happy Friday", "Monday morning sync") — audit #11. ("on Monday"
    # is intentionally excluded: it is as often past/scheduling as a deadline.)
    re.compile(
        r"\b(?:by|before|until|till|due|no later than)\s+"
        # optional "(the) end of" so "by (the) end of Friday" matches via the weekday
        # target — WITHOUT a bare "by the end of" token that fired on non-temporal
        # objects ("by the end of the meeting/book", Codex PR #140).
        r"(?:(?:the\s+)?end\s+of\s+)?"
        # optional time qualifier between the preposition and the weekday
        # ("by noon Friday", "before 5pm Friday", "by 9 Monday")
        r"(?:(?:noon|midnight|eod|\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\s+)?"
        # "on" is allowed here ONLY after the leading preposition ("by noon on
        # Friday") — a BARE "on Friday" still doesn't match (no leading by/before/...).
        r"(?:next\s+|this\s+|on\s+)?"
        r"(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b"
    ),
    # Turkish weekday deadline: "<weekday>(dative) kadar" ("cumaya kadar" = by
    # Friday), the day-noun form "<weekday> gününe kadar", and the time-of-day form
    # "<weekday> <akşam/sabah/öğle/gece>(dative) kadar" ("cuma akşamına kadar" = by
    # Friday evening, "pazartesi sabahına kadar" = by Monday morning). The dative is
    # enumerated (direct ye/ya/a; intervening noun = a known day/time stem + inflection
    # ENDING in a dative a/e) — never a bare \w* — so the COMPARATIVE "kadar"
    # ("pazarlama kadar önemli", "cuma kahve kadar güzel") does NOT mis-fire: neither
    # "pazarlama" nor the non-time noun "kahve" matches the stem set.
    re.compile(
        r"\b(?:"
        # Unambiguous weekdays: direct dative, day/time noun, or numeric time.
        r"(?:pazartesi|salı|sali|çarşamba|carsamba|perşembe|persembe|cuma|cumartesi)"
        r"(?:'?(?:ye|ya)"
        r"|\s+(?:g[üu]n[üu]?\s+)?(?:g[üu]n|son|akşam|aksam|sabah|öğle|ogle|öğlen|oglen|gece)\w*[ae]"
        # numeric time after the weekday ("cuma 5'e kadar", "pazartesi 17:00'ye kadar")
        r"|\s+\d{1,2}(?::\d{2})?'?\w*)"
        # "pazar" is ambiguous (Sunday vs "market"), so it requires an explicit
        # day/time noun ("pazar gününe/akşamına kadar") — NOT the bare dative
        # "pazara" (= "to the market", "pazara kadar yürüdük") — Codex PR #140.
        r"|pazar\s+(?:g[üu]n[üu]?\s+)?(?:g[üu]n|son|akşam|aksam|sabah|öğle|ogle|öğlen|oglen|gece)\w*[ae]"
        r")"
        # "dek" is a literary synonym of "kadar" (until) — same deadline sense.
        r"\s+(?:kadar|dek)\b"
    ),
)
_DEADLINE_TOKENS: tuple[str, ...] = (
    # Single words are matched with word boundaries by _token_in (so "till"
    # never matches inside "still"); only the genuinely multi-word phrases below
    # are substring-matched. ("due" + weekdays are handled by patterns above so
    # the causal "due to" and bare/past weekday mentions don't mis-fire — #11.)
    "deadline", "expires", "expiry",
    "no later than", "until", "till",
    # "due date(s)" → \bdue\s+dates?\b pattern; "by (the) end of <temporal>" and
    # "<weekday>" → the patterns above. There is NO bare "by end of" / "by the end
    # of" token: both over-fired on non-temporal objects ("by end of the book",
    # "by the end of the meeting") — Codex PR #140 / #141 retro.
    # Weekdays are NOT bare tokens — a weekday only signals a deadline inside the
    # deadline-context patterns above ("by Friday" / "cumaya kadar"), audit #11.
    # Turkish explicit deadline words.
    # NB: bare "kadar" is intentionally NOT a deadline token — it is far more
    # common outside deadlines ("ne kadar güzel", "bu kadar yeter"). The
    # "<weekday>(dative) kadar" deadline sense is carried by the pattern above.
    "son tarih", "vade", "teslim", "bitiş tarihi", "bitis tarihi",
)

# Person mention — @handle or email local-part ONLY in Phase 2. The noisy
# capitalized-word + relationship-verb heuristic is deferred until the
# corpus audit (PR-2c) shows it matters.
_PERSON_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?<![\w.])@[A-Za-z0-9_]{2,64}\b"),            # @handle
    re.compile(r"\b[\w.+-]{1,64}@[\w-]{1,255}\.[\w.-]{1,64}\b"),  # email (bounded: linear, no O(n^2))
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
_MAX_SCAN_LEN: int = 20_000      # cap regex scan window (defence-in-depth; the
                                 # bounded quantifiers above are the real O(n^2) guard)

# Identifiers. SHA = 7–64 hex; domains require a host/path shape (not a bare
# TLD); POSIX absolute paths. Floors at FACT.
_IDENTIFIER_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bPR#\d+\b", re.IGNORECASE),                   # PR#123
    re.compile(r"#\d{1,6}\b"),                                  # #123 issue/PR
    # git SHA: 7-64 hex that has BOTH a digit and an a-f letter, so all-alpha
    # words ("deadbeef", "facade") and bare numbers ("1234567") don't match.
    re.compile(r"\b(?=[0-9a-f]{7,64}\b)(?=[0-9a-f]*[0-9])(?=[0-9a-f]*[a-f])[0-9a-f]{7,64}\b"),
    re.compile(r"\b[\w.-]{1,128}\.[a-z]{2,24}/\S{1,256}"),      # host/path (bounded: linear)
    re.compile(r"(?:^|\s)/\w[\w.-]*/\S{1,256}"),                # POSIX abs path (needs an interior slash, not bare /etc)
)

# Secret / credential shapes. On match the importance boost is SKIPPED and
# secret_suspected is flagged so credential-bearing content is not amplified.
# The canonical list lives in b12_pii_scrubber (the authoritative redactor that
# runs on every write path BEFORE scoring); reuse it so the two never drift and
# every provider key it knows (sk-ant-/sk-proj-/AIza/Bearer/...) is covered here
# too. The local set below is only a standalone fallback if that import fails.
# All these rules are anchored on specific literals, so running them over the
# full content stays O(n) without a marker pre-filter.
_DANGEROUS_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(?:sk|pk)_(?:live|test)_[A-Za-z0-9]{8,}"),   # stripe-style
    re.compile(r"\bsk-(?:ant|proj)-[A-Za-z0-9_-]{20,}"),        # anthropic / openai-project
    re.compile(r"\bsk-[A-Za-z0-9]{32,}"),                       # openai classic
    re.compile(r"\bghp_[A-Za-z0-9]{16,}"),                      # github PAT
    re.compile(r"\bAIza[A-Za-z0-9_-]{20,}"),                    # google api key
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._-]{16,}"),          # bearer token
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),              # slack token
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),               # aws key id
    re.compile(r"-----BEGIN [A-Z ]{0,40}PRIVATE KEY-----"),     # PEM private key (header-only, bounded)
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),  # JWT
    re.compile(r"\b(?:api[_-]?key|secret|token|password|passwd)\s*[:=]\s*\S{12,}",
               re.IGNORECASE),                                  # key=value secret
)
# Header-only PEM detector — bounded, so it stays linear on a pathological blob
# of many "-----BEGIN ... PRIVATE KEY-----" headers (detection needs the header,
# not the whole block — the scrubber keeps the block-spanning rule for redaction).
_PEM_HEADER: re.Pattern[str] = re.compile(r"-----BEGIN [A-Z ]{0,40}PRIVATE KEY-----")
try:
    from b12_pii_scrubber import _PATTERNS as _SCRUBBER_PATTERNS
    # Reuse the scrubber's credential shapes for detection, EXCEPT its
    # pem_private_key rule (its lazy `[\s\S]*?` span-to-END is O(k·n) on a blob
    # with many BEGIN headers); substitute the bounded header-only matcher above.
    _SECRET_REGEXES: tuple[re.Pattern[str], ...] = tuple(
        p for label, p in _SCRUBBER_PATTERNS if label != "pem_private_key"
    ) + (_PEM_HEADER,)
except Exception:
    _SECRET_REGEXES = _DANGEROUS_PATTERNS


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
    lang_detected: str = "en"      # multilingual lang that fired (first in scan order
                                   # zh,hi,ar,ru,es,fr,pt,de,id; "en" = legacy/none).
                                   # ES/PT share tokens, so it reports the first to fire.
    secret_suspected: bool = False  # api-key/token shape seen; boost skipped, never logged


# ── Public API ────────────────────────────────────────────────────


def score(content: str | None, lang_code: str | None = None) -> float:
    """Compute the ingest-time importance for a memory content string.

    Returns a float in [0.0, IMPORTANCE_CAP=0.95]. Empty / None / whitespace-only
    content returns IMPORTANCE_TRIVIAL. `lang_code` optionally restricts the
    multilingual lexicon check to one language (default: auto-detect by script).
    """
    return score_with_breakdown(content, lang_code).score


def is_secret(content: str | None) -> bool:
    """True if the content looks credential-bearing (raw or already scrubbed).

    Callers that combine importance from multiple sources (e.g. an LLM-supplied
    value max'd with the heuristic) must use this to CAP credential-bearing
    content at baseline — `score()` already returns baseline, but a `max(...)`
    against a higher supplied value would otherwise re-amplify it.
    """
    if not content:
        return False
    return _detect_secret(content)


# memory_type -> importance floor. A meaningful type already signals value at
# WRITE time (the PR-2c audit found ~97% of the importance gap is typed), so the
# type floors the score when the content carries no explicit keyword cue.
#
# Covers the repo's actual type vocabulary, since finalize_importance is fed types
# from three paths that don't all normalize: the canonical types from
# classification (shared_patterns._PREFIX_MAP / the classifier-corpus LABEL_MAP —
# decision / error_fix / learning / preference / observation / knowledge), the LLM
# classifier's RAW output (memory_store passes resp["type"] un-normalized: gotcha /
# fact / ...), and whatever raw label a caller passes directly as the type field
# (architecture / pattern / infra / bugfix / feedback / progress / ...). All the
# raw aliases are floored to the same band as the canonical type they normalize to.
# Generic / bulk types (general / note / chat / session_summary / handoff) get no
# floor.
_TYPE_FLOOR: dict[str, float] = {
    # decision band
    "decision": IMPORTANCE_DECISION,
    # fact band — canonical types
    "error_fix": IMPORTANCE_FACT,
    "learning": IMPORTANCE_FACT,
    "preference": IMPORTANCE_FACT,
    "observation": IMPORTANCE_FACT,
    "knowledge": IMPORTANCE_FACT,
    # fact band — raw aliases a caller or the LLM classifier can pass un-normalized
    # (the keys/values of shared_patterns._PREFIX_MAP and the classifier-corpus
    # LABEL_MAP that resolve to a floored canonical type), matched by exact lookup.
    "error": IMPORTANCE_FACT,           # -> error_fix
    "error fix": IMPORTANCE_FACT,       # -> error_fix (prefix form)
    "bugfix": IMPORTANCE_FACT,          # -> error_fix
    "gotcha": IMPORTANCE_FACT,          # -> learning (also an LLM raw label)
    "fact": IMPORTANCE_FACT,            # LLM raw label
    "feedback": IMPORTANCE_FACT,        # -> preference
    "progress": IMPORTANCE_FACT,        # -> observation
    "architecture": IMPORTANCE_FACT,    # -> knowledge
    "pattern": IMPORTANCE_FACT,         # -> knowledge
    "reference": IMPORTANCE_FACT,       # -> knowledge
    "review": IMPORTANCE_FACT,          # -> knowledge
    "audit": IMPORTANCE_FACT,           # -> knowledge
    "test": IMPORTANCE_FACT,            # -> knowledge
    "infra": IMPORTANCE_FACT,           # -> knowledge
    "content_decision": IMPORTANCE_FACT,  # -> knowledge
}


def _normalize_supplied(raw: object) -> float:
    """RET-3 read-path normalization, used to COMPARE a caller/level value
    against the fractional heuristic. Bool / non-numeric / NaN / inf -> baseline;
    a level multiplier (>= 1.0) is halved; result clamped to [0, 1]."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return IMPORTANCE_BASELINE
    v = float(raw)
    if not math.isfinite(v):
        return IMPORTANCE_BASELINE
    if v >= 1.0:
        v = v / 2.0
    return max(0.0, min(1.0, v))


def finalize_importance(content: str | None, supplied: object = None,
                        memory_type: str | None = None) -> float:
    """Single chokepoint for the importance a writer should store.

    - A detected secret CAPS at baseline (overriding the type floor, the
      heuristic, and any caller/LLM-supplied value) — credential content is never
      amplified, on whichever write path calls this.
    - Otherwise the result is the strongest of: the heuristic `score(content)`,
      the `memory_type` floor, and any caller/LLM-supplied value. Caller vs
      heuristic is compared on the RET-3-normalized scale, and the winner's RAW
      value is returned so a level multiplier keeps its scale for the read path.

    Every Python writer (MCP store, write_time_merge, checkpoint, PreCompact, CLI)
    should route through this so the secret cap and the type floor are uniform.
    """
    if is_secret(content):
        return IMPORTANCE_BASELINE
    heuristic = max(score(content), _TYPE_FLOOR.get((memory_type or "").lower(), 0.0))
    if isinstance(supplied, bool) or not isinstance(supplied, (int, float)):
        return heuristic
    if not math.isfinite(float(supplied)):
        return heuristic
    return float(supplied) if _normalize_supplied(supplied) >= heuristic else heuristic


def score_with_breakdown(content: str | None, lang_code: str | None = None) -> ImportanceBreakdown:
    """Same as score() but also returns which signals fired.

    Band resolution is max-wins (the highest band any signal fires takes the
    score); the result is clamped to [0, IMPORTANCE_CAP] so the read-path RET-3
    dual-scale normalization is never perturbed. Phase-2 signals are GUARDED:
    the new MEMORABLE/DECISION-equivalent detectors fire only when the legacy
    remember/decision tokens did not, so breakdown booleans stay truthful and
    nothing is double-counted. `lang_code` optionally restricts the multilingual
    lexicon check to one language (default: auto-detect by script).
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
    # NFKC-normalise (so the multilingual lexicons match composed/compatibility
    # forms consistently — identity for ASCII and precomposed Turkish letters),
    # normalise the curly apostrophe (U+2019) to ASCII so "I'll" / "won't" are
    # detected straight or smart-quoted, and strip the combining dot (U+0307)
    # that str.lower() inserts for the Turkish dotted "İ".
    lower = (unicodedata.normalize("NFKC", scan).lower()
             .replace("’", "'").replace("‘", "'").replace("̇", ""))

    # Exact-match trivial check (single token like "ok", "tamam", "好的", "merci").
    # The multilingual part respects lang_code so an explicit restriction scopes it.
    if lower in _TRIVIAL_EXACTS or _ml_is_trivial_exact(lower, lang_code):
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
    # Secret check runs over the FULL content (not the bounded scan) so a
    # credential AFTER the scan window still suppresses the boost; identifier
    # positives stay on the bounded scan.
    secret_suspected = _detect_secret(stripped)
    identifier_hit = (not secret_suspected) and _detect_identifier(scan)

    # ── Multilingual signals (PR-2b: 9 languages beyond EN/TR) ──────
    ml_lang = ""
    ml_remember = False
    ml_decision = False
    for lg in ((lang_code,) if lang_code else _candidate_langs(lower)):
        if not ml_remember and _ml_match(lower, lg, "remember"):
            ml_remember = True
            ml_lang = ml_lang or lg
        if not ml_decision and _ml_match(lower, lg, "decision"):
            ml_decision = True
            ml_lang = ml_lang or lg
        if ml_remember and ml_decision:
            break

    def _build(band: str, value: float) -> ImportanceBreakdown:
        return ImportanceBreakdown(
            band, min(value, IMPORTANCE_CAP), remember_hit, decision_hit, fact_hits,
            commitment_hit=commitment_hit, deadline_hit=deadline_hit,
            person_hit=person_hit, cue_hit=cue_hit, numeric_hit=numeric_hit,
            identifier_hit=identifier_hit, lang_detected=(ml_lang or "en"),
            secret_suspected=secret_suspected,
        )

    # Credential-bearing content is NEVER boosted, even when other signals fire
    # (e.g. "save this token=..."): amplifying/resurfacing a memory that carries
    # a secret is exactly what we must avoid. This governs RANKING only (forces
    # BASELINE); redacting what actually gets stored is the PII scrubber's job,
    # which runs earlier on every write path. This module never stores or logs
    # the value — only the secret_suspected flag is kept.
    if secret_suspected:
        return _build("baseline", IMPORTANCE_BASELINE)

    # Pick the highest band that fired (max-wins).
    if remember_hit or cue_hit or ml_remember:
        return _build("memorable", IMPORTANCE_MEMORABLE)
    if decision_hit or commitment_hit or ml_decision:
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
    """Phrase-boundary match for multi-word / apostrophe tokens; word boundaries
    otherwise. Both anchor on \\w boundaries so a token never matches inside a
    larger word: "store this" not in "restore this backup", "pin this" not in
    "spin this up", "have to" not in "shave together", "going to" not in
    "ongoing total"."""
    if " " in token or "'" in token:
        return _phrase_match(haystack, token)
    return _word_match(haystack, token)


# ── Phase-2 signal detectors (each returns bool; identifiers returns a pair) ──


def _detect_cue(lower: str) -> bool:
    """Explicit save/store cue → MEMORABLE (guarded by remember in caller)."""
    return any(_token_in(lower, tok) for tok in _CUE_TOKENS)


def _detect_commitment(lower: str) -> bool:
    """Modal/obligation verb → DECISION (guarded by decision in caller).

    Negation is per-language. EN negated modals/obligations/futures are caught by
    _NEG_MODAL. Turkish obligations are negated only LOCALLY — an obligation word
    closely followed by "değil"/"yok" (_TR_NEG_OBLIGATION) or the negative
    -mamalı/-memeli infix (_TR_NEG_SUFFIX) — so an unrelated "değil"/"yok"
    elsewhere in the sentence no longer cancels a real obligation
    ("risk yok, bunu yapmalıyız" still commits).
    """
    if _NEG_MODAL.search(lower):
        return False
    tok_hit = (any(_token_in(lower, tok) for tok in _COMMIT_TOKENS)
               or bool(_COMMIT_TR_WORDS.search(lower)))
    suffix_hit = bool(_COMMIT_TR_SUFFIX.search(lower)) and not _TR_NEG_SUFFIX.search(lower)
    if not (tok_hit or suffix_hit):
        return False
    # Subtract the negation/deferral phrases CUMULATIVELY through one `working`
    # string, so a bilingual sentence that combines them ("gerek yok, we will see
    # how it goes") doesn't have one subtraction restore what the other removed
    # (Codex PR #141 retro: independent subs on the original `lower` re-introduced
    # the TR-negated "gerek" into the hedge residual → false DECISION).
    working = lower
    # A locally-negated TR obligation cancels its OWN word-token, but a separate
    # affirmative obligation in the same sentence still stands. Subtract the
    # negated phrase(s); if a -malı/-meli obligation or another obligation token
    # remains, it commits ("gerek yok ama zorundayız" → commit).
    if _TR_NEG_OBLIGATION.search(working) and not suffix_hit:
        working = _TR_NEG_OBLIGATION.sub(" ", working)
        residual_hit = (any(_token_in(working, tok) for tok in _COMMIT_TOKENS)
                        or bool(_COMMIT_TR_WORDS.search(working)))
        if not residual_hit:
            return False
    # "<subj> will see" is a deferral idiom, not a commitment. Subtract it (from the
    # already-negation-subtracted `working`); commit only if another non-hedge signal
    # remains ("we'll deploy, then we'll see"). ALL signals are recomputed — incl. the
    # Turkish -malı/-meli suffix — so a bilingual deferral whose obligation lives
    # INSIDE the hedge ("we'll see if bunu yapmalı mıyız") is not boosted.
    if _HEDGE_FUTURE.search(working):
        working = _HEDGE_FUTURE.sub(" ", working)
        residual_hit = (any(_token_in(working, tok) for tok in _COMMIT_TOKENS)
                        or bool(_COMMIT_TR_WORDS.search(working))
                        or (bool(_COMMIT_TR_SUFFIX.search(working))
                            and not _TR_NEG_SUFFIX.search(working)))
        if not residual_hit:
            return False
    return True


def _detect_deadline(lower: str) -> bool:
    """ISO/numeric date or a future-oriented deadline keyword → FACT."""
    if any(p.search(lower) for p in _DEADLINE_PATTERNS):
        return True
    return any(_token_in(lower, tok) for tok in _DEADLINE_TOKENS)


def _detect_person(stripped: str) -> bool:
    """@handle or email local-part → FACT (capitalized-word heuristic deferred)."""
    return any(p.search(stripped) for p in _PERSON_PATTERNS)


def _detect_numeric(lower: str) -> bool:
    """Number + context word → FACT (operates on the already-bounded scan)."""
    if not _NUMERIC_VALUE.search(lower):
        return False
    # Context words matched on word boundaries so "migrate"/"generate" don't hit
    # "rate" and "coffee" doesn't hit "fee".
    return any(_word_match(lower, ctx) for ctx in _NUMERIC_CONTEXT)


def _detect_secret(text: str) -> bool:
    """Credential/secret shape anywhere in the FULL content.

    A late secret (past the bounded scan window) must still suppress the boost,
    so this scans the whole string with the canonical scrubber patterns (anchored
    literals keep it O(n)). This only governs THIS module's importance output (it
    forces BASELINE so secrets are not amplified/resurfaced); redaction of the
    stored value is the PII scrubber's job, which runs earlier on every write
    path. The matched value is never returned, stored, or logged.

    Also treats the scrubber's own `[REDACTED:...]` marker as a secret: on the
    normal write path the scrubber runs BEFORE scoring, so by the time we score
    the raw credential is already redacted — without this, a cue like
    "save this token=[REDACTED:generic]" would still boost the (formerly
    credential-bearing) row. Detecting the marker keeps it at baseline.
    """
    if "[REDACTED:" in text:
        return True
    return any(p.search(text) for p in _SECRET_REGEXES)


def _detect_identifier(scan: str) -> bool:
    """PR#/SHA/host-path/abs-path in the bounded scan → FACT."""
    return any(p.search(scan) for p in _IDENTIFIER_PATTERNS)


# ── Multilingual lexicons (PR-2b: 9 languages beyond the EN/TR core) ──
#
# EN + TR keep their dedicated detectors above. These nine add remember (→
# MEMORABLE), decision (→ DECISION) and trivial (→ TRIVIAL, exact-full-content
# only) cues. Tokens are native-verified and curated for PRECISION — ambiguous
# homonyms and short substrings were deliberately excluded by the per-language
# research (e.g. zh bare 好/记/是, ar bare لا/تم, ru да/нет, es bare "si").
#
# Matching strategy by script:
#   SPACED (es/fr/pt/de/id/ru) → word-boundary regex; both the native (accented)
#       and a distinctive ASCII transliteration are listed, so accent-typing and
#       plain-typing both match. English-colliding ASCII (e.g. fr "decide",
#       "finalise") is intentionally omitted to avoid cross-language false hits.
#   IDEOGRAPHIC (zh) / DEVANAGARI (hi) / RTL (ar) → NFKC-normalized substring
#       (no word boundaries); ar is also matched after tashkeel stripping. Only
#       native script is listed (these langs are detected by script presence).
_ARABIC_TASHKEEL: re.Pattern[str] = re.compile(r"[ـً-ْٰ]")  # tatweel + harakat + superscript alef

_LEXICON_RAW: dict = {
    "zh": {"script": "IDEOGRAPHIC",
           "remember": ["记住", "记下来", "别忘了", "不要忘记", "请记得", "记一下", "重要的是"],
           "decision": ["决定了", "我们决定", "同意了", "达成一致", "确定下来", "敲定", "就这么定"],
           "trivial": ["好的", "谢谢", "明白了", "知道了", "没问题", "搞定了", "收到"]},
    "hi": {"script": "DEVANAGARI",
           "remember": ["याद रखना", "याद रखें", "मत भूलना", "नोट कर", "ध्यान रखना", "जरूरी है कि"],
           "decision": ["तय किया", "तय हुआ", "फैसला किया", "तय कर लिया", "सहमत हैं", "फाइनल कर", "तय रहा"],
           "trivial": ["ठीक है", "धन्यवाद", "शुक्रिया", "समझ गया", "हो गया", "बिल्कुल"]},
    "ar": {"script": "RTL",
           "remember": ["تذكر", "لا تنسى", "احفظ هذا", "من المهم", "دون هذا", "ضع في اعتبارك"],
           "decision": ["قررنا", "تقرر", "اتفقنا", "اخترنا", "تم الاتفاق", "استقر الرأي"],
           "trivial": ["حسنا", "شكرا", "تمام", "نعم", "فهمت", "تم الأمر"]},
    "ru": {"script": "SPACED",
           "remember": ["запомни", "запомните", "запиши", "сохрани", "не забудь", "напоминаю"],
           "decision": ["договорились", "решено", "решили", "выбрали", "утвердили", "окончательно"],
           "trivial": ["ок", "спасибо", "ясно", "понятно", "готово", "ага"]},
    "id": {"script": "SPACED",
           "remember": ["ingat", "catat", "simpan", "jangan lupa", "penting", "perhatikan"],
           "decision": ["diputuskan", "memutuskan", "sepakat", "disepakati", "finalisasi", "ditetapkan"],
           "trivial": ["oke", "makasih", "terima kasih", "siap", "sip", "mantap"]},
    "es": {"script": "SPACED",
           "remember": ["recuerda", "recuérdame", "recuerdame", "no olvides", "ten en cuenta",
                        "guarda esto", "anota", "importante recordar"],
           "decision": ["decidido", "decidí", "decidi", "decidimos", "acordamos", "quedamos en",
                        "elegí", "elegi", "finalizado"],
           "trivial": ["vale", "gracias", "perfecto", "listo", "de acuerdo", "entendido", "sí", "si"]},
    "fr": {"script": "SPACED",
           "remember": ["souviens-toi", "rappelle-toi", "n'oublie pas", "note bien", "à retenir",
                        "a retenir", "garde en mémoire", "garde en memoire", "important de retenir"],
           # NB: "on a decide" (ASCII of "on a décidé") is dropped — it is three
           # ordinary English words ("...on a decide...") and would cross-fire on
           # English text. The accented "on a décidé" is kept; "j'ai decide" stays
           # because the French "j'ai" anchor doesn't occur in English.
           "decision": ["décidé", "j'ai décidé", "j'ai decide", "nous avons décidé", "on a décidé",
                        "convenu", "j'ai choisi", "finalisé"],
           "trivial": ["merci", "d'accord", "parfait", "compris", "ça marche", "ca marche",
                       "très bien", "tres bien", "oui"]},
    "pt": {"script": "SPACED",
           "remember": ["lembre-se", "lembra disso", "não esqueça", "nao esqueca", "anote",
                        "guarde isso", "tenha em mente", "importante lembrar"],
           "decision": ["decidido", "decidi", "decidimos", "ficou decidido", "combinamos",
                        "escolhi", "finalizado"],
           "trivial": ["obrigado", "obrigada", "beleza", "perfeito", "entendi", "feito",
                       "tá bom", "ta bom"]},
    "de": {"script": "SPACED",
           "remember": ["merk dir", "merke dir", "nicht vergessen", "denk dran",
                        "behalte im hinterkopf", "wichtig zu merken", "notiere"],
           "decision": ["entschieden", "ich habe entschieden", "wir haben entschieden",
                        "beschlossen", "vereinbart", "festgelegt", "geeinigt"],
           "trivial": ["danke", "passt", "alles klar", "verstanden", "erledigt", "perfekt",
                       "in ordnung"]},
}


def _norm_tok(t: str, strip_tashkeel: bool = False) -> str:
    t = unicodedata.normalize("NFKC", t).lower()
    if strip_tashkeel:
        t = _ARABIC_TASHKEEL.sub("", t)
    return t


def _build_lexicon(raw: dict) -> tuple[dict, frozenset]:
    """Compile each language's remember/decision cues into a matcher and collect
    all trivial tokens into an exact-match set. SPACED → one word-boundary regex
    per category; substring scripts → a tuple of normalized tokens."""
    lex: dict = {}
    trivial_exact: set = set()
    for lang, e in raw.items():
        stype = e["script"]
        strip = stype == "RTL"
        cats: dict = {}
        for cat in ("remember", "decision"):
            toks = sorted({_norm_tok(t, strip) for t in e.get(cat, ()) if t.strip()})
            if not toks:
                cats[cat] = None
            elif stype == "SPACED":
                cats[cat] = ("re", re.compile(r"(?<!\w)(?:" + "|".join(re.escape(t) for t in toks) + r")(?!\w)"))
            else:
                cats[cat] = ("sub", tuple(toks))
        lang_trivial = {_norm_tok(t, strip) for t in e.get("trivial", ()) if t.strip()}
        lex[lang] = {"script": stype, "cats": cats, "trivial": frozenset(lang_trivial)}
        trivial_exact |= lang_trivial
    return lex, frozenset(trivial_exact)


_LEXICON, _ML_TRIVIAL_EXACT = _build_lexicon(_LEXICON_RAW)
_ML_LANGS: tuple[str, ...] = tuple(_LEXICON.keys())

# Script-presence detectors (non-Latin langs are scoped by their script so e.g.
# the Russian lexicon is only consulted when Cyrillic is present).
_SCRIPT_LANGS: dict = {
    "zh": re.compile(r"[一-鿿㐀-䶿豈-﫿]"),  # CJK Unified + Ext-A + Compatibility Ideographs
    "hi": re.compile(r"[ऀ-ॿ]"),
    "ar": re.compile(r"[؀-ۿݐ-ݿ]"),
    "ru": re.compile(r"[Ѐ-ӿ]"),
}
_LATIN_PATTERN: re.Pattern[str] = re.compile(r"[a-zA-ZÀ-ÖØ-öø-ÿĀ-ɏ]")
_LATIN_LANGS: tuple[str, ...] = ("es", "fr", "pt", "de", "id")
# Opt-in: try every language's lexicon regardless of detected script.
_UNION_MODE: bool = os.environ.get("B12_IMPORTANCE_UNION_MODE", "").lower() in ("1", "true", "yes")


def _candidate_langs(text: str) -> tuple[str, ...]:
    """Which language lexicons to consult, by script presence (or all in union mode)."""
    if _UNION_MODE:
        return _ML_LANGS
    langs = [lg for lg, pat in _SCRIPT_LANGS.items() if pat.search(text)]
    if _LATIN_PATTERN.search(text):
        langs.extend(_LATIN_LANGS)
    return tuple(langs)


def _ml_match(lower: str, lang: str, category: str) -> bool:
    """True if a multilingual remember/decision cue for `lang` fires in `lower`
    (already NFKC-normalized + lowercased)."""
    entry = _LEXICON.get(lang)
    if not entry:
        return False
    matcher = entry["cats"].get(category)
    if not matcher:
        return False
    kind, val = matcher
    if kind == "re":
        return bool(val.search(lower))
    hay = _ARABIC_TASHKEEL.sub("", lower) if entry["script"] == "RTL" else lower
    return any(tok in hay for tok in val)


def _ml_is_trivial_exact(lower: str, lang_code: str | None = None) -> bool:
    """True if the whole content is a single multilingual trivial token.

    When `lang_code` restricts the lexicon, only that language's trivial set is
    consulted — so the override reliably scopes the check and an unrelated
    language's one-token content is not demoted.
    """
    if lang_code:
        entry = _LEXICON.get(lang_code)
        toks = entry["trivial"] if entry else frozenset()
    else:
        toks = _ML_TRIVIAL_EXACT
    return lower in toks or _ARABIC_TASHKEEL.sub("", lower) in toks


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
        # ── Multilingual (PR-2b) — a few of the 9 languages ─────────
        ("请把这个记住一下", IMPORTANCE_MEMORABLE, "zh-remember"),
        ("我们决定用 postgres", IMPORTANCE_DECISION, "zh-decision"),
        ("decidimos usar postgres", IMPORTANCE_DECISION, "es-decision"),
        ("denk dran das morgen zu tun", IMPORTANCE_MEMORABLE, "de-remember"),
        ("قررنا أن نبدأ غدا", IMPORTANCE_DECISION, "ar-decision"),
        ("спасибо", IMPORTANCE_TRIVIAL, "ru-trivial"),
        # NB: the secret-filter skip path (credential-shaped content scores
        # BASELINE, never boosts) is covered in scripts/tests/test_importance_signals.py
        # — kept OUT of this in-module smoke so the production file carries no
        # secret-shaped literal (keeps gitleaks clean with no allowlist needed here).
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
