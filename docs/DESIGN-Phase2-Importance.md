# Phase 2 — Automatic Importance Detection

How B12 decides, at write time, how important a memory is — with no manual
tagging — and how that score flows into recall ranking. This is the canonical
reference for the `scripts/b12_importance.py` scorer and the Phase-2 work
(PRs 2a–2c).

## 1. Where importance comes from and where it goes

Every memory carries an `importance_score` in `metadata`. It is set on the
**write side** (before the row is stored) by `b12_importance.score(content)`,
and consumed on the **read side** by the unified recall score.

```
content ──> b12_importance.score() ──> metadata.importance_score ──> _unified_score (recall)
            (write side: this doc)                                    (read side: frozen by RET-3)
```

The scorer returns a float in `[0.0, 0.95]`. Five bands:

| band | value | meaning |
|---|---|---|
| trivial | 0.30 | chit-chat, bare acknowledgements ("ok", "tamam", "好的") |
| baseline | 0.50 | ordinary content, no signal |
| fact | 0.70 | structured-fact density (dates, prices, URLs, identifiers, …) |
| decision | 0.75 | a decision/commitment was made |
| memorable | 0.90 | explicit "remember this / save this" intent |

**Read-path invariant (RET-3).** The recall scorer normalizes two coexisting
importance scales — fractional `[0, 0.95]` (this scorer) and level multipliers
`[0.7, 2.0]` (a value `≥ 1.0` is divided by 2). This normalization is
triplicated across MCP `_unified_score`, the `memory-retrieval.sh` SQL, and the
OpenCode `unifiedScore`, and is guarded by the `test_ret3_*` suite. **Phase 2
never touches the read path** — it only changes *which float* the write side
emits, always clamped to `≤ 0.95` so it can never collide with the level-
multiplier branch.

## 2. Signal taxonomy (PR-2a)

Six language-agnostic detectors are layered onto the original
remember/decision/fact tokens. Band resolution is **max-wins** (the highest band
any signal fires). New detectors are **guarded** so they never double-count a
legacy hit.

| signal | band floor | notes |
|---|---|---|
| explicit save-cue | memorable | "save this / pin / bookmark"; guarded by the legacy remember token |
| commitment / obligation | decision | modal/obligation verbs; **negation-aware**; guarded by the legacy decision token |
| deadline / date | fact | ISO + relative keywords; future-oriented; `due` excludes the causal "due to" idiom |
| person mention | fact | `@handle` + email local-part only (capitalized-word heuristic deferred) |
| numeric value | fact | a number **plus** a context word (cost/budget/users/…) |
| identifier | fact | `PR#` / git-SHA (digit+hex) / host-path / abs-path |

**Negation** is handled per language. English negated modals/obligations/futures
(`won't`, `do/does/did not have to`, `will never`, with intervening adverbs and
punctuation) are suppressed. Turkish negation is **clause-local**: an obligation
word followed by `değil`/`yok` in the same clause (optionally after a particle
like `da`/`de`), the negative `-mamalı/-memeli` infix, or `zorunda kalma-`
(negated "be forced to") suppress the signal — while an unrelated `değil`/`yok`
in a *separate* clause does not (`risk yok, bunu yapmalıyız` still commits).

## 3. Multilingual lexicons (PR-2b)

The scorer covers **11 languages**: en, tr (the core) plus **zh, hi, es, fr, ar,
ru, pt, id, de**. Each language adds native-verified `remember`/`decision`/
`trivial` lexicons.

**Matching strategy by script:**

| script type | languages | strategy |
|---|---|---|
| SPACED | es, fr, pt, de, id, ru | word-boundary regex; native **+** distinctive ASCII transliteration |
| IDEOGRAPHIC | zh | NFKC-normalized substring |
| DEVANAGARI | hi | NFKC-normalized substring |
| RTL | ar | NFKC-normalized substring, **tashkeel-stripped** |

- Content is **NFKC-normalized once** before matching (identity for ASCII and
  precomposed Turkish letters, so EN/TR scores are bit-identical). Curly quotes
  (U+2018/U+2019) and the Turkish dotted-İ combining dot (U+0307) are also
  normalized.
