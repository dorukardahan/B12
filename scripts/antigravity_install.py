#!/usr/bin/env python3
"""Install/stage B12's native Antigravity plugin package."""
from __future__ import annotations

import json
import shutil
import stat
import sys
from pathlib import Path
from typing import Any

PLUGIN_NAME = "b12"


def load_json(path: Path, default: dict[str, Any] | None = None) -> dict[str, Any]:
    if not path.exists():
        return {} if default is None else dict(default)
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else ({} if default is None else dict(default))
    except json.JSONDecodeError:
        return {} if default is None else dict(default)


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


def b12_mcp_server(venv_python: str, server_script: str) -> dict[str, Any]:
    return {
        "command": venv_python,
        "args": [server_script],
        "env": {
            "MCP_EMBEDDING_MODEL": "BAAI/bge-m3",
            "MCP_MAX_RESPONSE_CHARS": "40000",
        },
    }


def merge_global_mcp_config(config_path: Path, venv_python: str, server_script: str) -> None:
    cfg = load_json(config_path)
    servers = cfg.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        cfg["mcpServers"] = servers = {}
    servers["B12"] = b12_mcp_server(venv_python, server_script)
    write_json(config_path, cfg)


def stage_plugin(repo_root: Path, dest: Path, venv_python: str, server_script: str, hook_adapter: str) -> None:
    src = repo_root / "plugins" / "antigravity" / "b12"
    if not src.exists():
        raise FileNotFoundError(f"plugin template not found: {src}")
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest)

    write_json(dest / "mcp_config.json", {"mcpServers": {"B12": b12_mcp_server(venv_python, server_script)}})
    write_json(
        dest / "hooks.json",
        {
            "hooks": {
                "PreInvocation": [{"command": f"{venv_python} {hook_adapter} PreInvocation"}],
                "PostToolUse": [{"command": f"{venv_python} {hook_adapter} PostToolUse"}],
                "Stop": [{"command": f"{venv_python} {hook_adapter} Stop"}],
            }
        },
    )
    for path in (dest / "rules").glob("*.md") if (dest / "rules").exists() else []:
        path.chmod(path.stat().st_mode | stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)


def main(argv: list[str] | None = None) -> int:
    argv = argv or sys.argv[1:]
    if len(argv) != 5:
        print(
            "usage: antigravity_install.py <repo-root> <stage-dir> <venv-python> <server-script> <hook-adapter>",
            file=sys.stderr,
        )
        return 2
    repo_root, stage_dir, venv_python, server_script, hook_adapter = argv
    stage_plugin(Path(repo_root), Path(stage_dir), venv_python, server_script, hook_adapter)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
