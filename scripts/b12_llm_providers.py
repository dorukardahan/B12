#!/usr/bin/env python3
"""
B12 LLM provider abstraction.

Three concrete implementations:

  AnthropicProvider   → POST https://api.anthropic.com/v1/messages
  OllamaProvider      → POST <OLLAMA_HOST>/api/chat
  NoneProvider        → no-op, used when extraction is disabled

All providers expose:
    extract(prompt, transcript_text, *, model, timeout_s) -> list[dict]

Each list element is a raw line dict from the LLM, normalized later by
`b12_llm_prompts.validate_extraction`. Providers do NOT validate output
beyond JSONL line splitting — validation lives in one place.

Why stdlib http (urllib.request) instead of the official Anthropic SDK?
  - Zero dependency growth keeps b12-venv minimal.
  - No npm/pip install gate when the user opts in.
  - Both endpoints are simple JSON POSTs — the SDK adds little value
    for a one-shot Messages API call.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import urllib.error
import urllib.request
from typing import Callable, Protocol, runtime_checkable

# Type for the optional error-logging callback the extractor injects.
_OnError = Callable[[str, BaseException | None], None]


def _noop_on_error(msg: str, exc: BaseException | None = None) -> None:  # noqa: ARG001
    """Default `on_error` — silent. The extractor passes its own logger."""


_ANTHROPIC_DEFAULT_MODEL = "claude-haiku-4-5-20251001"
_OLLAMA_DEFAULT_MODEL = "qwen2.5:1.5b"
_ANTHROPIC_VERSION = "2023-06-01"
_ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"


# ── Protocol ────────────────────────────────────────────────────────


@runtime_checkable
class LLMProvider(Protocol):
    name: str
    default_model: str

    def extract(
        self,
        prompt: str,
        transcript_text: str,
        *,
        model: str | None,
        timeout_s: int,
        on_error: _OnError = _noop_on_error,
    ) -> list[dict]:
        """Return parsed JSONL lines from the LLM as a list of dicts.

        Raises only on programmer error. Network failures, auth errors,
        timeouts, and malformed responses MUST be returned as an empty
        list. When `on_error` is provided, the provider MUST call it
        with a short error message and the exception so the caller's
        error log (`~/.B12/memory-logs/llm-extraction-errors.log`)
        records the failure. API keys, headers, and bodies must never
        be passed to `on_error`.
        """
        ...


# ── Shared helpers ─────────────────────────────────────────────────


def _split_jsonl(text: str) -> list[dict]:
    """Best-effort: split LLM output into JSON line dicts.

    Tolerates blank lines, leading prose, and trailing fences. Does
    NOT validate fields — that is `validate_extraction`'s job.
    """
    if not text:
        return []
    out: list[dict] = []
    for raw in text.splitlines():
        line = raw.strip()
        # Strip ```json fences if the model wraps output.
        if line.startswith("```"):
            continue
        if not line or not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _http_post_json(url: str, *, headers: dict, payload: dict, timeout_s: int) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # nosec B310 - https only
        raw = resp.read().decode("utf-8", errors="replace")
    return json.loads(raw)


# ── Anthropic ──────────────────────────────────────────────────────


class AnthropicProvider:
    name = "anthropic"
    default_model = _ANTHROPIC_DEFAULT_MODEL

    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY") or ""

    def extract(
        self,
        prompt: str,
        transcript_text: str,
        *,
        model: str | None,
        timeout_s: int,
        on_error: _OnError = _noop_on_error,
    ) -> list[dict]:
        if not self.api_key:
            on_error("anthropic: ANTHROPIC_API_KEY unset; skipping", None)
            return []
        if not transcript_text.strip():
            return []
        mdl = model or self.default_model
        headers = {
            "content-type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": _ANTHROPIC_VERSION,
        }
        payload = {
            "model": mdl,
            "max_tokens": 1024,
            "system": prompt,
            "messages": [
                {"role": "user", "content": transcript_text},
            ],
        }
        try:
            body = _http_post_json(
                _ANTHROPIC_URL,
                headers=headers,
                payload=payload,
                timeout_s=timeout_s,
            )
        except (urllib.error.URLError, socket.timeout, json.JSONDecodeError, OSError) as e:
            # Never include `headers` or `payload` here — both carry the
            # API key / transcript content. Stringified exception is the
            # bound of what the error log sees.
            on_error(f"anthropic: request failed model={mdl}", e)
            return []
        content = body.get("content") or []
        text_parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text_parts.append(block.get("text") or "")
        # Always log truncation — even when text was returned the cut
        # point can land mid-JSON and `_split_jsonl` silently drops the
        # partial line. The user benefits from seeing this in the log
        # because the fix is to bump `max_tokens` (currently hardcoded
        # at 1024 for the JSONL output shape).
        if body.get("stop_reason") == "max_tokens":
            on_error(
                f"anthropic: response truncated (stop_reason=max_tokens) "
                f"text_blocks={len(text_parts)}",
                None,
            )
        result = _split_jsonl("\n".join(text_parts))
        if text_parts and not result:
            on_error(
                "anthropic: response present but no JSONL lines extracted",
                None,
            )
        return result


# ── Ollama ─────────────────────────────────────────────────────────


class OllamaProvider:
    name = "ollama"
    default_model = _OLLAMA_DEFAULT_MODEL

    def __init__(self, host: str | None = None) -> None:
        self.host = (host or os.environ.get("OLLAMA_HOST")
                     or "http://127.0.0.1:11434").rstrip("/")

    def extract(
        self,
        prompt: str,
        transcript_text: str,
        *,
        model: str | None,
        timeout_s: int,
        on_error: _OnError = _noop_on_error,
    ) -> list[dict]:
        if not transcript_text.strip():
            return []
        mdl = model or self.default_model
        url = f"{self.host}/api/chat"
        payload = {
            "model": mdl,
            "stream": False,
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": transcript_text},
            ],
        }
        try:
            body = _http_post_json(
                url,
                headers={"content-type": "application/json"},
                payload=payload,
                timeout_s=timeout_s,
            )
        except (urllib.error.URLError, socket.timeout, json.JSONDecodeError, OSError) as e:
            on_error(f"ollama: request failed host={self.host} model={mdl}", e)
            return []
        message = body.get("message") or {}
        content = message.get("content") or ""
        return _split_jsonl(content)


# ── None ───────────────────────────────────────────────────────────


class NoneProvider:
    name = "none"
    default_model = ""

    def extract(
        self,
        prompt: str,
        transcript_text: str,
        *,
        model: str | None,
        timeout_s: int,
        on_error: _OnError = _noop_on_error,
    ) -> list[dict]:
        return []


# ── Factory ────────────────────────────────────────────────────────


def get_provider(name: str | None = None) -> LLMProvider:
    """Resolve a provider by name. Falls back to B12_LLM_PROVIDER env.

    Unknown names return NoneProvider so a misconfigured env var does
    not break the hook.
    """
    if name is None:
        name = os.environ.get("B12_LLM_PROVIDER")
    key = (name or "").strip().lower()
    if key == "anthropic":
        return AnthropicProvider()
    if key == "ollama":
        return OllamaProvider()
    return NoneProvider()


# ── CLI smoke ──────────────────────────────────────────────────────


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--list":
        for cls in (AnthropicProvider, OllamaProvider, NoneProvider):
            sys.stdout.write(f"{cls.name}\tdefault_model={cls.default_model}\n")
        sys.exit(0)
    sys.stderr.write("usage: b12_llm_providers.py --list\n")
    sys.exit(2)
