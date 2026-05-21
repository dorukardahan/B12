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
