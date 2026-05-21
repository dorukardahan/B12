import pytest


@pytest.fixture(autouse=True)
def _enable_pii_scrubbing_by_default(monkeypatch):
    monkeypatch.delenv("B12_DISABLE_PII_SCRUB", raising=False)
