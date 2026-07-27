#!/usr/bin/env python3
"""
B12 Health Check CLI — standalone diagnostic tool for verifying B12 installation.

Checks hook files, Python modules, SQLite schema, embed daemon, launchd plists,
Claude setup consistency, and MCP server configuration.

Usage:
    python3 scripts/b12_health.py
    python3 scripts/b12_health.py --json
    python3 scripts/b12_health.py --fix
"""

import argparse
import json
import os
import plistlib
import shutil
import socket
import sqlite3
import subprocess
import sys
try:
    import tomllib
except ImportError:  # pragma: no cover - Python < 3.11
    tomllib = None
from pathlib import Path

# ── Constants ────────────────────────────────────────────────────────

VERSION = "11.81.5"  # keep in sync with package.json / plugin.json / B12_VERSION (see docs/releasing.md)

_HOME = Path.home()
_B12_DIR = Path(os.environ.get("B12_DATA_DIR", _HOME / ".B12"))
_HOOK_DIR = Path(os.environ.get("B12_HOOK_DIR", str(_B12_DIR / "hooks")))
_SCRIPT_DIR = _HOOK_DIR / "scripts"
_VENV_PATH = _HOME / ".local" / "b12-venv"

REQUIRED_HOOKS = [
    "memory-session-start.sh",
    "memory-session-end.sh",
    "memory-precompact.sh",
]

REQUIRED_PYTHON_MODULES = [
    "shared_patterns.py",
    "embed_daemon.py",
    "write_time_merge.py",
]

REQUIRED_TABLES = ["memories", "memory_embeddings", "memory_fts"]

_UID = os.getuid() if hasattr(os, "getuid") else os.getpid()
SOCK_PATH = f"/tmp/b12-embed-{_UID}.sock"

CLAUDE_JSON_PATH = _HOME / ".claude.json"

# Platform-aware DB path
if sys.platform == "darwin":
    DB_PATH = _HOME / "Library" / "Application Support" / "mcp-memory" / "sqlite_vec.db"
elif sys.platform == "win32":
    DB_PATH = _HOME / "AppData" / "Local" / "mcp-memory" / "sqlite_vec.db"
else:
    DB_PATH = _HOME / ".local" / "share" / "mcp-memory" / "sqlite_vec.db"


# ── Result types ─────────────────────────────────────────────────────

class Status:
    OK = "OK"
    WARN = "WARN"
    FAIL = "FAIL"


class CheckResult:
    __slots__ = ("status", "label", "detail")

    def __init__(self, status: str, label: str, detail: str = ""):
        self.status = status
        self.label = label
        self.detail = detail

    def to_dict(self) -> dict:
        d = {"status": self.status, "label": self.label}
        if self.detail:
            d["detail"] = self.detail
        return d


# ── Color output helpers ─────────────────────────────────────────────

_COLORS = {
    Status.OK: "\033[0;32m",    # green
    Status.WARN: "\033[1;33m",  # yellow
    Status.FAIL: "\033[0;31m",  # red
    "reset": "\033[0m",
    "dim": "\033[2m",
    "bold": "\033[1m",
}


def _color(text: str, code: str) -> str:
    return f"{code}{text}{_COLORS['reset']}"


def _format_result(r: CheckResult, use_color: bool = True) -> str:
    tag = f"[{r.status}]"
    if use_color:
        tag = _color(tag.ljust(6), _COLORS.get(r.status, ""))
    else:
        tag = tag.ljust(6)
    line = f"{tag} {r.label}"
    if r.detail:
        detail_text = f"  {r.detail}" if use_color else f"  {r.detail}"
        if use_color:
            line += "\n" + _color(f"       {r.detail}", _COLORS["dim"])
        else:
            line += f" ({r.detail})"
    return line


# ── Individual checks ────────────────────────────────────────────────

def check_hook_directory(fix: bool = False) -> CheckResult:
    """Check that ~/.B12/hooks/ exists."""
    if _HOOK_DIR.is_dir():
        return CheckResult(Status.OK, "Hook directory exists")
    if fix:
        _HOOK_DIR.mkdir(parents=True, exist_ok=True)
        return CheckResult(Status.OK, "Hook directory created", str(_HOOK_DIR))
    return CheckResult(Status.FAIL, "Hook directory missing", str(_HOOK_DIR))


