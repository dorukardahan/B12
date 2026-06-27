"""
B12 Memory Refine — dedup, merge, and score raw memory candidates.

Works with the embed daemon for embedding similarity. No external LLM calls.
Designed to be called from the MCP server's memory_refine tool.
"""
import json, os, socket, base64, struct
from typing import Any

try:
    from b12_pii_scrubber import scrub as scrub_pii
except Exception:  # pragma: no cover
    def scrub_pii(value: str) -> str:
        return value

# Daemon socket path (same as b12_mcp_server.py)
_UID = os.getuid() if hasattr(os, 'getuid') else os.getpid()
SOCK_PATH = f"/tmp/b12-embed-{_UID}.sock"


def daemon_request(op: str, **kwargs) -> dict | None:
    """Send JSON to embed_daemon via Unix socket. Returns parsed dict or None.
    Protocol matches b12_mcp_server.py: newline-delimited JSON."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        # 20s: encode_batch (BGE-M3) can run >10s; the former 5s timed out
        # mid-encode and silently dropped refine's embeddings (audit #9).
        s.settimeout(float(os.environ.get("B12_DAEMON_CLIENT_TIMEOUT", "20")))
        s.connect(SOCK_PATH)
        s.sendall((json.dumps({"op": op, **kwargs}) + "\n").encode())
        data = b""
        while b"\n" not in data:
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
        resp = json.loads(data.decode().strip())
        return resp if resp.get("ok") else None
    except Exception:
        return None
    finally:
        s.close()


def refine_candidates(candidates: list[dict], similarity_threshold: float = 0.85) -> list[dict]:
    """
    Refine a list of candidate memories.

    Each candidate is a dict with at least: {"content": str, "memory_type": str}
    Optional fields: "tags": str

    Returns refined list with added "quality_score" and "group_id" fields.
    Duplicates (similarity > threshold) are merged into the longest/best candidate.
    """
    if not candidates:
        return []
    candidates = [_sanitize_candidate(c) for c in candidates if c.get("content")]
    if not candidates:
        return []

    # Step 1: Get embeddings for all candidates
    texts = [c["content"] for c in candidates]
    resp = daemon_request("encode_batch", texts=texts)

    if not resp or not resp.get("embeddings"):
        # Daemon unavailable — fall back to basic text dedup
        return _fallback_dedup(candidates)

    embeddings = [base64.b64decode(e) for e in resp["embeddings"]]

    # Step 2: Compute pairwise similarity and group near-duplicates
    n = len(candidates)
    groups: list[list[int]] = []  # Each group is a list of indices
    assigned: set[int] = set()

    for i in range(n):
        if i in assigned:
            continue
        group = [i]
        assigned.add(i)
        for j in range(i + 1, n):
            if j in assigned:
                continue
            sim = _cosine_similarity(embeddings[i], embeddings[j])
            if sim >= similarity_threshold:
                group.append(j)
                assigned.add(j)
        groups.append(group)

    # Step 3: For each group, pick the best candidate and score it
    refined = []
    for group_id, group in enumerate(groups):
        group_candidates = [candidates[i] for i in group]

        # Pick the longest/most specific candidate as representative
        best = max(group_candidates, key=lambda c: _specificity_score(c["content"]))

        # Score quality
        quality = _quality_score(best["content"], len(group))

        result = {
            "content": best["content"],
            "memory_type": best.get("memory_type", "general"),
            "tags": best.get("tags", ""),
            "quality_score": round(quality, 2),
            "group_id": group_id,
            "group_size": len(group),
            "extraction_method": "refined",
        }
        refined.append(result)

    # Sort by quality score descending
    refined.sort(key=lambda x: x["quality_score"], reverse=True)
    return refined


def _cosine_similarity(emb_a: bytes, emb_b: bytes) -> float:
    """Compute cosine similarity between two float32 embedding byte arrays."""
    n_floats = len(emb_a) // 4
    a = struct.unpack(f"{n_floats}f", emb_a)
    b = struct.unpack(f"{n_floats}f", emb_b)
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _specificity_score(text: str) -> float:
    """Score how specific/concrete a memory candidate is. Higher = more specific."""
    import re
    score = len(text)  # Base: longer is usually more specific

    # Bonus for concrete indicators
    if re.search(r'[/\\][\w.-]+\.\w+', text):  # file paths
        score += 100
    if re.search(r'v?\d+\.\d+', text):  # version numbers
        score += 80
    if re.search(r'`[^`]+`', text):  # inline code
        score += 60
    if re.search(r'\b(?:npm|pip|brew|cargo|docker|git|kubectl|yarn|bun|python|node)\b', text, re.I):
        score += 50  # tool names
    if re.search(r'\b[A-Z][a-z]+(?:[A-Z][a-z]+)+\b', text):  # CamelCase (class/function names)
        score += 40

    return score


def _quality_score(text: str, group_size: int) -> float:
    """Score overall quality of a memory candidate. Returns 0.0-1.0."""
    import re
    score = 0.0

    # Length scoring (too short = low quality, too long = potentially unfocused)
    text_len = len(text)
    if text_len < 20:
        score += 0.1
    elif text_len < 50:
        score += 0.3
    elif text_len < 200:
        score += 0.5
    elif text_len < 500:
        score += 0.4
    else:
        score += 0.3  # very long might be unfocused

    # Specificity indicators
    if re.search(r'[/\\][\w.-]+\.\w+', text): score += 0.1  # file paths
    if re.search(r'v?\d+\.\d+', text): score += 0.1  # versions
    if re.search(r'`[^`]+`', text): score += 0.05  # code references
    if re.search(r'\bbecause\b|\bdue to\b|\bçünkü\b|\bnedeniyle\b', text, re.I): score += 0.1  # reasoning

    # Dedup bonus: unique candidates score higher (group_size == 1 means unique)
    if group_size == 1:
        score += 0.15
    elif group_size == 2:
        score += 0.05
    # 3+ duplicates = no bonus (redundant info)

    return min(score, 1.0)


def _scrub_tags(value) -> str:
    if isinstance(value, (list, tuple)):
        return ",".join(scrub_pii(str(tag)) for tag in value)
    return scrub_pii(str(value or ""))


def _sanitize_candidate(candidate: dict) -> dict:
    safe = dict(candidate)
    safe["content"] = scrub_pii(str(candidate.get("content", "")))
    safe["memory_type"] = scrub_pii(str(candidate.get("memory_type", "general")))
    safe["tags"] = _scrub_tags(candidate.get("tags", ""))
    return safe


def _fallback_dedup(candidates: list[dict]) -> list[dict]:
    """Fallback dedup when daemon is unavailable. Uses simple text similarity."""
    seen_prefixes: set[str] = set()
    refined = []
    for i, raw in enumerate(candidates):
        c = _sanitize_candidate(raw)
        prefix = c["content"][:80].lower().strip()
        if prefix not in seen_prefixes:
            seen_prefixes.add(prefix)
            quality = _quality_score(c["content"], 1)
            refined.append({
                "content": c["content"],
                "memory_type": c.get("memory_type", "general"),
                "tags": c.get("tags", ""),
                "quality_score": round(quality, 2),
                "group_id": i,
                "group_size": 1,
                "extraction_method": "refined",
            })
    return refined
