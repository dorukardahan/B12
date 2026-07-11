#!/usr/bin/env python3
"""B12 MCP config template consistency validator (issue #158).

B12 ships one MCP server config template per supported tool in ``config/``.
The templates target wildly different host schemas (``mcpServers.B12``,
``servers.B12``, ``context_servers.B12``, ``amp.mcpServers.B12``,
``mcp.B12``, ``mcp_servers.B12``, a bare root ``B12``, a YAML list, ...),
so it is easy for one of them to silently drift out of consistency with
the rest — a missing env var, a stale model id, a dropped script arg —
and the only signal is degraded recall / truncated responses on that one
host.

This module is the single source of truth for the cross-tool consistency
contract every MCP-bearing template must satisfy:

  1. A ``B12`` server entry exists under the tool's native top-level key.
  2. ``env.MCP_EMBEDDING_MODEL == "BAAI/bge-m3"`` (the 1024-dim canonical
     model; any other value silently breaks vector recall).
  3. ``env.MCP_MAX_RESPONSE_CHARS == "40000"`` (tool response budget; a
     missing/stale value changes truncation behaviour per host).
  4. ``command`` is present and non-empty.
  5. The server script is referenced (either the ``__SCRIPT_PATH__``
     installer placeholder or a literal ``b12_mcp_server.py`` path).

Design
------
- Stdlib only (``json``, ``tomllib``, ``re``, ``pathlib``). PyYAML is NOT
  a declared project dependency, so the lone YAML template is parsed with
  a tiny purpose-built extractor for its fixed owned schema (with a
  best-effort ``yaml`` path when the module happens to be importable).
  Both paths validate the same fields, so behaviour is identical whether
  or not PyYAML is installed.
- Importable: ``validate_repo(root)`` is used by the pytest guard in
  ``scripts/tests/test_mcp_template_consistency.py`` as well as the CLI.
- Exit code: 0 = all templates consistent, 1 = drift detected.

Usage
-----
    python3 scripts/validate_mcp_templates.py            # report + exit 1 on drift
    python3 scripts/validate_mcp_templates.py --quiet    # only print problems
    python3 scripts/validate_mcp_templates.py --check    # alias; CI-friendly
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

try:  # Py3.11+ stdlib
    import tomllib  # type: ignore[import-not-found]
except ModuleNotFoundError:  # pragma: no cover - CI runs 3.11+
    tomllib = None  # type: ignore[assignment]

# ── Canonical consistency contract ──────────────────────────────────────
CANONICAL_MODEL = "BAAI/bge-m3"
CANONICAL_MAX_RESPONSE_CHARS = "40000"
SERVER_NAME = "B12"
SCRIPT_MARKER = "b12_mcp_server.py"
SCRIPT_PLACEHOLDER = "__SCRIPT_PATH__"
WRAPPER_MARKER = "start-mcp.sh"

# ── Template registry ───────────────────────────────────────────────────
# Each entry describes how to reach the B12 server node inside that file:
#   file     — repo-relative path under config/
#   fmt      — json | toml | yaml
#   keys     — list of literal dict keys to the B12 node (None for the
#              YAML list-under-mcpServers shape used by Continue.dev)
#   env      — name of the env dict key ("env" everywhere except OpenCode,
#              which uses the "environment" key)
#   script   — (field, index) tuple locating the script reference inside
#              the node. Most tools put it in args[0]; OpenCode bundles
#              command+script into a single command list, so it lives in
#              command[1].
TEMPLATES: list[dict[str, Any]] = [
    {"file": "mcp-b12-template.json", "fmt": "json",
     "keys": ["B12"], "env": "env", "script": ("args", 0)},
    {"file": "cursor-mcp-template.json", "fmt": "json",
     "keys": ["mcpServers", "B12"], "env": "env", "script": ("args", 0)},
    {"file": "cline-mcp-template.json", "fmt": "json",
     "keys": ["mcpServers", "B12"], "env": "env", "script": ("args", 0)},
    {"file": "kimi-mcp-template.json", "fmt": "json",
     "keys": ["mcpServers", "B12"], "env": "env", "script": ("args", 0)},
    {"file": "vscode-mcp-template.json", "fmt": "json",
     "keys": ["servers", "B12"], "env": "env", "script": ("args", 0)},
    {"file": "windsurf-mcp-template.json", "fmt": "json",
     "keys": ["mcpServers", "B12"], "env": "env", "script": ("args", 0)},
    {"file": "gemini-config-template.json", "fmt": "json",
     "keys": ["mcpServers", "B12"], "env": "env", "script": ("args", 0)},
    {"file": "jetbrains-ai-mcp-template.json", "fmt": "json",
     "keys": ["mcpServers", "B12"], "env": "env", "script": ("args", 0)},
    {"file": "amp-settings-template.json", "fmt": "json",
     "keys": ["amp.mcpServers", "B12"], "env": "env", "script": ("args", 0)},
    {"file": "zed-settings-template.json", "fmt": "json",
     "keys": ["context_servers", "B12"], "env": "env", "script": ("args", 0)},
    {"file": "opencode-config-template.json", "fmt": "json",
     "keys": ["mcp", "B12"], "env": "environment", "script": ("command", 1)},
    {"file": "continue-mcp-template.yaml", "fmt": "yaml",
     "keys": None, "env": "env", "script": ("args", 0)},
    {"file": "codex-config-template.toml", "fmt": "toml",
     "keys": ["mcp_servers", "B12"], "env": "env", "script": ("args", 0)},
    {"file": "grok-config-template.toml", "fmt": "toml",
     "keys": ["mcp_servers", "B12"], "env": "env", "script": ("args", 0)},
    {"path": ".mcp.json", "fmt": "json",
     "keys": ["mcpServers", "B12"], "env": "env", "script": ("args", 0)},
    {"path": "plugins/antigravity/b12/mcp_config.json", "fmt": "json",
     "keys": ["mcpServers", "B12"], "env": "env", "script": ("args", 0)},
    {"path": ".grok/plugins-available/b12/.mcp.json", "fmt": "json",
     "keys": ["mcpServers", "B12"], "env": "env", "script": ("args", 0)},
]


# ── Normalisation helpers ───────────────────────────────────────────────
def _norm_scalar(value: Any) -> str:
    """Coerce a parsed env value (str/int) to a clean comparable string."""
    if value is None:
        return ""
    return str(value).strip().strip('"').strip("'")


def _script_ref_ok(ref: Any) -> bool:
    """True if the launch target reaches the exact B12 MCP server entry point."""
    target = _norm_scalar(ref)
    if not target:
        return False
    if target == SCRIPT_PLACEHOLDER:
        return True
    basename = target.replace("\\", "/").rsplit("/", 1)[-1]
    return basename in {SCRIPT_MARKER, WRAPPER_MARKER}


def _launch_command_ok(command: Any, ref: Any) -> bool:
    """Validate the command/entry-point pair, allowing host path variants.

    Templates cannot share a byte-identical command: most invoke a virtualenv
    Python placeholder, Grok uses ``python3``, and the root development config
    intentionally uses the shell wrapper. They must still use one of the two
    canonical launch shapes: Python -> ``b12_mcp_server.py`` or shell ->
    ``start-mcp.sh``.
    """
    parts = command if isinstance(command, list) else [command]
    if not parts:
        return False
    executable = _norm_scalar(parts[0])
    target = _norm_scalar(ref)
    if not executable or not target:
        return False
    basename = executable.replace("\\", "/").rsplit("/", 1)[-1].lower()
    target_basename = target.replace("\\", "/").rsplit("/", 1)[-1]
    if target == SCRIPT_PLACEHOLDER or target_basename == SCRIPT_MARKER:
        return executable == "__VENV_PYTHON__" or basename in {
            "python",
            "python3",
            "python.exe",
            "python3.exe",
        }
    if target_basename == WRAPPER_MARKER:
        return basename in {"bash", "sh"}
    return False


# ── Per-format node extraction ──────────────────────────────────────────
def _load_json(path: Path) -> dict:
    return json.loads(path.read_text())


def _load_toml(path: Path) -> dict:
    if tomllib is None:
        raise RuntimeError("tomllib unavailable (need Python 3.11+)")
    return tomllib.loads(path.read_text())


def _load_yaml_node_fallback(path: Path) -> dict:
    """Parse the owned Continue YAML shape without borrowing sibling fields.

    This intentionally supports only the repository's small schema. It requires
    a top-level ``mcpServers`` list and returns fields from the list item whose
    ``name`` is exactly ``B12``. An absent/renamed item therefore cannot pass by
    contributing command/env values from another server.
    """
    in_servers = False
    node: dict[str, Any] | None = None
    section: str | None = None

    for raw_line in path.read_text().splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        stripped = raw_line.strip()

        if not in_servers:
            if indent == 0 and stripped == "mcpServers:":
                in_servers = True
            continue
        if indent == 0:
            break

        entry = re.match(r"^-\s+name\s*:\s*(.+)$", stripped)
        if indent == 2 and entry:
            if node is not None:
                return node
            name = _norm_scalar(entry.group(1))
            node = {"name": name} if name == SERVER_NAME else None
            section = None
            continue
        if node is None:
            continue

        if indent == 4:
            field = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)$", stripped)
            if not field:
                section = None
                continue
            key, raw_value = field.groups()
            if key in {"args", "env"} and not raw_value:
                section = key
                node.setdefault(key, [] if key == "args" else {})
            else:
                section = None
                node[key] = _norm_scalar(raw_value)
            continue

        if indent >= 6 and section == "args" and stripped.startswith("- "):
            node.setdefault("args", []).append(_norm_scalar(stripped[2:]))
            continue
        if indent >= 6 and section == "env":
            env_item = re.match(
                r"^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.+)$", stripped
            )
            if env_item:
                node.setdefault("env", {})[env_item.group(1)] = _norm_scalar(
                    env_item.group(2)
                )

    return node or {}


def _load_yaml_node(path: Path, env_field: str, script: tuple[str, int]) -> dict:
    """Extract the B12 node from continue-mcp-template.yaml.

    The schema is fixed and owned by this repo (mcpServers is a list of
    server dicts). PyYAML is not a declared dependency, so we prefer it
    when importable and otherwise fall back to a deterministic line
    parser that understands exactly this shape. Both paths populate the
    same normalised node dict.
    """
    raw = path.read_text()
    try:
        import yaml  # type: ignore[import-untyped]
        doc = yaml.safe_load(raw)
        servers = (doc or {}).get("mcpServers") or []
        for entry in servers:
            if isinstance(entry, dict) and entry.get("name") == SERVER_NAME:
                return entry
        return {}
    except ModuleNotFoundError:
        pass

    return _load_yaml_node_fallback(path)


def _resolve_node(doc: dict, spec: dict, path: Path) -> dict | None:
    fmt = spec["fmt"]
    if fmt == "yaml":
        return _load_yaml_node(path, spec["env"], spec["script"])
    node: Any = doc
    for key in spec["keys"] or []:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node if isinstance(node, dict) else None


# ── Consistency check ───────────────────────────────────────────────────
def check_node(node: dict | None, fname: str, spec: dict) -> list[str]:
    """Return a list of human-readable drift messages (empty = consistent)."""
    issues: list[str] = []
    if node is None:
        issues.append(f"{fname}: missing '{SERVER_NAME}' server entry")
        return issues
    if not isinstance(node, dict):
        issues.append(f"{fname}: '{SERVER_NAME}' entry is not an object")
        return issues

    # command present + non-empty
    cmd = node.get("command")
    if cmd is None or (isinstance(cmd, (list, str)) and not cmd):
        issues.append(f"{fname}: missing/empty 'command'")

    # script reference
    field, idx = spec["script"]
    container = node.get(field)
    ref = None
    if isinstance(container, list) and len(container) > idx:
        ref = container[idx]
    if not _script_ref_ok(ref):
        where = f"{field}[{idx}]" if isinstance(container, list) else field
        issues.append(
            f"{fname}: {where} does not reference {SCRIPT_MARKER} or {WRAPPER_MARKER} "
            f"(got {_norm_scalar(ref)!r})"
        )
    elif not _launch_command_ok(cmd, ref):
        issues.append(
            f"{fname}: launch command {_norm_scalar(cmd)!r} is not valid for "
            f"{_norm_scalar(ref)!r}"
        )

    # env block
    env = node.get(spec["env"])
    if not isinstance(env, dict) or not env:
        issues.append(f"{fname}: missing '{spec['env']}' env block")
        env = {}

    model = _norm_scalar(env.get("MCP_EMBEDDING_MODEL"))
    if model != CANONICAL_MODEL:
        issues.append(
            f"{fname}: MCP_EMBEDDING_MODEL={model!r} != {CANONICAL_MODEL!r}"
        )

    max_chars = _norm_scalar(env.get("MCP_MAX_RESPONSE_CHARS"))
    if max_chars != CANONICAL_MAX_RESPONSE_CHARS:
        issues.append(
            f"{fname}: MCP_MAX_RESPONSE_CHARS={max_chars!r} != "
            f"{CANONICAL_MAX_RESPONSE_CHARS!r}"
        )

    return issues


def discover_mcp_template_paths(root: Path) -> set[str]:
    """Find owned static MCP configs under config/, repo root, and plugins."""
    root = Path(root)
    found: set[str] = set()
    config_dir = root / "config"
    if config_dir.is_dir():
        for path in config_dir.iterdir():
            if not path.is_file() or path.suffix.lower() not in {
                ".json", ".toml", ".yaml", ".yml"
            }:
                continue
            try:
                text = path.read_text(encoding="utf-8").lower()
            except OSError:
                continue
            schema_markers = (
                "mcpservers",
                "mcp_servers",
                "context_servers",
                '"mcp"',
            )
            if "b12" in text and "command" in text and (
                "mcp" in path.name.lower()
                or any(marker in text for marker in schema_markers)
            ):
                found.add(path.relative_to(root).as_posix())

    root_config = root / ".mcp.json"
    if root_config.is_file():
        found.add(root_config.relative_to(root).as_posix())

    plugins_dir = root / "plugins"
    if plugins_dir.is_dir():
        for path in plugins_dir.rglob("*.json"):
            if path.name in {".mcp.json", "mcp_config.json"}:
                found.add(path.relative_to(root).as_posix())

    grok_plugins_dir = root / ".grok" / "plugins-available"
    if grok_plugins_dir.is_dir():
        for path in grok_plugins_dir.rglob(".mcp.json"):
            found.add(path.relative_to(root).as_posix())
    return found


def validate_grok_installer_contract(root: Path) -> list[str]:
    """Extract and structurally validate the Grok MCP block emitted by install.sh."""
    path = Path(root) / "install.sh"
    if not path.is_file():
        return ["install.sh: file missing"]
    text = path.read_text()
    marker = "b12_block = f'''"
    start = text.find(marker)
    if start < 0:
        return ["install.sh: Grok MCP config block missing"]
    start += len(marker)
    end = text.find("'''", start)
    if end < 0:
        return ["install.sh: Grok MCP config block is unterminated"]

    block = text[start:end]
    block = block.replace("{venv_python}", "__VENV_PYTHON__")
    block = block.replace("{server_script}", "__SCRIPT_PATH__")
    block = re.sub(
        r"\{os\.path\.expanduser\('[^']+'\)\}", "__B12_DATA_DIR__", block
    )
    try:
        doc = tomllib.loads(block) if tomllib is not None else None
    except Exception as exc:
        return [f"install.sh: Grok MCP config failed to parse ({exc})"]
    if doc is None:
        return ["install.sh: Grok MCP config requires Python 3.11+ tomllib"]

    spec = {
        "fmt": "toml",
        "keys": ["mcp_servers", "B12"],
        "env": "env",
        "script": ("args", 0),
    }
    node = _resolve_node(doc, spec, path)
    return check_node(node, "install.sh: Grok MCP config", spec)


def validate_repo(root: Path) -> dict[str, list[str]]:
    """Validate every registered template under ``root``.

    Returns ``{relative_path: [issue, ...]}``. Entries with empty issue
    lists are consistent.
    """
    root = Path(root)
    results: dict[str, list[str]] = {}
    registered: set[str] = set()
    for spec in TEMPLATES:
        rel = str(spec.get("path") or f"config/{spec['file']}")
        registered.add(rel)
        path = root / rel
        fname = rel
        if not path.exists():
            results[rel] = [f"{fname}: template file missing"]
            continue
        try:
            if spec["fmt"] == "json":
                doc = _load_json(path)
            elif spec["fmt"] == "toml":
                doc = _load_toml(path)
            else:
                doc = {}  # yaml loads the node directly
            node = _resolve_node(doc, spec, path)
            results[rel] = check_node(node, fname, spec)
        except Exception as exc:  # parse error → report as drift
            results[rel] = [f"{fname}: failed to parse ({exc})"]

    for rel in sorted(discover_mcp_template_paths(root) - registered):
        results[rel] = [f"{rel}: MCP template is not registered for validation"]
    results["install.sh: Grok-generated MCP config"] = validate_grok_installer_contract(root)
    return results


# ── CLI ─────────────────────────────────────────────────────────────────
def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    quiet = "--quiet" in argv or "-q" in argv
    root = Path(__file__).resolve().parents[1]

    results = validate_repo(root)
    drift = {p: i for p, i in results.items() if i}

    for rel, issues in results.items():
        if issues:
            for msg in issues:
                print(f"[FAIL] {msg}")
        elif not quiet:
            print(f"[PASS] {rel}: consistent")

    print()
    total = len(results)
    ok = total - len(drift)
    if drift:
        print(f"  {len(drift)} MCP config surface(s) with drift ({ok}/{total} consistent)")
        return 1
    print(f"  All {total} MCP config surfaces consistent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
