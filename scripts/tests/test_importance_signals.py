"""Phase-2 importance signal-taxonomy tests (PR-2a).

Covers the six new language-agnostic signal detectors layered onto
b12_importance.score_with_breakdown, the secret filter, and — critically —
the backward-compatibility parity lock: every legacy EN/TR _selftest case
must score bit-identically after the refactor, and every emitted value must
stay in [0.0, IMPORTANCE_CAP=0.95] so the read-path RET-3 dual-scale
normalization is never perturbed.

Run via:  python3 -m pytest scripts/tests/test_importance_signals.py -v
      or:  python3 scripts/tests/test_importance_signals.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import b12_importance as imp  # noqa: E402
from b12_importance import ImportanceBreakdown, score_with_breakdown  # noqa: E402


# ── Task 1: ImportanceBreakdown backward-compat ────────────────────────────

def test_breakdown_backward_compat_5_positional():
    """The legacy 5-positional construction must still work after extension."""
    b = ImportanceBreakdown("baseline", 0.50, False, False, 0)
    assert b.band == "baseline" and b.score == 0.50
    assert b.commitment_hit is False and b.deadline_hit is False
    assert b.person_hit is False and b.cue_hit is False
    assert b.numeric_hit is False and b.identifier_hit is False
    assert b.lang_detected == "en" and b.secret_suspected is False


# ── Task 2: deadline / date detector (FACT 0.70) ───────────────────────────

def test_deadline_iso_floors_fact():
    assert imp.score("ship the migration by 2026-07-01") >= imp.IMPORTANCE_FACT

def test_deadline_relative_en():
    assert imp.score("the invoice is due Friday") >= imp.IMPORTANCE_FACT

def test_deadline_relative_tr():
    assert imp.score("son tarih Pazartesi, atlamayalim") >= imp.IMPORTANCE_FACT

def test_no_deadline_stays_baseline():
    assert imp.score("just chatting about the weather") == imp.IMPORTANCE_BASELINE

def test_deadline_till_not_matched_inside_still():
    # Codex P2: "till" must match as a word, not as a substring of "still".
    out = score_with_breakdown("still waiting on the docs")
    assert out.deadline_hit is False
    assert out.score == imp.IMPORTANCE_BASELINE

def test_deadline_due_not_matched_inside_overdue_word():
    # "due" matches as a word ("due Friday") but not inside "overdue" alone.
    assert score_with_breakdown("nothing overdue here yet").deadline_hit is False

def test_deadline_bare_kadar_not_a_deadline():
    # Codex: bare Turkish "kadar" is common outside deadlines and must not fire.
    assert score_with_breakdown("ne kadar güzel bir gün").deadline_hit is False
    assert score_with_breakdown("bu kadar yeter").deadline_hit is False


# ── Task 3: commitment detector (DECISION 0.75, guarded + negation) ────────

def test_commitment_en_floors_decision():
    assert imp.score("I must finish the audit this week") >= imp.IMPORTANCE_DECISION

def test_commitment_tr():
    assert imp.score("bunu yapmak zorundayiz, gerek var") >= imp.IMPORTANCE_DECISION

def test_commitment_negation_does_not_fire():
    out = score_with_breakdown("I won't be doing that")
    assert out.commitment_hit is False

def test_commitment_guarded_by_legacy_decision():
    # legacy decision token present -> commitment_hit stays False (no double-count)
    out = score_with_breakdown("we decided and I will do it")
    assert out.decision_hit is True and out.commitment_hit is False

def test_commitment_tr_negation_degil():
    # Codex: negated Turkish obligation must NOT promote to DECISION.
    out = score_with_breakdown("bunu yapmak zorunda değiliz")
    assert out.commitment_hit is False
    assert out.score == imp.IMPORTANCE_BASELINE

def test_commitment_tr_negation_yok():
    out = score_with_breakdown("buna gerek yok")
    assert out.commitment_hit is False

def test_tr_unrelated_negation_does_not_cancel_obligation():
    # Codex (:445 r12): değil/yok in an unrelated clause must NOT kill a real
    # -malı/-meli obligation.
    assert score_with_breakdown("bu kolay değil ama bunu yapmalıyız").commitment_hit is True
    assert score_with_breakdown("risk yok, bunu yapmalıyız").commitment_hit is True
    # but a locally-negated obligation word is still suppressed
    assert score_with_breakdown("buna gerek yok").commitment_hit is False
    assert score_with_breakdown("bunu yapmak zorunda değiliz").commitment_hit is False

def test_en_negated_future_not_commitment():
    # Codex (:142 r12): negated futures with never / intervening punctuation.
    for s in ("we will never migrate", "we'll never migrate",
              "we will, however, not migrate"):
        assert score_with_breakdown(s).commitment_hit is False, s
    assert score_with_breakdown("we will migrate next week").commitment_hit is True


# ── Task 4: explicit memory-cue detector (MEMORABLE 0.90, guarded) ─────────

def test_cue_en_floors_memorable():
    assert imp.score("save this for later: the API base url") >= imp.IMPORTANCE_MEMORABLE

def test_cue_tr_floors_memorable():
    assert imp.score("sakla bunu: prod endpoint") >= imp.IMPORTANCE_MEMORABLE

def test_cue_guarded_by_legacy_remember():
    out = score_with_breakdown("remember this and save it")
    assert out.remember_hit is True and out.cue_hit is False


# ── Task 5: person-mention detector (FACT 0.70, @handle + email only) ──────

def test_person_handle_floors_fact():
    assert imp.score("ping @alice about the rollout") >= imp.IMPORTANCE_FACT

def test_person_email_floors_fact():
    assert imp.score("loop in bob@example.com") >= imp.IMPORTANCE_FACT

def test_capitalized_word_does_not_fire_person():
    # the noisy capitalized-word + relationship-verb heuristic is deferred
    assert score_with_breakdown("The Manager Spoke").person_hit is False


# ── Task 6: numeric-value detector (FACT 0.70, context-gated + length guard)

def test_numeric_with_context_floors_fact():
    assert imp.score("the budget is 50k for 500 users") >= imp.IMPORTANCE_FACT

def test_numeric_without_context_no_fire():
    assert score_with_breakdown("met someone interesting yesterday").numeric_hit is False

def test_numeric_length_guard():
    big = "x" * 200_000 + " cost 5"
    assert score_with_breakdown(big) is not None  # must not hang


# ── Task 7: identifier detector + secret filter ────────────────────────────

def test_identifier_pr_floors_fact():
    assert imp.score("fixed in PR#123") >= imp.IMPORTANCE_FACT

def test_identifier_sha_floors_fact():
    assert imp.score("see commit deadbeef1234567") >= imp.IMPORTANCE_FACT

def test_secret_skips_boost_and_flags():
    out = score_with_breakdown("sk_live_abc123DEF456ghi789jkl012mno345pqr")
    assert out.secret_suspected is True
    assert out.identifier_hit is False
    assert out.score == imp.IMPORTANCE_BASELINE  # boost skipped

def test_secret_value_never_in_breakdown_repr():
    out = score_with_breakdown("token=ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")
    assert "ghp_" not in repr(out)  # value not echoed

def test_secret_with_other_signal_still_baseline():
    # Codex P1: a credential co-occurring with a boost signal (cue/commitment/
    # deadline/...) must NOT be boosted — secret_suspected forces BASELINE so
    # credential-bearing memories are never amplified or resurfaced.
    out = score_with_breakdown("please save this token=abcdefghijklmnopqrstuvwxyz")
    assert out.secret_suspected is True
    assert out.cue_hit is True            # the cue did fire ...
    assert out.score == imp.IMPORTANCE_BASELINE  # ... but the score is held at baseline
    assert out.band == "baseline"


# ── Task 8: parity lock — legacy scores unchanged + clamp invariant ────────

def test_existing_selftest_scores_unchanged():
    """The 11 legacy _selftest cases must score IDENTICALLY post-refactor."""
    EXPECTED = {
        "": 0.30, "ok": 0.30, "tamam": 0.30,
        "just some plain text": 0.50,
        "we decided to ship on Friday": 0.75,
        "şuna karar verdik: cron 60s": 0.75,
        "contact me at foo@example.com or +905551234567": 0.70,
        "see https://github.com/dorukardahan/B12": 0.70,
        "remember this: never amend a merged commit": 0.90,
        "lütfen not al: $0G launchı Mayıs": 0.90,
        "hatırla, geçen sefer aynı hatayı yapmıştık": 0.90,
    }
    for content, exp in EXPECTED.items():
        assert abs(imp.score(content) - exp) < 1e-9, content

def test_all_scores_within_unit_band():
    """No Phase-2 path may emit a value outside [0, 0.95] (protects RET-3)."""
    samples = [
        "save this PR#1 by 2026-07-01 with @alice budget 50k must do it",
        "remember this decided due Friday cost 9000 users @bob 2026-01-01",
        None, "", "ok",
    ]
    for s in samples:
        v = imp.score(s)
        assert 0.0 <= v <= imp.IMPORTANCE_CAP, (s, v)


# ── Second-round review fixes (Codex re-review + adversarial workflow) ─────

def test_no_redos_on_pathological_content():
    # regex-perf-1: the legacy email + host/path patterns were O(n^2). Bounded
    # quantifiers make them linear; "a."*N must score fast, not in seconds.
    import time
    big = "a." * 40000  # 80k chars, no whitespace — the worst case
    t0 = time.perf_counter()
    imp.score(big)
    dt = time.perf_counter() - t0
    assert dt < 1.0, f"scoring took {dt:.3f}s — possible ReDoS regression"

def test_no_redos_on_many_pem_headers():
    # Codex (:482): a blob of many BEGIN headers (no END) must not stall the
    # full-content secret scan — detection uses a bounded header-only matcher.
    import time
    blob = "-----BEGIN PRIVATE KEY-----\n" * 5000
    t0 = time.perf_counter()
    out = score_with_breakdown(blob)
    dt = time.perf_counter() - t0
    assert dt < 1.0, f"PEM-blob secret scan took {dt:.3f}s — ReDoS regression"
    assert out.secret_suspected is True  # the header still flags it

def test_secret_after_scan_window_still_suppresses():
    # Codex P1 (:277): a credential PAST the 20k scan window must still force
    # BASELINE — the secret check runs over the full content.
    content = "please save this " + ("x" * 21000) + " token=abcdefghijklmnopqrstuvwxyz"
    out = score_with_breakdown(content)
    assert out.secret_suspected is True
    assert out.score == imp.IMPORTANCE_BASELINE

def test_en_negated_obligations_not_commitment():
    # Codex (:126): "don't/do not have to / need to" must not score as DECISION.
    for s in ("we don't have to migrate", "we do not need to migrate",
              "we don't need to migrate", "it doesn't have to be perfect"):
        assert score_with_breakdown(s).commitment_hit is False, s

def test_due_to_causal_not_deadline():
    # Codex (:139): causal "due to" is not a deadline; "due <day>" still is.
    assert score_with_breakdown("failed due to flaky tests").deadline_hit is False
    assert score_with_breakdown("late due to traffic").deadline_hit is False
    assert score_with_breakdown("the report is due Friday").deadline_hit is True

def test_tr_obligation_inflected_forms_fire():
    # IC-1: the common inflected obligation forms must score DECISION.
    for s in ("bunu yapmalıyız", "gitmeliyim", "beklemeliyiz", "yapmalısın",
              "yapmaliyiz"):
        assert imp.score(s) >= imp.IMPORTANCE_DECISION, s

def test_tr_obligation_word_inflections_fire():
    # Codex (:456 r14): inflected obligation WORDS must fire on their own.
    for s in ("bunu yapmak zorundayız", "bu işi yapmaya mecburuz", "bu lazımdır"):
        assert score_with_breakdown(s).commitment_hit is True, s
    # ambiguous derivations stay safe (no false DECISION)
    assert score_with_breakdown("gereksiz bir toplantı").commitment_hit is False
    assert score_with_breakdown("lazımlık nerede").commitment_hit is False

def test_tr_obligation_lookalike_nouns_do_not_fire():
    # CORR-3: accusative nouns / names ending -mali must NOT score DECISION.
    for s in ("ihtimali değerlendirelim", "normali kontrol et", "kemali aradı",
              "mali tablo hazır"):
        out = score_with_breakdown(s)
        assert out.commitment_hit is False, s

def test_tr_negative_obligation_not_commitment():
    # "-mamalı/-memeli" (must NOT) is a negation, not a commitment.
    assert score_with_breakdown("bunu yapmamalıyız").commitment_hit is False

def test_sha_requires_digit_and_hex_letter():
    # regex-perf-2: all-alpha or all-digit runs are not SHAs.
    assert score_with_breakdown("the deadbeef cafe").identifier_hit is False
    assert score_with_breakdown("ticket 1234567 closed").identifier_hit is False
    assert score_with_breakdown("see commit deadbeef1234567").identifier_hit is True

def test_abs_path_requires_interior_slash():
    # regex-perf-3: a bare "/etc" in prose is not an identifier.
    assert score_with_breakdown("check the /etc folder").identifier_hit is False
    assert score_with_breakdown("edit /etc/passwd now").identifier_hit is True

def test_curly_apostrophe_normalized():
    # CORR-4: smart-quoted contractions detect the same as ASCII.
    assert score_with_breakdown("I’ll finish the audit").commitment_hit is True
    assert score_with_breakdown("I won’t do that").commitment_hit is False

def test_multiword_cue_phrase_boundary():
    # Codex (:368): multi-word cues must not match inside neighbouring words.
    assert score_with_breakdown("restore this backup").cue_hit is False
    assert score_with_breakdown("spin this up locally").cue_hit is False
    assert score_with_breakdown("save this note for later").cue_hit is True

def test_multiword_commitment_phrase_boundary():
    # Same root fix: "have to"/"going to" must not match inside words.
    assert score_with_breakdown("ongoing total review meeting").commitment_hit is False
    assert score_with_breakdown("we have to ship today").commitment_hit is True

def test_numeric_context_word_boundary():
    # Codex (:418): context words must match on boundaries, not as substrings.
    assert score_with_breakdown("migrate 20 tests").numeric_hit is False
    assert score_with_breakdown("generate 20 fixtures").numeric_hit is False
    assert score_with_breakdown("coffee 20 cups").numeric_hit is False
    assert score_with_breakdown("the budget is 50k").numeric_hit is True

def test_en_past_tense_negated_obligations():
    # Codex (:138): "didn't / did not have to / need to" are non-commitments.
    for s in ("we didn't have to migrate", "we did not need to migrate"):
        assert score_with_breakdown(s).commitment_hit is False, s

def test_en_negated_obligation_with_intervening_words():
    # Codex (:143 r8): negators with adverbs between must still suppress.
    for s in ("we don't really have to migrate", "we do not necessarily need to migrate",
              "we should not have to migrate"):
        assert score_with_breakdown(s).commitment_hit is False, s
    assert score_with_breakdown("we have to ship today").commitment_hit is True

def test_en_negated_obligation_with_punctuation():
    # Codex (:146 r11): commas/parentheticals between negator and have/need to.
    for s in ("we do not, however, need to migrate", "we don't, in fact, have to migrate"):
        assert score_with_breakdown(s).commitment_hit is False, s

def test_due_context_alternatives_word_bounded():
    # Codex (:160 r11): "due only"/"due online" must not match the on/by prefixes.
    assert score_with_breakdown("failed due only to flaky tests").deadline_hit is False
    assert score_with_breakdown("delay due online outage").deadline_hit is False
    assert score_with_breakdown("the task is due on Monday").deadline_hit is True

def test_tr_pazar_market_not_deadline():
    # Codex (:165): bare "pazar" (market) is not a deadline; "pazar günü" is.
    assert score_with_breakdown("pazar araştırması yapalım").deadline_hit is False
    assert score_with_breakdown("teslim pazar günü").deadline_hit is True

def test_due_idioms_not_deadline():
    # Codex (:153): non-deadline "due" idioms must not fire.
    for s in ("due diligence is done", "give due credit", "due process matters",
              "the report is not due"):
        assert score_with_breakdown(s).deadline_hit is False, s
    # real deadline uses still fire
    assert score_with_breakdown("the invoice is due Friday").deadline_hit is True
    assert score_with_breakdown("the task is due tomorrow").deadline_hit is True

def test_turkish_dotted_capital_i_normalized():
    # Codex (:305): "İ" lowercases to i + combining dot; strip it so TR tokens fire.
    assert score_with_breakdown("İşaretle bunu").cue_hit is True
    assert score_with_breakdown("BİTİŞ TARİHİ yaklaşıyor").deadline_hit is True

def test_due_to_predicate_idiom_not_deadline():
    # Codex (:161): "credit is due to X" is an acknowledgement, not a deadline.
    assert score_with_breakdown("credit is due to the reviewer").deadline_hit is False
    assert score_with_breakdown("the invoice is due Friday").deadline_hit is True

def test_predicate_due_idioms_not_deadline():
    # Codex (:183 r15): "is due diligence/process/credit" are idioms, not deadlines.
    for s in ("this is due diligence", "that is due process", "this is due credit"):
        assert score_with_breakdown(s).deadline_hit is False, s
    # predicate due at end / before punctuation / temporal still fires
    assert score_with_breakdown("the report is due").deadline_hit is True
    assert score_with_breakdown("the report is due tomorrow").deadline_hit is True

def test_tr_negation_with_particles():
    # Codex (:150 r15): Turkish particles (da/de) between obligation and değil/yok.
    assert score_with_breakdown("bunu yapmak zorunda da değiliz").commitment_hit is False
    assert score_with_breakdown("buna gerek de yok").commitment_hit is False

def test_tr_unrelated_yok_across_clause_does_not_cancel():
    # Codex (:151 r16): a separate clause's yok/değil (across a comma) must NOT
    # cancel a real obligation.
    assert score_with_breakdown("bunu yapmak zorundayız, risk yok").commitment_hit is True
    assert score_with_breakdown("bunu yapmalıyız, sorun değil").commitment_hit is True

def test_tr_zorunda_kalma_negation():
    # Codex (:154 r17): "zorunda kalma..." (be forced to, negated) is not a commitment.
    assert score_with_breakdown("bunu yapmak zorunda kalmayacağız").commitment_hit is False
    assert score_with_breakdown("zorunda kalmayacagiz").commitment_hit is False
    # positive obligation still fires
    assert score_with_breakdown("bunu yapmak zorundayız").commitment_hit is True

def test_tr_zorunda_kalmak_positive_infinitive():
    # Codex (:156 r18): the positive infinitive/gerund "kalmak"/"kalmanız" is NOT
    # a negation, so the obligation still commits.
    assert score_with_breakdown("bunu yapmak zorunda kalmak gerekiyor").commitment_hit is True
    assert score_with_breakdown("bunu yapmak zorunda kalmanız gerekecek").commitment_hit is True

def test_tr_negated_plus_affirmative_obligation_commits():
    # Codex (:483 r18): a separate affirmative obligation survives a negated one.
    assert score_with_breakdown("buna gerek yok ama bunu yapmak zorundayız").commitment_hit is True
    # still suppressed when the only obligation is the negated one
    assert score_with_breakdown("buna gerek yok").commitment_hit is False

def test_is_secret_public_helper():
    # Public helper used by callers (memory_store, llm_extractor) to cap secrets.
    assert imp.is_secret("sk_live_abc123DEF456ghi789jkl012mno345pqr") is True
    assert imp.is_secret("token=[REDACTED:generic]") is True       # post-scrub marker
    assert imp.is_secret("save this for later") is False
    assert imp.is_secret(None) is False
    assert imp.is_secret("") is False

def test_bare_will_requires_subject():
    # Codex (:112): the name "Will" / noun "will" must not score DECISION.
    assert score_with_breakdown("Will reviewed the patch").commitment_hit is False
    assert score_with_breakdown("free will is a hard problem").commitment_hit is False
    assert score_with_breakdown("going to the store for milk").commitment_hit is False
    # but a real subject+future is still a commitment
    assert score_with_breakdown("I will finish the audit").commitment_hit is True
    assert score_with_breakdown("I'm going to ship it today").commitment_hit is True

def test_scrubbed_secret_marker_stays_baseline():
    # Codex (:320): on the production path the scrubber runs BEFORE scoring, so
    # the scorer sees the [REDACTED:...] marker — it must still hold at baseline.
    out = score_with_breakdown("please save this token=[REDACTED:generic]")
    assert out.secret_suspected is True
    assert out.score == imp.IMPORTANCE_BASELINE

def test_provider_keys_detected_as_secret():
    # Codex (:207): hyphenated provider keys must be caught (via the shared
    # scrubber patterns) so credential-bearing memories are not boosted.
    cases = [
        "save this sk-ant-api03-" + ("a" * 45),
        "store this sk-proj-" + ("b" * 45),
        "remember AIza" + ("C" * 35),
        "key: Bearer " + ("d" * 24),
    ]
    for s in cases:
        out = score_with_breakdown(s)
        assert out.secret_suspected is True, s
        assert out.score == imp.IMPORTANCE_BASELINE, s


# ── PR-2b: multilingual lexicons (9 languages beyond the EN/TR core) ───────

_ML_REMEMBER = [
    ("zh", "请把这个记住一下"),
    ("hi", "इसे याद रखना ज़रूरी है"),
    ("ar", "تذكر هذا الأمر جيدا"),
    ("ru", "запомни этот адрес"),
    ("id", "jangan lupa backup datanya"),
    ("es", "recuérdame comprar leche"),
    ("fr", "n'oublie pas la réunion"),
    ("pt", "não esqueça de salvar isso"),
    ("de", "denk dran das morgen zu tun"),
]

_ML_DECISION = [
    ("zh", "我们决定用 postgres"),
    ("hi", "हमने तय किया"),
    ("ar", "قررنا أن نبدأ غدا"),
    ("ru", "мы решили начать"),
    ("id", "kami sepakat untuk lanjut"),
    ("es", "decidimos usar postgres"),
    ("fr", "nous avons décidé de migrer"),
    ("pt", "decidimos seguir em frente"),
    ("de", "wir haben entschieden zu migrieren"),
]

_ML_TRIVIAL = [
    ("zh", "好的"), ("ar", "شكرا"), ("ru", "спасибо"),
    ("es", "vale"), ("de", "danke"), ("id", "oke"), ("hi", "ठीक है"),
]

def test_ml_remember_floors_memorable():
    for lang, s in _ML_REMEMBER:
        assert score_with_breakdown(s).band == "memorable", (lang, s)

def test_ml_decision_floors_decision():
    for lang, s in _ML_DECISION:
        b = score_with_breakdown(s)
        assert b.band == "decision", (lang, s, b.band)

def test_ml_trivial_exact_only():
    # A trivial token alone → TRIVIAL ...
    for lang, s in _ML_TRIVIAL:
        assert score_with_breakdown(s).band == "trivial", (lang, s)
    # ... but the same token inside a substantive sentence must NOT demote it.
    assert score_with_breakdown("gracias por todo el trabajo de hoy").band == "baseline"
    assert score_with_breakdown("danke an das ganze team für den launch").band == "baseline"

def test_ml_lang_detected_recorded():
    assert score_with_breakdown("我们决定用 postgres").lang_detected == "zh"
    assert score_with_breakdown("decidimos usar postgres").lang_detected == "es"

def test_ml_no_cross_language_false_positive():
    # English text must not match FR/ES decision lexicons (dropped colliding ASCII).
    out = score_with_breakdown("please finalize the report and decide the budget")
    assert out.band == "baseline" and out.lang_detected == "en"

def test_ml_does_not_perturb_en_tr():
    # EN/TR scoring is unchanged by the multilingual layer.
    assert score_with_breakdown("just some plain text").band == "baseline"
    assert score_with_breakdown("we decided to ship on Friday").band == "decision"
    assert score_with_breakdown("bu işi yapmalıyız").band == "decision"
    assert score_with_breakdown("hatırla bunu").band == "memorable"

def test_ml_mixed_script():
    # zh remember cue embedded in latin text still fires.
    assert score_with_breakdown("hit a bug today, 记住这个").band == "memorable"

def test_ml_lang_code_override():
    # lang_code restricts the lexicon checked.
    assert imp.score("decidimos usar postgres", lang_code="es") >= imp.IMPORTANCE_DECISION
    assert imp.score("decidimos usar postgres", lang_code="de") == imp.IMPORTANCE_BASELINE

def test_ml_lang_code_scopes_trivial():
    # Codex: lang_code must scope the exact-trivial check too — another
    # language's trivial token is not consulted under a restriction.
    assert score_with_breakdown("danke", lang_code="es").band == "baseline"
    assert score_with_breakdown("好的", lang_code="es").band == "baseline"
    assert score_with_breakdown("danke", lang_code="de").band == "trivial"
    # legacy EN/TR trivial stays always-on regardless of lang_code
    assert score_with_breakdown("ok", lang_code="es").band == "trivial"

def test_ml_candidate_langs_by_script():
    assert imp._candidate_langs("记住") == ("zh",)
    assert imp._candidate_langs("запомни") == ("ru",)
    assert imp._candidate_langs("تذكر") == ("ar",)
    assert imp._candidate_langs("याद") == ("hi",)
    assert set(imp._candidate_langs("recuerda")) == {"es", "fr", "pt", "de", "id"}

def test_ml_no_redos_on_cjk_blob():
    import time
    t0 = time.perf_counter()
    score_with_breakdown("的" * 20000)
    assert time.perf_counter() - t0 < 1.0


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
