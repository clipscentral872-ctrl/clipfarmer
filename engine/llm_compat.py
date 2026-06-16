"""LLM provider compat shim.

Lets us swap the heavy Anthropic Claude calls out for a cheaper provider
(Gemini Flash, default) without rewriting every call site.

Usage in callers — change just the import:
    # OLD: import anthropic; client = anthropic.Anthropic(api_key=k)
    from engine.llm_compat import Anthropic
    client = Anthropic(api_key=k)
    resp = client.messages.create(model="...", max_tokens=N, messages=[...])
    text = "".join(b.text for b in resp.content if b.type == "text")

The factory `Anthropic(...)` returns:
  - settings.llm_provider == "anthropic"  -> the real `anthropic.Anthropic` client (unchanged)
  - settings.llm_provider == "gemini"     -> a shim that proxies to Gemini's
                                              OpenAI-compatible endpoint
                                              (gemini-2.0-flash by default).

Supports:
  - text-only messages
  - Anthropic-style image blocks (type=image, source.media_type, source.data base64) -> OpenAI image_url
  - system messages (separate `system=...` kwarg)
  - max_tokens (mapped to OpenAI max_completion_tokens / max_tokens)

Does NOT support:
  - Anthropic tool_use / tools= arg (raises NotImplementedError)
  - Streaming
The chat bot (bot/chat_agent.py) uses tool_use — keep that on Anthropic.
"""
from __future__ import annotations

import base64
import os
from typing import Any, Iterable, Optional

from loguru import logger

from config import settings


# ---------------------------------------------------------------------- factory


def Anthropic(api_key: Optional[str] = None, **kwargs):  # noqa: N802 (mimics SDK name)
    """Drop-in replacement for anthropic.Anthropic(...)."""
    # Live env var wins so we can flip provider without re-importing settings.
    provider = (os.environ.get("LLM_PROVIDER")
                or getattr(settings, "llm_provider", None)
                or "anthropic").lower()
    if provider == "gemini":
        return _GeminiShim(api_key=api_key, **kwargs)
    if provider == "anthropic":
        try:
            import anthropic as _real
        except ImportError as e:
            raise RuntimeError("LLM_PROVIDER=anthropic but anthropic SDK not installed") from e
        return _real.Anthropic(api_key=api_key, **kwargs)
    raise ValueError(f"unknown LLM_PROVIDER={provider!r}; use 'anthropic' or 'gemini'")


# ---------------------------------------------------------------------- Gemini shim


class _GeminiShim:
    """Looks like anthropic.Anthropic; routes to Gemini's OpenAI-compatible API."""

    BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"

    def __init__(self, api_key: Optional[str] = None, **_ignored) -> None:
        try:
            from openai import OpenAI
        except ImportError as e:
            raise RuntimeError(
                "openai SDK required for Gemini compat — pip install openai"
            ) from e
        key = (
            getattr(settings, "gemini_api_key", None)
            or os.environ.get("GEMINI_API_KEY")
            or os.environ.get("GOOGLE_API_KEY")
        )
        if not key:
            raise RuntimeError(
                "GEMINI_API_KEY (or GOOGLE_API_KEY) must be set when LLM_PROVIDER=gemini"
            )
        self._oai = OpenAI(api_key=key, base_url=self.BASE_URL)
        self._gemini_model = (
            getattr(settings, "gemini_model", None)
            or os.environ.get("GEMINI_MODEL")
            or "gemini-2.0-flash"
        )
        self.messages = _Messages(self._oai, self._gemini_model)


# ---------------------------------------------------------------------- messages


class _Messages:
    def __init__(self, oai_client, default_model: str) -> None:
        self._oai = oai_client
        self._default_model = default_model

    def create(
        self,
        model: Optional[str] = None,        # accepted but ignored — we always use Gemini
        max_tokens: int = 4096,
        messages: Optional[list[dict]] = None,
        system: Any = None,
        tools: Optional[list] = None,
        tool_choice: Any = None,
        **_ignored,
    ):
        if tools or tool_choice:
            raise NotImplementedError(
                "Anthropic tool_use isn't bridged to Gemini in this shim — "
                "keep tool-using callers on LLM_PROVIDER=anthropic"
            )
        oai_messages: list[dict] = []
        if system is not None:
            oai_messages.append({"role": "system", "content": _flatten_content(system)})
        for m in messages or []:
            oai_messages.append({
                "role": m["role"],
                "content": _convert_content(m.get("content")),
            })
        logger.info(f"[llm-compat] gemini call: model={self._default_model} max_tokens={max_tokens} "
                    f"messages={len(oai_messages)}")
        resp = self._oai.chat.completions.create(
            model=self._default_model,
            max_tokens=max_tokens,
            messages=oai_messages,
        )
        text = resp.choices[0].message.content or ""
        return _AnthropicLikeResponse(text)


# ---------------------------------------------------------------------- response objects


class _TextBlock:
    """Mimics anthropic's TextBlock: `block.type == "text"`, `block.text`."""
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class _AnthropicLikeResponse:
    """Mimics anthropic Response: `.content` list of blocks."""

    def __init__(self, text: str) -> None:
        self.content: list[_TextBlock] = [_TextBlock(text)]
        self.stop_reason = "end_turn"


# ---------------------------------------------------------------------- content conversion


def _flatten_content(content: Any) -> str:
    """Reduce arbitrary content into a single string for system messages."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, Iterable):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return str(content)


def _convert_content(content: Any) -> Any:
    """Translate Anthropic content to OpenAI content.

    Anthropic format:
        "string"
        OR
        [
          {"type": "text", "text": "..."},
          {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "..."}},
        ]

    OpenAI format:
        "string"
        OR
        [
          {"type": "text", "text": "..."},
          {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}},
        ]
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return _flatten_content(content)

    out: list[dict] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            out.append({"type": "text", "text": block.get("text", "")})
        elif btype == "image":
            src = block.get("source") or {}
            media = src.get("media_type") or "image/png"
            data = src.get("data") or ""
            if src.get("type") == "base64" and data:
                url = f"data:{media};base64,{data}"
            elif src.get("type") == "url":
                url = src.get("url") or ""
            else:
                url = ""
            if url:
                out.append({"type": "image_url", "image_url": {"url": url}})
        # silently drop unknown block types (tool_use, tool_result, document, ...)
    return out