def check_hook_files() -> CheckResult:
    """Check required hook shell scripts are present."""
    found = []
    missing = []
    for name in REQUIRED_HOOKS:
        path = _HOOK_DIR / name
        if path.is_file():
            found.append(name)
        else:
            missing.append(name)
    total = len(REQUIRED_HOOKS)
    if not missing:
        return CheckResult(Status.OK, f"Hook files present ({total}/{total})")
    if found:
        return CheckResult(
            Status.WARN,
            f"Hook files partial ({len(found)}/{total})",
            f"Missing: {', '.join(missing)}",
        )
    return CheckResult(
        Status.FAIL,
        f"Hook files missing (0/{total})",
        f"Missing: {', '.join(missing)}",
    )


def check_python_modules() -> CheckResult:
    """Check required Python support modules are present."""
    found = []
    missing = []
    for name in REQUIRED_PYTHON_MODULES:
        path = _SCRIPT_DIR / name
        if path.is_file():
            found.append(name)
        else:
            missing.append(name)
    total = len(REQUIRED_PYTHON_MODULES)
    if not missing:
        return CheckResult(Status.OK, f"Python modules present ({total}/{total})")
    if found:
        return CheckResult(
            Status.WARN,
            f"Python modules partial ({len(found)}/{total})",
            f"Missing: {', '.join(missing)}",
        )
    return CheckResult(
        Status.FAIL,
        f"Python modules missing (0/{total})",
        f"Missing: {', '.join(missing)}",
    )


def check_sqlite_database() -> CheckResult:
    """Check SQLite database is accessible and has required tables."""
    if not DB_PATH.is_file():
        return CheckResult(Status.FAIL, "SQLite database not found", str(DB_PATH))
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=5)
        conn.execute("PRAGMA busy_timeout=5000")
        # Get all table names (including virtual tables)
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'virtual table')"
        ).fetchall()
        tables = {r[0] for r in rows}
        conn.close()
    except sqlite3.Error as e:
        return CheckResult(Status.FAIL, "SQLite database error", str(e))

    found = []
    missing = []
    for t in REQUIRED_TABLES:
        if t in tables:
            found.append(t)
        else:
            missing.append(t)

    if not missing:
        return CheckResult(Status.OK, "SQLite database accessible", f"Tables: {', '.join(found)}")
    if found:
        return CheckResult(
            Status.WARN,
            f"SQLite schema incomplete ({len(found)}/{len(REQUIRED_TABLES)} tables)",
            f"Missing: {', '.join(missing)}",
        )
    return CheckResult(
        Status.FAIL,
        "SQLite schema missing all required tables",
        f"Missing: {', '.join(missing)}",
    )


def check_embed_daemon() -> CheckResult:
    """Check if the embed daemon Unix socket is alive and responsive."""
    if not os.path.exists(SOCK_PATH):
        return CheckResult(Status.WARN, "Embed daemon not running", f"Socket not found: {SOCK_PATH}")
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(3)
        sock.connect(SOCK_PATH)
        request = json.dumps({"op": "health"}) + "\n"
        sock.sendall(request.encode())
        response = b""
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            response += chunk
            if b"\n" in response:
                break
        sock.close()
        data = json.loads(response.decode().strip())
        if data.get("ok"):
            uptime = data.get("uptime", 0)
            served = data.get("requests_served", 0)
            return CheckResult(
                Status.OK,
                "Embed daemon running",
                f"Uptime: {int(uptime)}s, requests: {served}",
            )
        return CheckResult(Status.WARN, "Embed daemon responded with error", str(data))
    except (socket.error, json.JSONDecodeError, OSError) as e:
        return CheckResult(
            Status.WARN,
            "Embed daemon socket exists but not responsive",
            str(e),
        )


