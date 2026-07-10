"""Cross-tool MCP config template consistency guard (issue #158).

B12 ships one MCP server config template per supported editor/CLI in
``config/``. Because each host tool uses a different native schema
(``mcpServers.B12``, ``servers.B12``, ``context_servers.B12``,
``amp.mcpServers.B12``, ``mcp.B12``, ``mcp_servers.B12``, a bare root
``B12``, a YAML list, ...), a template can silently drift out of
consistency with the rest — a missing env var, a stale model id, a
dropped script arg — and the only symptom is degraded recall or
truncated responses on that one host.

This guard reuses the canonical validator in
``scripts/validate_mcp_templates.py`` and fails the test run on any
drift, so regressions are caught in CI before they ship.

It also carries an in-process regression test: a deliberately broken
template snapshot must produce a non-empty issue list, proving the
validator actually detects drift (not just passes vacuously).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
VALIDATOR = ROOT / "scripts" / "validate_mcp_templates.py"


def _load_validator():
    """Import the validator module by path (it lives outside the package)."""
    spec = importlib.util.spec_from_file_location("validate_mcp_templates", VALIDATOR)
    assert spec and spec.loader, "could not build spec for validator"
    mod = importlib.util.module_from_spec(spec)
    sys.modules["validate_mcp_templates"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def validator():
    return _load_validator()


def test_all_mcp_templates_consistent(validator):
    """Every registered MCP template must satisfy the consistency contract."""
    results = validator.validate_repo(ROOT)
    drift = {p: issues for p, issues in results.items() if issues}
    assert not drift, (
        f"{len(drift)} MCP config template(s) drifted:\n"
        + "\n".join(f"  {p}:\n" + "\n".join(f"    - {m}" for m in issues)
                     for p, issues in drift.items())
    )
    assert ".mcp.json" in results
    assert "plugins/antigravity/b12/mcp_config.json" in results


def test_validator_detects_drift(validator):
    """Regression: the validator must flag a template with stale env vars.

    Without this, a validator that always returns an empty issue list
    would make ``test_all_mcp_templates_consistent`` vacuously green.
    """
    fake_node = {
        "command": "__VENV_PYTHON__",
        "args": ["__SCRIPT_PATH__"],
        "env": {
            "MCP_EMBEDDING_MODEL": "all-MiniLM-L6-v2",  # stale model
            # MCP_MAX_RESPONSE_CHARS intentionally missing
        },
    }
    spec = {"file": "fake.json", "fmt": "json", "env": "env",
            "script": ("args", 0)}
    issues = validator.check_node(fake_node, "fake.json", spec)
    assert issues, "validator failed to flag a deliberately broken node"
    joined = "\n".join(issues)
    assert "MCP_EMBEDDING_MODEL" in joined
    assert "MCP_MAX_RESPONSE_CHARS" in joined


def test_validator_detects_missing_script_ref(validator):
    """Regression: a node without a valid script reference must be flagged."""
    fake_node = {
        "command": "__VENV_PYTHON__",
        "args": ["some-other-script.py"],
        "env": {
            "MCP_EMBEDDING_MODEL": "BAAI/bge-m3",
            "MCP_MAX_RESPONSE_CHARS": "40000",
        },
    }
    spec = {"file": "fake.json", "fmt": "json", "env": "env",
            "script": ("args", 0)}
    issues = validator.check_node(fake_node, "fake.json", spec)
    assert any("b12_mcp_server.py" in m for m in issues), \
        "validator missed an invalid script reference"


def test_validator_rejects_script_name_substrings(validator):
    """Backups/stale prefixed filenames are not the canonical entry point."""
    fake_node = {
        "command": "python3",
        "args": ["/path/to/old_b12_mcp_server.py.bak"],
        "env": {
            "MCP_EMBEDDING_MODEL": "BAAI/bge-m3",
            "MCP_MAX_RESPONSE_CHARS": "40000",
        },
    }
    spec = {"file": "fake.json", "fmt": "json", "env": "env",
            "script": ("args", 0)}
    issues = validator.check_node(fake_node, "fake.json", spec)
    assert any("does not reference" in issue for issue in issues)


def test_validator_detects_wrong_launch_command(validator):
    """A valid script argument must not make an unrelated launcher pass."""
    fake_node = {
        "command": "node",
        "args": ["b12_mcp_server.py"],
        "env": {
            "MCP_EMBEDDING_MODEL": "BAAI/bge-m3",
            "MCP_MAX_RESPONSE_CHARS": "40000",
        },
    }
    spec = {"file": "fake.json", "fmt": "json", "env": "env",
            "script": ("args", 0)}
    issues = validator.check_node(fake_node, "fake.json", spec)
    assert any("launch command" in issue for issue in issues)


def test_validator_handles_opencode_environment_key(validator):
    """OpenCode uses ``environment`` (not ``env``) and bundles command+script
    into ``command``. The validator must resolve env via the spec's env key
    and the script via ``command[1]``."""
    fake_node = {
        "type": "local",
        "command": ["__VENV_PYTHON__", "__SCRIPT_PATH__"],
        "enabled": True,
        "environment": {
            "MCP_EMBEDDING_MODEL": "BAAI/bge-m3",
            "MCP_MAX_RESPONSE_CHARS": "40000",
        },
    }
    spec = {"file": "fake.json", "fmt": "json", "env": "environment",
            "script": ("command", 1)}
    issues = validator.check_node(fake_node, "fake.json", spec)
    assert not issues, \
        f"validator misread the OpenCode shape: {issues}"


def test_yaml_fallback_requires_named_b12_server(validator, tmp_path):
    """The dependency-free parser must not borrow fields from another server."""
    template = tmp_path / "continue.yaml"
    template.write_text(
        "name: Example\n"
        "mcpServers:\n"
        "  - name: NotB12\n"
        "    command: __VENV_PYTHON__\n"
        "    args:\n"
        "      - __SCRIPT_PATH__\n"
        "    env:\n"
        "      MCP_EMBEDDING_MODEL: BAAI/bge-m3\n"
        "      MCP_MAX_RESPONSE_CHARS: '40000'\n"
    )
    assert validator._load_yaml_node_fallback(template) == {}


def test_yaml_fallback_reads_only_b12_server(validator, tmp_path):
    """A valid B12 list entry is parsed without requiring PyYAML."""
    template = tmp_path / "continue.yaml"
    template.write_text(
        "mcpServers:\n"
        "  - name: Other\n"
        "    command: node\n"
        "  - name: B12\n"
        "    command: __VENV_PYTHON__\n"
        "    args:\n"
        "      - __SCRIPT_PATH__\n"
        "    env:\n"
        "      MCP_EMBEDDING_MODEL: BAAI/bge-m3\n"
        "      MCP_MAX_RESPONSE_CHARS: '40000'\n"
    )
    node = validator._load_yaml_node_fallback(template)
    assert node["name"] == "B12"
    assert node["command"] == "__VENV_PYTHON__"
    assert node["args"] == ["__SCRIPT_PATH__"]
    assert node["env"]["MCP_MAX_RESPONSE_CHARS"] == "40000"


def test_yaml_loader_uses_fallback_when_pyyaml_is_unavailable(
    validator, tmp_path, monkeypatch
):
    """The normal loader must preserve validation when optional PyYAML is absent."""
    import builtins

    template = tmp_path / "continue.yaml"
    template.write_text(
        "mcpServers:\n"
        "  - name: B12\n"
        "    command: __VENV_PYTHON__\n"
        "    args:\n"
        "      - __SCRIPT_PATH__\n"
        "    env:\n"
        "      MCP_EMBEDDING_MODEL: BAAI/bge-m3\n"
        "      MCP_MAX_RESPONSE_CHARS: '40000'\n"
    )
    real_import = builtins.__import__

    def import_without_yaml(name, *args, **kwargs):
        if name == "yaml":
            raise ModuleNotFoundError("forced no-yaml test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_yaml)
    node = validator._load_yaml_node(template, "env", ("args", 0))
    assert node["name"] == "B12"
    assert node["env"]["MCP_EMBEDDING_MODEL"] == "BAAI/bge-m3"


def test_grok_installer_config_follows_canonical_contract(validator):
    """The Grok config emitted by install.sh is validated as structured TOML."""
    assert validator.validate_grok_installer_contract(ROOT) == []


@pytest.mark.parametrize(
    "old,new,expected",
    [
        (
            'command = "{venv_python}"',
            'command = "node"',
            "launch command",
        ),
        (
            'args = ["{server_script}"]',
            'args = ["old_b12_mcp_server.py.bak"]',
            "does not reference",
        ),
        (
            'MCP_MAX_RESPONSE_CHARS = "40000"',
            'MCP_MAX_RESPONSE_CHARS = "123"',
            "MCP_MAX_RESPONSE_CHARS",
        ),
    ],
)
def test_grok_installer_config_detects_structural_drift(
    validator, tmp_path, old, new, expected
):
    """A bad command, script, or env value in the generated block must fail."""
    source = (ROOT / "install.sh").read_text()
    marker = "b12_block = f'''"
    prefix, grok_block = source.split(marker, 1)
    assert old in grok_block
    mutated = prefix + marker + grok_block.replace(old, new, 1)
    (tmp_path / "install.sh").write_text(mutated)
    issues = validator.validate_grok_installer_contract(tmp_path)
    assert any(expected in issue for issue in issues), issues


def test_validator_cli_exits_nonzero_on_drift(validator, monkeypatch, capsys):
    """The CLI entry must exit 1 when drift is present."""
    monkeypatch.setattr(
        validator, "validate_repo", lambda _root: {
            "config/fake.json": ["fake.json: template file missing"]
        }
    )
    rc = validator.main(["--quiet"])
    captured = capsys.readouterr()
    assert rc == 1, f"expected exit 1 on drift, got {rc}"
    assert "[FAIL] fake.json: template file missing" in captured.out
