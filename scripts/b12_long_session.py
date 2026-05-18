#!/usr/bin/env python3
"""Q2 long-session re-surface — periodic recall of early-session high-importance memories.

In a 1M-context Opus 4.7 session, a memory stored at turn 5 can slide out of
the model's effective working window by turn 60. The retrieval hook only
fires on prompts that semantically match — high-importance facts that the
user is no longer asking about silently drop off the radar. This module
decides whether the current UserPromptSubmit should *also* receive a small
batch of "early-session, high-importance" memories so they stay live for
the rest of the session.

Public API:
    should_resurface(session_id, every_n=20)        — (bool, turn_number)
    bump_turn_counter(session_id) -> int            — atomic increment
    pick_resurface_ids(db_path, session_id, limit)  — list[int]

Trigger semantics (every 20 turns by default, override via env):
    Turn 1..19  → no re-surface
    Turn 20     → re-surface fires
    Turn 21..39 → no
    Turn 40     → fires again
    ...

State file: ``$B12_DATA_DIR/state/session-turn-counter-<sid[:12]>.txt``
(one integer per line, lockstep with b12_token_budget._sid12).

Selection rule for re-surface candidates:
    metadata.source_session == current_session[:12]
        AND metadata.importance_score >= 0.7
        AND created_at within the current session
    ORDER BY importance_score DESC, created_at ASC
    LIMIT N (default 3)

We require source_session match instead of "any high-importance memory" so
re-surface stays scoped to "things THIS session captured" — otherwise we'd
re-show high-importance memories from unrelated past sessions every 20
turns, which would burn the T2 cumulative cap fast.
"""
from __future__ import annotations

import argparse
import math
import os
import sqlite3
import sys
import tempfile
import time


DEFAULT_RESURFACE_EVERY_N = 20
DEFAULT_RESURFACE_LIMIT = 3
RESURFACE_MIN_IMPORTANCE = 0.7
_SID_PREFIX_LEN = 12


def _base_dir() -> str:
    return os.environ.get('B12_DATA_DIR', os.path.expanduser('~/.B12'))


def _state_dir() -> str:
    d = os.path.join(_base_dir(), 'state')
    os.makedirs(d, exist_ok=True)
    return d


def _sid12(session_id: str) -> str:
    """Mirror b12_token_budget._sid12 (R10 lockstep)."""
    s = (session_id or '').strip().replace('/', '_').replace('\\', '_')
    return s[:_SID_PREFIX_LEN] if s else 'unknown'


def turn_counter_path(session_id: str) -> str:
    return os.path.join(
        _state_dir(),
        f'session-turn-counter-{_sid12(session_id)}.txt',
    )


def read_turn(session_id: str) -> int:
    p = turn_counter_path(session_id)
    try:
        with open(p, 'r') as f:
            return int(f.read().strip() or '0')
    except (FileNotFoundError, ValueError):
        return 0


def bump_turn_counter(session_id: str) -> int:
    """Atomic +1 and return the new turn number."""
    new_turn = read_turn(session_id) + 1
    p = turn_counter_path(session_id)
    fd, tmp = tempfile.mkstemp(prefix='b12turn-', dir=_state_dir())
    try:
        with os.fdopen(fd, 'w') as f:
            f.write(str(new_turn))
        os.replace(tmp, p)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return new_turn


def should_resurface(
    session_id: str,
    every_n: int = DEFAULT_RESURFACE_EVERY_N,
    bump: bool = True,
) -> tuple[bool, int]:
    """Decide whether the current turn should trigger a re-surface.

    By default this also bumps the counter — call sites that only want
    a peek pass ``bump=False``.
    """
    if every_n <= 0:
        return (False, read_turn(session_id))
    turn = bump_turn_counter(session_id) if bump else read_turn(session_id)
    # Fire on the Nth turn and every multiple thereafter.
    fire = (turn >= every_n) and (turn % every_n == 0)
    return (fire, turn)


