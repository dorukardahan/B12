#!/usr/bin/env python3
"""Per-session B12 injection budget (T2 of the proactive-recall plan).

B12 must never spend more than ~8% of a host context window on its own
injected memories. On Claude Code Opus 4.7 (1M ctx) that is ~80,000 tokens
TOTAL across the entire session — accumulated across every UserPromptSubmit,
PreToolUse Read/Edit/Write, PostToolUse Bash, and PreCompact fire. Once the
budget is exhausted, further injections are skipped and logged so the user
keeps their context for their actual work.

Token accounting is char-based (chars * 0.25 ≈ tokens) to avoid loading a
real tokenizer in a sync hook path. The proxy is ±15% accurate, which is
why the cap (80K) carries a margin against the real ceiling.

State lives at ``$B12_DATA_DIR/state/session-tok-<session_id[:12]>.txt``:
one integer per line, total tokens injected so far.

Public API:
    proxy_tokens(s)           — char-based token estimate
    cumulative_used(sid)      — read state file → int
    record_inject(sid, n)     — atomically add n tokens to state
    can_inject(sid, n, ceil)  — (ok, remaining_budget, would_be_total)
    clear_session(sid)        — drop the state file (for self-test)
"""
from __future__ import annotations

import argparse
import math
import os
import sys
import tempfile
import time


DEFAULT_MAX_TOKENS = 80_000
TOKEN_RATIO = 0.25  # chars * 0.25 ≈ tokens (~85% accurate for prose mixes EN/TR)
_SID_PREFIX_LEN = 12  # lockstep with hooks/memory-session-end.sh:907 (R10)


def _base_dir() -> str:
    return os.environ.get('B12_DATA_DIR', os.path.expanduser('~/.B12'))


def _state_dir() -> str:
    d = os.path.join(_base_dir(), 'state')
    os.makedirs(d, exist_ok=True)
    return d


def _sid12(session_id: str) -> str:
    """Truncate the session id to the canonical 12-char window.

    Hooks that write source_session: session_id[:12] (regex pipeline) read
    back via this same function — keep them in lockstep.
    """
    s = (session_id or '').strip().replace('/', '_').replace('\\', '_')
    return s[:_SID_PREFIX_LEN] if s else 'unknown'


def state_path(session_id: str) -> str:
    return os.path.join(_state_dir(), f'session-tok-{_sid12(session_id)}.txt')


def proxy_tokens(text: str | bytes) -> int:
    """Char-based token proxy. ``chars * 0.25`` rounded up."""
    if not text:
        return 0
    if isinstance(text, bytes):
        n = len(text)
    else:
        n = len(text)
    return int(math.ceil(n * TOKEN_RATIO))


def cumulative_used(session_id: str) -> int:
    """Return total tokens recorded for this session (0 if no state)."""
    p = state_path(session_id)
    try:
        with open(p, 'r') as f:
            v = f.read().strip()
        return int(v) if v else 0
    except (FileNotFoundError, ValueError):
        return 0


def record_inject(session_id: str, tokens: int) -> int:
    """Atomically add `tokens` to the session counter. Returns new total.

    Uses write-then-rename for atomicity so a crash mid-write leaves the
    previous integer intact (no partial-file parses).
    """
    if tokens <= 0:
        return cumulative_used(session_id)
    total = cumulative_used(session_id) + int(tokens)
    p = state_path(session_id)
    fd, tmp = tempfile.mkstemp(prefix='b12tok-', dir=_state_dir())
    try:
        with os.fdopen(fd, 'w') as f:
            f.write(str(total))
        os.replace(tmp, p)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return total


def can_inject(
    session_id: str,
    requested_tokens: int,
    ceiling: int = DEFAULT_MAX_TOKENS,
) -> tuple[bool, int, int]:
    """Decide whether the next injection fits the per-session budget.

    Returns (ok, remaining_budget_before_this_inject, would_be_total_after).
    `ok=False` means the requested injection would exceed `ceiling`.
    """
    used = cumulative_used(session_id)
    remaining = max(0, ceiling - used)
    would_be = used + max(0, int(requested_tokens))
    return (would_be <= ceiling, remaining, would_be)


