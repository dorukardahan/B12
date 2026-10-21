"""Guards for hook watchdogs holding the host's stdout pipe open.

A watchdog started as `( sleep N && ... ) &` inherits the hook's stdout. The
hook's EXIT trap kills the watchdog subshell but not its `sleep` child, so the
orphaned `sleep` keeps the pipe's write end open and the host sees EOF only when
the timer expires. Claude Code refuses hook output that injects context when
stdout goes quiet before EOF, so surfaced memories were silently dropped.
Every watchdog must detach from the hook's stdio.
"""
from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HOOKS = ROOT / "hooks"

_WATCHDOG = re.compile(r"^\(\s*sleep\s+\S+\s*&&\s*kill\b.*\)(?P<tail>.*)&\s*$")


def test_every_inline_watchdog_detaches_stdout():
    offenders = []
    for path in sorted(HOOKS.rglob("*.sh")):
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            m = _WATCHDOG.match(line.strip())
            if m and ">/dev/null" not in m.group("tail"):
                offenders.append(f"{path.relative_to(ROOT)}:{lineno}")
    assert not offenders, f"watchdog inherits hook stdout: {offenders}"


def _run_with_sync_watchdog(cap: str, body: str, tmp_path: Path) -> tuple[float, float, bytes]:
    script = (
        f'export B12_DATA_DIR="{tmp_path}"; '
        f'. "{HOOKS / "_b12_common.sh"}"; '
        f"b12_sync_watchdog {cap} test; {body}"
    )
    start = time.monotonic()
    proc = subprocess.Popen(["bash", "-c", script], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    proc.wait(timeout=10)
    exited = time.monotonic() - start
    assert proc.stdout is not None
    out = proc.stdout.read()
    eof = time.monotonic() - start
    return exited, eof, out


def test_sync_watchdog_releases_stdout_at_exit(tmp_path):
    # The 0.1 s of work lets the watchdog fork its `sleep` before the hook exits,
    # which is what happens in real hooks.
    payload = '{"hookSpecificOutput":{"additionalContext":"x"}}'
    exited, eof, out = _run_with_sync_watchdog(
        "2.0", f"sleep 0.1; b12_mark_output_emitted; echo '{payload}'; exit 0", tmp_path
    )
    assert out.strip() == payload.encode()
    assert eof - exited < 0.5, f"stdout EOF {eof:.2f}s vs exit {exited:.2f}s: watchdog held the pipe"


def test_sync_watchdog_still_emits_fallback_when_cap_fires(tmp_path):
    exited, _eof, out = _run_with_sync_watchdog("0.3", "sleep 3; echo late", tmp_path)
    assert out.strip() == b"{}"
    assert exited < 2.0, f"cap did not fire: hook ran {exited:.2f}s"
    log = tmp_path / "memory-logs" / "sync-cap-hits.jsonl"
    assert log.exists() and '"label":"test"' in log.read_text()
