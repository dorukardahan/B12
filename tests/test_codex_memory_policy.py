"""Guard Codex's targeted B12 policy against eager memory invocation.

Codex applies skill triggers and AGENTS.md rules more literally than Claude
Code. With the shared Claude skill (recall-verb triggers) and a mandatory
AGENTS.md block, it searched B12 even for content still visible in the
conversation and called `memory_session_context` at every session start, work
the Codex hooks already do. Claude Code keeps the shared skill.
"""
from __future__ import annotations

import hashlib
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
SHIPPED_HASHES = ROOT / "config" / "codex-skills" / "shipped-skill-sha256.txt"


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


def _run_codex_skill_functions(home: Path, call: str) -> subprocess.CompletedProcess:
    source = INSTALLER.read_text()
    functions = "".join(
        re.search(rf"\n{name}\(\)\s*\{{.*?\n\}}\n", source, re.DOTALL).group(0)
        for name in ("b12_sha256", "b12_file_is_shipped", "b12_skill_is_installer_owned", "install_codex_skill",
                     "b12_verify_codex_skill")
    )
    script = (
        'info() { echo "INFO $*"; }; warn() { echo "WARN $*"; }\n'
        f'SCRIPT_DIR="{ROOT}"\n' + functions + call + "\n"
    )
    return subprocess.run(["bash", "-c", script], env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
                          capture_output=True, text=True, timeout=60)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _run_install_codex_skill(home: Path) -> subprocess.CompletedProcess:
    return _run_codex_skill_functions(home, "install_codex_skill")


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
def test_installer_keeps_a_user_skill_that_reuses_the_b12_header(tmp_path):
    # Name and header text alone do not prove that B12 wrote a file.
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    mine = home / ".agents" / "skills" / "b12-memory"
    mine.mkdir(parents=True)
    own_skill = "---\nname: b12-memory\ndescription: mine\n---\n\n# B12 Memory for Codex\n\nMy own version.\n"
    (mine / "SKILL.md").write_text(own_skill)

    result = _run_install_codex_skill(home)
    assert result.returncode == 0, result.stderr
    assert (mine / "SKILL.md").read_text() == own_skill
    assert "not written by the B12 installer" in result.stdout


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_installer_keeps_its_own_skill_once_the_user_edits_it(tmp_path):
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    assert _run_install_codex_skill(home).returncode == 0
    skill = home / ".agents" / "skills" / "b12-memory" / "SKILL.md"
    edited = skill.read_text() + "\nMy own rule.\n"
    skill.write_text(edited)

    result = _run_install_codex_skill(home)
    assert result.returncode == 0, result.stderr
    assert skill.read_text() == edited
    assert "was edited since" in result.stdout


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_installer_replaces_an_older_copy_it_marked(tmp_path):
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    dest = home / ".agents" / "skills" / "b12-memory"
    dest.mkdir(parents=True)
    (dest / "agents").mkdir(parents=True)
    older, older_yaml = "---\nname: b12-memory\ndescription: an older B12 release\n---\n", "interface: {}\n"
    (dest / "SKILL.md").write_text(older)
    (dest / "agents" / "openai.yaml").write_text(older_yaml)
    (dest / ".b12-installed").write_text(
        f"{_sha256(older.encode())}  SKILL.md\n{_sha256(older_yaml.encode())}  agents/openai.yaml\n")

    result = _run_install_codex_skill(home)
    assert result.returncode == 0, result.stderr
    assert (dest / "SKILL.md").read_text() == CODEX_SKILL.read_text()
    assert (dest / "agents" / "openai.yaml").read_text() == CODEX_SKILL_YAML.read_text()
    assert (dest / ".b12-installed").read_text() == (
        f"{_sha256(CODEX_SKILL.read_bytes())}  SKILL.md\n{_sha256(CODEX_SKILL_YAML.read_bytes())}  agents/openai.yaml\n")


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_installer_keeps_a_sidecar_the_user_edited(tmp_path):
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    assert _run_install_codex_skill(home).returncode == 0
    sidecar = home / ".agents" / "skills" / "b12-memory" / "agents" / "openai.yaml"
    edited = sidecar.read_text().replace('"B12 Memory"', '"My B12"')
    sidecar.write_text(edited)

    result = _run_codex_skill_functions(home, 'install_codex_skill; b12_verify_codex_skill; echo "problems=$?"')
    assert result.returncode == 0, result.stderr
    assert sidecar.read_text() == edited
    assert "was edited since" in result.stdout
    assert "problems=1" in result.stdout


def test_shipped_skill_hashes_are_well_formed():
    entries = [line for line in SHIPPED_HASHES.read_text().splitlines() if line and not line.startswith("#")]
    assert len(entries) >= 6 and len(set(entries)) == len(entries)
    paths = {"skills/b12/SKILL.md", "skills/b12-memory/SKILL.md", "config/codex-skills/b12-memory/SKILL.md",
             "config/codex-skills/b12-memory/agents/openai.yaml"}
    for entry in entries:
        match = re.fullmatch(r"([0-9a-f]{64})  (\S+)", entry)
        assert match and match.group(2) in paths, entry


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_installer_keeps_a_marked_skill_swapped_for_another_b12_version(tmp_path):
    # Once a marker exists, only the marker proves ownership; older B12 hashes do not.
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    assert _run_install_codex_skill(home).returncode == 0
    skill = home / ".agents" / "skills" / "b12-memory" / "SKILL.md"
    shutil.copy(SHARED_SKILL, skill)

    result = _run_install_codex_skill(home)
    assert result.returncode == 0, result.stderr
    assert skill.read_text() == SHARED_SKILL.read_text()
    assert "was edited since" in result.stdout


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_installer_keeps_a_sidecar_the_user_removed(tmp_path):
    # e.g. to lift the Codex-only product restriction
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    assert _run_install_codex_skill(home).returncode == 0
    sidecar = home / ".agents" / "skills" / "b12-memory" / "agents" / "openai.yaml"
    sidecar.unlink()

    result = _run_install_codex_skill(home)
    assert result.returncode == 0, result.stderr
    assert not sidecar.exists()
    assert "was edited since" in result.stdout


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
@pytest.mark.parametrize("user_skill", [False, True])
def test_verify_reports_whether_the_b12_skill_is_installed(tmp_path, user_skill):
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    if user_skill:
        mine = home / ".agents" / "skills" / "b12-memory"
        mine.mkdir(parents=True)
        (mine / "SKILL.md").write_text("---\nname: b12-memory\ndescription: mine\n---\n\nPersonal.\n")

    result = _run_codex_skill_functions(home, 'install_codex_skill; b12_verify_codex_skill; echo "problems=$?"')
    assert result.returncode == 0, result.stderr
    if user_skill:
        assert "problems=1" in result.stdout
        assert "the B12 Codex skill is NOT installed" in result.stdout
        assert "Verify: B12 skill installed" not in result.stdout
    else:
        assert "problems=0" in result.stdout
        assert "Verify: B12 skill installed" in result.stdout


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
    assert "reports the b12 skill as not installed" in setup
    assert "left unchanged with a warning" in readme
    assert "targeted model recall" in setup
    # The Codex UserPromptSubmit hook only tracks /goal; it never injects context.
    assert "automatic (`userpromptsubmit`)" not in setup
    assert "✓ via `userpromptsubmit` hook | ✓ via `userpromptsubmit` hook" not in setup