def clear_session(session_id: str) -> None:
    p = state_path(session_id)
    try:
        os.unlink(p)
    except FileNotFoundError:
        pass


# ── Dedup ledger (T3) ────────────────────────────────────────
# Same memory.id should not be injected twice in the same session. Ledger
# file: ``state/session-injected-<sid[:12]>.txt`` — one integer per line,
# most-recent-first, LRU-evicted at 500 entries.

DEDUP_LRU_LIMIT = 500


def ledger_path(session_id: str) -> str:
    return os.path.join(_state_dir(), f'session-injected-{_sid12(session_id)}.txt')


def load_ledger(session_id: str) -> list[int]:
    """Return injected memory IDs in most-recent-first order."""
    p = ledger_path(session_id)
    try:
        with open(p, 'r') as f:
            out = []
            for line in f:
                s = line.strip()
                if not s:
                    continue
                try:
                    out.append(int(s))
                except ValueError:
                    continue
            return out
    except FileNotFoundError:
        return []


def filter_unseen(session_id: str, mem_ids: list[int]) -> list[int]:
    """Return only the IDs that are not yet in the session ledger."""
    seen = set(load_ledger(session_id))
    return [m for m in mem_ids if int(m) not in seen]


def record_injected(session_id: str, mem_ids: list[int]) -> int:
    """Prepend mem_ids to the ledger and LRU-evict to DEDUP_LRU_LIMIT.

    Returns new ledger length. Atomic via write-then-rename.
    """
    if not mem_ids:
        return len(load_ledger(session_id))
    existing = load_ledger(session_id)
    seen = set(existing)
    head = []
    for m in mem_ids:
        mi = int(m)
        if mi in seen:
            continue
        head.append(mi)
        seen.add(mi)
    merged = head + existing
    merged = merged[:DEDUP_LRU_LIMIT]
    p = ledger_path(session_id)
    fd, tmp = tempfile.mkstemp(prefix='b12led-', dir=_state_dir())
    try:
        with os.fdopen(fd, 'w') as f:
            for mi in merged:
                f.write(str(mi) + '\n')
        os.replace(tmp, p)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return len(merged)


def warn_log_path() -> str:
    d = os.path.join(_base_dir(), 'memory-logs')
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, 'token-budget-skips.jsonl')


def log_skip(session_id: str, reason: str, requested: int, used: int,
             ceiling: int) -> None:
    """Append a JSON line documenting a skip. Best-effort, never raises."""
    import json
    rec = {
        'ts': int(time.time()),
        'session_id': _sid12(session_id),
        'reason': reason,
        'requested_tokens': int(requested),
        'cumulative_used': int(used),
        'ceiling': int(ceiling),
    }
    try:
        with open(warn_log_path(), 'a') as f:
            f.write(json.dumps(rec, ensure_ascii=False) + '\n')
    except OSError:
        pass


