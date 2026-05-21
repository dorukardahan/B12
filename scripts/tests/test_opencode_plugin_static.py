from pathlib import Path
import json
import shlex

ROOT = Path(__file__).resolve().parents[2]
OPENCODE = ROOT / "plugins" / "opencode"


def _read(relative: str) -> str:
    return (OPENCODE / relative).read_text()


def test_opencode_regexes_do_not_use_inline_case_flags():
    source = _read("src/lib/patterns.ts")

    assert "(?i)" not in source


def test_opencode_build_keeps_native_dependencies_external():
    package_json = json.loads(_read("package.json"))
    build_args = shlex.split(package_json["scripts"]["build"])

    assert "--external" in build_args
    assert "better-sqlite3" in build_args


def test_opencode_package_test_builds_dist_and_disables_daemon():
    package_json = json.loads(_read("package.json"))
    test_script = package_json["scripts"]["test"]

    assert test_script.startswith("bun run typecheck && bun run build && ")
    assert "B12_DISABLE_DAEMON=1" in shlex.split(test_script)
    assert test_script.endswith("bun test")


def test_opencode_package_includes_documentation_for_js_runtime():
    package_json = json.loads(_read("package.json"))

    assert "README.md" in package_json["files"]
    assert (OPENCODE / "README.md").is_file()
