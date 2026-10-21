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


# A line that backgrounds a job ends in a lone `&` (not `&&`, `>&`, `|&`).
_BACKGROUNDED = re.compile(r"(?<![&>|])&\s*(?:#.*)?$")
# stdout (fd 1, written as `>`, `1>`, `>>` or `1>>`) must go to a file; `2>...`
# leaves fd 1 on the host pipe and `>&2` only moves it to another inherited fd.
_STDOUT_TO_FILE = re.compile(r"(?:^|\s)1?>{1,2}\s*[^\s&]")


def _group_tail(line: str) -> str | None:
    # Text after the group that opens the line, found by depth so `${VAR}` and
    # `$(cmd)` inside it do not end it early. Quoted text is skipped.
    open_ch, close_ch = line[0], ")" if line[0] == "(" else "}"
    depth, quote = 0, None
    for i, ch in enumerate(line):
        if quote:
            quote = None if ch == quote else quote
        elif ch in "'\"":
            quote = ch
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return line[i + 1 :]
    return None


def _background_redirects(line: str) -> str | None:
    """The redirections that apply to a backgrounded job, or None if the line
    does not background one. Inner redirections of a group do not count."""
    line = line.strip()
    if not line or line.startswith("#") or not _BACKGROUNDED.search(line):
        return None
    if line[0] in ")}":  # end of a multi-line ( ... ) or { ...; } block
        return line[1:]
    if line[0] in "({":  # one-line group
        tail = _group_tail(line)
        return tail if tail is not None else line
    return line  # a plain command or pipeline


def _detaches_stdout(tail: str) -> bool:
    return bool(_STDOUT_TO_FILE.search(" " + tail))


def test_stdout_redirect_matcher():
    assert _detaches_stdout(" </dev/null >/dev/null 2>&1 3>&- ")
    assert _detaches_stdout(" > /dev/null 2>&1 ")
    assert _detaches_stdout(' >> "$LOG" 2>&1 ')
    assert _detaches_stdout(" 1>/dev/null ")
    assert not _detaches_stdout(" 2>/dev/null ")
    assert not _detaches_stdout(" 2>>errors.log ")
    assert not _detaches_stdout(" >&2 ")
    assert not _detaches_stdout(" ")


def test_background_redirects_finds_the_outer_redirections():
    assert _background_redirects("cmd && other") is None
    assert _background_redirects("cmd 2>&1") is None
    assert _background_redirects("# sleep 1 &") is None
    assert _detaches_stdout(_background_redirects(") </dev/null >/dev/null 2>&1 &"))
    assert _detaches_stdout(_background_redirects('( "$@" ) >>"$_log" 2>&1 < /dev/null &'))
    assert not _detaches_stdout(_background_redirects('( "$@" ) 2>>"$_log" < /dev/null &'))
    assert not _detaches_stdout(_background_redirects("( cmd >/tmp/x ) &"))
    assert not _detaches_stdout(_background_redirects("{ cmd >/tmp/x; } &"))
    assert _detaches_stdout(_background_redirects('cmd >"${LOG}" 2>&1 &'))
    assert not _detaches_stdout(_background_redirects('cmd "$(date)" &'))
    assert _detaches_stdout(_background_redirects('echo "$X" | bash "$B" >/dev/null 2>&1 &'))


def test_every_backgrounded_job_detaches_stdout():
    # Any backgrounded job in a hook, whether a multi-line block, a one-line
    # ( ... ) / { ...; } group or a plain command, must not inherit the hook's
    # stdout, or it keeps the host's pipe open after the hook exits.
    offenders = []
    for path in sorted(HOOKS.rglob("*.sh")):
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            tail = _background_redirects(line)
            if tail is not None and not _detaches_stdout(tail):
                offenders.append(f"{path.relative_to(ROOT)}:{lineno}")
    assert not offenders, f"backgrounded job inherits hook stdout: {offenders}"
