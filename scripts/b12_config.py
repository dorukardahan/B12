"""B12 user-config reader (~/.B12/config.toml). Stdlib-only.

Returns sensible defaults when the file is missing, malformed, or any key
is absent — hooks and daemons must remain functional with no config at all.

Usage:
    from b12_config import get
    if get("recall", "ann", "enabled", default=False):
        ...
"""
from __future__ import annotations

import os
from functools import lru_cache

try:
    import tomllib  # Py3.11+
except ImportError:  # pragma: no cover
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ImportError:
        tomllib = None  # type: ignore[assignment]


@lru_cache(maxsize=1)
def _load() -> dict:
    path = os.path.join(
        os.environ.get("B12_DATA_DIR", os.path.expanduser("~/.B12")),
        "config.toml",
    )
    if not tomllib or not os.path.isfile(path):
        return {}
    try:
        with open(path, "rb") as fh:
            return tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):  # type: ignore[attr-defined]
        return {}


def get(*path: str, default=None):
    """Dotted-path lookup with default. e.g. get('recall', 'ann', 'enabled')."""
    node = _load()
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return default
        node = node[key]
    return node
