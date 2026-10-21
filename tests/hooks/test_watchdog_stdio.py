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
# stdout (fd 1) must go to a file or be closed: `>`, `1>`, `>>`, `1>>`, `&>`,
# `&>>` or `>&-`. `2>...` leaves fd 1 on the host pipe and `>&2` only moves it
# to another inherited fd.
_STDOUT_TO_FILE = re.compile(r"(?:^|\s)(?:(?:1?|&)>{1,2}\s*[^\s&]|1?>&-)")
# Control operators that start a new job, and those that chain an and-or list.
_JOB_SEPARATOR = re.compile(r"(;|(?<![&>|<])&(?![&>]))")
_AND_OR = re.compile(r"&&|\|\|")
_PIPE = re.compile(r"\|&?")


def _top_level(line: str) -> str:
    """The line with quoted text and nested ( ), $( ), { } and ${ } groups
    collapsed to `x`, so only the outer shell syntax is left."""
    out, i = [], 0
    while i < len(line):
        ch = line[i]
        if ch == "\\":
            out.append("x")
            i += 2
        elif ch in "'\"`":
            end = line.find(ch, i + 1)
            out.append(ch + "x" + ch)
            i = len(line) if end < 0 else end + 1
        elif ch in "({":
            close, depth, j = (")" if ch == "(" else "}"), 0, i
            while j < len(line):
                depth += (line[j] == ch) - (line[j] == close)
                if depth == 0:
                    break
                j += 1
            out.append("x")
            i = j + 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _stdout_holders(line: str) -> list[str] | None:
    """For a line that backgrounds work, the commands whose stdout is still the
    hook's: the last pipeline stage of every and-or element of every job that
    ends in `&`. None if the line backgrounds nothing."""
    line = line.strip()
    if not line or line.startswith("#") or not _BACKGROUNDED.search(line):
        return None
    if line[0] in ")}":  # closes a multi-line group; what follows covers all of it
        line = "x" + line[1:]
    flat = re.sub(r"\s#.*$", "", _top_level(line))
    parts = _JOB_SEPARATOR.split(flat)
    holders = []
    for k in range(0, len(parts) - 1, 2):
        if parts[k + 1] == "&":
            holders += [_PIPE.split(element)[-1] for element in _AND_OR.split(parts[k])]
    return holders


def _detaches_stdout(command: str) -> bool:
    return bool(_STDOUT_TO_FILE.search(" " + command))


def _leaks(line: str) -> bool:
    holders = _stdout_holders(line)
    return holders is not None and not all(_detaches_stdout(h) for h in holders)


def test_stdout_redirect_matcher():
    assert _detaches_stdout(" </dev/null >/dev/null 2>&1 3>&- ")
    assert _detaches_stdout(" > /dev/null 2>&1 ")
    assert _detaches_stdout(' >> "$LOG" 2>&1 ')
    assert _detaches_stdout(" 1>/dev/null ")
    assert not _detaches_stdout(" 2>/dev/null ")
    assert not _detaches_stdout(" 2>>errors.log ")
    assert not _detaches_stdout(" >&2 ")
    assert not _detaches_stdout(" ")


def test_stdout_holders_follow_shell_structure():
    for line in ("cmd && other", "cmd 2>&1", "# sleep 1 &", "x=1"):
        assert _stdout_holders(line) is None
    detached = [
        ") </dev/null >/dev/null 2>&1 &",
        "} >/dev/null 2>&1 &",
        '( "$@" ) >>"$_log" 2>&1 < /dev/null &',
        "( sleep 5 && kill -TERM $$ 2>/dev/null ) </dev/null >/dev/null 2>&1 &",
        'cmd >"${LOG}" 2>&1 &',
        'echo "$X" | bash "$B" >/dev/null 2>&1 &',
        "find . \\( -name '*.js' \\) -print > \"$raw\" 2>/dev/null &",
        "a >/dev/null && b >/dev/null &",
        "foreground; bg >/dev/null 2>&1 &",
        "cmd &>/dev/null &",
        "cmd >&- &",
    ]
    leaking = [
        '( "$@" ) 2>>"$_log" < /dev/null &',
        "( cmd >/tmp/x ) &",
        "{ cmd >/tmp/x; } &",
        "producer >/tmp/x | sleep 30 &",
        "} | tee x &",
        'cmd "$(date >/tmp/x)" &',
        "a && b >/dev/null &",
        "quiet >/dev/null & loud &",
        "cmd >&2 &",
    ]
    assert [line for line in detached if _leaks(line)] == []
    assert [line for line in leaking if not _leaks(line)] == []


def test_every_backgrounded_job_detaches_stdout():
    # Any backgrounded job in a hook (a multi-line block, a one-line group, a
    # plain command, a pipeline or an and-or list) must not leave a command on
    # the hook's stdout, or that command keeps the host's pipe open after the
    # hook exits.
    offenders = []
    for path in sorted(HOOKS.rglob("*.sh")):
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            if _leaks(line):
                offenders.append(f"{path.relative_to(ROOT)}:{lineno}")
    assert not offenders, f"backgrounded job inherits hook stdout: {offenders}"