- Language is **auto-detected by script presence** (`_candidate_langs`): the
  non-Latin lexicons are consulted only when their script is present; any Latin
  text consults the five Latin-script lexicons. `score(content, lang_code=…)`
  restricts to one language; `B12_IMPORTANCE_UNION_MODE=1` checks every lexicon.
- **Trivial cues fire only on exact full-content match**, so an acknowledgement
  word inside a substantive memory never demotes it.

**Precision over recall.** Each language's lexicon was curated to exclude
ambiguous homonyms and short substrings (e.g. zh bare `好`/`是`/`记`, ar `لا`/`تم`,
ru `да`/`нет`, es bare `si`, de `ja`/`gut`, tr `kadar`/`pazar`). **English-
colliding ASCII transliterations are omitted** (e.g. fr `decide`/`finalise`,
`on a decide`) so checking the Latin lexicons against English/Turkish text
produces **no cross-language false positives** — verified by control tests.

## 4. Secrets are never amplified

A credential-shaped string (api key / PAT / JWT / PEM / AWS / Bearer /
`key=value`, detected via the shared `b12_pii_scrubber` patterns so the two
never drift, **and** the scrubber's own `[REDACTED:…]` marker since scrubbing
runs before scoring) **caps the memory at baseline** — overriding even a caller-
or LLM-supplied importance. This is enforced on every **Python** write path that
flows through this scorer: MCP `memory_store`, `write_time_merge._augment_importance`
(insert), the legacy metadata-string format, and the semantic-merge update. The
scorer never stores or logs the secret value (redaction itself is the scrubber's
job).

**Known gap:** the OpenCode plugin's *native* TypeScript write path
(`plugins/opencode/src/lib/db.ts`) does not call this Python scorer — it
serializes caller-supplied metadata (some hooks set `importance_score` directly),
so it does **not** apply this secret cap. OpenCode-captured/staged credential
content is not held at baseline by this mechanism; closing that would mean adding
an equivalent cap (and PII scrubbing) on the plugin side.

## 5. ReDoS safety

`score()` runs synchronously on un-length-bounded content (checkpoint hook,
extractor). All regexes are bounded to linear matching: the email / host-path /
identifier patterns use bounded quantifiers, the PEM detector is header-only,
and a 20k scan window is a defence-in-depth backstop. Worst-case scoring of a
40k pathological blob is ~90ms (down from seconds). A perf test guards it.

## 6. The ML gate (PR-2c) — and why PR-2e is shelved

`scripts/audit_importance_gap.py` is a read-only audit that measures the
**importance gap**: high-value memories (stored importance ≥ 0.75, RET-3-
normalized) that the heuristic would only score at baseline. It excludes TTL-
expired and secret-suppressed rows, and **splits the gap by `memory_type`**.

The gate question was: is a semantic ML classifier head (PR-2e, BGE-M3 + a
trained head) worth building? The audit's answer on the live corpus:

- Gap = **20.8%** of eligible high-value memories — above the proposed 15% bar.
- **But 97% of the gap is TYPED** (`error_fix`, `observation`, `decision`,
  `learning`, `gotcha`, `infra`, `preference`). These got their high importance
  from their `memory_type` — a label **already known at write time**. They are
  closable by a cheap, deterministic `memory_type`→importance mapping.
- Only **0.5%** of eligible is **untyped/general** — the residual where a
  content-only ML head could conceivably help.

**Decision: PR-2e (the ML classifier head) is NOT warranted on content alone.**
A `memory_type`→importance mapping addresses the gap deterministically, far more
cheaply than a trained model with a corpus/retrain burden and per-write latency.
PR-2e stays shelved; the cheaper type-mapping is the recommended follow-up. The
audit (`--json`) remains the data source if this is revisited.

## 7. Test surface

`scripts/tests/test_importance_signals.py` (per-signal EN/TR, multilingual per-
language remember/decision/trivial, cross-language + EN/TR controls, negation/
morphology edges, secret-cap across write paths, ReDoS guard, the EN/TR pre/post
parity lock) and `scripts/tests/test_audit_importance_gap.py` (gap arithmetic,
RET-3 normalization, TTL exclusion, legacy-metadata parsing, secret-suppressed
exclusion, NaN, typed/untyped split, read-only invariant, forced redaction). The
`test_ret3_*` suite and the OpenCode `RET-3:` suite guard the read path.
