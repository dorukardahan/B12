import { expect, test } from "bun:test"

import { unifiedScore } from "../src/lib/db"

// RET-3: importance_score is authored in [0, 0.95]. The read path must use it
// un-halved, default missing/null/non-numeric to the 0.50 baseline (parity with
// the Python `_unified_score` and the hook SQL), preserve a stored 0, and clamp
// to [0, 1]. score = 0.3*decay + 0.3*importance + 0.4*relevance; with identical
// decay/strength and relevance=0, score deltas isolate the importance term.

const BASE = { last_accessed_at: 1_700_000_000, created_at: 1_700_000_000, strength: 1.0 };
const row = (metadata: string) => ({ ...BASE, metadata }) as never;

test("RET-3: importance is used un-halved (0.95 outranks baseline by 0.3*0.45)", () => {
  const hi = unifiedScore(row('{"importance_score":0.95}'), 0);
  const base = unifiedScore(row('{"importance_score":0.50}'), 0);
  expect(hi).toBeGreaterThan(base);
  expect(hi - base).toBeCloseTo(0.3 * (0.95 - 0.5), 9);
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
    expect(unifiedScore(row(metadata), 0)).toBeCloseTo(baseline, 9);
  }
});

test("RET-3: a legitimately-stored 0 is preserved (not coerced to baseline or 1.0)", () => {
  const zero = unifiedScore(row('{"importance_score":0}'), 0);
  const baseline = unifiedScore(row('{"importance_score":0.50}'), 0);
  // importance contributes 0.3*0 vs 0.3*0.5 → baseline is higher by exactly 0.15
  expect(baseline - zero).toBeCloseTo(0.3 * 0.5, 9);
});

test("RET-3: level multipliers (>= 1) normalize by /2 — 2.0→1.0, 1.5→0.75, 1.0→0.5", () => {
  const crit = unifiedScore(row('{"importance_score":2.0}'), 0);
  const imp = unifiedScore(row('{"importance_score":1.5}'), 0);
  const norm = unifiedScore(row('{"importance_score":1.0}'), 0);
  expect(crit - norm).toBeCloseTo(0.3 * (1.0 - 0.5), 9);
  expect(imp - norm).toBeCloseTo(0.3 * (0.75 - 0.5), 9);
  // level normal (1.0 → 0.5) lands on the same value as fractional baseline 0.50
  expect(norm).toBeCloseTo(unifiedScore(row('{"importance_score":0.50}'), 0), 9);
});

test("RET-3: result clamps to [0, 1] — 3.0 (→1.5) caps at 1.0, negative floors at 0", () => {
  const capped = unifiedScore(row('{"importance_score":2.0}'), 0); // → 1.0
  expect(unifiedScore(row('{"importance_score":3.0}'), 0)).toBeCloseTo(capped, 9);
  const floored = unifiedScore(row('{"importance_score":0}'), 0); // → 0
  expect(unifiedScore(row('{"importance_score":-1.0}'), 0)).toBeCloseTo(floored, 9);
});
