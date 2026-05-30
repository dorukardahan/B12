"""B12 PII / secret scrubber.

Memory ingestion is a great honeypot: a user pastes `OPENAI_API_KEY=sk-...`
into Claude Code, B12 captures the line as a "decision" and surfaces it on
every future search. This module runs a fast regex sweep on every write
and replaces matches with `[REDACTED:<type>]` BEFORE the row hits SQLite.

Conservative by design — false positives are preferable to leaks. The
sweep is gated by `B12_DISABLE_PII_SCRUB=1` for users who explicitly
want raw capture (debugging the daemon, etc).

Public API:
    scrub(content: str) -> str
"""
import os
import re

# Pattern catalog. Order matters for `re.sub` chaining — most specific first.
_PATTERNS = [
    # Anthropic API keys (sk-ant-...)
    ("anthropic", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{40,}\b")),
    # OpenAI project API keys (sk-proj-...)
    ("openai_project", re.compile(r"\bsk-proj-[A-Za-z0-9_\-]{40,}\b")),
    # GitHub personal access tokens
    ("github_pat", re.compile(r"\bghp_[A-Za-z0-9]{36,}\b")),
    # GitHub fine-grained PATs
    ("github_fg", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{50,}\b")),
    # Slack bot tokens
    ("slack_bot", re.compile(r"\bxoxb-[0-9]{10,}-[0-9]{10,}-[A-Za-z0-9]{20,}\b")),
    # OpenAI generic (sk-...)
    ("openai", re.compile(r"\bsk-[A-Za-z0-9]{40,}\b")),
    # AWS access keys (AKIA / ASIA)
    ("aws_access", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    # AWS secret keys (40-char base64-ish, only when preceded by aws_secret context)
    ("aws_secret", re.compile(
        r"(?i)aws[_\-]?secret[_\-]?(?:access[_\-]?)?key\s*[=:]\s*['\"]?([A-Za-z0-9/+=]{40})['\"]?"
    )),
    # Bearer tokens in headers (HTTP auth schemes are case-insensitive per RFC 7235)
    ("bearer", re.compile(r"(?i)\bBearer\s+[A-Za-z0-9_\-.]{20,}\b")),
    # JWT (three base64url segments)
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]+\.eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\b")),
    # Google API keys (AIza…)
    ("google_api", re.compile(r"\bAIza[A-Za-z0-9_\-]{35}\b")),
    # Stripe secret / restricted keys (sk_live_/sk_test_/rk_live_/rk_test_)
    ("stripe", re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{20,}\b")),
    # PEM private-key blocks (RSA/EC/OpenSSH/DSA/PKCS#8-ENCRYPTED/PGP) — multi-line,
    # redact whole block. `ENCRYPTED` covers PKCS#8 (ssh-keygen -m PKCS8 / OpenSSL
    # encrypted keys); optional ` BLOCK` covers GPG armored `PGP PRIVATE KEY BLOCK`.
    ("pem_private_key", re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED |PGP )?PRIVATE KEY(?: BLOCK)?-----"
        r"[\s\S]*?-----END (?:RSA |EC |OPENSSH |DSA |ENCRYPTED |PGP )?PRIVATE KEY(?: BLOCK)?-----"
    )),
    # DB connection strings carrying inline credentials (scheme://user:pass@host).
    # Requires the `user:pass@` segment so credential-free URIs are left alone.
    ("db_uri", re.compile(
        r"\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqps?)://"
        r"[^:\s/@]+:[^@\s/]+@[^\s'\"]+"
    )),
    # Generic `api_key=...`, `password=...`, `secret=...` — fallback catch.
    # Bilingual (EN + TR) credential keywords per the B12 language rule.
    # Limit length so we don't redact short config keys without secret payload.
    ("generic", re.compile(
        r"(?i)\b(api[_\-]?key|password|passwd|secret|token"
        r"|parola|şifre|sifre|gizli[_\- ]?anahtar)\s*[=:]\s*['\"]?([A-Za-z0-9_\-+=/.]{12,})['\"]?"
    )),
]


def _replace(label: str):
    """Build a re.sub replacement that respects single-group patterns."""
    def repl(m):
        # For patterns with a captured secret group (aws_secret, generic),
        # keep the prefix and only mask the value.
        if m.groups():
            prefix = m.string[m.start():m.start(m.lastindex or 1)]
            return prefix + f"[REDACTED:{label}]"
        return f"[REDACTED:{label}]"
    return repl


def scrub(content: str) -> str:
    """Return content with known secret patterns redacted.

    Honors `B12_DISABLE_PII_SCRUB=1` env var as an explicit opt-out for
    callers who want raw capture (rare; usually for daemon debug).
    """
    if not content:
        return content
    if os.environ.get("B12_DISABLE_PII_SCRUB", "").lower() in ("1", "true", "yes"):
        return content
    out = content
    for label, pat in _PATTERNS:
        out = pat.sub(_replace(label), out)
    return out