# ── Self-test ───────────────────────────────────────────────────
def _self_test() -> int:
    failures: list[str] = []
    sid = f'test-{int(time.time())}-budget'

    # Use a tmpdir so we never touch the user's real ~/.B12/state.
    with tempfile.TemporaryDirectory() as tmp:
        os.environ['B12_DATA_DIR'] = tmp

        # Reset module-level path cache (none here — but re-deriving each call).
        if cumulative_used(sid) != 0:
            failures.append('initial cumulative_used != 0')

        # proxy_tokens
        if proxy_tokens('') != 0:
            failures.append('empty proxy_tokens != 0')
        if proxy_tokens('x' * 400) != 100:
            failures.append('400-char proxy != 100 tokens')

        # record + accumulate
        record_inject(sid, 100)
        record_inject(sid, 200)
        if cumulative_used(sid) != 300:
            failures.append(f'after two records expected 300 got {cumulative_used(sid)}')

        # can_inject within budget
        ok, remaining, would = can_inject(sid, 100, ceiling=1000)
        if not ok or remaining != 700 or would != 400:
            failures.append(f'can_inject within budget wrong: {ok=}, {remaining=}, {would=}')

        # can_inject exceeds
        ok, remaining, would = can_inject(sid, 800, ceiling=1000)
        if ok or remaining != 700 or would != 1100:
            failures.append(f'can_inject exceeds wrong: {ok=}, {remaining=}, {would=}')

        # session_id truncation: only first 12 chars matter
        sid_long = sid + 'extra-suffix-should-not-affect-truncation'
        if cumulative_used(sid_long) != 300:
            failures.append('sid truncation did not match — state files diverged')

        # log_skip writes a line
        log_skip(sid, 'over_budget', 800, 300, 1000)
        log_path = warn_log_path()
        with open(log_path) as f:
            lines = [ln for ln in f.read().splitlines() if ln.strip()]
        if not lines:
            failures.append('log_skip produced no line')

        # clear_session removes state
        clear_session(sid)
        if cumulative_used(sid) != 0:
            failures.append('clear_session did not reset')

        # Tampered state file: graceful 0
        with open(state_path(sid), 'w') as f:
            f.write('NOT_AN_INT')
        if cumulative_used(sid) != 0:
            failures.append('tampered state file did not fall back to 0')

        # ── Dedup ledger ────────────────────────────────────────
        sid2 = f'ledger-test-{int(time.time())}'
        if load_ledger(sid2) != []:
            failures.append('initial ledger not empty')
        record_injected(sid2, [101, 102, 103])
        if load_ledger(sid2) != [101, 102, 103]:
            failures.append(f'ledger after first write: {load_ledger(sid2)}')
        # New IDs prepend, repeat IDs stay deduped
        record_injected(sid2, [104, 102, 105])
        if load_ledger(sid2) != [104, 105, 101, 102, 103]:
            failures.append(f'ledger after second write: {load_ledger(sid2)}')
        # filter_unseen drops already-injected
        unseen = filter_unseen(sid2, [101, 999, 102, 1000])
        if set(unseen) != {999, 1000}:
            failures.append(f'filter_unseen returned {unseen}')
        # LRU eviction at DEDUP_LRU_LIMIT
        record_injected(sid2, list(range(10_000, 10_000 + DEDUP_LRU_LIMIT)))
        if len(load_ledger(sid2)) != DEDUP_LRU_LIMIT:
            failures.append(f'LRU did not cap at {DEDUP_LRU_LIMIT}')

    if failures:
        print('SELF-TEST FAILED:')
        for f in failures:
            print(f'  - {f}')
        return 1
    print('SELF-TEST OK (13 cases)')
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--self-test', action='store_true')
    p.add_argument('--session', help='session id to query')
    p.add_argument('--inject', type=int, default=0,
                   help='record this many tokens for the session')
    p.add_argument('--reset', action='store_true',
                   help='clear the named session\'s state')
    p.add_argument('--ceiling', type=int, default=DEFAULT_MAX_TOKENS)
    args = p.parse_args(argv)

    if args.self_test:
        return _self_test()
    if not args.session:
        p.error('--session required unless --self-test')
        return 2

    if args.reset:
        clear_session(args.session)
        print('cleared')
        return 0

    if args.inject:
        total = record_inject(args.session, args.inject)
        print(f'recorded {args.inject} → cumulative {total}/{args.ceiling}')
    else:
        used = cumulative_used(args.session)
        ok, remaining, would = can_inject(args.session, 0, ceiling=args.ceiling)
        print(f'session {args.session[:_SID_PREFIX_LEN]}: used={used} '
              f'remaining={remaining} ceiling={args.ceiling}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
