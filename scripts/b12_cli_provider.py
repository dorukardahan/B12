#!/usr/bin/env python3
"""
B12 cross-CLI host detection + event name canonicalization.

When a B12 hook fires, it needs to know which host CLI invoked it
(Claude Code, Codex, Gemini, OpenCode, ...) and translate the raw
event name into the single canonical vocabulary B12's hook scripts
share.

Why a pure-Python module: B12's hooks are bash scripts that today
detect provider via inline `if [ -n "$CLAUDECODE" ]` checks. Once a
third or fourth CLI joins the mix, those inline checks duplicate. A
single Python helper (importable from `b12_mcp_server.py` and
shellable via `python3 -m scripts.b12_cli_provider`) keeps the
detection table in one place.

Pure stdlib. No third-party deps. Safe to import from any context.

Detection priority:
  1. CLAUDECODE=1                → claude-cli
  2. CODEX_SESSION_ID or
     CODEX_VERSION (set by 0.130.0+) → codex-cli
  3. GEMINI_CLI_SESSION          → gemini-cli
  4. OPENCODE_SESSION_ID         → opencode-cli
  5. KIMI_SESSION_ID             → kimi-cli
  6. GROK_SESSION_ID             → grok-cli
  7. anything else               → "unknown"

Event-name canonicalization (CLI raw → B12 canonical):
  claude-cli: SessionStart      → session_start
              UserPromptSubmit  → user_prompt
              PreToolUse        → pre_tool_use
              PostToolUse       → post_tool_use
              PreCompact        → pre_compact
              SessionEnd        → session_end
  codex-cli:  SessionStart      → session_start
              UserPromptSubmit  → user_prompt
              Stop              → session_end  (Codex emits Stop at end-of-session)
  gemini-cli: same as Codex shape
  opencode-cli: same as Codex shape
  kimi-cli:   same as Codex shape
  grok-cli:   SessionStart, UserPromptSubmit, Stop — same shape

Pattern mirrors AytuncYildizli/B12 PR 3 (8e3adaf, lifecycle hooks)
canonicalization table, adapted to B12's actual event set.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


# ── Provider names ─────────────────────────────────────────────────

PROVIDER_CLAUDE = "claude-cli"
PROVIDER_CODEX = "codex-cli"
PROVIDER_GEMINI = "gemini-cli"
PROVIDER_OPENCODE = "opencode-cli"
PROVIDER_KIMI = "kimi-cli"
PROVIDER_GROK = "grok-cli"
PROVIDER_UNKNOWN = "unknown"


# ── Canonical event vocabulary ────────────────────────────────────

CANONICAL_EVENTS: frozenset[str] = frozenset({
    "session_start",
    "user_prompt",
    "pre_tool_use",
    "post_tool_use",
    "pre_compact",
    "session_end",
})


# Per-provider raw event → canonical mapping.
_EVENT_MAP: dict[str, dict[str, str]] = {
    PROVIDER_CLAUDE: {
        "SessionStart": "session_start",
        "UserPromptSubmit": "user_prompt",
        "PreToolUse": "pre_tool_use",
        "PostToolUse": "post_tool_use",
        "PreCompact": "pre_compact",
        "SessionEnd": "session_end",
    },
    PROVIDER_CODEX: {
        "SessionStart": "session_start",
        "UserPromptSubmit": "user_prompt",
        "Stop": "session_end",
        # Codex doesn't emit pre-compact (auto-compaction is internal).
    },
    PROVIDER_GEMINI: {
        "SessionStart": "session_start",
        "UserPromptSubmit": "user_prompt",
        "Stop": "session_end",
    },
    PROVIDER_OPENCODE: {
        "SessionStart": "session_start",
        "UserPromptSubmit": "user_prompt",
        "PreToolUse": "pre_tool_use",
        "PostToolUse": "post_tool_use",
        "Stop": "session_end",
    },
    PROVIDER_KIMI: {
        "SessionStart": "session_start",
        "UserPromptSubmit": "user_prompt",
        "Stop": "session_end",
    },
    PROVIDER_GROK: {
        "SessionStart": "session_start",
        "UserPromptSubmit": "user_prompt",
        "Stop": "session_end",
    },
}


# ── Env-var detection table ─────────────────────────────────────

# Ordered list of (env_var, provider). First non-empty wins.
# Note: CLAUDECODE wins ties because Claude Code's session may co-exist
# in same shell with other CLI env vars from a previous run.
_ENV_DETECTION: tuple[tuple[str, str], ...] = (
    ("CLAUDECODE", PROVIDER_CLAUDE),
    ("CLAUDE_SESSION_ID", PROVIDER_CLAUDE),
    ("CODEX_SESSION_ID", PROVIDER_CODEX),
    ("CODEX_VERSION", PROVIDER_CODEX),
    ("GEMINI_CLI_SESSION", PROVIDER_GEMINI),
    ("OPENCODE_SESSION_ID", PROVIDER_OPENCODE),
    ("KIMI_SESSION_ID", PROVIDER_KIMI),
    ("GROK_SESSION_ID", PROVIDER_GROK),
)


# ── Result ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class HostInfo:
    """Result of host detection.

    `provider` is one of the PROVIDER_* constants.
    `session_id` is the host's session identifier when available
    (empty string if the host doesn't expose one through env).
    `version` is the host's CLI version string when set.
    """
    provider: str
    session_id: str
    version: str


# ── Public API ────────────────────────────────────────────────


def detect_provider(env: dict[str, str] | None = None) -> HostInfo:
    """Detect host CLI from environment variables.

    Pass an explicit `env` dict for testing; defaults to `os.environ`.
    """
    e = env if env is not None else dict(os.environ)
    provider = PROVIDER_UNKNOWN
    for var, prov in _ENV_DETECTION:
        if e.get(var):
            provider = prov
            break

    # Best-effort session_id + version per provider.
    sid_keys = {
        PROVIDER_CLAUDE: ("CLAUDE_SESSION_ID",),
        PROVIDER_CODEX: ("CODEX_SESSION_ID",),
        PROVIDER_GEMINI: ("GEMINI_CLI_SESSION",),
        PROVIDER_OPENCODE: ("OPENCODE_SESSION_ID",),
        PROVIDER_KIMI: ("KIMI_SESSION_ID",),
        PROVIDER_GROK: ("GROK_SESSION_ID",),
    }
    ver_keys = {
        PROVIDER_CLAUDE: ("CLAUDE_CODE_VERSION", "CLAUDECODE_VERSION"),
        PROVIDER_CODEX: ("CODEX_VERSION",),
        PROVIDER_GEMINI: ("GEMINI_VERSION",),
        PROVIDER_OPENCODE: ("OPENCODE_VERSION",),
        PROVIDER_KIMI: ("KIMI_VERSION",),
        PROVIDER_GROK: ("GROK_VERSION",),
    }
    sid = ""
    for k in sid_keys.get(provider, ()):
        if e.get(k):
            sid = e[k]
            break
    ver = ""
    for k in ver_keys.get(provider, ()):
        if e.get(k):
            ver = e[k]
            break
    return HostInfo(provider=provider, session_id=sid, version=ver)


def canonicalize_event(raw_event: str, provider: str | None = None) -> str:
    """Map a raw event name to B12's canonical vocabulary.

    If `provider` is not given, the host is auto-detected from env.
    If the raw event name doesn't appear in the provider's table,
    the raw name is lowercased and underscored as a best-effort
    fallback so the caller can still log it (vs raising).
    """
    if provider is None:
        provider = detect_provider().provider
    table = _EVENT_MAP.get(provider, {})
    mapped = table.get(raw_event)
    if mapped:
        return mapped
    # Fallback: snake_case the raw name. "SessionStart" → "session_start".
    out_chars: list[str] = []
    for i, ch in enumerate(raw_event or ""):
        if ch.isupper() and i > 0 and out_chars and out_chars[-1] != "_":
            out_chars.append("_")
        out_chars.append(ch.lower())
    return "".join(out_chars) or "unknown"


def is_canonical(event: str) -> bool:
    """True if `event` is in the canonical vocabulary."""
    return event in CANONICAL_EVENTS


# ── CLI ────────────────────────────────────────────────────────


def _cli_main(argv: list[str]) -> int:
    """`python3 -m scripts.b12_cli_provider [detect|canonicalize EVENT]`."""
    if not argv or argv[0] in {"--help", "-h", "help"}:
        print("Usage:")
        print("  python3 -m scripts.b12_cli_provider detect")
        print("  python3 -m scripts.b12_cli_provider canonicalize <RawEventName>")
        print("  python3 -m scripts.b12_cli_provider self-test")
        return 0

    cmd = argv[0]
    if cmd == "detect":
        info = detect_provider()
        print(f"provider={info.provider} session_id={info.session_id} version={info.version}")
        return 0
    if cmd == "canonicalize":
        if len(argv) < 2:
            print("ERROR: pass an event name", end="\n")
            return 2
        out = canonicalize_event(argv[1])
        print(out)
        return 0
    if cmd == "self-test":
        return _selftest()
    print(f"ERROR: unknown command '{cmd}'")
    return 2


def _selftest() -> int:
    """Smoke-test the table. Returns 0 on all-pass, 1 otherwise."""
    cases: list[tuple[dict[str, str], str, str, str]] = [
        # (env, raw_event, expected_provider, expected_canonical)
        ({"CLAUDECODE": "1"}, "SessionStart", PROVIDER_CLAUDE, "session_start"),
        ({"CLAUDECODE": "1"}, "PreCompact", PROVIDER_CLAUDE, "pre_compact"),
        ({"CLAUDECODE": "1"}, "SessionEnd", PROVIDER_CLAUDE, "session_end"),
        ({"CODEX_SESSION_ID": "abc"}, "Stop", PROVIDER_CODEX, "session_end"),
        ({"CODEX_VERSION": "0.130.0"}, "UserPromptSubmit", PROVIDER_CODEX, "user_prompt"),
        ({"GEMINI_CLI_SESSION": "x"}, "Stop", PROVIDER_GEMINI, "session_end"),
        ({"OPENCODE_SESSION_ID": "y"}, "PreToolUse", PROVIDER_OPENCODE, "pre_tool_use"),
        ({"KIMI_SESSION_ID": "z"}, "SessionStart", PROVIDER_KIMI, "session_start"),
        ({"GROK_SESSION_ID": "w"}, "Stop", PROVIDER_GROK, "session_end"),
        ({}, "SessionStart", PROVIDER_UNKNOWN, "session_start"),  # snake_case fallback
        ({}, "WeirdNewEvent", PROVIDER_UNKNOWN, "weird_new_event"),  # snake_case fallback
    ]
    failed = 0
    for env, raw, exp_prov, exp_can in cases:
        info = detect_provider(env)
        got_can = canonicalize_event(raw, info.provider)
        ok = info.provider == exp_prov and got_can == exp_can
        status = "OK" if ok else "FAIL"
        if not ok:
            failed += 1
        print(f"  [{status}] env={list(env.keys()) or '(none)'!s:35s}  raw={raw:18s}  → provider={info.provider:14s}  canonical={got_can}")
    print()
    if failed:
        print(f"FAILED: {failed} / {len(cases)} cases")
        return 1
    print(f"PASSED: {len(cases)} / {len(cases)} cases")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_cli_main(sys.argv[1:]))
