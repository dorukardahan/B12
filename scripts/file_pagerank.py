"""File-graph PageRank (Plan §B3) — top-N likely-next-files by import-graph
centrality. Pure stdlib + numpy (no networkx; Mini Shai-Hulud surface).

Pipeline:
1. Walk project root, collect .py/.ts/.tsx/.js/.jsx (skip noisy dirs).
2. Regex-scan each for imports, resolve to project files via basename.
3. PageRank iterate (damping 0.85, ~30 iters) by SPARSE power iteration over
   the adjacency lists — O(nodes + edges) memory, never an n×n dense matrix.
4. Cache at ~/.B12/state/pagerank-<hash>.json; invalidate on
   git HEAD change OR cache age >24h.

Memory safety (the 2026-06 OOM fix):
- `_pagerank` is sparse: a dense n-by-n float32 matrix on a `$HOME`-sized root
  (~167k files) reserved ~112 GB per process and panicked the machine. The
  sparse form uses a few MB even for 100k+ nodes.
- `top_n` refuses roots above ``B12_PAGERANK_MAX_NODES`` (default 20000):
  it skips and logs rather than walking/ranking an unbounded tree.
- Run as a script, the process additionally self-limits via a SIGALRM
  wall-clock timeout (``B12_PAGERANK_TIMEOUT_S``, default 8s), ``os.setsid``
  (so a caller can group-kill it), and a best-effort ``RLIMIT_AS`` ceiling
  (``B12_PAGERANK_MAX_MEM_MB``, default 2048; a no-op on macOS, which does
  not let a process lower its own address-space limit).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time

# Above this many candidate files, refuse to rank (skip + log) rather than
# walk/read an unbounded tree. Env-overridable; <=0 disables the cap.
DEFAULT_MAX_NODES = 20000


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except (TypeError, ValueError):
        return default


def _log(msg: str) -> None:
    """Diagnostics go to stderr so they never pollute the top-N stdout that
    the SessionStart hook captures."""
    try:
        sys.stderr.write(f"[file_pagerank] {msg}\n")
        sys.stderr.flush()
    except Exception:
        pass

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
    """Sparse PageRank power iteration over adjacency lists.

    Equivalent to the former dense `rank = tp + damping * (M @ rank)` where M
    is column-stochastic (each source distributes weight 1/outdeg to its
    targets, duplicates accumulated), but it NEVER materializes the n×n M.
    Memory is O(nodes + edges): three 1-D edge arrays + one rank vector, so a
    100k-node graph costs a few MB instead of tens of GB. Verified to match
    the dense result to float rounding (top-N identical) — see
    scripts/tests/test_file_pagerank_oom.py.
    """
    nodes = list(adj.keys())
    if not nodes:
        return {}
    import numpy as np
    n = len(nodes)
    idx = {nm: i for i, nm in enumerate(nodes)}
    # Flatten edges to parallel (src, dst, weight) arrays. Weight is 1/outdeg
    # of the source; iterating the raw `dsts` (not a deduped set) preserves
    # the dense `+=` accumulation when a file resolves two imports to the
    # same target.
    src_list: list[int] = []
    dst_list: list[int] = []
    w_list: list[float] = []
    for src, dsts in adj.items():
        if not dsts:
            continue
        si = idx[src]
        w = 1.0 / len(dsts)
        for dst in dsts:
            di = idx.get(dst)
            if di is not None:
                src_list.append(si)
                dst_list.append(di)
                w_list.append(w)
    tp = (1.0 - damping) / n
    rank = np.full(n, 1.0 / n, dtype=np.float64)
    if src_list:
        src_arr = np.asarray(src_list, dtype=np.int64)
        dst_arr = np.asarray(dst_list, dtype=np.int64)
        w_arr = np.asarray(w_list, dtype=np.float64)
        for _ in range(iters):
            contrib = damping * w_arr * rank[src_arr]
            new = np.full(n, tp, dtype=np.float64)
            # Unbuffered scatter-add: handles repeated dst indices correctly
            # (plain `new[dst_arr] += contrib` would drop duplicates).
            np.add.at(new, dst_arr, contrib)
            rank = new
    else:
        # No edges: every node converges to the teleport mass `tp`.
        rank = np.full(n, tp, dtype=np.float64)
    return {nodes[i]: float(rank[i]) for i in range(n)}


def top_n(project_root: str, n: int = 5) -> list[str]:
    files = _walk(project_root)
    if not files:
        return []
    # Hard node cap: above this many candidate files, refuse to rank. This is
    # the in-process backstop to the SessionStart hook's pre-count guard — a
    # giant root (e.g. $HOME, ~167k files) would otherwise read every file and
    # build a graph large enough to matter. Bail BEFORE the import-read loop.
    max_nodes = _env_int("B12_PAGERANK_MAX_NODES", DEFAULT_MAX_NODES)
    if max_nodes > 0 and len(files) > max_nodes:
        _log(f"skip: {len(files)} candidate files > cap {max_nodes} "
             f"(set B12_PAGERANK_MAX_NODES to override); root={project_root!r}")
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


def _install_self_guards() -> None:
    """Belt-and-suspenders limits for the standalone process so that, however
    it is invoked (and even if it is orphaned when the SessionStart hook's own
    watchdog kills the parent shell mid command-substitution — the exact path
    that leaked a runaway numpy allocator and panicked the machine), it cannot
    run away.

    1. SIGALRM wall-clock timeout — orphan-proof; works on every POSIX OS.
    2. os.setsid() — become a process-group leader so a caller can group-kill
       us with `kill -- -<pid>` and never miss a child.
    3. RLIMIT_AS ceiling — a real backstop on Linux; a no-op on macOS, which
       refuses to let a process lower its own address-space soft limit
       ("current limit exceeds maximum limit"). Best-effort, never fatal.
    """
    timeout_s = _env_int("B12_PAGERANK_TIMEOUT_S", 8)
    if timeout_s > 0:
        try:
            import signal

            def _on_timeout(signum, frame):  # noqa: ANN001
                _log(f"self-timeout after {timeout_s}s — exiting")
                os._exit(0)  # 0: a missing hint is a non-event for the hook

            signal.signal(signal.SIGALRM, _on_timeout)
            signal.alarm(timeout_s)
        except (ValueError, OSError, AttributeError):
            pass  # no SIGALRM (e.g. non-main thread / Windows) — skip

    try:
        os.setsid()
    except OSError:
        pass  # already a group leader, or unsupported — fine

    max_mem_mb = _env_int("B12_PAGERANK_MAX_MEM_MB", 2048)
    if max_mem_mb > 0:
        try:
            import resource

            limit = max_mem_mb * 1024 * 1024
            _, hard = resource.getrlimit(resource.RLIMIT_AS)
            new_hard = hard if hard == resource.RLIM_INFINITY else min(hard, limit)
            resource.setrlimit(resource.RLIMIT_AS, (limit, new_hard))
        except (ValueError, OSError, ImportError):
            pass  # macOS rejects this; Linux honors it. Either way, non-fatal.


if __name__ == "__main__":
    _install_self_guards()
    root = sys.argv[1] if len(sys.argv) > 1 else "."
    k = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    for f in cached_top_n(root, k):
        print(f)
