#!/usr/bin/env python3
"""
B12 Patch: Disable MCP SDK input validation in server_impl.py

Problem:
    mcp-memory-service's server_impl.py registers call_tool() with
    validate_input=True (MCP SDK default). The SDK's jsonschema.validate()
    intermittently rejects valid tool arguments, causing:
        "Input validation error: 'content' is a required property"
    even when content IS present. FastMCP explicitly sets validate_input=False
    because handlers do their own validation.

Fix:
    @self.server.call_tool()
    ->
    @self.server.call_tool(validate_input=False)

Usage:
    python3 scripts/patch_validate_input.py           # Apply patch
    python3 scripts/patch_validate_input.py --check    # Check status only
    python3 scripts/patch_validate_input.py --revert   # Revert to original

Idempotent: safe to run multiple times.
"""

import sys
import os
import glob

# The exact strings we're looking for / replacing
ORIGINAL = "@self.server.call_tool()"
PATCHED  = "@self.server.call_tool(validate_input=False)"


def find_server_impl():
    """Find server_impl.py in the pipx venv."""
    patterns = [
        os.path.expanduser(
            "~/.local/pipx/venvs/mcp-memory-service/lib/python*/site-packages/"
            "mcp_memory_service/server_impl.py"
        ),
    ]
    for pattern in patterns:
        matches = glob.glob(pattern)
        if matches:
            return matches[0]
    return None


def check_status(path):
    """Check if the patch is already applied."""
    with open(path, "r") as f:
        content = f.read()

    if PATCHED in content:
        return "patched"
    elif ORIGINAL in content:
        return "unpatched"
    else:
        return "unknown"


def apply_patch(path):
    """Apply the validate_input=False patch."""
    with open(path, "r") as f:
        content = f.read()

    status = check_status(path)

    if status == "patched":
        print(f"  [OK] Already patched: {path}")
        return True

    if status == "unknown":
        print(f"  [WARN] Could not find expected pattern in {path}")
        print(f"         Looking for: {ORIGINAL}")
        print(f"         File may have been modified by upstream.")
        return False

    # Apply the patch
    new_content = content.replace(ORIGINAL, PATCHED, 1)

    # Verify the replacement happened
    if new_content == content:
        print(f"  [ERR] Replacement failed (no change)")
        return False

    with open(path, "w") as f:
        f.write(new_content)

    print(f"  [OK] Patched: {ORIGINAL}")
    print(f"     -> {PATCHED}")
    return True


def revert_patch(path):
    """Revert the patch to original."""
    with open(path, "r") as f:
        content = f.read()

    status = check_status(path)

    if status == "unpatched":
        print(f"  [OK] Already unpatched: {path}")
        return True

    if status == "unknown":
        print(f"  [WARN] Could not find expected pattern in {path}")
        return False

    new_content = content.replace(PATCHED, ORIGINAL, 1)
    with open(path, "w") as f:
        f.write(new_content)

    print(f"  [OK] Reverted to original: {ORIGINAL}")
    return True


def main():
    check_only = "--check" in sys.argv
    revert = "--revert" in sys.argv

    path = find_server_impl()
    if not path:
        print("  [WARN] server_impl.py not found (mcp-memory-service not installed?)")
        sys.exit(0)  # Exit 0 — not an error, just not installed

    print(f"  Found: {path}")

    if check_only:
        status = check_status(path)
        print(f"  Status: {status}")
        sys.exit(0 if status == "patched" else 1)

    if revert:
        ok = revert_patch(path)
        sys.exit(0 if ok else 1)

    ok = apply_patch(path)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