def pick_resurface_ids(
    db_path: str,
    session_id: str,
    limit: int = DEFAULT_RESURFACE_LIMIT,
    min_importance: float = RESURFACE_MIN_IMPORTANCE,
    skip_ids: list[int] | None = None,
) -> list[dict]:
    """Return a small batch of same-session high-importance memories.

    Each item: {id, content_hash, importance, project, source_session,
    memory_type, preview}.
    """
    if not os.path.exists(db_path):
        return []
    sid12 = _sid12(session_id)
    skip_set = set(int(x) for x in (skip_ids or []) if str(x).isdigit())

    conn = sqlite3.connect(db_path, timeout=10)
    try:
        rows = conn.execute(
            """
            SELECT m.id,
                   m.content_hash,
                   COALESCE(json_extract(m.metadata, '$.importance_score'), 0.5) AS importance,
                   COALESCE(json_extract(m.metadata, '$.project'), '') AS project,
                   COALESCE(SUBSTR(json_extract(m.metadata, '$.source_session'), 1, 12), '') AS source_session,
                   m.memory_type,
                   SUBSTR(replace(replace(m.content, char(10), ' '), char(9), ' '), 1, 80) AS preview
            FROM memories m
            WHERE m.deleted_at IS NULL
              AND (m.valid_until IS NULL OR m.valid_until > datetime('now'))
              AND m.memory_type NOT IN ('session_summary', 'progress')
              AND COALESCE(json_extract(m.metadata, '$.source_session'), '') = ?
              AND COALESCE(json_extract(m.metadata, '$.importance_score'), 0.5) >= ?
            ORDER BY importance DESC, m.created_at ASC
            LIMIT 50
            """,
            (sid12, float(min_importance)),
        ).fetchall()
    finally:
        conn.close()

    out = []
    for row in rows:
        mid = int(row[0])
        if mid in skip_set:
            continue
        out.append({
            'id': mid,
            'content_hash': row[1],
            'importance': float(row[2] or 0.5),
            'project': row[3] or '',
            'source_session': row[4] or '',
            'memory_type': row[5] or '',
            'preview': row[6] or '',
        })
        if len(out) >= max(1, int(limit)):
            break
    return out


def reset_turn_counter(session_id: str) -> None:
    """For self-test / external reset only."""
    p = turn_counter_path(session_id)
    try:
        os.unlink(p)
    except FileNotFoundError:
        pass


# ── Q2 topic-shift re-surface ────────────────────────────────────
# Triggered orthogonally to the periodic-N counter: when consecutive user
# prompts drift below the cosine threshold (default 0.55), the session has
# "topic-shifted" and the model can benefit from a fresh injection of this
# session's high-importance memories that may have slid out of working
# context. Stricter filter than the periodic path (importance>=0.8, older
# than the session midpoint) so we re-surface only the genuinely-anchoring
# facts on a topic shift, not every same-session note.
TOPIC_SHIFT_DEFAULT_COSINE = 0.55
TOPIC_SHIFT_MIN_IMPORTANCE = 0.8
TOPIC_SHIFT_DEFAULT_LIMIT = 3


def topic_shift_state_path(session_id: str) -> str:
    """Per-session embedding-of-previous-prompt state file."""
    return os.path.join(
        _state_dir(),
        f'topicshift-{_sid12(session_id)}.txt',
    )


