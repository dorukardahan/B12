"""process_executable_path() must expose a deleted runtime on macOS.

When Homebrew removes a versioned Python keg, a long-running daemon keeps
executing the deleted framework binary. On macOS proc_pidpath() then fails with
ENOENT; falling back to sys.executable (the venv launcher, still on disk) hid
the deletion, so the daemons never self-healed and b12_health reported them as
fine. The unit tests fake libproc so they also run on Linux CI; the last test
deletes a real binary on macOS.
"""
from __future__ import annotations

import ctypes
import errno
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import b12_health  # noqa: E402
import b12_mcp_daemon  # noqa: E402
import embed_daemon  # noqa: E402
import shared_patterns  # noqa: E402


class _FakeLibproc:
    def __init__(self, results):
        self._results = list(results)

    def proc_pidpath(self, pid, buf, size):
        path, err = self._results.pop(0)
        if path:
            buf.value = os.fsencode(path)
            return len(buf.value)
        ctypes.set_errno(err)
        return 0


@pytest.fixture
def fake_darwin(monkeypatch):
    def install(results):
        lib = _FakeLibproc(results)
        monkeypatch.setattr(shared_patterns.ctypes, "CDLL", lambda *args, **kwargs: lib)
        return lib

    monkeypatch.setattr(shared_patterns.sys, "platform", "darwin")
    monkeypatch.setattr(shared_patterns, "_own_executable", "")
    return install


def test_own_deleted_runtime_reports_the_last_known_path(fake_darwin, tmp_path):
    runtime = tmp_path / "Cellar" / "python@3.14" / "3.14.8" / "Python"
    runtime.parent.mkdir(parents=True)
    runtime.write_text("")
    fake_darwin([(str(runtime), 0), (None, errno.ENOENT), (None, errno.ENOENT), (None, errno.ENOENT)])

    assert shared_patterns.process_executable_path() == str(runtime)
    runtime.unlink()
    assert shared_patterns.process_executable_path() == str(runtime)
    # Both daemons now see the vanished runtime and schedule a clean restart.
    assert embed_daemon._missing_interpreter_path() == str(runtime)
    assert b12_mcp_daemon._missing_interpreter_path() == str(runtime)


def test_own_deleted_runtime_without_a_prior_lookup_is_still_missing(fake_darwin):
    fake_darwin([(None, errno.ENOENT)])
    path = shared_patterns.process_executable_path()
    assert path == shared_patterns.DELETED_EXECUTABLE and not os.path.exists(path)


def test_health_flags_another_process_with_a_deleted_runtime(fake_darwin):
    fake_darwin([(None, errno.ENOENT)])
    command = "/home/user/.local/b12-venv/bin/python3 /home/user/.B12/hooks/scripts/b12_mcp_daemon.py"
    executable = b12_health._process_executable(99999, command)
    assert executable == shared_patterns.DELETED_EXECUTABLE, "must not fall back to the still-present venv argv"


@pytest.mark.parametrize("err", [errno.ESRCH, errno.EPERM])
def test_other_failures_keep_the_previous_fallback(fake_darwin, err):
    fake_darwin([(None, err), (None, err)])
    assert shared_patterns.process_executable_path(99999) == ""
    assert shared_patterns.process_executable_path() == sys.executable


@pytest.mark.skipif(sys.platform != "darwin" or shutil.which("cc") is None, reason="needs macOS and a C compiler")
def test_real_deleted_binary_on_macos():
    work = Path(tempfile.mkdtemp(prefix="b12x-", dir="/tmp"))
    try:
        source = work / "sleeper.c"
        source.write_text("#include <unistd.h>\nint main(void){sleep(30);return 0;}\n")
        binary = work / "sleeper"
        subprocess.run(["cc", "-o", str(binary), str(source)], check=True, capture_output=True)
        proc = subprocess.Popen([str(binary)])
        try:
            deadline = time.monotonic() + 5
            while shared_patterns.process_executable_path(proc.pid) != os.path.realpath(binary):
                assert time.monotonic() < deadline, "binary path never reported"
                time.sleep(0.05)
            binary.unlink()
            assert shared_patterns.process_executable_path(proc.pid) == shared_patterns.DELETED_EXECUTABLE
        finally:
            proc.kill()
            proc.wait()
    finally:
        shutil.rmtree(work, ignore_errors=True)
