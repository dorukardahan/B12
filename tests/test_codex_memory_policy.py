"""Guard Codex's targeted B12 policy against eager memory invocation.

Codex applies skill triggers and AGENTS.md rules more literally than Claude
Code. With the shared Claude skill (recall-verb triggers) and a mandatory
AGENTS.md block, it searched B12 even for content still visible in the
conversation and called `memory_session_context` at every session start, work
the Codex hooks already do. Claude Code keeps the shared skill.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CODEX_AGENTS = ROOT / "config" / "codex-agents-template.md"
CODEX_SKILL_DIR = ROOT / "config" / "codex-skills" / "b12-memory"
CODEX_SKILL = CODEX_SKILL_DIR / "SKILL.md"
CODEX_SKILL_YAML = CODEX_SKILL_DIR / "agents" / "openai.yaml"
INSTALLER = ROOT / "install.sh"
SHARED_SKILL = ROOT / "skills" / "b12-memory" / "SKILL.md"


def _frontmatter(text: str) -> tuple[str, str]:
    match = re.match(r"\A---\n(.*?)\n---\n", text, re.DOTALL)
    assert match, "Codex B12 skill must start with YAML frontmatter"
    frontmatter = match.group(1)
    name = re.search(r"^name:\s*(.+)$", frontmatter, re.MULTILINE)
    description = re.search(r"^description:\s*>\n(?P<body>(?:[ \t]+.*\n?)+)", frontmatter, re.MULTILINE)
    assert name, "Codex B12 skill must declare a name"
    assert description, "Codex B12 skill must declare a folded description"
    folded = " ".join(line.strip() for line in description.group("body").splitlines())
    return name.group(1).strip(), folded


def test_codex_skill_has_narrow_invocation_boundary():
    text = CODEX_SKILL.read_text()
    name, description = _frontmatter(text)
    lower = text.lower()
    flat = " ".join(lower.split())

    assert name == "b12-memory"
    # Codex shortens long descriptions when many skills are installed.
    assert len(description) <= 300
    assert "cross-session" in description.lower()
    assert "absent from the visible conversation" in description.lower()
    assert "explicit" in description.lower()

    # Positive triggers: genuinely missing history and explicit writes.
    assert "previous session" in flat
    assert "remember this for future sessions" in flat
    assert "correct the saved preference" in flat
    assert "forget that saved memory" in flat

    # Negative triggers: visible context, routine work, and lifecycle.
    assert "two messages ago" in flat
    assert "routine task" in flat
    assert "session start or session end" in flat

    # These phrases caused the eager behavior and must not return.
    assert "any recall verb" not in lower
    assert "start of any non-trivial task" not in lower
    assert "search memory on startup" not in lower
    assert "store silently when learning" not in lower


def test_codex_skill_is_limited_to_codex():
    # ChatGPT chat can read ~/.agents/skills too, but has no B12 MCP server.
    # OpenAI documents the sidecar's products as CHAT and/or CODEX; Codex 0.163
    # also accepts these uppercase spellings.
    yaml = CODEX_SKILL_YAML.read_text()
    products = re.search(r"^[ \t]+products:\n((?:[ \t]+- .*\n?)+)", yaml, re.MULTILINE)
    assert products, "policy.products must be set"
    values = re.findall(r"- ([A-Za-z]+)", products.group(1))
    assert values == ["CODEX"]
    assert set(values) <= {"CHAT", "CODEX"}
    display = re.search(r'^\s+display_name:\s*"([^"]+)"', yaml, re.MULTILINE)
    short = re.search(r'^\s+short_description:\s*"([^"]+)"', yaml, re.MULTILINE)
    assert display and 0 < len(display.group(1)) <= 64
    assert short and 25 <= len(short.group(1)) <= 64


def test_codex_agents_makes_current_context_authoritative():
    lower = CODEX_AGENTS.read_text().lower()
    flat = " ".join(lower.split())

    assert "visible conversation" in flat
    assert "current project files" in flat
    assert "absent from current context" in flat
    assert "hooks own the session lifecycle" in flat
    assert "do not call `memory_session_context` at startup" in flat
    assert "do not manually store a session summary at the end" in flat

    assert "mandatory" not in lower
    assert "before first response" not in lower
    assert "after last response" not in lower
    assert "do not rely on conversation context alone" not in lower


def test_installer_keeps_codex_and_claude_skill_sources_separate():
    installer = INSTALLER.read_text()
    codex_function = installer.split("install_codex_skill() {", 1)[1].split("\n}\n", 1)[0]
    claude_function = installer.split("install_claude_skill() {", 1)[1].split("\n}\n", 1)[0]

    assert 'SKILL_SRC="$SCRIPT_DIR/config/codex-skills/b12-memory"' in codex_function
    assert 'SKILL_DEST="$HOME/.agents/skills/b12-memory"' in codex_function
    assert 'SKILL_SRC="$SCRIPT_DIR/skills/b12-memory"' in claude_function
    assert SHARED_SKILL.is_file()


def _run_install_codex_skill(home: Path) -> subprocess.CompletedProcess:
    source = INSTALLER.read_text()
    helper = re.search(r"\nb12_skill_is_installer_owned\(\)\s*\{.*?\n\}\n", source, re.DOTALL).group(0)
    function = re.search(r"\ninstall_codex_skill\(\)\s*\{.*?\n\}\n", source, re.DOTALL).group(0)
    script = (
        'info() { echo "INFO $*"; }; warn() { echo "WARN $*"; }\n'
        f'SCRIPT_DIR="{ROOT}"\n' + helper + function + "install_codex_skill\n"
    )
    return subprocess.run(["bash", "-c", script], env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
                          capture_output=True, text=True, timeout=60)


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_installer_moves_the_codex_skill_to_the_agents_root(tmp_path):
    home = tmp_path / "home"
    legacy = home / ".codex" / "skills" / "b12-memory"
    legacy.mkdir(parents=True)
    shutil.copy(SHARED_SKILL, legacy / "SKILL.md")  # what earlier installers shipped
    older = home / ".codex" / "skills" / "b12"
    older.mkdir()
    shutil.copy(SHARED_SKILL, older / "SKILL.md")

    result = _run_install_codex_skill(home)
    assert result.returncode == 0, result.stderr

    installed = home / ".agents" / "skills" / "b12-memory"
    assert (installed / "SKILL.md").read_text() == CODEX_SKILL.read_text()
    assert (installed / "agents" / "openai.yaml").read_text() == CODEX_SKILL_YAML.read_text()
    assert not legacy.exists() and not older.exists(), "stale copies would load next to the new skill"


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_installer_keeps_a_user_authored_skill_with_the_same_name(tmp_path):
    home = tmp_path / "home"
    mine = home / ".codex" / "skills" / "b12-memory"
    mine.mkdir(parents=True)
    (mine / "SKILL.md").write_text("---\nname: b12-memory\ndescription: my own notes\n---\n\nPersonal.\n")

    result = _run_install_codex_skill(home)
    assert result.returncode == 0, result.stderr
    assert (mine / "SKILL.md").exists()
    assert "WARN" in result.stdout and "Inspect it manually" in result.stdout


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_installer_never_overwrites_a_user_skill_in_the_agents_root(tmp_path):
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    mine = home / ".agents" / "skills" / "b12-memory"
    (mine / "agents").mkdir(parents=True)
    own_skill = "---\nname: b12-memory\ndescription: my own notes\n---\n\nPersonal.\n"
    (mine / "SKILL.md").write_text(own_skill)
    (mine / "agents" / "openai.yaml").write_text("interface:\n  display_name: Mine\n")

    stale = home / ".codex" / "skills" / "b12-memory"
    stale.mkdir(parents=True)
    shutil.copy(SHARED_SKILL, stale / "SKILL.md")  # left by an earlier installer

    result = _run_install_codex_skill(home)
    assert result.returncode == 0, result.stderr
    assert (mine / "SKILL.md").read_text() == own_skill
    assert (mine / "agents" / "openai.yaml").read_text() == "interface:\n  display_name: Mine\n"
    assert "not written by the B12 installer" in result.stdout
    # The stale B12 copy is still removed, or Codex would list two b12-memory skills.
    assert not stale.exists()


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_installer_replaces_its_own_older_skill_in_the_agents_root(tmp_path):
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    dest = home / ".agents" / "skills" / "b12-memory"
    dest.mkdir(parents=True)
    shutil.copy(SHARED_SKILL, dest / "SKILL.md")  # moved there from ~/.codex/skills by hand

    result = _run_install_codex_skill(home)
    assert result.returncode == 0, result.stderr
    assert (dest / "SKILL.md").read_text() == CODEX_SKILL.read_text()
    assert (dest / "agents" / "openai.yaml").read_text() == CODEX_SKILL_YAML.read_text()


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_installer_skips_machines_without_codex(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    assert _run_install_codex_skill(home).returncode == 0
    assert not (home / ".agents").exists()


def test_docs_describe_codex_hook_lifecycle_and_targeted_recall():
    readme = (ROOT / "README.md").read_text().lower()
    setup = (ROOT / "docs" / "setup.md").read_text().lower()

    assert "codex-specific b12 skill" in readme
    assert "missing cross-session context" in readme
    assert "~/.agents/skills/b12-memory" in setup
    assert "leaves it unchanged and warns" in setup
    assert "left unchanged with a warning" in readme
    assert "targeted model recall" in setup
    # The Codex UserPromptSubmit hook only tracks /goal; it never injects context.
    assert "automatic (`userpromptsubmit`)" not in setup
    assert "✓ via `userpromptsubmit` hook | ✓ via `userpromptsubmit` hook" not in setup
