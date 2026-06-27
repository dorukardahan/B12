"""Guards for the version/doc-drift fixes (2026-06-27 audit #15 + #16).

#15: .claude-plugin/marketplace.json's plugin-entry version was never synced by
     release.sh, so it drifted ~62 releases behind what users install.
#16: docs/architecture.md said the OpenCode plugin "scrubs nothing" — stale after
     PR #124 added a TypeScript scrubber + secret cap to its native write path.
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_marketplace_plugin_version_in_sync():
    mp = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
    pkg = json.loads((ROOT / "package.json").read_text())
    assert mp["plugins"][0]["version"] == pkg["version"], (
        f"marketplace plugin version {mp['plugins'][0]['version']} != package.json {pkg['version']} (#15)"
    )


def test_release_script_syncs_and_verifies_marketplace():
    src = (ROOT / "scripts" / "release.sh").read_text()
    # Syncs the PLUGIN ENTRY version (not the catalog metadata.version).
    assert 'mp["plugins"][0]["version"] = NEW' in src, "release.sh doesn't sync marketplace plugin version (#15)"
    assert 'mp["metadata"]["version"]' not in src, "release.sh must not touch the catalog metadata.version"
    # And verifies it post-write.
    assert ".claude-plugin/marketplace.json:" in src, "release.sh doesn't verify marketplace.json (#15)"


def test_architecture_doc_opencode_scrub_corrected():
    doc = (ROOT / "docs" / "architecture.md").read_text()
    assert "OpenCode plugin scrubs nothing" not in doc, "architecture.md still claims OpenCode scrubs nothing (#16)"
    assert "OpenCode plugin now scrubs" in doc, "architecture.md doesn't reflect the #124 OpenCode scrubber (#16)"
