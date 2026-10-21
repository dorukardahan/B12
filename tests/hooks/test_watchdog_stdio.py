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



# Control operators that start a new job (`;` and a lone `&`, not `&&`, `>&`,
# `&>` or `|&`), and those that chain an and-or list or a pipeline.
_JOB_SEPARATOR = re.compile(r"(;|(?<![&>|<])&(?![&>]))")
_AND_OR = re.compile(r"&&|\|\|")
_PIPE = re.compile(r"(\|&?)")
# One redirection: an fd (or `&` for both outputs), the operator, `&` when it
# duplicates or closes, and the target (a file, an fd number or `-`).
_REDIRECT = re.compile(r"(?<!\S)(\d*|&)(>>?|<)(&?)\s*([^\s;&|<>]+)")
# A heredoc opener (`<<EOF`, `<<-'EOF'`), not a `<<<` here-string.
_HEREDOC = re.compile(r"(?<!<)<<-?\s*(['\"]?)(\w+)\1(?!<)")


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


def _background_commands(line: str) -> list[tuple[str, dict[int, str]]] | None:
    """For a line that backgrounds work, every pipeline stage of every and-or
    element of every job followed by `&` (wherever it sits on the line), with
    the fds it starts with: a stage that feeds a pipe has its stdout (and, for
    `|&`, its stderr) on that pipe; everything else starts on the hook's own
    pipes. None if nothing is backgrounded."""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if line[0] in ")}":  # closes a multi-line group; what follows covers all of it
        line = "x" + line[1:]
    parts = _JOB_SEPARATOR.split(re.sub(r"\s#.*$", "", _top_level(line)))
    commands = []
    for k in range(0, len(parts) - 1, 2):
        if parts[k + 1] != "&":
            continue
        for element in _AND_OR.split(parts[k]):
            stages = _PIPE.split(element)
            for i in range(0, len(stages), 2):
                into = stages[i + 1] if i + 1 < len(stages) else None
                fds = {1: "pipe" if into else "hook", 2: "pipe" if into == "|&" else "hook"}
                commands.append((stages[i], fds))
    return commands or None


def _hook_fds_left(command: str, start: dict[int, str] | None = None) -> set[int]:
    """Apply the command's redirections left to right, as the shell does, and
    return which of fd 1 and fd 2 still point at one of the hook's own output
    pipes. `2>&1 >/dev/null` leaves fd 2 on the hook's stdout."""
    fds = dict(start or {1: "hook", 2: "hook"})
    for fd, op, dup, target in _REDIRECT.findall(command):
        if op == "<":
            continue
        if fd == "&":
            fds[1] = fds[2] = "file"
        elif not dup:
            fds[int(fd or 1)] = "file"
        elif target == "-":
            fds[int(fd or 1)] = "closed"
        else:
            fds[int(fd or 1)] = fds.get(int(target), "other") if target.isdigit() else "file"
    return {n for n in (1, 2) if fds[n] == "hook"}


def _leaks(line: str) -> bool:
    return any(_hook_fds_left(c, fds) for c, fds in _background_commands(line) or [])


def _shell_lines(path: Path):
    """(lineno, line) for the shell code of a hook, skipping heredoc bodies
    such as embedded Python, where `&` is not a shell operator."""
    terminator = None
    for lineno, line in enumerate(path.read_text().splitlines(), 1):
        if terminator is not None:
            if line.strip() == terminator:
                terminator = None
            continue
        yield lineno, line
        heredoc = _HEREDOC.search(line)
        if heredoc:
            terminator = heredoc.group(2)


def test_redirections_apply_left_to_right():
    assert _hook_fds_left(" </dev/null >/dev/null 2>&1 3>&- ") == set()
    assert _hook_fds_left(' >> "$LOG" 2>&1 ') == set()
    assert _hook_fds_left(" &>/dev/null ") == set()
    assert _hook_fds_left(" >&- 2>&- ") == set()
    assert _hook_fds_left(" 2>&1 >/dev/null ") == {2}
    assert _hook_fds_left(" >/dev/null ") == {2}
    assert _hook_fds_left(" 2>/dev/null ") == {1}
    assert _hook_fds_left(" >&2 2>/dev/null ") == {1}
    assert _hook_fds_left(" ") == {1, 2}
    assert _hook_fds_left(" ", {1: "pipe", 2: "hook"}) == {2}
    assert _hook_fds_left(" 2>/dev/null ", {1: "pipe", 2: "hook"}) == set()
    assert _hook_fds_left(" ", {1: "pipe", 2: "pipe"}) == set()


def test_background_commands_follow_shell_structure():
    for line in ("cmd && other", "cmd 2>&1", "# sleep 1 &", "x=1", 'echo "a & b"'):
        assert _background_commands(line) is None
    detached = [
        ") </dev/null >/dev/null 2>&1 &",
        "} >/dev/null 2>&1 &",
        '( "$@" ) >>"$_log" 2>&1 < /dev/null &',
        "( sleep 5 && kill -TERM $$ 2>/dev/null ) </dev/null >/dev/null 2>&1 &",
        'cmd >"${LOG}" 2>&1 &',
        '{ echo "$X" | bash "$B"; } >/dev/null 2>&1 &',
        "sleep 30 2>/dev/null | cat >/dev/null 2>&1 &",
        "a |& b >/dev/null 2>&1 &",
        "find . \\( -name '*.js' \\) -print > \"$raw\" 2>/dev/null &",
        "a >/dev/null 2>&1 && b >/dev/null 2>&1 &",
        "foreground; bg >/dev/null 2>&1 &",
        "slow >/dev/null 2>&1 & echo started",
        "cmd &>/dev/null &",
    ]
    leaking = [
        '( "$@" ) 2>>"$_log" < /dev/null &',
        "( cmd >/tmp/x 2>&1 ) &",
        "{ cmd >/tmp/x 2>&1; } &",
        "producer >/tmp/x | sleep 30 &",
        "sleep 30 | cat >/dev/null 2>&1 &",
        'echo "$X" | bash "$B" >/dev/null 2>&1 &',
        "} | tee x &",
        'cmd "$(date >/tmp/x)" &',
        "a && b >/dev/null 2>&1 &",
        "quiet >/dev/null 2>&1 & loud &",
        "slow_command & echo done",
        "cmd 2>&1 >/dev/null &",
        "cmd >&2 &",
    ]
    assert [line for line in detached if _leaks(line)] == []
    assert [line for line in leaking if not _leaks(line)] == []


def test_heredoc_bodies_are_not_scanned(tmp_path):
    hook = tmp_path / "hook.sh"
    hook.write_text("python3 - <<'PYEOF'\nmask = flags & 0xFF\nPYEOF\nslow &\n")
    assert [n for n, line in _shell_lines(hook) if _leaks(line)] == [4]


def test_every_backgrounded_job_detaches_stdout():
    # Any backgrounded job in a hook (a multi-line block, a one-line group, a
    # plain command, a pipeline or an and-or list, at the end of a line or not)
    # must not leave stdout or stderr on the hook's own pipes, or that command
    # keeps the host's pipe open after the hook exits.
    offenders = []
    for path in sorted(HOOKS.rglob("*.sh")):
        for lineno, line in _shell_lines(path):
            if _leaks(line):
                offenders.append(f"{path.relative_to(ROOT)}:{lineno}")
    assert not offenders, f"backgrounded job inherits the hook's output: {offenders}"