def check_launchd_plists() -> CheckResult:
    """Check launchd plist files point to correct B12 paths (macOS only)."""
    if sys.platform != "darwin":
        return CheckResult(Status.OK, "Launchd check skipped (not macOS)")

    launch_agents = _HOME / "Library" / "LaunchAgents"
    if not launch_agents.is_dir():
        return CheckResult(Status.WARN, "LaunchAgents directory not found")

    plist_files = sorted(launch_agents.glob("com.b12.*.plist"))
    if not plist_files:
        return CheckResult(Status.WARN, "No B12 launchd plists found")

    issues = []
    valid_count = 0
    for pf in plist_files:
        try:
            with open(pf, "rb") as f:
                plist = plistlib.load(f)
            # Check ProgramArguments for correct paths
            prog_args = plist.get("ProgramArguments", [])
            prog_str = " ".join(str(a) for a in prog_args)
            # Plists should reference ~/.B12/ paths, not old ~/.claude/ paths
            if "/.claude/" in prog_str:
                issues.append(f"{pf.name}: references old ~/.claude/ path")
            else:
                valid_count += 1
        except Exception as e:
            issues.append(f"{pf.name}: parse error ({e})")

    total = len(plist_files)
    if not issues:
        return CheckResult(Status.OK, f"Launchd plists configured ({total} plists)")
    return CheckResult(
        Status.WARN,
        f"Launchd plist issues ({valid_count}/{total} OK)",
        "; ".join(issues),
    )


def check_claude_setups() -> CheckResult:
    """Check all ~/.claude* setup directories have consistent hook paths."""
    setup_dirs = []
    for entry in _HOME.iterdir():
        if entry.is_dir() and entry.name.startswith(".claude"):
            settings = entry / "settings.json"
            if settings.is_file():
                setup_dirs.append(entry)

    if not setup_dirs:
        return CheckResult(Status.WARN, "No Claude setup directories found")

    consistent = []
    inconsistent = []
    expected_hook_prefix = str(_HOOK_DIR)
    # Also accept ~ notation
    expected_hook_prefix_tilde = "~/.B12/hooks/"

    for sd in sorted(setup_dirs):
        settings_path = sd / "settings.json"
        try:
            with open(settings_path) as f:
                settings = json.load(f)
        except (json.JSONDecodeError, OSError):
            inconsistent.append(f"{sd.name}: cannot read settings.json")
            continue

        hooks_config = settings.get("hooks", {})
        if not hooks_config:
            inconsistent.append(f"{sd.name}: no hooks configured")
            continue

        # Collect all command paths from hooks config
        commands = []
        for event_name, event_list in hooks_config.items():
            if not isinstance(event_list, list):
                continue
            for matcher_block in event_list:
                inner_hooks = matcher_block.get("hooks", [])
                for h in inner_hooks:
                    cmd = h.get("command", "")
                    if cmd:
                        commands.append(cmd)

        if not commands:
            inconsistent.append(f"{sd.name}: no hook commands found")
            continue

        # Check all commands reference ~/.B12/hooks/
        bad_paths = []
        for cmd in commands:
            expanded = cmd.replace("~", str(_HOME))
            if not expanded.startswith(str(_HOOK_DIR)):
                bad_paths.append(cmd)

        if bad_paths:
            inconsistent.append(
                f"{sd.name}: {len(bad_paths)} hooks reference wrong path"
            )
        else:
            consistent.append(sd.name)

    total = len(setup_dirs)
    if not inconsistent:
        return CheckResult(
            Status.OK,
            f"Claude setups consistent ({total}/{total})",
        )
    detail = "; ".join(inconsistent)
    if consistent:
        return CheckResult(
            Status.WARN,
            f"Claude setups partially consistent ({len(consistent)}/{total})",
            detail,
        )
    return CheckResult(Status.FAIL, f"Claude setups inconsistent (0/{total})", detail)


