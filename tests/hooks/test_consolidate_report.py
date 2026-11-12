"""The nightly --auto consolidation log must not carry memory text.

The launchd job appends every run's report to one log. Listing every pair with
80 characters of both memories made that log grow ~6 MB a night and turned it
into a second, unrotated copy of the memory store. Dry runs, which a person
reads, keep the text.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

HOOK = Path(__file__).resolve().parents[2] / "hooks" / "memory-consolidate.py"


def _load():
    spec = importlib.util.spec_from_file_location("memory_consolidate_hook", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


REPORT = [
    {"type": "dedup", "ids": [11, 12], "similarity": 0.991,
     "snippets": ["SECRET-LOOKING memory text one", "SECRET-LOOKING memory text two"]},
    {"type": "merge_unverified", "ids": [21, 22], "similarity": 0.95,
     "snippets": ["unverified pair text a", "unverified pair text b"]},
    {"type": "merge_unverified", "ids": [23, 24], "similarity": 0.94,
     "snippets": ["unverified pair text c", "unverified pair text d"]},
    {"type": "contradiction", "ids": [31, 32], "similarity": 0.93, "nli_score": 0.97,
     "snippets": ["contradiction text a", "contradiction text b"]},
]


def test_auto_run_prints_ids_and_counts_but_no_memory_text():
    out = "\n".join(_load().format_pair_details(REPORT, dry_run=False))
    for entry in REPORT:
        for snippet in entry["snippets"]:
            assert snippet not in out
    assert "contradiction=1" in out and "dedup=1" in out and "merge_unverified=2" in out
    assert "#11 <-> #12" in out and "0.991" in out
    assert "#21" not in out  # only dedup pairs are listed by id
    assert "No NLI verdict for 2 pairs" in out


def test_dry_run_keeps_every_pair_with_its_text():
    out = "\n".join(_load().format_pair_details(REPORT, dry_run=True))
    for entry in REPORT:
        assert f"#{entry['ids'][0]} <-> #{entry['ids'][1]}" in out
        for snippet in entry["snippets"]:
            assert snippet in out
    assert "NLI:0.97" in out


def test_empty_report_prints_nothing():
    mod = _load()
    assert mod.format_pair_details([], dry_run=False) == []
    assert mod.format_pair_details([], dry_run=True) == []


def test_auto_run_without_unverified_pairs_has_no_nli_line():
    out = "\n".join(_load().format_pair_details(REPORT[:1], dry_run=False))
    assert "NLI" not in out
