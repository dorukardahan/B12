"""Guards for migrating legacy launchd interpreters to the B12 venv.

Older installs ran B12 Python jobs (memory-consolidate) with /usr/bin/python3,
which is 3.9 on macOS. The scripts require Python 3.11+, so the job failed on
every run while the installer only migrated paths. update_launchd_plists() now
also rewrites that interpreter, without reloading jobs the user has unloaded or
disabled and without touching /bin/bash jobs.

Jobs that start the venv Python directly are also moved to /bin/sh, which execs
Python: launchd records the program it starts, and a Homebrew Python upgrade
otherwise gets the first spawn after it killed with "Launch Constraint
Violation".

The function is extracted from install.sh and run under bash with stub
`uname`/`launchctl` commands, so the test never touches a real launchd domain.
"""
from __future__ import annotations

import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALL = REPO_ROOT / "install.sh"
CONSOLIDATE = REPO_ROOT / "hooks" / "memory-consolidate.py"
LABEL = "com.b12.memory-consolidate"
WRAP = ["/bin/sh", "-c", 'exec "$0" "$@"']


def _function_body() -> str:
    m = re.search(r"\nupdate_launchd_plists\(\)\s*\{.*?\n\}\n", INSTALL.read_text(), re.DOTALL)
    assert m, "update_launchd_plists() not found in install.sh"
    return m.group(0)


def _write_plist(path: Path, args: list[str], label: str | None = None) -> None:
    path.write_bytes(plistlib.dumps({"Label": label or path.stem, "ProgramArguments": args}))


def _setup(tmp_path: Path, *, loaded: list[str], venv: bool = True,
           consolidate_file: str = f"{LABEL}.plist", no_plutil: bool = False) -> dict[str, Path]:
    home = tmp_path / "home"
    agents = home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    venv_python = home / ".local" / "b12-venv" / "bin" / "python3"
    if venv:
        venv_python.parent.mkdir(parents=True)
        venv_python.write_text("#!/bin/sh\n")
        venv_python.chmod(0o755)

    stubs = tmp_path / "stubs"
    stubs.mkdir()
    calls = tmp_path / "launchctl.calls"
    listing = "".join(f"-\t0\t{label}\n" for label in loaded)
    exact = " ".join(loaded)
    (stubs / "uname").write_text("#!/bin/sh\necho Darwin\n")
    # `launchctl list` prints the listing; `launchctl list <label>` succeeds only
    # for an exactly matching loaded label, like the real command.
    (stubs / "launchctl").write_text(
        "#!/bin/sh\n"
        f'echo "$*" >> "{calls}"\n'
        'if [ "$1" = list ]; then\n'
        f'  if [ -z "$2" ]; then printf "%b" "{listing}"; exit 0; fi\n'
        f'  for l in {exact}; do [ "$l" = "$2" ] && exit 0; done\n'
        "  exit 113\n"
        "fi\n"
        "exit 0\n"
    )
    if no_plutil:
        # Simulate a host without plutil so the awk label fallback runs.
        (stubs / "plutil").write_text("#!/bin/sh\nexit 127\n")
    for stub in stubs.iterdir():
        stub.chmod(0o755)

    consolidate = agents / consolidate_file
    _write_plist(consolidate, ["/usr/bin/python3", f"{home}/.B12/hooks/memory-consolidate.py", "--auto"], LABEL)
    bash_job = agents / "com.b12.memory-backup.plist"
    _write_plist(bash_job, ["/bin/bash", f"{home}/.B12/hooks/memory-backup.sh"])
    return {
        "home": home, "stubs": stubs, "calls": calls, "venv": venv_python,
        "consolidate": consolidate, "bash_job": bash_job,
    }


def _run(env_paths: dict[str, Path]) -> None:
    script = (
        'info() { echo "$*"; }\n'
        f'VENV_PYTHON="{env_paths["venv"]}"\n'
        f"{_function_body()}\n"
        "update_launchd_plists\n"
    )
    env = {
        "HOME": str(env_paths["home"]),
        "PATH": f'{env_paths["stubs"]}{os.pathsep}/usr/bin{os.pathsep}/bin',
    }
    result = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def _program(path: Path) -> list[str]:
    return plistlib.loads(path.read_bytes())["ProgramArguments"]


