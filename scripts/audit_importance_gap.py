#!/usr/bin/env python3
"""Read-only corpus audit: how many high-value memories would the write-side
heuristic (b12_importance) MISS?

Phase-2 PR-2c. This measures the "importance gap" — memories STORED at a high
importance but which the keyword/regex heuristic alone scores at baseline (no
signal fires). A large gap is the only thing that would justify the gated ML
classifier (PR-2e); a small gap means the heuristic + multilingual lexicons are
sufficient and ML stays shelved. The owner sets the real go/no-go threshold from
this output.

Strictly read-only: opens the DB in `mode=ro`, never writes. Content samples are
PII/secret-scrubbed and truncated before display.

Usage:
  python3 scripts/audit_importance_gap.py [--db PATH] [--high 0.75]
                                          [--samples 10] [--json]
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# memory_types that carry no inherent importance signal — the residual where a
# content-based ML head (not a type->importance mapping) would be the only lever.
_GENERIC_TYPES = frozenset({"", "general", "note", "progress", "chat",
                            "session_summary", "working-context", "working_context"})


def _default_db_path() -> str:
    """Mirror b12_mcp_server's DB_PATH resolution (no import to stay light)."""
    home = os.path.expanduser("~")
    if sys.platform == "darwin":
        return os.path.join(home, "Library", "Application Support", "mcp-memory", "sqlite_vec.db")
    if sys.platform == "win32":
        return os.path.join(home, "AppData", "Local", "mcp-memory", "sqlite_vec.db")
    return os.path.join(home, ".local", "share", "mcp-memory", "sqlite_vec.db")


