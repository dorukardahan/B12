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


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
