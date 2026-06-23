// PII / secret scrubber for the OpenCode plugin write path.
//
// FAITHFUL PORT of scripts/b12_pii_scrubber.py — KEEP THE TWO IN SYNC. Any pattern
// added/changed there must be mirrored here (and vice-versa); scrubber.test.ts has
// a parity-style suite over the same credential shapes. Like the Python module, this
// redacts matches to `[REDACTED:<label>]` BEFORE a row is persisted, and honors the
// `B12_DISABLE_PII_SCRUB=1` opt-out. Conservative by design — false positives beat
// leaks.

export const IMPORTANCE_BASELINE = 0.5;

interface SecretPattern {
  label: string;
  re: RegExp;
}

// Order matters (most specific first), mirroring b12_pii_scrubber._PATTERNS. All
// regexes are global (`g`) so every occurrence is redacted; `i` mirrors the Python
// `(?i)` where present.
const PATTERNS: SecretPattern[] = [
  { label: "anthropic", re: /\bsk-ant-[A-Za-z0-9_\-]{40,}\b/g },
  { label: "openai_project", re: /\bsk-proj-[A-Za-z0-9_\-]{40,}\b/g },
  { label: "github_pat", re: /\bghp_[A-Za-z0-9]{36,}\b/g },
  { label: "github_fg", re: /\bgithub_pat_[A-Za-z0-9_]{50,}\b/g },
  { label: "slack_bot", re: /\bxoxb-[0-9]{10,}-[0-9]{10,}-[A-Za-z0-9]{20,}\b/g },
  { label: "openai", re: /\bsk-[A-Za-z0-9]{40,}\b/g },
  { label: "aws_access", re: /\b(?:AKIA|ASIA)[A-Z0-9]{16}\b/g },
  // aws_secret + generic capture the secret VALUE in their LAST group; the
  // keyword/prefix is kept and only the value is redacted (see VALUE_GROUP below).
  {
    label: "aws_secret",
    re: /aws[_\-]?secret[_\-]?(?:access[_\-]?)?key\s*[=:]\s*['"]?([A-Za-z0-9/+=]{40})['"]?/gi,
  },
  { label: "bearer", re: /\bBearer\s+[A-Za-z0-9_\-.]{20,}\b/gi },
  { label: "jwt", re: /\beyJ[A-Za-z0-9_\-]+\.eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\b/g },
  { label: "google_api", re: /\bAIza[A-Za-z0-9_\-]{35}\b/g },
  { label: "stripe", re: /\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{20,}\b/g },
  {
    // JS-side hardening: the block span is bounded to 16 KB so that a large
    // malformed paste with many BEGIN headers and no matching END can't make the
    // lazy scan retry to end-of-string from each header (a ~20s stall on a ~1 MB
    // paste was measured; the bound keeps it sub-second). A real PEM private key
    // is well under 16 KB. (Python's b12_importance uses a bounded header-only
    // PEM check for the same reason; this runs on OpenCode lifecycle writes.)
    label: "pem_private_key",
    re: /-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED |PGP )?PRIVATE KEY(?: BLOCK)?-----[\s\S]{0,16384}?-----END (?:RSA |EC |OPENSSH |DSA |ENCRYPTED |PGP )?PRIVATE KEY(?: BLOCK)?-----/g,
  },
  {
    label: "db_uri",
    re: /\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqps?):\/\/[^:\s/@]+:[^@\s/]+@[^\s'"]+/g,
  },
  {
    // Leading boundary: Python's `\b` is Unicode-aware, but JS `\b` is ASCII-only
    // and would fail before the non-ASCII Turkish keyword `şifre`. A negative
    // lookbehind for an ASCII word char is the faithful equivalent — it matches at
    // string start / after whitespace / after punctuation for ASCII *and* `ş`.
    //
    // Turkish dotted-I: JS `/i` does NOT case-fold the uppercase dotted `İ`
    // (U+0130) to ASCII `i`, so all-caps `ŞİFRE` / `GİZLİ ANAHTAR` would slip
    // through (Python's Unicode `re.IGNORECASE` folds them). The `[iİ]` classes on
    // the Turkish keywords restore that coverage. (`Ş`→`ş` and `İ`'s siblings the
    // ASCII keywords need are already folded by `/i`.)
    label: "generic",
    re: /(?<![A-Za-z0-9_])(api[_\-]?key|password|passwd|secret|token|parola|ş[iİ]fre|s[iİ]fre|g[iİ]zl[iİ][_\- ]?anahtar)\s*[=:]\s*['"]?([A-Za-z0-9_\-+=/.]{12,})['"]?/gi,
  },
];

// Patterns whose LAST capture group is the secret VALUE. Mirrors the Python
// `_replace`: keep everything up to the value, redact only the value (the trailing
// quote, if any, is dropped — same as Python).
const VALUE_GROUP: Record<string, number> = { aws_secret: 1, generic: 2 };

function scrubDisabled(): boolean {
  const v =
    (typeof process !== "undefined" && process.env && process.env.B12_DISABLE_PII_SCRUB) || "";
  return ["1", "true", "yes"].includes(String(v).toLowerCase());
}

/**
 * Return `content` with known secret patterns redacted to `[REDACTED:<label>]`.
 * Honors B12_DISABLE_PII_SCRUB=1 (raw capture opt-out), exactly like the Python.
 */
export function scrubSecrets(content: string): string {
  if (!content) return content;
  if (scrubDisabled()) return content;
  let out = content;
  for (const { label, re } of PATTERNS) {
    const valueGroup = VALUE_GROUP[label];
    out = out.replace(re, (match: string, ...rest: unknown[]) => {
      if (valueGroup) {
        const value = rest[valueGroup - 1] as string | undefined;
        if (value) {
          const vStart = match.indexOf(value);
          if (vStart >= 0) return match.slice(0, vStart) + `[REDACTED:${label}]`;
        }
      }
      return `[REDACTED:${label}]`;
    });
  }
  return out;
}

/**
 * True if `content` looks credential-bearing — any secret pattern matches, or the
 * scrubber's own `[REDACTED:…]` marker is already present. Detection ignores the
 * B12_DISABLE_PII_SCRUB opt-out: the importance cap must still apply even when raw
 * capture is enabled (mirrors b12_importance.is_secret).
 */
export function isSecret(content: string): boolean {
  if (!content) return false;
  if (content.includes("[REDACTED:")) return true;
  for (const { re } of PATTERNS) {
    re.lastIndex = 0;
    const hit = re.test(content);
    re.lastIndex = 0;
    if (hit) return true;
  }
  return false;
}