def _wrapped(env_paths: dict[str, Path]) -> list[str]:
    # The legacy interpreter is first moved to the venv, then wrapped in /bin/sh.
    return WRAP + [str(env_paths["venv"]), f'{env_paths["home"]}/.B12/hooks/memory-consolidate.py', "--auto"]


def _reload_calls(env_paths: dict[str, Path]) -> list[str]:
    if not env_paths["calls"].exists():
        return []
    return [c for c in env_paths["calls"].read_text().splitlines() if c.split()[0] in ("load", "unload")]


def test_unloaded_job_gets_venv_interpreter_without_reload(tmp_path):
    paths = _setup(tmp_path, loaded=[])
    bash_before = paths["bash_job"].read_bytes()
    _run(paths)
    assert _program(paths["consolidate"]) == _wrapped(paths)
    assert _reload_calls(paths) == [], "an unloaded/disabled job must not be loaded by the migration"
    assert paths["bash_job"].read_bytes() == bash_before, "/bin/bash jobs must be left untouched"


def test_loaded_job_is_reloaded_after_rewrite(tmp_path):
    paths = _setup(tmp_path, loaded=[LABEL])
    _run(paths)
    assert _program(paths["consolidate"]) == _wrapped(paths)
    assert _reload_calls(paths) == [f'unload {paths["consolidate"]}', f'load {paths["consolidate"]}']


@pytest.mark.parametrize("no_plutil", [False, True], ids=["plutil", "awk-fallback"])
@pytest.mark.parametrize("loaded", [[], [LABEL]], ids=["unloaded", "loaded"])
def test_docs_installed_plist_name_is_migrated_by_its_label(tmp_path, loaded, no_plutil):
    # README/docs/setup.md copy the template as launchd-consolidate.plist; the
    # job label inside it is still com.b12.memory-consolidate.
    paths = _setup(tmp_path, loaded=loaded, consolidate_file="launchd-consolidate.plist", no_plutil=no_plutil)
    _run(paths)
    assert _program(paths["consolidate"]) == _wrapped(paths)
    expected = [f'unload {paths["consolidate"]}', f'load {paths["consolidate"]}'] if loaded else []
    assert _reload_calls(paths) == expected


@pytest.mark.parametrize("no_plutil", [False, True], ids=["plutil", "awk-fallback"])
def test_unrelated_launchd_named_plist_is_left_alone(tmp_path, no_plutil):
    # A third-party launchd-*.plist must not be rewritten or reloaded, even if
    # its contents look like something the migration would touch.
    paths = _setup(tmp_path, loaded=["org.example.tool"], no_plutil=no_plutil)
    foreign = paths["consolidate"].parent / "launchd-tool.plist"
    _write_plist(foreign, ["/usr/bin/python3", f'{paths["home"]}/.B12/hooks/tool.py',
                           f'{paths["home"]}/.claude/hooks/tool.sh'], "org.example.tool")
    before = foreign.read_bytes()
    _run(paths)
    assert foreign.read_bytes() == before
    assert not any("launchd-tool.plist" in c for c in _reload_calls(paths))


def test_look_alike_loaded_label_does_not_reload_unloaded_job(tmp_path):
    # Dots in the label must not act as regex wildcards against other jobs.
    paths = _setup(tmp_path, loaded=["comXb12Ymemory-consolidate"])
    _run(paths)
    assert _program(paths["consolidate"]) == _wrapped(paths)
    assert _reload_calls(paths) == []


def test_missing_venv_leaves_plist_unchanged(tmp_path):
    paths = _setup(tmp_path, loaded=[LABEL], venv=False)
    before = paths["consolidate"].read_bytes()
    _run(paths)
    assert paths["consolidate"].read_bytes() == before
    assert _reload_calls(paths) == []


def test_migration_is_idempotent(tmp_path):
    paths = _setup(tmp_path, loaded=[LABEL])
    _run(paths)
    after_first = paths["consolidate"].read_bytes()
    paths["calls"].unlink()
    _run(paths)
    assert paths["consolidate"].read_bytes() == after_first
    assert _reload_calls(paths) == []