def pick_topic_shift_ids(
    db_path: str,
    session_id: str,
    limit: int = TOPIC_SHIFT_DEFAULT_LIMIT,
    min_importance: float = TOPIC_SHIFT_MIN_IMPORTANCE,
    skip_ids: list[int] | None = None,
) -> list[dict]:
    """Topic-shift re-surface candidates.

    Stricter than ``pick_resurface_ids``:
      - same source_session (this session captured the fact)
      - importance_score >= 0.8 (only the genuinely-anchoring facts)
      - created_at < session-midpoint (older than half the elapsed session,
        so we re-surface facts the model captured *early* — the ones most
        likely to have slid out of effective working context)
      - id not in skip_ids (T3 dedup ledger)
    """
    if not os.path.exists(db_path):
        return []
    sid12 = _sid12(session_id)
    skip_set = set(int(x) for x in (skip_ids or []) if str(x).isdigit())

    conn = sqlite3.connect(db_path, timeout=10)
    try:
        # First: find session-start (earliest same-session memory) to
        # compute the midpoint. If the session has only one memory or
        # all memories are < 5 min old, "older than midpoint" filters
        # everything out — fall back to the full set.
        #
        # `memories.created_at` is stored as Unix-epoch REAL (verified
        # against b12_mcp_server.py:_ensure_schema and production rows
        # like 1779097617.716011). `julianday(epoch_real)` returns NULL
        # in SQLite without the 'unixepoch' modifier — Codex PR #34 P1.
        # We work directly in epoch seconds; both endpoints are REAL,
        # the threshold compares REAL-to-REAL, no datetime() conversion
        # involved.
        bounds = conn.execute(
            """
            SELECT MIN(m.created_at)                    AS first_ts,
                   CAST(strftime('%s', 'now') AS REAL)  AS now_ts
            FROM memories m
            WHERE m.deleted_at IS NULL
              AND COALESCE(json_extract(m.metadata, '$.source_session'), '') = ?
            """,
            (sid12,),
        ).fetchone()
        first_ts = bounds[0] if bounds else None
        now_ts = bounds[1] if bounds else None
        midpoint_ts = None
        if first_ts is not None and now_ts is not None and now_ts > first_ts:
            midpoint_ts = float(first_ts) + (float(now_ts) - float(first_ts)) * 0.5

        # If we couldn't compute a midpoint (single-memory session), skip
        # the age filter and just return importance-ordered candidates.
        if midpoint_ts is not None:
            rows = conn.execute(
                """
                SELECT m.id,
                       m.content_hash,
                       COALESCE(json_extract(m.metadata, '$.importance_score'), 0.5) AS importance,
                       COALESCE(json_extract(m.metadata, '$.project'), '') AS project,
                       COALESCE(SUBSTR(json_extract(m.metadata, '$.source_session'), 1, 12), '') AS source_session,
                       m.memory_type,
                       SUBSTR(replace(replace(m.content, char(10), ' '), char(9), ' '), 1, 80) AS preview
                FROM memories m
                WHERE m.deleted_at IS NULL
                  AND (m.valid_until IS NULL OR m.valid_until > datetime('now'))
                  AND m.memory_type NOT IN ('session_summary', 'progress')
                  AND COALESCE(json_extract(m.metadata, '$.source_session'), '') = ?
                  AND COALESCE(json_extract(m.metadata, '$.importance_score'), 0.5) >= ?
                  AND m.created_at < ?
                ORDER BY importance DESC, m.created_at ASC
                LIMIT 50
                """,
                (sid12, float(min_importance), midpoint_ts),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT m.id,
                       m.content_hash,
                       COALESCE(json_extract(m.metadata, '$.importance_score'), 0.5) AS importance,
                       COALESCE(json_extract(m.metadata, '$.project'), '') AS project,
                       COALESCE(SUBSTR(json_extract(m.metadata, '$.source_session'), 1, 12), '') AS source_session,
                       m.memory_type,
                       SUBSTR(replace(replace(m.content, char(10), ' '), char(9), ' '), 1, 80) AS preview
                FROM memories m
                WHERE m.deleted_at IS NULL
                  AND (m.valid_until IS NULL OR m.valid_until > datetime('now'))
                  AND m.memory_type NOT IN ('session_summary', 'progress')
                  AND COALESCE(json_extract(m.metadata, '$.source_session'), '') = ?
                  AND COALESCE(json_extract(m.metadata, '$.importance_score'), 0.5) >= ?
                ORDER BY importance DESC, m.created_at ASC
                LIMIT 50
                """,
                (sid12, float(min_importance)),
            ).fetchall()
    finally:
        conn.close()

    out = []
    for row in rows:
        mid = int(row[0])
        if mid in skip_set:
            continue
        out.append({
            'id': mid,
            'content_hash': row[1],
            'importance': float(row[2] or 0.5),
            'project': row[3] or '',
            'source_session': row[4] or '',
            'memory_type': row[5] or '',
            'preview': row[6] or '',
        })
        if len(out) >= max(1, int(limit)):
            break
    return out


def cosine_drift(prev_emb_b64: str, cur_emb_b64: str) -> float:
    """Return cosine similarity in [-1, 1] between two base64 float32
    embeddings (as produced by embed_daemon's `encode_batch` op).

    Bare math — no numpy required so this can run inside the retrieval
    hook's tiny Python heredoc without pulling in heavy deps on a hot
    path that needs to stay sub-200ms.
    """
    import base64 as _b64
    import struct as _struct

    try:
        a = _b64.b64decode(prev_emb_b64)
        b = _b64.b64decode(cur_emb_b64)
    except Exception:
        return 1.0  # corrupt state → behave as "same topic", don't trigger
    if not a or not b or len(a) != len(b) or len(a) % 4 != 0:
        return 1.0
    n = len(a) // 4
    av = _struct.unpack(f'<{n}f', a)
    bv = _struct.unpack(f'<{n}f', b)
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(av, bv):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0.0 or nb <= 0.0:
        return 1.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


def topic_shift_check(
    session_id: str,
    current_emb_b64: str,
    threshold: float = TOPIC_SHIFT_DEFAULT_COSINE,
) -> tuple[bool, float]:
    """Compare the current prompt embedding against the previously-stored
    one. Returns (shifted, cosine). On first call (no prior state), records
    the embedding and returns (False, 1.0). Always persists the current
    embedding for the next call.
    """
    path = topic_shift_state_path(session_id)
    prev = None
    try:
        with open(path, 'r') as f:
            prev = f.read().strip() or None
    except FileNotFoundError:
        prev = None

    fd, tmp = tempfile.mkstemp(prefix='b12ts-', dir=_state_dir())
    try:
        with os.fdopen(fd, 'w') as f:
            f.write(current_emb_b64)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass

    if not prev:
        return (False, 1.0)
    cos = cosine_drift(prev, current_emb_b64)
    return (cos < float(threshold), cos)


# ── Cross-session re-surface candidates (Phase E) ────────────────
# Complements pick_resurface_ids: same-project, high-importance, OLD
# memories from OTHER sessions. Designed to be merged into the Q2
# periodic re-surface so cross-session continuity also benefits, not
# just within-session anchoring.
CROSS_SESSION_DEFAULT_LIMIT = 2
CROSS_SESSION_MIN_IMPORTANCE = 0.8
CROSS_SESSION_MIN_AGE_DAYS = 7


def pick_cross_session_ids(
    db_path: str,
    session_id: str,
    project: str,
    limit: int = CROSS_SESSION_DEFAULT_LIMIT,
    min_importance: float = CROSS_SESSION_MIN_IMPORTANCE,
    min_age_days: float = CROSS_SESSION_MIN_AGE_DAYS,
    skip_ids: list[int] | None = None,
) -> list[dict]:
    """High-importance memories from PRIOR sessions of the same project.

    Selection rule:
      - source_session != current (cross-session — that's the point)
      - importance_score >= 0.8
      - created_at older than ``min_age_days`` ago (so we re-surface
        durable knowledge, not yesterday's in-flight notes)
      - project == current project (scope discipline — don't pollute
        with universal-tag memories from unrelated projects)
      - id NOT IN skip_ids (ledger dedup, same as same-session path)

    Returns same item shape as ``pick_resurface_ids`` so the caller
    can merge the two lists with a single format function.
    """
    if not os.path.exists(db_path):
        return []
    if not project:
        return []
    sid12 = _sid12(session_id)
    skip_set = set(int(x) for x in (skip_ids or []) if str(x).isdigit())

    # `memories.created_at` is stored as Unix-epoch REAL (Codex PR #36 P1
    # caught the mixed-type compare bug in the first iteration). Compute
    # the cutoff in Python and pass as a numeric REAL parameter so the
    # filter is REAL-vs-REAL throughout.
    age_cutoff_ts = time.time() - float(min_age_days) * 86400.0
    conn = sqlite3.connect(db_path, timeout=10)
    try:
        rows = conn.execute(
            """
            SELECT m.id,
                   m.content_hash,
                   COALESCE(json_extract(m.metadata, '$.importance_score'), 0.5) AS importance,
                   COALESCE(json_extract(m.metadata, '$.project'), '') AS project,
                   COALESCE(SUBSTR(json_extract(m.metadata, '$.source_session'), 1, 12), '') AS source_session,
                   m.memory_type,
                   SUBSTR(replace(replace(m.content, char(10), ' '), char(9), ' '), 1, 80) AS preview
            FROM memories m
            WHERE m.deleted_at IS NULL
              AND (m.valid_until IS NULL OR m.valid_until > datetime('now'))
              AND m.memory_type NOT IN ('session_summary', 'progress')
              AND COALESCE(json_extract(m.metadata, '$.project'), '') = ?
              AND COALESCE(SUBSTR(json_extract(m.metadata, '$.source_session'), 1, 12), '') != ?
              AND COALESCE(json_extract(m.metadata, '$.importance_score'), 0.5) >= ?
              AND m.created_at < ?
            ORDER BY importance DESC, m.created_at DESC
            LIMIT 50
            """,
            (project, sid12, float(min_importance), age_cutoff_ts),
        ).fetchall()
    finally:
        conn.close()

    out = []
    for row in rows:
        mid = int(row[0])
        if mid in skip_set:
            continue
        out.append({
            'id': mid,
            'content_hash': row[1],
            'importance': float(row[2] or 0.5),
            'project': row[3] or '',
            'source_session': row[4] or '',
            'memory_type': row[5] or '',
            'preview': row[6] or '',
        })
        if len(out) >= max(1, int(limit)):
            break
    return out


# ── Self-test ───────────────────────────────────────────────────
def _self_test() -> int:
    failures: list[str] = []
    sid = f'longsess-{int(time.time())}-test'

    with tempfile.TemporaryDirectory() as tmp:
        os.environ['B12_DATA_DIR'] = tmp

        # Counter starts at 0.
        if read_turn(sid) != 0:
            failures.append('initial turn != 0')

        # 20 bumps → fire on the 20th.
        fired_at = None
        for i in range(1, 21):
            ok, turn = should_resurface(sid, every_n=20)
            if ok:
                fired_at = turn
        if fired_at != 20:
            failures.append(f'should_resurface did not fire at turn 20 (got {fired_at})')

        # Fire again at 40.
        for _ in range(19):
            should_resurface(sid, every_n=20)
        ok, turn = should_resurface(sid, every_n=20)
        if not ok or turn != 40:
            failures.append(f'should_resurface should fire at turn 40, got ({ok=}, {turn=})')

        # Peek (bump=False) does not advance.
        before = read_turn(sid)
        should_resurface(sid, every_n=20, bump=False)
        after = read_turn(sid)
        if before != after:
            failures.append(f'bump=False advanced counter: {before} -> {after}')

        # Build a tiny synthetic DB and check pick_resurface_ids filtering.
        db_path = os.path.join(tmp, 'test.db')
        conn = sqlite3.connect(db_path)
        conn.executescript(
            """
            CREATE TABLE memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content_hash TEXT UNIQUE NOT NULL,
                content TEXT NOT NULL,
                tags TEXT, memory_type TEXT, metadata TEXT,
                created_at REAL, updated_at REAL,
                deleted_at REAL DEFAULT NULL,
                valid_until TEXT DEFAULT NULL,
                strength REAL DEFAULT 1.0
            );
            """
        )
        sid12 = _sid12(sid)
        rows = [
            # (id, hash, content, metadata, deleted)
            (1, 'h1', 'high-importance same session, should surface',
             f'{{"importance_score":0.9,"source_session":"{sid12}","project":"B12"}}', None),
            (2, 'h2', 'medium-importance same session, should NOT surface',
             f'{{"importance_score":0.5,"source_session":"{sid12}","project":"B12"}}', None),
            (3, 'h3', 'high-importance different session, should NOT surface',
             '{"importance_score":0.95,"source_session":"OTHER","project":"X"}', None),
            (4, 'h4', 'high-importance but deleted',
             f'{{"importance_score":0.9,"source_session":"{sid12}","project":"B12"}}', 1.0),
        ]
        for r in rows:
            conn.execute(
                "INSERT INTO memories (id, content_hash, content, metadata, deleted_at, created_at, updated_at, memory_type) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (r[0], r[1], r[2], r[3], r[4], 1000+r[0], 1000+r[0], 'decision'),
            )
        conn.commit()
        conn.close()

        picks = pick_resurface_ids(db_path, sid, limit=3)
        pick_ids = {p['id'] for p in picks}
        if pick_ids != {1}:
            failures.append(f'pick_resurface_ids returned {pick_ids}, expected {{1}}')

        # skip_ids excludes already-injected memories.
        picks2 = pick_resurface_ids(db_path, sid, limit=3, skip_ids=[1])
        if picks2:
            failures.append(f'skip_ids=[1] should empty result, got {picks2}')

        # ── Topic-shift cosine drift ──
        import base64 as _b64
        import struct as _struct
        # Synthetic 4-dim embeddings (unit-normalised) — same vector vs an
        # orthogonal one. Real BGE-M3 returns 1024-dim normalised float32.
        emb_a = _b64.b64encode(_struct.pack('<4f', 1.0, 0.0, 0.0, 0.0)).decode('ascii')
        emb_b = _b64.b64encode(_struct.pack('<4f', 0.0, 1.0, 0.0, 0.0)).decode('ascii')
        emb_a2 = _b64.b64encode(_struct.pack('<4f', 0.99, 0.05, 0.0, 0.0)).decode('ascii')
        cos_self = cosine_drift(emb_a, emb_a)
        if cos_self < 0.999:
            failures.append(f'cosine_drift(self, self) = {cos_self}, expected ~1.0')
        cos_orth = cosine_drift(emb_a, emb_b)
        if not (-0.05 < cos_orth < 0.05):
            failures.append(f'cosine_drift(orthogonal) = {cos_orth}, expected ~0')

        # topic_shift_check first call returns False (no prior state).
        shifted0, _ = topic_shift_check(sid, emb_a)
        if shifted0:
            failures.append('topic_shift_check first call should not trigger')
        # Second call with similar embedding → not shifted.
        shifted_same, cos_same = topic_shift_check(sid, emb_a2)
        if shifted_same:
            failures.append(f'topic_shift_check near-identical should not trigger (cos={cos_same:.3f})')
        # Third call with orthogonal embedding → shifted.
        shifted_orth, cos_orth = topic_shift_check(sid, emb_b)
        if not shifted_orth:
            failures.append(f'topic_shift_check orthogonal should trigger (cos={cos_orth:.3f})')

        # pick_topic_shift_ids: only importance >= 0.8 returned.
        picks_ts = pick_topic_shift_ids(db_path, sid, limit=3)
        ts_ids = {p['id'] for p in picks_ts}
        if 2 in ts_ids or 3 in ts_ids or 4 in ts_ids:
            failures.append(f'pick_topic_shift_ids leaked low-importance/cross-session/deleted: {ts_ids}')

        # ── Cross-session re-surface (Phase E) ──
        # Production stores created_at as Unix-epoch REAL — use numeric
        # timestamps so the age filter exercises the same REAL-vs-REAL
        # path real data takes (Codex PR #36 P2).
        _now_ts = time.time()
        _old_ts = _now_ts - 30 * 86400.0   # 30 days ago — past the 7-day gate
        _recent_ts = _now_ts - 600.0       # 10 minutes ago — inside the gate
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "INSERT INTO memories (id, content_hash, content, metadata, created_at, updated_at, memory_type) "
                "VALUES (?,?,?,?,?,?,?)",
                (110, 'h110', 'cross-session high-importance OLD same-project, should surface',
                 '{"importance_score":0.9,"source_session":"OTHERSESS","project":"B12"}',
                 _old_ts, _old_ts, 'decision'),
            )
            conn.execute(
                "INSERT INTO memories (id, content_hash, content, metadata, created_at, updated_at, memory_type) "
                "VALUES (?,?,?,?,?,?,?)",
                (111, 'h111', 'cross-session high-importance RECENT, should NOT surface (age gate)',
                 '{"importance_score":0.9,"source_session":"OTHERSESS","project":"B12"}',
                 _recent_ts, _recent_ts, 'decision'),
            )
            conn.execute(
                "INSERT INTO memories (id, content_hash, content, metadata, created_at, updated_at, memory_type) "
                "VALUES (?,?,?,?,?,?,?)",
                (112, 'h112', 'cross-session low-importance, should NOT surface',
                 '{"importance_score":0.6,"source_session":"OTHERSESS","project":"B12"}',
                 _old_ts, _old_ts, 'decision'),
            )
            conn.execute(
                "INSERT INTO memories (id, content_hash, content, metadata, created_at, updated_at, memory_type) "
                "VALUES (?,?,?,?,?,?,?)",
                (113, 'h113', 'cross-session wrong-project, should NOT surface',
                 '{"importance_score":0.9,"source_session":"OTHERSESS","project":"OtherProj"}',
                 _old_ts, _old_ts, 'decision'),
            )
            conn.commit()
        finally:
            conn.close()

        cs_picks = pick_cross_session_ids(db_path, sid, project='B12', limit=5)
        cs_ids = {p['id'] for p in cs_picks}
        if cs_ids != {110}:
            failures.append(f'pick_cross_session_ids returned {cs_ids}, expected {{110}}')

        cs_skip = pick_cross_session_ids(db_path, sid, project='B12', limit=5, skip_ids=[110])
        if cs_skip:
            failures.append(f'pick_cross_session_ids with skip_ids=[110] should be empty, got {cs_skip}')

        cs_noproj = pick_cross_session_ids(db_path, sid, project='', limit=5)
        if cs_noproj:
            failures.append(f'pick_cross_session_ids with empty project should be empty, got {cs_noproj}')

    if failures:
        print('SELF-TEST FAILED:')
        for f in failures:
            print(f'  - {f}')
        return 1
    print('SELF-TEST OK (14 cases: counter init, fire at N, fire at 2N, peek, project-scoped pick, skip_ids, cosine self, cosine orth, topic_shift first/same/orth, pick_topic_shift filtering, cross_session filter, cross_session skip, cross_session empty-project)')
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--self-test', action='store_true')
    p.add_argument('--session', help='session id to query')
    p.add_argument('--bump', action='store_true', help='increment + report')
    p.add_argument('--peek', action='store_true', help='read without bumping')
    p.add_argument('--every', type=int, default=DEFAULT_RESURFACE_EVERY_N)
    p.add_argument('--db', help='database path (for --pick)')
    p.add_argument('--pick', action='store_true', help='print re-surface candidates')
    p.add_argument('--limit', type=int, default=DEFAULT_RESURFACE_LIMIT)
    args = p.parse_args(argv)

    if args.self_test:
        return _self_test()
    if not args.session:
        p.error('--session required unless --self-test')
        return 2

    if args.bump:
        fire, turn = should_resurface(args.session, every_n=args.every)
        print(f'turn {turn} every={args.every} fire={fire}')
    elif args.peek:
        print(f'turn {read_turn(args.session)}')

    if args.pick and args.db:
        import json as _json
        rows = pick_resurface_ids(args.db, args.session, limit=args.limit)
        for r in rows:
            print(_json.dumps(r, ensure_ascii=False))

    return 0


if __name__ == '__main__':
    sys.exit(main())
