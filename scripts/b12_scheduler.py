"""
B12-FSRS Hybrid Scheduler — spaced repetition for memory strength.

Wraps py-fsrs (FSRS-6 algorithm) to replace the primitive Ebbinghaus
decay model (fixed +0.2 boost, -0.05/week decay).

FSRS-6 advantages:
- Per-memory difficulty tracking (some memories are harder to retain)
- Adaptive intervals (review spacing grows with successful recalls)
- desired_retention parameter (configurable target recall probability)

Usage:
    from b12_scheduler import review_memory, get_retention, migrate_card

    # Memory was retrieved (accessed by user/LLM)
    new_card_data = review_memory(stability, difficulty, due_date, rating="good")

    # Get current retention probability
    retention = get_retention(stability, last_accessed_ts)
"""

from __future__ import annotations

import math
from datetime import datetime, timezone, timedelta
from typing import Optional

# Try to import FSRS; fall back to simple Ebbinghaus if not available
try:
    from fsrs import Scheduler, Card, Rating, State
    FSRS_AVAILABLE = True
except ImportError:
    FSRS_AVAILABLE = False

# Default desired retention (probability of recall at review time)
DEFAULT_DESIRED_RETENTION = 0.85

# Singleton scheduler (reusable, stateless)
_scheduler: Optional[object] = None


def _get_scheduler() -> object:
    global _scheduler
    if _scheduler is None and FSRS_AVAILABLE:
        _scheduler = Scheduler(desired_retention=DEFAULT_DESIRED_RETENTION)
    return _scheduler


def _rating_from_str(rating: str) -> int:
    """Convert string rating to FSRS Rating enum value."""
    if not FSRS_AVAILABLE:
        return 3  # Good
    mapping = {
        "again": Rating.Again,    # 1 — forgot / not found when needed
        "hard": Rating.Hard,      # 2 — found but took effort
        "good": Rating.Good,      # 3 — normal retrieval
        "easy": Rating.Easy,      # 4 — instantly recalled
    }
    return mapping.get(rating.lower(), Rating.Good)


def review_memory(
    stability: float = 1.0,
    difficulty: float = 5.0,
    due_date: Optional[str] = None,
    rating: str = "good",
    access_count: int = 0,
    now: Optional[datetime] = None,
) -> dict:
    """Review a memory and return updated FSRS card data.

    Parameters
    ----------
    stability : float
        Current FSRS stability (or legacy strength value).
    difficulty : float
        Current FSRS difficulty (0-10 scale, 5.0 = medium).
    due_date : str or None
        ISO format due date, or None for new cards.
    rating : str
        One of: "again", "hard", "good", "easy".
    access_count : int
        Number of times this memory has been accessed.
    now : datetime or None
        Current time (defaults to utcnow).

    Returns
    -------
    dict with keys: stability, difficulty, due_date, state
    """
    if now is None:
        now = datetime.now(timezone.utc)

    if not FSRS_AVAILABLE:
        # Fallback to simple Ebbinghaus boost
        new_strength = min(stability + 0.2, 5.0)
        new_due = (now + timedelta(days=max(1, new_strength))).isoformat()
        return {
            "stability": new_strength,
            "difficulty": difficulty,
            "due_date": new_due,
            "state": "review",
        }

    scheduler = _get_scheduler()

    # Reconstruct FSRS Card from stored values
    card = Card()

    if due_date and access_count > 0:
        # Existing reviewed card
        card.stability = max(stability, 0.1)
        card.difficulty = max(min(difficulty, 10.0), 0.0)
        card.state = State.Review
        try:
            card.due = datetime.fromisoformat(due_date)
        except (ValueError, TypeError):
            card.due = now
        card.reps = access_count
    # else: new card with defaults

    fsrs_rating = _rating_from_str(rating)
    card, log = scheduler.review_card(card, fsrs_rating, now)

    return {
        "stability": round(card.stability, 4),
        "difficulty": round(card.difficulty, 4),
        "due_date": card.due.isoformat(),
        "state": card.state.name.lower() if hasattr(card.state, 'name') else str(card.state),
    }


def get_retention(stability: float, last_accessed_ts: float, now_ts: Optional[float] = None) -> float:
    """Get current retention probability for a memory.

    Uses FSRS power forgetting curve: R = (1 + t/(9*S))^(-1)
    Falls back to Ebbinghaus R = e^(-t/S) if FSRS unavailable.

    Parameters
    ----------
    stability : float
        FSRS stability value (days until ~90% retention).
    last_accessed_ts : float
        Unix timestamp of last access.
    now_ts : float or None
        Current unix timestamp (defaults to time.time()).

    Returns
    -------
    float in (0, 1] — probability of recall.
    """
    import time
    if now_ts is None:
        now_ts = time.time()

    if now_ts <= last_accessed_ts:
        return 1.0

    t_days = (now_ts - last_accessed_ts) / 86400.0

    if stability <= 0:
        return 0.01

    if FSRS_AVAILABLE:
        # FSRS-6 power forgetting curve
        return (1 + t_days / (9 * stability)) ** (-1)
    else:
        # Ebbinghaus fallback
        return math.exp(-t_days / stability)


def migrate_card(strength: float, access_count: int) -> dict:
    """Convert legacy Ebbinghaus strength to FSRS card data.

    Maps:
    - strength → stability (direct mapping, both measure days-to-forget)
    - access_count → inferred difficulty:
        0 access = new (difficulty 5.0)
        1-2 = hard (difficulty 7.0)
        3-5 = good (difficulty 5.0)
        6+ = easy (difficulty 3.0)

    Parameters
    ----------
    strength : float
        Legacy strength value (1.0 - 5.0).
    access_count : int
        Number of times the memory was accessed.

    Returns
    -------
    dict with keys: stability, difficulty, due_date
    """
    stability = max(strength, 0.5)

    if access_count == 0:
        difficulty = 5.0
    elif access_count <= 2:
        difficulty = 7.0
    elif access_count <= 5:
        difficulty = 5.0
    else:
        difficulty = 3.0

    # Due date: now + stability days (next review point)
    due = datetime.now(timezone.utc) + timedelta(days=stability)

    return {
        "stability": round(stability, 4),
        "difficulty": round(difficulty, 4),
        "due_date": due.isoformat(),
    }


if __name__ == "__main__":
    print(f"FSRS available: {FSRS_AVAILABLE}")

    # Test review cycle
    data = review_memory(stability=1.0, difficulty=5.0, rating="good")
    print(f"After good review: {data}")

    data2 = review_memory(
        stability=data["stability"],
        difficulty=data["difficulty"],
        due_date=data["due_date"],
        rating="good",
        access_count=1,
    )
    print(f"After 2nd good: {data2}")

    # Test retention
    import time
    r = get_retention(stability=2.0, last_accessed_ts=time.time() - 86400)
    print(f"Retention after 1 day (stability=2): {r:.4f}")

    # Test migration
    m = migrate_card(strength=2.5, access_count=4)
    print(f"Migrated card: {m}")
