#!/usr/bin/env python3
"""
B12 LLM extraction subagent — main module + CLI entry.

Reads a session transcript, asks a configured LLM provider for durable
memories, validates output, re-scores importance via b12_importance,
deduplicates against same-session DB rows, and writes through
write_time_merge.merge_or_insert so semantic dedup catches paraphrase
overlap with the regex pipeline.

Architecture summary (see docs/B12_llm_extraction_design.md):

    SessionEnd hook ─┐
                     │
                     ├─→ embed daemon (existing, unchanged)
                     │
                     └─→ b12_llm_extractor (NEW, fire-and-forget)
                            │
                            ├─→ normalize_transcript via b12_llm_prompts
                            ├─→ provider.extract(SYSTEM_PROMPT, transcript)
                            ├─→ validate_extraction per line
                            ├─→ same-session DB dedup on
                            │      (source_session=sid[:12], content_hash)
                            └─→ merge_or_insert (semantic dedup ⇒ merge)

Always exits 0. Internal failures are appended to
~/.B12/memory-logs/llm-extraction-errors.log.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

# Local-dir imports: this file lives in scripts/ in the repo and in
# ~/.B12/hooks/scripts/ when deployed. Both layouts put siblings on
# sys.path automatically when invoked as a script; do it explicitly
# for safety when imported as a module.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)


# ── Defaults / env knobs ────────────────────────────────────────────

_DEFAULT_TIMEOUT_S = 60
_DEFAULT_MAX_MEMORIES = 10
_DEFAULT_TRANSCRIPT_CAP = 50000
_OLLAMA_TRANSCRIPT_CAP = 25000

_TAG_LLM = "llm-extracted"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default


def _resolve_caps(provider_name: str) -> int:
    """Provider-specific transcript char cap.

    Ollama's default small models (Qwen 2.5 1.5B) have a 32K context;
    cap conservatively at 25K. Anthropic Haiku gets the full 50K
    default. Both can be overridden via B12_LLM_TRANSCRIPT_CAP_CHARS.
    """
    env_cap = os.environ.get("B12_LLM_TRANSCRIPT_CAP_CHARS")
    if env_cap:
        try:
            return max(1000, int(env_cap))
        except ValueError:
            pass
    if provider_name == "ollama":
        return _OLLAMA_TRANSCRIPT_CAP
    return _DEFAULT_TRANSCRIPT_CAP


# ── Error log ──────────────────────────────────────────────────────


def _error_log_path() -> Path:
    base = os.environ.get("B12_DATA_DIR") or os.path.join(
        os.path.expanduser("~"), ".B12"
    )
    p = Path(base) / "memory-logs" / "llm-extraction-errors.log"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _log_error(msg: str, exc: BaseException | None = None) -> None:
    try:
        path = _error_log_path()
        with path.open("a", encoding="utf-8") as f:
            f.write(f"[{datetime.now(timezone.utc).isoformat()}] {msg}\n")
            if exc is not None:
                f.write(traceback.format_exception_only(type(exc), exc)[-1])
                f.write(traceback.format_exc())
                f.write("\n")
    except OSError:
        # Logging itself must never raise.
        pass


# ── Embedding ──────────────────────────────────────────────────────


# Module-level cache: SentenceTransformer instantiation is ~2-3s + ~90MB.
# Loading inside the per-candidate loop would multiply that by max_memories,
# pushing the background worker well past its 60s timeout.
_MODEL_CACHE: dict = {}  # str → SentenceTransformer (loose typing, model lives in optional dep)


def _get_embedding_model():
    """Lazy-load and cache the SentenceTransformer for this process.

    Returns the model on success, or None if sentence-transformers is
    unavailable. Cache key is the model name so MCP_EMBEDDING_MODEL
    overrides still get a fresh model when users switch.
    """
    try:
        import warnings as _w
        _w.filterwarnings("ignore")
        os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
        from sentence_transformers import SentenceTransformer
    except Exception:
        return None
    model_name = os.environ.get(
        "MCP_EMBEDDING_MODEL", "paraphrase-multilingual-MiniLM-L12-v2"
    )
    cached = _MODEL_CACHE.get(model_name)
    if cached is not None:
        return cached
    try:
        model = SentenceTransformer(model_name, device="cpu")
    except Exception:
        return None
    _MODEL_CACHE[model_name] = model
    return model


def _encode_embedding(text: str) -> bytes | None:
    """Compute a 384-dim float32 embedding for a single string.

    Returns None if sentence-transformers is unavailable. Uses the
    module-level model cache so repeated calls within one worker
    pay the model-load cost exactly once.
    """
    model = _get_embedding_model()
    if model is None:
        return None
    try:
        import numpy as np
        emb = model.encode([text], convert_to_numpy=True)[0]
        emb = np.asarray(emb, dtype=np.float32)
        if emb.ndim != 1 or emb.shape[0] != 384:
            return None
        return emb.tobytes()
    except Exception:
        return None


# ── DB plumbing ────────────────────────────────────────────────────


def _open_db() -> sqlite3.Connection | None:
    try:
        from shared_patterns import DB_PATH  # type: ignore[import-not-found]
    except ImportError:
        _log_error("shared_patterns.DB_PATH unavailable; cannot open DB")
        return None
    if not os.path.exists(DB_PATH):
        _log_error(f"DB not found at {DB_PATH}")
        return None
    try:
        conn = sqlite3.connect(DB_PATH, timeout=10.0)
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn
    except sqlite3.Error as e:
        _log_error("sqlite3.connect failed", e)
        return None


def _same_session_exists(
    conn: sqlite3.Connection, session_short: str, content_hash: str
) -> bool:
    """Match the regex pipeline's session_id[:12] storage format.

    The regex pipeline writes `source_session=session_id[:12]` (see
    `hooks/memory-session-end.sh:907`). The LLM extractor must use the
    same truncation or this dedup degrades to a no-op.
    """
    try:
        row = conn.execute(
            "SELECT 1 FROM memories "
            "WHERE json_extract(metadata, '$.source_session') = ? "
            "  AND content_hash = ? "
            "LIMIT 1",
            (session_short, content_hash),
        ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


# ── Core: extract_and_store ────────────────────────────────────────


def extract_and_store(
    transcript_path: str,
    session_id: str,
    project_name: str,
    setup_context: str,
    source_event: str,
    *,
    provider: str | None = None,
    model: str | None = None,
    max_memories: int = _DEFAULT_MAX_MEMORIES,
    timeout_s: int = _DEFAULT_TIMEOUT_S,
    dry_run: bool = False,
) -> int:
    """Run the LLM extraction pipeline. Returns count of memories written.

    Never raises. Internal failures are logged and the function returns
    0. `source_event` is currently always "session_end"; reserved for
    future events.
    """
    if source_event != "session_end":
        _log_error(f"unsupported source_event={source_event!r}; expected session_end")
        return 0

    from b12_llm_providers import get_provider  # type: ignore[import-not-found]
    from b12_llm_prompts import (  # type: ignore[import-not-found]
        SYSTEM_PROMPT, normalize_transcript, validate_extraction,
    )

    prov = get_provider(provider)
    if prov.name == "none":
        return 0

    if not session_id:
        _log_error("empty session_id; aborting")
        return 0
    session_short = session_id[:12]

    # B12_LLM_MODEL env override (documented in README; previously a dead
    # variable). Explicit CLI/keyword `model` argument still wins.
    if model is None:
        env_model = os.environ.get("B12_LLM_MODEL")
        if env_model:
            model = env_model

    cap_chars = _resolve_caps(prov.name)
    transcript_text = normalize_transcript(transcript_path, cap_chars=cap_chars)
    if not transcript_text.strip():
        return 0

    raw_lines = prov.extract(
        SYSTEM_PROMPT,
        transcript_text,
        model=model,
        timeout_s=timeout_s,
        on_error=_log_error,
    )
    if not raw_lines:
        return 0

    candidates: list[dict] = []
    for raw in raw_lines[: max_memories * 3]:
        try:
            parsed = validate_extraction(json.dumps(raw, ensure_ascii=False))
        except (TypeError, ValueError):
            parsed = None
        if parsed is None:
            continue
        candidates.append(parsed)
        if len(candidates) >= max_memories:
            break

    if not candidates:
        return 0

    if dry_run:
        for c in candidates:
            sys.stdout.write(json.dumps(c, ensure_ascii=False) + "\n")
        return len(candidates)

    return _write_candidates(
        candidates,
        session_short=session_short,
        project_name=project_name,
        setup_context=setup_context,
        provider_name=prov.name,
        model_name=(model or prov.default_model or "unknown"),
    )


def _write_candidates(
    candidates: list[dict],
    *,
    session_short: str,
    project_name: str,
    setup_context: str,
    provider_name: str,
    model_name: str,
) -> int:
    """Persist validated candidates through merge_or_insert. Returns count written.

    The function is import-isolated so unit tests can replace either
    `_encode_embedding` or merge_or_insert at module scope without
    pulling in sqlite-vec / sentence-transformers.
    """
    try:
        from shared_patterns import content_hash as _content_hash  # type: ignore[import-not-found]
        from shared_patterns import DB_PATH  # type: ignore[import-not-found]
        from write_time_merge import merge_or_insert  # type: ignore[import-not-found]
        import b12_importance  # type: ignore[import-not-found]
    except ImportError as e:
        _log_error("required B12 modules unavailable", e)
        return 0

    conn = _open_db()
    if conn is None:
        return 0

    written = 0
    now = datetime.now(timezone.utc)
    try:
        for c in candidates:
            content = c["content"]
            mtype = c["type"]
            llm_imp = float(c["importance"])
            try:
                heuristic_imp = float(b12_importance.score(content))
            except Exception:
                heuristic_imp = 0.50
            final_imp = max(llm_imp, heuristic_imp)

            h = _content_hash(content)

            if _same_session_exists(conn, session_short, h):
                continue

            emb = _encode_embedding(content)
            if emb is None:
                _log_error("embedding unavailable; skipping insert")
                continue

            tags = ",".join(
                [
                    f"proj:{project_name}",
                    f"user:{setup_context}",
                    mtype,
                    now.strftime("%Y-%m"),
                    f"tag:{_TAG_LLM}",
                ]
            )
            metadata = {
                "project": project_name,
                "setup": setup_context,
                "scope": "project",
                "type": mtype,
                "source_session": session_short,
                "importance_score": final_imp,
                "extraction_method": f"llm-{provider_name}",
                "extraction_model": model_name,
                "llm_reason": c.get("reason", ""),
            }

            try:
                result = merge_or_insert(
                    conn,
                    content=content,
                    content_hash=h,
                    tags=tags,
                    memory_type=mtype,
                    metadata=metadata,
                    embedding_bytes=emb,
                    now=now,
                    db_path=DB_PATH,
                )
            except Exception as e:
                _log_error(f"merge_or_insert raised for content {content[:80]!r}", e)
                continue

            if result.action in ("inserted", "merged"):
                written += 1
        try:
            conn.commit()
        except sqlite3.Error as e:
            # A commit failure (disk full, busy after the 30s timeout)
            # discards the writes for this batch; logging it is the
            # only signal the caller has.
            _log_error("commit failed at end of LLM extraction batch", e)
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass
    return written


# ── CLI ────────────────────────────────────────────────────────────


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="b12_llm_extractor",
        description="B12 LLM extraction subagent (background worker).",
    )
    p.add_argument("--transcript", default="", help="Path to session JSONL transcript.")
    p.add_argument("--session", default="", help="Full session id (truncated to 12 internally).")
    p.add_argument("--project", default="unknown", help="Project name (basename of cwd).")
    p.add_argument("--setup", default="personal", help="Setup context (personal/work).")
    p.add_argument("--event", default="session_end", choices=["session_end"],
                   help="Hook event that triggered extraction.")
    p.add_argument("--provider", default=None, help="Override B12_LLM_PROVIDER.")
    p.add_argument("--model", default=None, help="Override provider model.")
    p.add_argument("--max-memories", type=int, default=_env_int("B12_LLM_MAX_MEMORIES", _DEFAULT_MAX_MEMORIES))
    p.add_argument("--timeout-s", type=int, default=_env_int("B12_LLM_TIMEOUT_S", _DEFAULT_TIMEOUT_S))
    p.add_argument("--dry-run", action="store_true", help="Print JSONL to stdout; no DB write.")
    p.add_argument("--self-test", action="store_true", help="Run the embedded test suite.")
    return p


def main(argv: list[str]) -> int:
    p = _build_argparser()
    args = p.parse_args(argv)

    if args.self_test:
        return _self_test()

    start = time.time()
    try:
        count = extract_and_store(
            args.transcript,
            args.session,
            args.project,
            args.setup,
            args.event,
            provider=args.provider,
            model=args.model,
            max_memories=args.max_memories,
            timeout_s=args.timeout_s,
            dry_run=args.dry_run,
        )
    except Exception as e:  # last-resort safety net; should not reach here
        _log_error("uncaught exception in extract_and_store", e)
        count = 0

    elapsed = time.time() - start
    if count > 0:
        sys.stderr.write(
            f"[b12_llm_extractor] wrote={count} session={args.session[:12]} elapsed={elapsed:.1f}s\n"
        )
    # Always exit 0 per design — never block the hook chain.
    return 0


# ── Self-test (network-free) ───────────────────────────────────────


class _MockProvider:
    """In-process provider used by --self-test. Returns canned JSONL dicts."""

    name = "mock"
    default_model = "mock-1"

    def __init__(self, lines: list[dict]) -> None:
        self._lines = lines
        self.last_model: str | None = None

    def extract(
        self,
        prompt: str,
        transcript_text: str,
        *,
        model: str | None,
        timeout_s: int,
        on_error=None,  # match the Protocol signature; no-op for mock
    ) -> list[dict]:
        self.last_model = model
        return list(self._lines)


def _self_test() -> int:  # noqa: PLR0915 - many small assertions inline
    failures: list[str] = []

    def expect(cond: bool, label: str) -> None:
        marker = "OK  " if cond else "FAIL"
        print(f"  [{marker}] {label}")
        if not cond:
            failures.append(label)

    from b12_llm_prompts import (  # noqa: PLC0415 - lazy import in self-test
        validate_extraction, normalize_transcript, detect_dominant_language,
    )

    # 1. Schema validation: happy path
    good = '{"type":"decision","content":"ship feature X on Friday","importance":0.75,"reason":"team alignment"}'
    parsed = validate_extraction(good)
    expect(parsed is not None and parsed["type"] == "decision", "1. schema_valid_basic")

    # 2. Schema validation: bad type
    bad_type = '{"type":"banana","content":"x","importance":0.5,"reason":"y"}'
    expect(validate_extraction(bad_type) is None, "2. schema_reject_bad_type")

    # 3. Importance clamping: 0.77 is closer to 0.75 (|0.02|) than to 0.90 (|0.13|)
    clamp = '{"type":"learning","content":"foo","importance":0.77,"reason":"y"}'
    p3 = validate_extraction(clamp)
    expect(p3 is not None and p3["importance"] == 0.75, "3. importance_clamp_to_band")

    # 4. Content truncation
    long = '{"type":"fact","content":"' + ("x" * 800) + '","importance":0.7,"reason":"r"}'
    p4 = validate_extraction(long)
    expect(
        p4 is not None and len(p4["content"]) <= 600 and p4["content"].endswith("(truncated)"),
        "4. content_truncation_at_600",
    )

    # 5. JSON parse error
    expect(validate_extraction("not-json-at-all") is None, "5. json_parse_error_returns_none")

    # 6. Empty provider output: extract_and_store returns 0
    import b12_llm_providers as providers  # noqa: PLC0415
    saved_get = providers.get_provider
    providers.get_provider = lambda name=None: _MockProvider([])
    try:
        out = extract_and_store(
            "/nonexistent/path", "deadbeefcafeXYZ", "B12", "personal",
            "session_end", provider="mock", dry_run=True,
        )
        expect(out == 0, "6. empty_provider_output_returns_zero")
    finally:
        providers.get_provider = saved_get

    # 7. Transcript normalization with missing file returns ""
    expect(normalize_transcript("/nonexistent/__b12_test_missing__.jsonl") == "",
           "7. transcript_missing_returns_empty")

    # 8. Language detection: Turkish
    sample_tr = "Şu kararı verdik: pnpm 10 kullanacağız çünkü daha güvenli."
    expect(detect_dominant_language(sample_tr) == "tr", "8. language_detect_turkish")

    # 9. Truncated session id parameter format
    sid = "abcdef0123456789-and-much-more"
    expect(sid[:12] == "abcdef012345", "9. session_id_truncation_format")

    # 10. Dedup hash match: same content → same hash via shared_patterns
    try:
        from shared_patterns import content_hash as _ch  # noqa: PLC0415
        h1 = _ch("Decision: use pnpm 10 in this project")
        h2 = _ch("Decision: use pnpm 10 in this project")
        expect(h1 == h2 and len(h1) == 64, "10. dedup_hash_stable_64hex")
    except ImportError:
        expect(False, "10. dedup_hash_stable_64hex (shared_patterns missing)")

    # 11. Importance band override: low LLM imp but heuristic = decision
    try:
        import b12_importance  # noqa: PLC0415
        # "we settled on X" → b12_importance.score >= 0.75
        s = b12_importance.score("we settled on Anthropic Haiku for extraction")
        expect(s >= 0.75, "11. importance_band_override_decision")
    except ImportError:
        expect(False, "11. importance_band_override_decision (b12_importance missing)")

    # 12. Dry-run mock provider write path. Uses a tempfile because
    # extract_and_store gates on non-empty transcript_text (a regular
    # file at the path; /dev/null is a char device and skipped).
    import tempfile  # noqa: PLC0415
    providers.get_provider = lambda name=None: _MockProvider([
        {"type": "decision", "content": "use stdlib HTTP", "importance": 0.75, "reason": "no deps"},
        {"type": "learning", "content": "Pyright flags Protocol-required unused params", "importance": 0.5, "reason": "tooling"},
    ])
    try:
        with tempfile.NamedTemporaryFile(
            "w", suffix=".jsonl", delete=False, encoding="utf-8"
        ) as tf:
            tf.write(json.dumps({"type": "human", "message": {"content": "test"}}) + "\n")
            tf.write(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "ok"}]}}) + "\n")
            tpath = tf.name
        try:
            out = extract_and_store(
                tpath, "feedfacedeadbeef-extra", "B12", "personal",
                "session_end", provider="mock", dry_run=True,
            )
            expect(out == 2, "12. dry_run_writes_count_matches_candidates")
        finally:
            try:
                os.unlink(tpath)
            except OSError:
                pass

        # 13. NaN importance falls back to baseline 0.70 (not silently 0.50)
        nan_line = '{"type":"fact","content":"x","importance":NaN,"reason":"y"}'
        p13 = validate_extraction(nan_line)
        expect(p13 is not None and p13["importance"] == 0.70, "13. nan_importance_baseline")

        # 14. Infinity importance falls back to baseline 0.70
        inf_line = '{"type":"fact","content":"x","importance":Infinity,"reason":"y"}'
        p14 = validate_extraction(inf_line)
        expect(p14 is not None and p14["importance"] == 0.70, "14. infinity_importance_baseline")

        # 15. B12_LLM_MODEL env override reaches the provider
        mock = _MockProvider([
            {"type": "decision", "content": "env override test", "importance": 0.75, "reason": "y"},
        ])
        providers.get_provider = lambda name=None: mock
        with tempfile.NamedTemporaryFile(
            "w", suffix=".jsonl", delete=False, encoding="utf-8"
        ) as tf:
            tf.write(json.dumps({"type": "human", "message": {"content": "test"}}) + "\n")
            tpath15 = tf.name
        env_saved = os.environ.get("B12_LLM_MODEL")
        os.environ["B12_LLM_MODEL"] = "custom-model-xyz"
        try:
            extract_and_store(
                tpath15, "session-xyz-0123", "B12", "personal",
                "session_end", provider="mock", dry_run=True,
            )
            expect(mock.last_model == "custom-model-xyz", "15. B12_LLM_MODEL_env_reaches_provider")
        finally:
            if env_saved is None:
                os.environ.pop("B12_LLM_MODEL", None)
            else:
                os.environ["B12_LLM_MODEL"] = env_saved
            try:
                os.unlink(tpath15)
            except OSError:
                pass

        # 16. Provider error callback receives an error message (no API key leak)
        error_calls: list[tuple[str, BaseException | None]] = []
        def capture(msg: str, exc: BaseException | None = None) -> None:
            error_calls.append((msg, exc))
        from b12_llm_providers import AnthropicProvider  # noqa: PLC0415
        prov = AnthropicProvider(api_key="")  # forces missing-key path
        result = prov.extract("sys", "user", model=None, timeout_s=1, on_error=capture)
        expect(
            result == [] and len(error_calls) == 1
            and "ANTHROPIC_API_KEY" in error_calls[0][0]
            and "sk-" not in error_calls[0][0],
            "16. provider_error_logged_without_key_leak",
        )
    finally:
        providers.get_provider = saved_get

    total = 16
    print()
    if failures:
        print(f"FAILED: {len(failures)} / {total} cases  →  {failures}")
        return 1
    print(f"PASSED: {total} / {total} cases")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
