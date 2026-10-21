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



# The guard below models one line of shell at a time: quoting, ( ) and { }
# groups, jobs, and-or lists, pipelines and redirections applied in order,
# plus the fds a hook changes for itself with `exec`. Every fd that still
# points at one of the hook's own output pipes ("hook") when a job is put in
# the background is a leak.

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
# A line that only changes the hook's own fds, such as `exec 3>&2`.
_EXEC_REDIRECT = re.compile(r"^exec((?:\s+(?:\d*|&)(?:>>?|<)&?\s*\S+)+)\s*$")
# A group collapsed by _top_level, numbered in order of appearance.
_GROUP = re.compile("\x00(\\d+)\x00")
_HOOK_FDS = {1: "hook", 2: "hook"}


def _quote_end(line: str, i: int) -> int:
    """Index just past the quoted string that opens at line[i]. Backslash
    escapes count inside double quotes and backticks, not single quotes."""
    quote, j = line[i], i + 1
    while j < len(line):
        if line[j] == "\\" and quote != "'":
            j += 2
        elif line[j] == quote:
            return j + 1
        else:
            j += 1
    return len(line)


def _group_end(line: str, i: int) -> int:
    """Index just past the ( ) or { } group that opens at line[i], skipping
    quoted and escaped delimiters."""
    open_ch, close_ch = line[i], ")" if line[i] == "(" else "}"
    depth, j = 0, i
    while j < len(line):
        ch = line[j]
        if ch == "\\":
            j += 2
            continue
        if ch in "'\"`":
            j = _quote_end(line, j)
            continue
        depth += (ch == open_ch) - (ch == close_ch)
        j += 1
        if depth == 0:
            return j
    return len(line)


def _top_level(line: str) -> tuple[str, list[str]]:
    """The line with only its outer shell syntax left: quoted text, `$( )`,
    `${ }` and process substitutions collapse to `x`, and each ( ) or { }
    group collapses to a numbered placeholder whose body is returned."""
    out, groups, i = [], [], 0
    while i < len(line):
        ch = line[i]
        if ch == "\\":
            out.append("x")
            i += 2
        elif ch in "'\"`":
            out.append(ch + "x" + ch)
            i = _quote_end(line, i)
        elif ch in "({":
            end = _group_end(line, i)
            if i and line[i - 1] in "$<>":
                out.append("x")
            else:
                out.append(f"\x00{len(groups)}\x00")
                groups.append(line[i + 1 : end - 1])
            i = end
        else:
            out.append(ch)
            i += 1
    return "".join(out), groups


def _apply_redirections(command: str, fds: dict[int, str]) -> dict[int, str]:
    """The fds after the command's redirections, applied left to right as the
    shell does: `2>&1 >/dev/null` leaves fd 2 on whatever fd 1 was."""
    fds = dict(fds)
    for fd, op, dup, target in _REDIRECT.findall(command):
        if op == "<":
            continue
        if fd == "&":
            fds[1] = fds[2] = "file"
        elif not dup:
            fds[int(fd or 1)] = "file"
        elif target == "-":
            fds.pop(int(fd or 1), None)
        else:
            fds[int(fd or 1)] = fds.get(int(target), "other") if target.isdigit() else "file"
    return fds


def _hook_fds_left(command: str, start: dict[int, str] | None = None) -> set[int]:
    return {n for n, v in _apply_redirections(command, start or _HOOK_FDS).items() if v == "hook"}