def check_mcp_config() -> CheckResult:
    """Check MCP server config in ~/.claude.json has correct B12 path."""
    if not CLAUDE_JSON_PATH.is_file():
        return CheckResult(Status.WARN, "~/.claude.json not found")

    try:
        with open(CLAUDE_JSON_PATH) as f:
            config = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        return CheckResult(Status.FAIL, "Cannot parse ~/.claude.json", str(e))

    mcp_servers = config.get("mcpServers", {})
    if "B12" not in mcp_servers:
        return CheckResult(Status.FAIL, "MCP server 'B12' not configured in ~/.claude.json")

    b12_config = mcp_servers["B12"]
    command = b12_config.get("command", "")
    args = b12_config.get("args", [])

    issues = []

    # Check command points to venv python
    expected_venv_python = str(_VENV_PATH / "bin" / "python3")
    if sys.platform == "win32":
        expected_venv_python = str(_VENV_PATH / "Scripts" / "python.exe")

    if command != expected_venv_python:
        # Not necessarily wrong — just warn if venv python doesn't exist
        if not Path(command).is_file():
            issues.append(f"Python not found: {command}")

    # Check args point to b12_mcp_server.py
    if args:
        server_path = args[0]
        if not Path(server_path).is_file():
            issues.append(f"MCP server script not found: {server_path}")
        expected_server = str(_SCRIPT_DIR / "b12_mcp_server.py")
        if server_path != expected_server:
            # Warn but don't fail — path might be valid but different
            if not Path(server_path).is_file():
                issues.append(f"Expected server at: {expected_server}")
    else:
        issues.append("No args specified (missing server script path)")

    if not issues:
        return CheckResult(Status.OK, "MCP server configured")
    # FAIL if the python binary or server script doesn't exist; WARN otherwise
    severity = Status.FAIL if any("not found" in i for i in issues) else Status.WARN
    return CheckResult(severity, "MCP server config issues", "; ".join(issues))


# ── Host-side MCP plugin-load probe ──────────────────────────────────


# Per-host probe table:
#   name           — display name shown in the probe table
#   config_path    — absolute path to the host's MCP config file
#   detect_b12     — callable(config_dict, raw_text) → bool ("is B12 registered")
#   plugin_paths   — list of extra files (JS, Py) to verify exist+load on disk
_PluginCheck = tuple[str, callable, bool]


def _detect_b12_json(cfg: dict, _raw: str) -> bool:
    """Generic JSON config: look for B12 under common mcpServers shapes."""
    if not isinstance(cfg, dict):
        return False
    for key in ("mcpServers", "mcp_servers", "mcp"):
        v = cfg.get(key)
        if isinstance(v, dict) and "B12" in v:
            return True
    return False


def _detect_b12_opencode(cfg: dict, _raw: str) -> bool:
    if not isinstance(cfg, dict):
        return False
    # OpenCode uses a top-level "mcp" map keyed by server name.
    mcp = cfg.get("mcp")
    if isinstance(mcp, dict) and "B12" in mcp:
        return True
    return False


def _detect_b12_toml(_cfg: dict, raw: str) -> bool:
    # Codex / Grok use TOML; no stdlib parser in 3.10 fallback path, so
    # use the same string match install.sh uses.
    return "[mcp_servers.B12]" in raw or 'mcp_servers."B12"' in raw


def _b12_json_entry(cfg: dict) -> dict | None:
    if not isinstance(cfg, dict):
        return None
    for key in ("mcpServers", "mcp_servers", "mcp"):
        section = cfg.get(key)
        if isinstance(section, dict) and isinstance(section.get("B12"), dict):
            return section["B12"]
    return None


def _b12_toml_entry(raw: str) -> dict | None:
    if tomllib is not None:
        try:
            cfg = tomllib.loads(raw)
            section = cfg.get("mcp_servers") or cfg.get("mcpServers") or {}
            entry = section.get("B12") if isinstance(section, dict) else None
            if isinstance(entry, dict):
                return entry
        except Exception:
            pass
    if not _detect_b12_toml({}, raw):
        return None
    entry: dict = {}
    in_b12 = False
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_b12 = stripped in ("[mcp_servers.B12]", '[mcp_servers."B12"]')
            continue
        if not in_b12 or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        value = value.strip()
        if key == "command":
            entry["command"] = value.strip('"')
        elif key == "args":
            try:
                entry["args"] = json.loads(value)
            except json.JSONDecodeError:
                entry["args"] = []
    return entry