@pytest.mark.parametrize("loaded", [[], ["com.b12.graph-enrich"]], ids=["unloaded", "loaded"])
def test_venv_python_job_is_started_through_sh(tmp_path, loaded):
    # A job that starts the venv Python directly is killed once after each
    # Homebrew Python upgrade ("Launch Constraint Violation"); /bin/sh avoids it.
    paths = _setup(tmp_path, loaded=loaded)
    job = paths["consolidate"].parent / "com.b12.graph-enrich.plist"
    args = [str(paths["venv"]), f'{paths["home"]}/.B12/hooks/scripts/graph_enrich.py', "--apply"]
    _write_plist(job, args)
    _run(paths)
    assert _program(job) == WRAP + args
    expected = [f"unload {job}", f"load {job}"] if loaded else []
    assert [c for c in _reload_calls(paths) if "graph-enrich" in c] == expected


def test_daemon_plist_is_left_to_its_renderer(tmp_path):
    # install_mcp_daemon renders the daemon plist from the template; the
    # migration must not rewrite or reload it a second time.
    paths = _setup(tmp_path, loaded=["com.b12.mcp.daemon"])
    daemon = paths["consolidate"].parent / "com.b12.mcp.daemon.plist"
    _write_plist(daemon, [str(paths["venv"]), f'{paths["home"]}/.B12/hooks/scripts/b12_mcp_daemon.py'])
    before = daemon.read_bytes()
    _run(paths)
    assert daemon.read_bytes() == before
    assert not any("mcp.daemon" in c for c in _reload_calls(paths))


def test_templates_start_venv_python_through_sh():
    wrapped = []
    for template in sorted((REPO_ROOT / "config").glob("*.plist")):
        args = plistlib.loads(template.read_bytes()).get("ProgramArguments", [])
        pythons = [a for a in args if a == "B12_HOME_PYTHON" or a.endswith("/b12-venv/bin/python3")]
        if not pythons:
            continue
        assert args[:3] == WRAP and args[3] == pythons[0], f"{template.name} starts Python without /bin/sh"
        wrapped.append(template.name)
    assert {"com.b12.mcp.daemon.plist", "com.b12.graph-enrich.plist", "launchd-consolidate.plist"} <= set(wrapped)


def test_sh_wrapper_execs_python_in_place_with_its_arguments():
    code = "import json, os, sys; print(json.dumps([os.getpid()] + sys.argv[1:]))"
    proc = subprocess.Popen(WRAP + [sys.executable, "-c", code, "two words", "--flag"],
                            stdout=subprocess.PIPE, text=True)
    out, _ = proc.communicate(timeout=30)
    assert proc.returncode == 0
    # Same PID: launchd's KeepAlive and signals reach Python, not a shell parent.
    assert json.loads(out) == [proc.pid, "two words", "--flag"]


def _isolated_env(tmp_path: Path) -> dict[str, str]:
    # A temporary HOME points the script's DB path at a missing file, so even a
    # regressed guard can never read or consolidate a real memory database.
    return {"HOME": str(tmp_path), "PATH": os.environ.get("PATH", "/usr/bin:/bin")}


def test_consolidate_script_refuses_old_python_cleanly(tmp_path):
    code = (
        "import runpy, sys\n"
        "sys.version_info = (3, 9, 6, 'final', 0)\n"
        f"runpy.run_path({str(CONSOLIDATE)!r}, run_name='__main__')\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30,
                            env=_isolated_env(tmp_path))
    assert result.returncode == 2
    assert "requires Python 3.11+" in result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.skipif(shutil.which("python3.9") is None and not Path("/usr/bin/python3").exists(),
                    reason="no legacy interpreter available")
def test_consolidate_script_refuses_real_legacy_interpreter(tmp_path):
    legacy = shutil.which("python3.9") or "/usr/bin/python3"
    version = subprocess.run([legacy, "-c", "import sys; print(sys.version_info[:2] < (3, 11))"],
                             capture_output=True, text=True).stdout.strip()
    if version != "True":
        pytest.skip("available python3 is already 3.11+")
    result = subprocess.run([legacy, str(CONSOLIDATE), "--stats"], capture_output=True, text=True, timeout=30,
                            env=_isolated_env(tmp_path))
    assert result.returncode == 2
    assert "Traceback" not in result.stderr