def _norm_importance(raw) -> float:
    """Normalize a stored importance_score the way the read path (RET-3) does:
    a level multiplier (>= 1.0) is halved; bool / non-numeric / missing -> 0.50;
    result clamped to [0, 1]."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return 0.50
    v = float(raw)
    if not math.isfinite(v):  # NaN / inf (json.loads accepts NaN) -> baseline
        return 0.50
    if v >= 1.0:
        v = v / 2.0
    return max(0.0, min(1.0, v))


def _scrub(text: str) -> str:
    """Always redact a sample before display — even if B12_DISABLE_PII_SCRUB=1 is
    set (the audit must never print raw corpus secrets/PII). The opt-out is
    temporarily cleared for the scrub call; if the scrubber is unavailable, the
    sample is OMITTED rather than leaked."""
    try:
        from b12_pii_scrubber import scrub
    except Exception:
        return "[sample omitted: scrubber unavailable]"
    saved = os.environ.pop("B12_DISABLE_PII_SCRUB", None)
    try:
        return scrub(text)
    except Exception:
        return "[sample omitted: scrub failed]"
    finally:
        if saved is not None:
            os.environ["B12_DISABLE_PII_SCRUB"] = saved


def audit(db_path: str, high: float, samples: int) -> dict:
    if not os.path.exists(db_path):
        return {"error": f"database not found: {db_path}"}

    try:
        import b12_importance
    except Exception as e:  # pragma: no cover - defensive
        return {"error": f"cannot import b12_importance: {e}"}

    # Canonical metadata normalizer (handles dict / JSON / the legacy
    # "key:val, ..." string format) so legacy high-importance rows aren't dropped.
    try:
        from write_time_merge import _metadata_to_str as _mts
    except Exception:
        _mts = None

    def _meta_dict(meta):
        for parse in ((lambda m: json.loads(_mts(m))) if _mts else None,
                      (lambda m: json.loads(m) if m else {})):
            if parse is None:
                continue
            try:
                d = parse(meta)
                if isinstance(d, dict):
                    return d
            except Exception:
                continue
        return {}

    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    # Match the retrieval paths: exclude soft-deleted AND TTL-expired rows so the
    # gap reflects memories users can actually retrieve (b12_mcp_server.py:1096-1097).
    queries = (
        "SELECT content, metadata, memory_type FROM memories WHERE deleted_at IS NULL "
        "AND (valid_until IS NULL OR valid_until > datetime('now'))",
        "SELECT content, metadata, memory_type FROM memories WHERE deleted_at IS NULL",
        "SELECT content, metadata, '' FROM memories",
    )
    rows = None
    try:
        for q in queries:
            try:
                rows = conn.execute(q).fetchall()
                break
            except sqlite3.OperationalError:
                continue
    finally:
        conn.close()
    if rows is None:
        return {"error": f"no readable memories table in {db_path}"}

    total = len(rows)
    bands = {"trivial": 0, "baseline": 0, "fact": 0, "decision": 0, "memorable": 0}
    high_value = 0
    gap = 0               # high-value, heuristic fires NO signal — a genuine miss
    secret_suppressed = 0  # high-value but credential-bearing: deliberately baselined, NOT a miss
    gap_by_type: dict = {}
    gap_samples: list = []
    FACT = getattr(b12_importance, "IMPORTANCE_FACT", 0.70)

    for content, meta, mtype in rows:
        content = content or ""
        md = _meta_dict(meta)
        mt = (mtype or md.get("type") or "general")
        stored = _norm_importance(md.get("importance_score"))
        bd = b12_importance.score_with_breakdown(content)
        bands[bd.band] = bands.get(bd.band, 0) + 1
        if stored >= high:
            high_value += 1
            if bd.secret_suspected:
                # Credential-bearing: the scorer deliberately baselines this and
                # no ML head should ever boost it — so it is NOT a heuristic miss.
                secret_suppressed += 1
            elif bd.score < FACT:
                # "no signal" = the heuristic would only have given baseline/trivial.
                gap += 1
                gap_by_type[mt] = gap_by_type.get(mt, 0) + 1
                if len(gap_samples) < samples:
                    snippet = _scrub(content)[:120].replace("\n", " ")
                    gap_samples.append({"stored": round(stored, 3),
                                        "heuristic_band": bd.band,
                                        "memory_type": mt,
                                        "snippet": snippet})

    # Split the gap: a meaningful memory_type already signals value at WRITE time
    # (closable by a cheap memory_type -> importance mapping, no ML), versus the
    # generic/untyped residual where only content-based ML could help.
    typed_gap = sum(n for t, n in gap_by_type.items() if t not in _GENERIC_TYPES)
    untyped_gap = gap - typed_gap

    # Gap % is over the ML-ADDRESSABLE set (high-value minus secret-suppressed
    # rows, which no heuristic/ML head may ever boost) — otherwise a pile of
    # credential rows would dilute the rate and wrongly shelve the ML head.
    eligible = high_value - secret_suppressed
    gap_pct = (gap / eligible * 100.0) if eligible else 0.0
    untyped_pct = (untyped_gap / eligible * 100.0) if eligible else 0.0
    return {
        "db": db_path,
        "total_memories": total,
        "high_threshold": high,
        "high_value": high_value,
        "secret_suppressed": secret_suppressed,
        "eligible": eligible,
        "gap": gap,
        "gap_pct_of_eligible": round(gap_pct, 1),
        "typed_gap": typed_gap,
        "untyped_gap": untyped_gap,
        "untyped_pct_of_eligible": round(untyped_pct, 1),
        "gap_by_memory_type": dict(sorted(gap_by_type.items(), key=lambda kv: -kv[1])),
        "heuristic_band_distribution": bands,
        "gap_samples": gap_samples,
    }


def _recommendation(gap_pct: float, eligible: int, typed_gap: int, untyped_gap: int,
                    untyped_pct: float) -> str:
    if eligible == 0:
        return ("No ML-addressable high-value memories found — corpus too small / "
                "unseeded to judge. Re-run after more usage before considering the "
                "ML head (PR-2e).")
    lead = (f"Gap is {gap_pct:.1f}% of eligible, but it splits into {typed_gap} TYPED "
            f"(known memory_type — closable by a cheap memory_type->importance mapping, "
            f"no ML) and {untyped_gap} UNTYPED/general ({untyped_pct:.1f}% of eligible — "
            f"the only content-only ML candidate).\n  ")
    if untyped_pct >= 15.0:
        return lead + ("Even the untyped residual is large: the gated ML classifier "
                       "(PR-2e) MAY be worth prototyping. Owner sets the final threshold.")
    return lead + ("The untyped residual is small: a memory_type->importance mapping "
                   "addresses most of the gap deterministically; the ML head (PR-2e) is "
                   "NOT warranted on content alone. Recommend shelving PR-2e.")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Read-only importance-gap audit (PR-2c).")
    ap.add_argument("--db", default=_default_db_path(), help="path to the sqlite_vec.db")
    ap.add_argument("--high", type=float, default=0.75, help="high-value importance threshold (normalized)")
    ap.add_argument("--samples", type=int, default=10, help="max anonymized gap samples to show")
    ap.add_argument("--json", action="store_true", help="emit raw JSON")
    args = ap.parse_args(argv)

    result = audit(args.db, args.high, args.samples)
    if "error" in result:
        print(f"audit_importance_gap: {result['error']}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    print("── Importance-gap audit (read-only) ─────────────────────────")
    print(f"  DB                 : {result['db']}")
    print(f"  Total memories     : {result['total_memories']}")
    print(f"  High-value (>= {result['high_threshold']}) : {result['high_value']}")
    print(f"  Secret-suppressed (credential -> baselined, NOT a miss): {result['secret_suppressed']}")
    print(f"  Eligible (ML-addressable high-value)    : {result['eligible']}")
    print(f"  Gap (eligible but heuristic gives no signal): {result['gap']} "
          f"({result['gap_pct_of_eligible']}% of eligible)")
    print(f"    ├─ typed   (closable by memory_type->importance): {result['typed_gap']}")
    print(f"    └─ untyped (content-only / ML candidate): {result['untyped_gap']} "
          f"({result['untyped_pct_of_eligible']}% of eligible)")
    if result["gap_by_memory_type"]:
        top = list(result["gap_by_memory_type"].items())[:10]
        print("  Gap by memory_type : " + ", ".join(f"{t}={n}" for t, n in top))
    print(f"  Heuristic bands    : {result['heuristic_band_distribution']}")
    if result["gap_samples"]:
        print("  Gap samples (scrubbed):")
        for s in result["gap_samples"]:
            print(f"    [{s['memory_type']} stored={s['stored']} heuristic={s['heuristic_band']}] {s['snippet']}")
    print("────────────────────────────────────────────────────────────")
    print("  " + _recommendation(result["gap_pct_of_eligible"], result["eligible"],
                                  result["typed_gap"], result["untyped_gap"],
                                  result["untyped_pct_of_eligible"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
