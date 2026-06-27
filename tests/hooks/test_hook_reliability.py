"""Guards for the hook-reliability fixes (2026-06-27 audit #8 + #18).

#8: BSD-first `stat -f` poisons mtime/inode on Linux (`-f` = --file-system there,
    which writes a report to stdout instead of failing cleanly). Three sites must
    be GNU-first (`-c`) with a BSD fallback + an all-digits guard.
#18: memory-proactive-surface continued UNLOCKED when the mutex couldn't be
    acquired — under the exact contention the lock exists for, two fires both
    read/bump/write the surfacing state and double-surface. It must bail.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_stat_sites_are_gnu_first():
    ss = (ROOT / "hooks" / "memory-session-start.sh").read_text()
    assert 'stat -c %Y "$1" 2>/dev/null || stat -f %m' in ss, "session-start file_mtime not GNU-first (#8)"

    ps = (ROOT / "hooks" / "memory-proactive-surface.sh").read_text()
    assert 'stat -c %Y "$SURFACE_LOCK" 2>/dev/null || stat -f %m' in ps, "proactive-surface mtime not GNU-first (#8)"

    sm = (ROOT / "scripts" / "b12_smoke.sh").read_text()
    assert 'stat -c %i "$setup_raw" 2>/dev/null || stat -f %i' in sm, "b12_smoke inode not GNU-first (#8)"

    # All three must carry the all-digits guard (catches a GNU FS-report leaking in).
    for src in (ss, ps, sm):
        assert "''|*[!0-9]*)" in src, "a stat site is missing the all-digits guard (#8)"


def test_all_digits_guard_rejects_gnu_fsid_shape():
    """The guard must reject the hex-ish FS-id shapes GNU `stat -f` can emit on
    Linux, while passing a real numeric mtime/inode through."""
    for val, expect in [("1000ab", "EMPTY"), ("0:1d", "EMPTY"), ("", "EMPTY"), ("12345", "12345")]:
        out = subprocess.run(
            ["bash", "-c", f'm="{val}"; case "$m" in \'\'|*[!0-9]*) echo EMPTY ;; *) echo "$m" ;; esac'],
            capture_output=True, text=True,
        )
        assert out.stdout.strip() == expect, f"guard mishandled {val!r}: {out.stdout!r}"


def test_gnu_first_stat_yields_a_number_on_this_platform(tmp_path):
    """The GNU-first chain must produce a numeric mtime on whatever platform runs
    the tests (Linux -> -c %Y; macOS -> -f %m fallback)."""
    f = tmp_path / "x"
    f.write_text("hi")
    out = subprocess.run(
        ["bash", "-c", f'stat -c %Y "{f}" 2>/dev/null || stat -f %m "{f}" 2>/dev/null'],
        capture_output=True, text=True,
    )
    assert out.stdout.strip().isdigit(), f"GNU-first stat returned non-digit: {out.stdout!r}"


def test_surface_bails_when_lock_not_acquired():
    ps = (ROOT / "hooks" / "memory-proactive-surface.sh").read_text()
    m = re.search(r"if _b12_surface_lock_acquire; then.*?\nelse\n(.*?)\nfi", ps, re.DOTALL)
    assert m, "no else branch on the surface lock-acquire (proceeds unlocked, #18)"
    body = m.group(1)
    assert "exit 0" in body, "surface does not bail (exit 0) when the lock can't be acquired (#18)"
    # Must be `exit 0`, never a top-level `return` (statement-level check so a
    # comment containing the word "return" doesn't trip it).
    assert not re.search(r"^\s*return\b", body, re.M), "surface lock-fail must use exit 0, not a top-level return"