def _probe_registered_server_config(name: str, cfg: dict, raw: str) -> tuple[bool, str]:
    entry = _b12_toml_entry(raw) if name in ("codex", "grok") else _b12_json_entry(cfg)
    if not entry:
        return False, "B12 config entry missing"
    command = entry.get("command")
    args = entry.get("args", [])
    if isinstance(args, str):
        args = [args]
    if not isinstance(args, list):
        args = []

    if isinstance(command, str) and command:
        command_path = Path(os.path.expanduser(command))
        if (os.sep in command or command.startswith(".")) and not command_path.is_file():
            return False, f"configured command missing: {command}"
        if os.sep not in command and shutil.which(command) is None:
            return False, f"configured command not found on PATH: {command}"
    else:
        return False, "configured command missing"

    script_arg = next((str(arg) for arg in args if str(arg).endswith("b12_mcp_server.py")), "")
    if not script_arg:
        return False, "configured server script arg missing"
    script_path = Path(os.path.expanduser(script_arg))
    if not script_path.is_file():
        return False, f"configured server missing: {script_path}"
    return True, f"server: {script_path}"


def _load_json_safe(path: Path) -> tuple[dict | None, str, str | None]:
    """Read a JSON file. Returns (parsed | None, raw_text, error | None)."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as e:
        return None, "", f"read failed: {e}"
    try:
        return json.loads(raw), raw, None
    except json.JSONDecodeError as e:
        return None, raw, f"json parse error: {e}"


def _opencode_plugin_loadable(opencode_dir: Path) -> tuple[bool, str]:
    """For OpenCode, the B12 plugin lives at plugins/b12/index.js (bundled).

    Returns (loadable, detail). "Loadable" here means the bundled JS file
    exists and is non-empty. We can't actually `import` it without bun;
    file presence + size is the strongest signal a non-bun probe gets.
    """
    plugin_dir = opencode_dir / "plugins" / "b12"
    js_path = plugin_dir / "index.js"
    if not plugin_dir.is_dir():
        return False, f"plugin dir missing: {plugin_dir}"
    if not js_path.is_file():
        return False, f"plugin js missing: {js_path}"
    try:
        size = js_path.stat().st_size
    except OSError as e:
        return False, f"stat failed: {e}"
    if size == 0:
        return False, "plugin js is empty (build may have failed)"
    return True, f"index.js {size} bytes"


def _antigravity_plugin_loadable() -> tuple[bool, str]:
    """Verify the staged Antigravity plugin is installed, not just its MCP entry."""
    stage_dir = _HOME / ".B12" / "antigravity-plugin" / "b12"
    required = ("plugin.json", "mcp_config.json", "hooks.json")
    missing = [name for name in required if not (stage_dir / name).is_file()]
    if missing:
        return False, f"plugin structure incomplete: missing {', '.join(missing)}"

    agy = shutil.which("agy")
    if agy is None:
        return False, "agy command not found on PATH"
    try:
        result = subprocess.run(
            [agy, "plugin", "list"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"agy plugin list failed: {exc}"
    if result.returncode != 0:
        return False, f"agy plugin list exited {result.returncode}"
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        return False, f"agy plugin list returned invalid JSON: {exc}"
    imports = payload.get("imports", []) if isinstance(payload, dict) else []
    if not any(isinstance(item, dict) and item.get("name") == "b12" for item in imports):
        return False, "B12 Antigravity plugin is not listed as installed"
    return True, f"plugin installed: {stage_dir}"


def check_mcp_hosts() -> CheckResult:
    """Probe each known MCP host for B12 registration + plugin loadability.

    Builds a per-host table:
        host | registered | plugin loadable | last load error

    Returned as a single CheckResult whose `detail` carries the table.
    Status:
      OK   — every host directory present has B12 registered.
      WARN — at least one detected host directory is missing B12.
      FAIL — a B12-registered host references a plugin/server that
             doesn't exist on disk.
    """
    server_path = _SCRIPT_DIR / "b12_mcp_server.py"
    server_exists = server_path.is_file()

    hosts = [
        ("claude",       _HOME / ".claude.json",                             _detect_b12_json),
        ("codex",        _HOME / ".codex" / "config.toml",                   _detect_b12_toml),
        ("antigravity",  _HOME / ".gemini" / "config" / "mcp_config.json",   _detect_b12_json),
        ("gemini",       _HOME / ".gemini" / "settings.json",                _detect_b12_json),
        ("kimi",         _HOME / ".kimi" / "mcp.json",                       _detect_b12_json),
        ("cursor",       _HOME / ".cursor" / "mcp.json",                     _detect_b12_json),
        ("windsurf",     _HOME / ".codeium" / "windsurf" / "mcp_config.json", _detect_b12_json),
        ("opencode",     _HOME / ".config" / "opencode" / "opencode.json", _detect_b12_opencode),
        ("grok",         _HOME / ".grok" / "config.toml",                    _detect_b12_toml),
    ]

    rows: list[tuple[str, str, str, str]] = []
    any_warn = False
    any_fail = False

    for name, cfg_path, detect in hosts:
        # Detect "host present at all". For most hosts a parent-dir
        # check is correct, but ~/.claude.json's parent is $HOME which
        # always exists — that would surface Claude as "installed,
        # config missing" on every machine. Treat Claude as present
        # only when the config file itself exists.
        if name == "claude":
            if not cfg_path.is_file():
                continue
        elif name == "gemini":
            # ~/.gemini is shared with Antigravity. Do not infer that the
            # legacy Gemini CLI is installed from Antigravity's directory.
            if not cfg_path.is_file() and shutil.which("gemini") is None:
                continue
        elif name == "antigravity":
            if not cfg_path.is_file() and shutil.which("agy") is None:
                continue
        else:
            parent = cfg_path.parent
            if not parent.is_dir():
                continue  # Not installed; nothing to probe.

        registered = "no"
        plugin_loadable = "n/a"
        last_error = ""

        if not cfg_path.is_file():
            rows.append((name, "no", "n/a", "config file missing"))
            any_warn = True
            continue

        try:
            if name in ("codex", "grok"):
                raw = cfg_path.read_text(encoding="utf-8")
                cfg = {}
                is_registered = detect(cfg, raw)
            else:
                cfg, raw, parse_err = _load_json_safe(cfg_path)
                if cfg is None:
                    rows.append((name, "?", "n/a", parse_err or "parse failed"))
                    any_warn = True
                    continue
                is_registered = detect(cfg, raw)
        except OSError as e:
            rows.append((name, "?", "n/a", f"read failed: {e}"))
            any_warn = True
            continue

        registered = "yes" if is_registered else "no"
        if not is_registered:
            any_warn = True

        # Plugin/server loadability check
        if name == "antigravity":
            server_ok, server_detail = _probe_registered_server_config(name, cfg, raw)
            plugin_ok, plugin_detail = _antigravity_plugin_loadable()
            loadable = server_ok and plugin_ok
            plugin_loadable = "yes" if loadable else "no"
            if not server_ok:
                last_error = server_detail
            elif not plugin_ok:
                last_error = plugin_detail
            if registered == "yes" and not loadable:
                any_fail = True
        elif name == "opencode":
            loadable, detail = _opencode_plugin_loadable(cfg_path.parent)
            plugin_loadable = "yes" if loadable else "no"
            if registered == "yes" and not loadable:
                any_fail = True
                last_error = detail
            elif not loadable:
                last_error = detail
        else:
            loadable, detail = _probe_registered_server_config(name, cfg, raw)
            if loadable:
                plugin_loadable = "yes"
            else:
                plugin_loadable = "no"
                if registered == "yes":
                    any_fail = True
                    last_error = detail
                elif not server_exists:
                    last_error = f"server missing: {server_path}"

        rows.append((name, registered, plugin_loadable, last_error))

    if not rows:
        return CheckResult(Status.WARN, "MCP hosts: no host directories detected")

    # Build table for detail.
    widths = (
        max(len("host"), max(len(r[0]) for r in rows)),
        max(len("registered"), max(len(r[1]) for r in rows)),
        max(len("plugin loadable"), max(len(r[2]) for r in rows)),
        max(len("last load error"), max(len(r[3]) for r in rows)),
    )
    sep = "  "
    header = sep.join((
        "host".ljust(widths[0]),
        "registered".ljust(widths[1]),
        "plugin loadable".ljust(widths[2]),
        "last load error".ljust(widths[3]),
    ))
    lines = [header, sep.join("-" * w for w in widths)]
    for r in rows:
        lines.append(sep.join((
            r[0].ljust(widths[0]),
            r[1].ljust(widths[1]),
            r[2].ljust(widths[2]),
            r[3].ljust(widths[3]),
        )))

    detail = "\n" + "\n".join(lines)

    if any_fail:
        return CheckResult(Status.FAIL, "MCP hosts: at least one registered host cannot load plugin", detail)
    if any_warn:
        return CheckResult(Status.WARN, "MCP hosts: at least one detected host missing B12", detail)
    return CheckResult(Status.OK, f"MCP hosts: {len(rows)}/{len(rows)} healthy", detail)


# ── Auto-fix helpers ─────────────────────────────────────────────────

def _apply_fixes() -> list[str]:
    """Apply safe auto-fixes. Returns list of actions taken."""
    actions = []

    # Create missing directories
    for d in [_B12_DIR, _HOOK_DIR, _SCRIPT_DIR]:
        if not d.is_dir():
            d.mkdir(parents=True, exist_ok=True)
            actions.append(f"Created directory: {d}")

    # Create memory-logs directory
    log_dir = _B12_DIR / "memory-logs"
    if not log_dir.is_dir():
        log_dir.mkdir(parents=True, exist_ok=True)
        actions.append(f"Created directory: {log_dir}")

    return actions


# ── Main runner ──────────────────────────────────────────────────────

def run_health_check(fix: bool = False) -> list[CheckResult]:
    """Run all health checks and return results."""
    fix_actions = []
    if fix:
        fix_actions = _apply_fixes()

    results = [
        check_hook_directory(fix=fix),
        check_hook_files(),
        check_python_modules(),
        check_sqlite_database(),
        check_embed_daemon(),
        check_launchd_plists(),
        check_claude_setups(),
        check_mcp_config(),
        check_mcp_hosts(),
    ]

    return results, fix_actions


def main():
    parser = argparse.ArgumentParser(
        description="B12 Health Check — verify B12 installation integrity",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output results as JSON",
    )
    parser.add_argument(
        "--fix",
        action="store_true",
        help="Attempt auto-fixes for common issues (e.g., create missing dirs)",
    )
    args = parser.parse_args()

    results, fix_actions = run_health_check(fix=args.fix)

    # Count statuses
    counts = {Status.OK: 0, Status.WARN: 0, Status.FAIL: 0}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1

    if args.json_output:
        output = {
            "version": VERSION,
            "checks": [r.to_dict() for r in results],
            "summary": counts,
        }
        if fix_actions:
            output["fixes_applied"] = fix_actions
        print(json.dumps(output, indent=2))
    else:
        # Header
        use_color = sys.stdout.isatty()
        header = f"B12 Health Check v{VERSION}"
        if use_color:
            header = _color(header, _COLORS["bold"])
        print(header)
        print("\u2500" * 35)

        # Results
        for r in results:
            print(_format_result(r, use_color=use_color))

        # Fix actions
        if fix_actions:
            print()
            fix_header = "Fixes applied:"
            if use_color:
                fix_header = _color(fix_header, _COLORS["bold"])
            print(fix_header)
            for a in fix_actions:
                prefix = _color("  +", _COLORS[Status.OK]) if use_color else "  +"
                print(f"{prefix} {a}")

        # Summary line
        print("\u2500" * 35)
        parts = []
        if counts[Status.OK]:
            s = f"{counts[Status.OK]} OK"
            parts.append(_color(s, _COLORS[Status.OK]) if use_color else s)
        if counts[Status.WARN]:
            s = f"{counts[Status.WARN]} WARN"
            parts.append(_color(s, _COLORS[Status.WARN]) if use_color else s)
        if counts[Status.FAIL]:
            s = f"{counts[Status.FAIL]} FAIL"
            parts.append(_color(s, _COLORS[Status.FAIL]) if use_color else s)
        print(f"Result: {', '.join(parts)}")

    # Exit code
    if counts[Status.FAIL] > 0:
        sys.exit(2)
    elif counts[Status.WARN] > 0:
        sys.exit(1)
    else:
        sys.exit(0)


if __name__ == "__main__":
    main()
