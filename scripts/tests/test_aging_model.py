import os, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

def _row(age_days, importance, strength):
    now = time.time()
    return {"last_accessed_at": now - age_days*86400.0, "created_at": now - age_days*86400.0,
            "strength": strength, "metadata": f'{{"importance_score": {importance}}}'}

def test_age_discriminates_among_old_memories():
    # Same importance+strength+relevance, only age differs. Under OLD exp decay both
    # floor to 0.01 -> equal scores (test fails). Under eff_stability they differ.
    import b12_mcp_server as M
    younger = M._unified_score(_row(30, 0.50, 1.0), 0.5)
    older = M._unified_score(_row(365, 0.50, 1.0), 0.5)
    assert younger > older + 1e-6, (younger, older)

def test_importance_slows_decay_beyond_its_additive_term():
    # Two same-age(365d) same-strength rows differing only in importance. The score gap
    # under OLD exp == exactly w_importance*(0.9-0.5) (decay equal at 0.01). Under the new
    # formula importance ALSO raises eff_stability -> decay term adds MORE. So the gap must
    # EXCEED the pure additive importance gap.
    import b12_mcp_server as M
    w_imp = M._DEFAULT_WEIGHTS["importance"]
    hi = M._unified_score(_row(365, 0.90, 1.0), 0.5)
    lo = M._unified_score(_row(365, 0.50, 1.0), 0.5)
    assert (hi - lo) > w_imp * (0.90 - 0.50) + 1e-6, (hi, lo, w_imp)

def test_reinforcement_slows_decay_beyond_its_additive_term():
    # Same idea for strength: gap under OLD exp == exactly w_strength*(min(5/5,1)-min(1/5,1)).
    # New formula: strength also raises eff_stability -> decay adds more -> gap exceeds it.
    import b12_mcp_server as M
    w_str = M._DEFAULT_WEIGHTS["strength"]
    hi = M._unified_score(_row(365, 0.50, 5.0), 0.5)
    lo = M._unified_score(_row(365, 0.50, 1.0), 0.5)
    additive_only = w_str * (min(5.0/5.0, 1.0) - min(1.0/5.0, 1.0))
    assert (hi - lo) > additive_only + 1e-6, (hi, lo, additive_only)

def test_high_relevance_still_dominates():
    import b12_mcp_server as M
    assert M._unified_score(_row(365, 0.30, 1.0), 0.95) > M._unified_score(_row(1, 0.30, 1.0), 0.05)
