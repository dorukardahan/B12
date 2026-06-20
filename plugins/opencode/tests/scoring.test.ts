import { expect, test } from "bun:test"

import { unifiedScore } from "../src/lib/db"

// RET-3: importance_score is authored in [0, 0.95]. The read path must use it
// un-halved, default missing/null/non-numeric to the 0.50 baseline (parity with
// the Python `_unified_score` and the hook SQL), preserve a stored 0, and clamp
// to [0, 1].
//
// With effective-stability decay (decay = 1/(1+age/(9*strength*(1+ALPHA*importance)))),
// decay now depends on importance, so raw score deltas no longer isolate the
// importance term alone. Tests use ordering/inequality assertions that preserve
// the original RET-3 intent without relying on exact deltas across rows with
// different effective-stability values.

const BASE = { last_accessed_at: 1_700_000_000, created_at: 1_700_000_000, strength: 1.0 };
const row = (metadata: string) => ({ ...BASE, metadata }) as never;

test("RET-3: importance is used un-halved (0.95 outranks baseline 0.50)", () => {
  const hi = unifiedScore(row('{"importance_score":0.95}'), 0);
  const base = unifiedScore(row('{"importance_score":0.50}'), 0);
  // With eff-stability the gap is LARGER than pure additive w_imp*Δimp because
  // higher importance also slows decay; confirm hi > base by at least the additive share.
  expect(hi).toBeGreaterThan(base);
  expect(hi - base).toBeGreaterThan(0.25 * (0.95 - 0.5));
});

test("RET-3: missing / null / non-numeric importance fall back to baseline 0.50", () => {
  const baseline = unifiedScore(row('{"importance_score":0.50}'), 0);
  for (const metadata of [
    "{}",
    '{"importance_score":null}', // Number(null)===0 would wrongly score MIN — regression Codex caught
    '{"importance_score":"high"}',
    '{"importance_score":true}', // typeof "boolean" — must not coerce to 1
    '{"importance_score":false}',
    '{"other":1}',
  ]) {
    // Each fallback row has the same importance=0.50 → same effStability → same decay;
    // scores must be identical to the explicit-0.50 baseline.
    expect(unifiedScore(row(metadata), 0)).toBeCloseTo(baseline, 9);
  }
});

test("RET-3: a legitimately-stored 0 is preserved (not coerced to baseline or 1.0)", () => {
  const zero = unifiedScore(row('{"importance_score":0}'), 0);
  const baseline = unifiedScore(row('{"importance_score":0.50}'), 0);
  // importance=0 → lower effStability → faster decay AND lower importance term;
  // baseline must strictly exceed zero regardless of decay difference.
  expect(baseline).toBeGreaterThan(zero);
});

test("RET-3: level multipliers (>= 1) normalize by /2 — 2.0→1.0 > 1.5→0.75 > 1.0→0.5", () => {
  const crit = unifiedScore(row('{"importance_score":2.0}'), 0);
  const imp = unifiedScore(row('{"importance_score":1.5}'), 0);
  const norm = unifiedScore(row('{"importance_score":1.0}'), 0);
  // Ordering must hold: crit > imp > norm.
  // With eff-stability the gap is LARGER than the pure additive w_imp*Δimp because
  // higher importance also slows decay; so the strict inequalities here are stronger
  // than in the old constant-decay model.
  expect(crit).toBeGreaterThan(imp);
  expect(imp).toBeGreaterThan(norm);
  // level normal (1.0 → 0.5) has the same importance as fractional baseline 0.50,
  // so they share the same effStability → same decay → same score.
  expect(norm).toBeCloseTo(unifiedScore(row('{"importance_score":0.50}'), 0), 9);
});

test("RET-3: result clamps to [0, 1] — 3.0 (→1.5) caps at 1.0 same as 2.0 (→1.0)", () => {
  // Both 2.0 and 3.0 normalize to importance=1.0 (2.0/2=1.0; min(3.0/2,1)=1.0).
  // Same importance → same effStability → same decay → same score.
  const capped = unifiedScore(row('{"importance_score":2.0}'), 0);
  expect(unifiedScore(row('{"importance_score":3.0}'), 0)).toBeCloseTo(capped, 9);
  // negative floors at 0 — same importance as an explicit stored-0.
  const floored = unifiedScore(row('{"importance_score":0}'), 0);
  expect(unifiedScore(row('{"importance_score":-1.0}'), 0)).toBeCloseTo(floored, 9);
});

// ── Aging / effective-stability tests ───────────────────────────────────────
// These tests FAIL on the old `1/(1+age/(9*strength))` decay and PASS only
// with eff-stability = strength*(1+ALPHA*importance) (mirrors test_aging_model.py).

function rowAS(ageDays: number, importance: number, strength: number) {
  const now = Math.floor(Date.now() / 1000);
  return {
    last_accessed_at: null,
    created_at: now - Math.floor(ageDays * 86400),
    strength,
    metadata: JSON.stringify({ importance_score: importance }),
  } as never;
}

test("old important beats old trivial (same relevance)", () => {
  expect(unifiedScore(rowAS(365, 0.9, 1), 0.5)).toBeGreaterThan(
    unifiedScore(rowAS(365, 0.3, 1), 0.5),
  );
});

test("importance slows decay beyond additive term", () => {
  // The total gap must exceed the pure additive contribution of w_importance*Δimp
  // — proving that higher importance also raised effStability → slower decay.
  const hi = unifiedScore(rowAS(365, 0.9, 1), 0.5);
  const lo = unifiedScore(rowAS(365, 0.5, 1), 0.5);
  expect(hi - lo).toBeGreaterThan(0.25 * (0.9 - 0.5) + 1e-6);
});

test("reinforcement slows decay beyond additive term", () => {
  // strength=5 vs strength=1 (same importance=0.5): gap must exceed the pure
  // additive contribution of w_strength*Δ(strengthScore).
  // Threshold 0.115: old `1/(1+age/(9*strength))` decay (no effStability) produces a
  // gap of ~0.101 at age 365; the new eff-stability model produces ~0.130.
  // 0.101 < 0.115 < 0.130 → FAILS on old model, PASSES on eff-stability model only.
  const hi = unifiedScore(rowAS(365, 0.5, 5), 0.5);
  const lo = unifiedScore(rowAS(365, 0.5, 1), 0.5);
  expect(hi - lo).toBeGreaterThan(0.115);
});

test("high relevance dominates over freshness", () => {
  expect(unifiedScore(rowAS(365, 0.3, 1), 0.95)).toBeGreaterThan(
    unifiedScore(rowAS(1, 0.3, 1), 0.05),
  );
});