def _leaks(line: str, fds: dict[int, str] | None = None) -> bool:
    """True if the line puts work in the background with an fd still on the
    hook's pipes. Every pipeline stage of every and-or element is checked with
    the fds it starts with (a stage feeding a pipe has stdout, and for `|&`
    stderr, on that pipe), and the body of a ( ) or { } group is checked with
    the fds the group runs with, so `( sleep 30 & echo )` is caught too."""
    fds = dict(fds or _HOOK_FDS)
    line = line.strip()
    if not line or line.startswith("#"):
        return False
    if line[0] in ")}":  # closes a multi-line group; what follows covers all of it
        line = "x" + line[1:]
    flat, groups = _top_level(line)
    parts = _JOB_SEPARATOR.split(re.sub(r"\s#.*$", "", flat))
    for k in range(0, len(parts), 2):
        background = k + 1 < len(parts) and parts[k + 1] == "&"
        for element in _AND_OR.split(parts[k]):
            stages = _PIPE.split(element)
            for i in range(0, len(stages), 2):
                into = stages[i + 1] if i + 1 < len(stages) else None
                start = {**fds, **({1: "pipe"} if into else {}), **({2: "pipe"} if into == "|&" else {})}
                after = _apply_redirections(stages[i], start)
                if background and "hook" in after.values():
                    return True
                if any(_leaks(groups[int(n)], after) for n in _GROUP.findall(stages[i])):
                    return True
    return False


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


def _leaking_lines(path: Path) -> list[int]:
    """Lines of a hook that background work with an fd on the hook's pipes,
    following `exec` redirections such as `exec 3>&2` (a second handle on the
    hook's stderr) down the file."""
    fds, found = dict(_HOOK_FDS), []
    for lineno, line in _shell_lines(path):
        exec_line = _EXEC_REDIRECT.match(_top_level(line.strip())[0])
        if exec_line:
            fds = _apply_redirections(exec_line.group(1), fds)
        elif _leaks(line, fds):
            found.append(lineno)
    return found


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
    assert _hook_fds_left(" >/dev/null 2>&1 ", {1: "hook", 2: "hook", 3: "hook"}) == {3}
    assert _hook_fds_left(" >/dev/null 2>&1 3>&- ", {1: "hook", 2: "hook", 3: "hook"}) == set()


def test_leaks_follow_shell_structure():
    for line in ("cmd && other", "cmd 2>&1", "# sleep 1 &", "x=1", 'echo "a & b"', "( a; b )"):
        assert not _leaks(line)
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
        '( sleep 30; echo ")" ) >/dev/null 2>&1 &',
        'cmd "a \\" & b" >/dev/null 2>&1 &',
        "( sleep 30 & echo done ) >/dev/null 2>&1",
        'x="$(sleep 1 &)"',
    ]
    leaking = [
        '( "$@" ) 2>>"$_log" < /dev/null &',
        "( cmd >/tmp/x 2>&1 ) &",
        '( sleep 30; echo ")" >/dev/null 2>&1 ) &',
        "( sleep 30; echo '}' \\) >/dev/null 2>&1 ) &",
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
        "( sleep 30 & echo done )",
        "{ sleep 30 & }",
        "if true; then ( sleep 30 & ); fi",
    ]
    assert [line for line in detached if _leaks(line)] == []
    assert [line for line in leaking if not _leaks(line)] == []


def test_exec_redirections_carry_down_the_file(tmp_path):
    hook = tmp_path / "hook.sh"
    hook.write_text(
        "exec 3>&2\n"
        "( work ) </dev/null >/dev/null 2>&1 &\n"
        "( work ) </dev/null >/dev/null 2>&1 3>&- &\n"
        "exec 2>>/tmp/err.log 3>&-\n"
        "( work ) >/dev/null &\n"
    )
    assert _leaking_lines(hook) == [2]


def test_heredoc_bodies_are_not_scanned(tmp_path):
    hook = tmp_path / "hook.sh"
    hook.write_text("python3 - <<'PYEOF'\nmask = flags & 0xFF\nPYEOF\nslow &\n")
    assert _leaking_lines(hook) == [4]


def test_every_backgrounded_job_detaches_stdout():
    # Any backgrounded job in a hook (a multi-line block, a one-line group, a
    # plain command, a pipeline or an and-or list, at the end of a line or not,
    # or inside a foreground group) must not leave an fd on the hook's own
    # pipes, including copies made with `exec`, or that command keeps the
    # host's pipe open after the hook exits.
    offenders = [
        f"{path.relative_to(ROOT)}:{lineno}"
        for path in sorted(HOOKS.rglob("*.sh"))
        for lineno in _leaking_lines(path)
    ]
    assert not offenders, f"backgrounded job inherits the hook's output: {offenders}"
