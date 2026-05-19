# Security Policy

B12 stores conversation memory locally in SQLite databases on the user's
machine and may, depending on conversation content, end up holding sensitive
information (API keys accidentally pasted into chat, project secrets,
personal data). Take security reports seriously — please follow the
responsible-disclosure process below.

## Reporting a vulnerability

**Do not open a public GitHub issue for security bugs.** Use one of the
following private channels:

- Email: `security-advisory@users.noreply.github.com`
- GitHub security advisory: <https://github.com/dorukardahan/B12/security/advisories/new>

Please include:

- A description of the vulnerability and its impact
- Steps to reproduce, including affected B12 version (`./install.sh --version`
  or the `chore(release): v...` commit on `main`)
- Your suggested fix (optional but appreciated)
- Whether you would like credit in the release notes

We aim to acknowledge reports within 72 hours, ship a patch for confirmed
high-severity issues within 14 days, and publish a CVE / GitHub advisory
on patch release.

## Supported versions

| Version       | Supported          |
|---------------|--------------------|
| `v11.x` (current) | ✅ active security fixes |
| `v10.x` and older | ❌ no fixes — please upgrade |

There is no LTS branch. Security fixes ship on the latest `main` line.

## Threat model & built-in mitigations

B12 is a local-first system; the threat model assumes the user's machine
itself is trusted, but acknowledges three realistic risks:

1. **Secret-honeypot risk.** If a user pastes
   `OPENAI_API_KEY=sk-...` into Claude Code, B12 captures it and surfaces
   it on every future search. The Phase 3 PII scrubber
   (`scripts/b12_pii_scrubber.py`) detects common secret patterns
   (`sk-ant-`, `sk-proj-`, `ghp_`, `xoxb-`, AWS access keys, Bearer
   tokens, JWT, generic `api_key=` / `password=` / `secret=`) and rewrites
   matches to `[REDACTED:<type>]` before INSERT. The scrubber can be
   disabled with `B12_DISABLE_PII_SCRUB=1` (not recommended).
2. **Supply-chain attack on Python deps.** B12 honors the
   "Mini Shai-Hulud" rule from the user's global Claude Code config:
   global `npm config set ignore-scripts true`, `resolution-mode=time-based`
   with `minimum-release-age=7d` in `~/.npmrc`, and a 4-step manual
   confirmation protocol before installing any new pip/npm dep. New B12
   dependencies must clear this protocol — see CONTRIBUTING.md.
3. **MCP tool poisoning.** The B12 MCP server only writes to local SQLite
   under `${B12_DATA_DIR:-~/.B12}/` and `${B12_HOOK_DIR:-~/.B12/hooks}/`.
   It does not make outbound network calls, does not execute shell
   commands from memory content, and does not load arbitrary code. The
   embedding daemon runs locally over a Unix socket — no remote attach.

## Out-of-scope reports

- Issues in the host application (Claude Code, Cursor, OpenCode, Zed,
  etc.) — please report those upstream.
- Issues that require an attacker to already have arbitrary code execution
  on the user's machine (B12 explicitly trusts the local user).
- Theoretical "what if SQLite has a 0day" reports without a concrete
  exploitation path against B12 specifically.

## Credits

A "Hall of Fame" section in CHANGELOG.md credits reporters of accepted
vulnerabilities, unless they request anonymity.
