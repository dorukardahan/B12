#!/usr/bin/env python3
"""
Ebbinghaus forgetting curve helpers + simple weighted scoring.

This module is intentionally stdlib-only so it can be vendored into services
without extra dependencies.

Functions
---------
- decay_score(strength, last_accessed, now) -> float
    Returns an Ebbinghaus-style retention score:
        R = e^(-t / S)
    where:
        t = (now - last_accessed) / 86400   (days)
        S = strength                        (higher means slower decay)

- boost_strength(current_strength) -> float
    Increments strength by 0.3 each retrieval, capped at 5.0.

- combined_score(recency, importance, relevance, ...) -> float
    Park et al.-style weighted combination score.
"""

from __future__ import annotations

import math


SECONDS_PER_DAY = 86400.0


def decay_score(strength: float, last_accessed: float, now: float) -> float:
    """
    Compute retention R = e^(-t/S).

    Parameters
    ----------
    strength:
        Positive strength scalar (typically in [1.0, 5.0]).
    last_accessed:
        Unix timestamp (seconds) of last retrieval.
    now:
        Unix timestamp (seconds) representing "current time".

    Returns
    -------
    float in (0, 1], where 1 means "just accessed".
    """
    if strength <= 0:
        raise ValueError("strength must be > 0")

    # Guard against clock skew / bad inputs.
    if now <= last_accessed:
        return 1.0

    t_days = (now - last_accessed) / SECONDS_PER_DAY
    r = math.exp(-t_days / strength)

    # Numerical safety: clamp to a sane range.
    if r < 0.0:
        return 0.0
    if r > 1.0:
        return 1.0
    return r


def boost_strength(current_strength: float) -> float:
    """
    Increase memory strength per retrieval, capped at 5.0.
    """
    return min(current_strength + 0.3, 5.0)


def combined_score(
    recency: float,
    importance: float,
    relevance: float,
    alpha_r: float = 0.3,
    alpha_i: float = 0.3,
    alpha_rel: float = 0.4,
) -> float:
    """
    Weighted scoring function (Park et al.).

    By default, weights sum to 1.0. If the caller supplies weights that do not
    sum to 1.0, they are normalized so the output stays on the same scale.
    """
    total = alpha_r + alpha_i + alpha_rel
    if total <= 0:
        raise ValueError("alpha weights must sum to a positive value")

    wr = alpha_r / total
    wi = alpha_i / total
    wrel = alpha_rel / total
    return (wr * recency) + (wi * importance) + (wrel * relevance)


if __name__ == "__main__":
    # Lightweight self-tests / examples.
    import time

    now_ts = time.time()
    two_days_ago = now_ts - 2 * SECONDS_PER_DAY

    r1 = decay_score(strength=1.0, last_accessed=two_days_ago, now=now_ts)
    r2 = decay_score(strength=3.0, last_accessed=two_days_ago, now=now_ts)
    print("decay_score examples:")
    print("  strength=1.0, t=2d  ->", round(r1, 6))
    print("  strength=3.0, t=2d  ->", round(r2, 6))
    assert 0.0 < r1 < r2 <= 1.0

    print("\nboost_strength examples:")
    print("  1.0 ->", boost_strength(1.0))
    print("  4.9 ->", boost_strength(4.9))
    assert abs(boost_strength(1.0) - 1.3) < 1e-9
    assert boost_strength(4.9) == 5.0

    print("\ncombined_score example:")
    c = combined_score(recency=0.8, importance=0.5, relevance=0.2)
    print("  combined_score(0.8, 0.5, 0.2) ->", round(c, 6))
    assert abs(c - (0.3 * 0.8 + 0.3 * 0.5 + 0.4 * 0.2)) < 1e-9

