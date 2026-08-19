"""Keep the install-help form aligned with README supported hosts."""

from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
INSTALL_HELP = ROOT / ".github" / "ISSUE_TEMPLATE" / "install_help.yml"
CHECK = ROOT / "scripts" / "check_readme_platforms.py"

# README uses the short public names; the form already uses these labels.
README_TO_FORM = {
    "Continue": "Continue.dev",
    "Gemini": "Gemini CLI",
    "Kimi": "Kimi Code",
    "VS Code/Copilot": "VS Code / Copilot",
}


def _load_readme_check():
    spec = importlib.util.spec_from_file_location("check_readme_platforms", CHECK)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _dropdown_options(text: str) -> list[str]:
    options: list[str] = []
    in_options = False
    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if stripped == "options:":
            in_options = True
            continue
        if in_options:
            if stripped.startswith("- "):
                options.append(stripped[2:].strip())
                continue
            if stripped:
                break
    return options


def test_install_help_covers_readme_hosts_plus_other():
    check = _load_readme_check()
    readme = README.read_text(encoding="utf-8")
    summary = check._single_match(
        check._SUMMARY_RE, readme, "Cross-tool memory platform summary"
    )
    readme_hosts = check._platform_names(summary)
    form_options = _dropdown_options(INSTALL_HELP.read_text(encoding="utf-8"))

    expected = [README_TO_FORM.get(name, name) for name in readme_hosts]
    missing = [name for name in expected if name not in form_options]

    assert "Google Antigravity" in form_options
    assert "Other" in form_options
    assert missing == [], (
        "install_help.yml is missing named hosts from the README "
        f"supported-platform list: {missing}"
    )
    assert form_options[-1] == "Other"
