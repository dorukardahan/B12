"""Unit tests for b12_pii_scrubber.scrub.

Run via:  python3 -m pytest scripts/tests/test_b12_pii_scrubber.py -v
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from b12_pii_scrubber import scrub


def test_anthropic_sk_ant_redacted():
    src = "key is sk-ant-api03-Abc123XYZdef456ghi789jklmnoPQRSTUVWXYZ-abcdefg_HIJKLMNOP"
    out = scrub(src)
    assert "[REDACTED:anthropic]" in out
    assert "sk-ant-api03" not in out


def test_openai_project_key_redacted():
    src = "OPENAI_API_KEY=sk-proj-AbcDefGhi123456jklMnoPQRSTUVwxyz789abcDEFGHIJ"
    out = scrub(src)
    assert "[REDACTED:openai_project]" in out
    assert "sk-proj-AbcDefGhi123456jklMnoPQRSTUVwxyz789abcDEFGHIJ" not in out


def test_github_pat_redacted():
    src = "use ghp_AbcDefGhiJklMnoPqrStUvWxYz0123456789 for CI"
    out = scrub(src)
    assert "[REDACTED:github_pat]" in out
    assert "ghp_" not in out


def test_slack_bot_token_redacted():
    src = "SLACK_BOT_TOKEN=xoxb-1234567890-9876543210-AbcDefGhiJklMnoPqrSt"
    out = scrub(src)
    assert "[REDACTED:slack_bot]" in out
    assert "xoxb-1234567890-9876543210-AbcDefGhiJklMnoPqrSt" not in out


def test_aws_access_key_redacted():
    src = "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE"
    out = scrub(src)
    assert "[REDACTED:aws_access]" in out
    assert "AKIA" not in out


def test_aws_secret_key_redacted():
    src = 'aws_secret_access_key = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"'
    out = scrub(src)
    assert "[REDACTED:aws_secret]" in out
    assert "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY" not in out


def test_bearer_token_redacted():
    src = 'curl -H "Authorization: Bearer abc123def456ghi789jklmnopqr"'
    out = scrub(src)
    assert "[REDACTED:bearer]" in out
    assert "abc123def456ghi789jklmnopqr" not in out


def test_jwt_redacted():
    src = "session token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTYifQ.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c after login"
    out = scrub(src)
    assert "[REDACTED:jwt]" in out
    assert "eyJhbGciOiJIUzI1NiJ9" not in out


def test_generic_api_key_pattern_redacted():
    src = "api_key=plaintext_secret_value_here_long_enough"
    out = scrub(src)
    assert "[REDACTED:generic]" in out
    assert "plaintext_secret_value_here_long_enough" not in out


def test_true_negative_env_var_reference():
    # Mentioning OPENAI_API_KEY without a value should NOT be redacted.
    src = "set OPENAI_API_KEY in your env file before running"
    out = scrub(src)
    assert out == src, f"unchanged content was modified: {out!r}"


def test_google_api_key_redacted():
    # Real Google API keys are "AIza" + 35 chars = 39 total.
    src = "GOOGLE_API_KEY=AIza" + "SyD0123456789abcdefghijklmnopqrstuv" + " end"
    out = scrub(src)
    assert "[REDACTED:google_api]" in out
    assert "AIzaSyD0123456789" not in out


def test_stripe_secret_key_redacted():
    # Build the fixture by concatenation so no contiguous key literal lands in
    # source (avoids tripping GitHub secret-scanning push protection); the
    # regex still matches the runtime-joined value.
    key = "sk_" + "live_" + "51AbcDefGhiJklMnoPqrStUv0123"
    src = f"STRIPE_KEY={key} done"
    out = scrub(src)
    assert "[REDACTED:stripe]" in out
    assert key not in out


def test_pem_private_key_block_redacted():
    src = (
        "before\n-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEA1234567890abcdef\nlinetwo\n"
        "-----END RSA PRIVATE KEY-----\nafter"
    )
    out = scrub(src)
    assert "[REDACTED:pem_private_key]" in out
    assert "MIIEowIBAAKCAQEA" not in out
    # surrounding non-secret text preserved
    assert "before" in out and "after" in out


def test_pem_encrypted_pkcs8_block_redacted():
    # PKCS#8 encrypted keys (ssh-keygen -m PKCS8 / OpenSSL) use "ENCRYPTED".
    src = (
        "x\n-----BEGIN ENCRYPTED PRIVATE KEY-----\n"
        "MIIFHzBJBgkq1234567890abcdef\nbody\n"
        "-----END ENCRYPTED PRIVATE KEY-----\ny"
    )
    out = scrub(src)
    assert "[REDACTED:pem_private_key]" in out
    assert "MIIFHzBJBgkq" not in out


def test_pem_pgp_private_key_block_redacted():
    # GPG armored secret-key export: "PGP PRIVATE KEY BLOCK".
    src = (
        "x\n-----BEGIN PGP PRIVATE KEY BLOCK-----\n"
        "lQVYBGabcdef1234567890\nbody\n"
        "-----END PGP PRIVATE KEY BLOCK-----\ny"
    )
    out = scrub(src)
    assert "[REDACTED:pem_private_key]" in out
    assert "lQVYBGabcdef" not in out


def test_db_uri_with_credentials_redacted():
    src = "DATABASE_URL=postgres://admin:s3cretPassw0rd@db.internal:5432/app"
    out = scrub(src)
    assert "[REDACTED:db_uri]" in out
    assert "s3cretPassw0rd" not in out


def test_db_uri_without_credentials_not_redacted():
    # No user:pass@ segment → must NOT be redacted.
    src = "connect to postgres://localhost:5432/app for local dev"
    out = scrub(src)
    assert out == src, f"credential-free URI was modified: {out!r}"


def test_turkish_credential_keywords_redacted():
    for src in (
        "parola=cokGizliParola123",
        "şifre: superSecretValue99",
        "gizli anahtar = abcdef1234567890",
    ):
        out = scrub(src)
        assert "[REDACTED:generic]" in out, f"not redacted: {src!r} -> {out!r}"


def test_version_string_not_redacted():
    # A dotted version number must not trip the generic/secret patterns.
    src = "upgraded to version=1.2.3 and host localhost:5432/app"
    out = scrub(src)
    assert out == src, f"benign string was modified: {out!r}"


def test_disable_env_var_skips_scrub():
    src = "OPENAI_API_KEY=sk-proj-AbcDefGhi123456jklMnoPQRSTUVwxyz789abcDEFGHIJ"
    previous = os.environ.get("B12_DISABLE_PII_SCRUB")
    os.environ["B12_DISABLE_PII_SCRUB"] = "1"
    try:
        out = scrub(src)
        assert out == src
    finally:
        if previous is None:
            os.environ.pop("B12_DISABLE_PII_SCRUB", None)
        else:
            os.environ["B12_DISABLE_PII_SCRUB"] = previous


if __name__ == "__main__":
    import sys
    rc = 0
    fns = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    for fn in fns:
        try:
            fn()
            print(f"OK: {fn.__name__}")
        except AssertionError as e:
            print(f"FAIL: {fn.__name__}: {e}")
            rc = 1
    sys.exit(rc)
