import shutil
import tempfile
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _enable_pii_scrubbing_by_default(monkeypatch):
    monkeypatch.delenv("B12_DISABLE_PII_SCRUB", raising=False)


@pytest.fixture
def short_tmp_path():
    """A short temp dir for AF_UNIX sockets.

    Socket paths are limited to 104 bytes on macOS, and pytest's tmp_path there
    (/private/var/folders/...) is already too long, so daemons fail to bind.
    """
    path = Path(tempfile.mkdtemp(prefix="b12-", dir="/tmp" if Path("/tmp").is_dir() else None))
    yield path
    shutil.rmtree(path, ignore_errors=True)
