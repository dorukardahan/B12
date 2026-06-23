import { test, expect, describe } from "bun:test";
import { scrubSecrets, isSecret, IMPORTANCE_BASELINE } from "../src/lib/scrubber.js";

// Parity-style suite over the same credential shapes as scripts/b12_pii_scrubber.py.
// Fake secrets only — these are pattern fixtures, never real keys.
describe("scrubSecrets", () => {
  test("redacts no-group patterns to [REDACTED:label]", () => {
    const cases: Array<[string, string]> = [
      ["sk-ant-" + "A".repeat(50), "anthropic"],
      ["sk-proj-" + "B".repeat(50), "openai_project"],
      ["ghp_" + "c".repeat(40), "github_pat"],
      ["github_pat_" + "d".repeat(55), "github_fg"],
      ["xoxb-1234567890-1234567890-" + "E".repeat(25), "slack_bot"],
      ["sk-" + "F".repeat(45), "openai"],
      ["AKIA" + "ABCDEFGHIJKLMNOP", "aws_access"],
      ["Bearer " + "g".repeat(30), "bearer"],
      ["eyJabc.eyJdef.ghi123", "jwt"],
      ["AIza" + "h".repeat(35), "google_api"],
      ["sk_live_" + "i".repeat(24), "stripe"],
    ];
    for (const [raw, label] of cases) {
      const out = scrubSecrets(`value: ${raw} end`);
      expect(out).toContain(`[REDACTED:${label}]`);
      expect(out).not.toContain(raw);
    }
  });

  test("keeps the prefix and redacts only the value (grouped patterns)", () => {
    const aws = scrubSecrets('aws_secret_access_key="' + "Z".repeat(40) + '"');
    expect(aws).toContain("aws_secret_access_key=");
    expect(aws).toContain("[REDACTED:aws_secret]");
    expect(aws).not.toContain("Z".repeat(40));

    const generic = scrubSecrets("api_key=SuperSecretValue123");
    expect(generic).toContain("api_key=");
    expect(generic).toContain("[REDACTED:generic]");
    expect(generic).not.toContain("SuperSecretValue123");
  });

  test("redacts a PEM private-key block", () => {
    const pem =
      "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\nDEFghi\n-----END RSA PRIVATE KEY-----";
    const out = scrubSecrets(pem);
    expect(out).toContain("[REDACTED:pem_private_key]");
    expect(out).not.toContain("MIIabc");
  });

  test("redacts a DB URI carrying inline credentials but leaves clean URIs alone", () => {
    expect(scrubSecrets("postgres://user:p4ss@db.example/app")).toContain(
      "[REDACTED:db_uri]",
    );
    const clean = "postgres://db.example/app";
    expect(scrubSecrets(clean)).toBe(clean);
  });

  test("Turkish credential keywords fire the generic pattern", () => {
    const out = scrubSecrets("şifre: HunterTwoSecret42");
    expect(out).toContain("[REDACTED:generic]");
  });

  test("leaves ordinary content untouched", () => {
    const plain = "we shipped the migration on 2026-07-01, see PR#42";
    expect(scrubSecrets(plain)).toBe(plain);
  });

  test("honors B12_DISABLE_PII_SCRUB", () => {
    const raw = "ghp_" + "k".repeat(40);
    process.env.B12_DISABLE_PII_SCRUB = "1";
    try {
      expect(scrubSecrets(raw)).toBe(raw); // raw capture opt-out
    } finally {
      delete process.env.B12_DISABLE_PII_SCRUB;
    }
  });
});

describe("isSecret", () => {
  test("detects credential shapes and the [REDACTED:] marker", () => {
    expect(isSecret("ghp_" + "m".repeat(40))).toBe(true);
    expect(isSecret("token=[REDACTED:generic]")).toBe(true);
    expect(isSecret("just a normal note about the weather")).toBe(false);
    expect(isSecret("")).toBe(false);
  });

  test("detects even when scrubbing is disabled (cap must still apply)", () => {
    process.env.B12_DISABLE_PII_SCRUB = "1";
    try {
      expect(isSecret("sk-ant-" + "n".repeat(50))).toBe(true);
    } finally {
      delete process.env.B12_DISABLE_PII_SCRUB;
    }
  });
});

test("IMPORTANCE_BASELINE is 0.5 (matches the Python band)", () => {
  expect(IMPORTANCE_BASELINE).toBe(0.5);
});
