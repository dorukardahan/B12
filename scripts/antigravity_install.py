#!/usr/bin/env python3
"""Install/stage B12's native Antigravity plugin package."""
from __future__ import annotations

import json
import os
import shlex
import shutil
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any

PLUGIN_NAME = "b12"


def load_json(path: Path, default: dict[str, Any] | None = None) -> dict[str, Any]:
    if not path.exists():
        return {} if default is None else dict(default)
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"refusing to overwrite invalid JSON config: {path}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"refusing to overwrite non-object JSON config: {path}")
    return data


def write_json(path: Path, data: dict[str, Any]) -> None:
    """Atomically write JSON while preserving an existing file's permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    old_mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(tmp_name, old_mode)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


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
        raise ValueError(f"refusing to replace non-object mcpServers in: {config_path}")
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
    command = f"{shlex.quote(venv_python)} {shlex.quote(hook_adapter)}"
    write_json(
        dest / "hooks.json",
        {
            "b12-memory": {
                "PreInvocation": [
                    {"type": "command", "command": f"{command} PreInvocation", "timeout": 25}
                ],
                "PostToolUse": [
                    {
                        "matcher": "*",
                        "hooks": [
                            {"type": "command", "command": f"{command} PostToolUse", "timeout": 10}
                        ],
                    }
                ],
                "Stop": [
                    {"type": "command", "command": f"{command} Stop", "timeout": 45}
                ],
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
