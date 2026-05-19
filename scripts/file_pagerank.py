"""File-graph PageRank (Plan §B3) — top-N likely-next-files by import-graph
centrality. Pure stdlib + numpy (no networkx; Mini Shai-Hulud surface).

Pipeline:
1. Walk project root, collect .py/.ts/.tsx/.js/.jsx (skip noisy dirs).
2. Regex-scan each for imports, resolve to project files via basename.
3. PageRank iterate (damping 0.85, ~30 iters) on the adjacency matrix.
4. Cache at ~/.B12/state/pagerank-<hash>.json; invalidate on
   git HEAD change OR cache age >24h.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time

_IMPORT_RES = [
    re.compile(r"^\s*from\s+([\w\.]+)\s+import\s+", re.MULTILINE),
    re.compile(r"^\s*import\s+([\w\.]+)", re.MULTILINE),
    re.compile(r"""require\(\s*['\"]([^'\"]+)['\"]\s*\)"""),
    re.compile(r"""^\s*import\s+(?:[^'\"]+from\s+)?['\"]([^'\"]+)['\"]""", re.MULTILINE),
]
_EXTS = (".py", ".ts", ".tsx", ".js", ".jsx")
_SKIP = {".git", "node_modules", ".venv", "venv", "__pycache__",
         ".pytest_cache", "dist", "build", ".next", "target"}
_STDLIB_SKIP = {"os", "sys", "json", "re", "time", "math", "io", "subprocess"}


def _walk(root: str) -> list[str]:
    out: list[str] = []
    for dp, dn, fn in os.walk(root):
        dn[:] = [d for d in dn if d not in _SKIP and not d.startswith(".")]
        out.extend(os.path.join(dp, f) for f in fn if f.endswith(_EXTS))
    return out


def _imports(path: str) -> list[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            text = fh.read(60_000)
    except OSError:
        return []
    seen: list[str] = []
    for rx in _IMPORT_RES:
        for m in rx.finditer(text):
            mod = m.group(1)
            if mod and mod not in seen:
                seen.append(mod)
    return seen


def _resolve(mod: str, by_basename: dict[str, list[str]]) -> str | None:
    base = mod.lstrip(".").split(".")[0].split("/")[-1]
    if not base or base in _STDLIB_SKIP:
        return None
    cands = by_basename.get(base)
    return cands[0] if cands else None


def _pagerank(adj: dict[str, list[str]], damping=0.85, iters=30) -> dict[str, float]:
    nodes = list(adj.keys())
    if not nodes:
        return {}
    import numpy as np
    n = len(nodes)
    idx = {nm: i for i, nm in enumerate(nodes)}
    M = np.zeros((n, n), dtype=np.float32)
    for src, dsts in adj.items():
        if not dsts:
            continue
        w = 1.0 / len(dsts)
        for dst in dsts:
            if dst in idx:
                M[idx[dst], idx[src]] += w
    rank = np.full(n, 1.0 / n, dtype=np.float32)
    tp = (1.0 - damping) / n
    for _ in range(iters):
        rank = tp + damping * (M @ rank)
    return {nodes[i]: float(rank[i]) for i in range(n)}


def top_n(project_root: str, n: int = 5) -> list[str]:
    files = _walk(project_root)
    if not files:
        return []
    by_basename: dict[str, list[str]] = {}
    for f in files:
        by_basename.setdefault(os.path.splitext(os.path.basename(f))[0], []).append(f)
    adj: dict[str, list[str]] = {f: [] for f in files}
    for f in files:
        for mod in _imports(f):
            tgt = _resolve(mod, by_basename)
            if tgt and tgt != f:
                adj[f].append(tgt)
    ranked = sorted(_pagerank(adj).items(), key=lambda kv: kv[1], reverse=True)
    return [os.path.relpath(p, project_root) for p, _ in ranked[:n]]


def cached_top_n(project_root: str, n: int = 5, max_age_s: int = 86400) -> list[str]:
    cache_dir = os.path.join(
        os.environ.get("B12_DATA_DIR", os.path.expanduser("~/.B12")), "state")
    os.makedirs(cache_dir, exist_ok=True)
    key = hashlib.sha256(project_root.encode()).hexdigest()[:12]
    cache = os.path.join(cache_dir, f"pagerank-{key}.json")
    head = ""
    try:
        head = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=project_root,
            stderr=subprocess.DEVNULL, timeout=2).decode().strip()
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        pass
    if os.path.isfile(cache):
        try:
            with open(cache) as fh:
                d = json.load(fh)
            if (d.get("head") == head and (time.time() - d.get("ts", 0)) < max_age_s
                    and d.get("n", 0) >= n):
                return d.get("top", [])[:n]
        except (OSError, json.JSONDecodeError):
            pass
    top = top_n(project_root, n)
    try:
        with open(cache, "w") as fh:
            json.dump({"head": head, "ts": time.time(), "n": n, "top": top}, fh)
    except OSError:
        pass
    return top


if __name__ == "__main__":
    import sys
    root = sys.argv[1] if len(sys.argv) > 1 else "."
    k = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    for f in cached_top_n(root, k):
        print(f)
