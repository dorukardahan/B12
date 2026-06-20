import os, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

def _row(age_days, importance, strength):
    now = time.time()
    return {"last_accessed_at": now - age_days*86400.0, "created_at": now - age_days*86400.0,
            "strength": strength, "metadata": f'{{"importance_score": {importance}}}'}

def test_old_important_beats_old_trivial_same_relevance():
    import b12_mcp_server as M
    rel = 0.5
    assert M._unified_score(_row(365, 0.90, 1.0), rel) > M._unified_score(_row(365, 0.30, 1.0), rel)

def test_old_reinforced_beats_old_neveraccessed_same_relevance():
    import b12_mcp_server as M
    rel = 0.5
    assert M._unified_score(_row(365, 0.50, 5.0), rel) > M._unified_score(_row(365, 0.50, 1.0), rel)

def test_importance_slows_decay_component():
    import b12_mcp_server as M
    assert M._unified_score(_row(180, 0.90, 1.0), 0.0) > M._unified_score(_row(180, 0.30, 1.0), 0.0)

def test_high_relevance_still_dominates():
    import b12_mcp_server as M
    assert M._unified_score(_row(365, 0.30, 1.0), 0.95) > M._unified_score(_row(1, 0.30, 1.0), 0.05)
