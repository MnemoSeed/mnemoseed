"""Anthropic Messages API chat driver (PRD-02 T6; design/02 §6 deep_reflection).

POST /v1/messages with ``x-api-key`` + ``anthropic-version`` headers. The
Messages API requires ``max_tokens``, which the per-role config supplies. Usage
maps input/output tokens plus the two cache legs (cache_read_input_tokens /
cache_creation_input_tokens) onto the provider-neutral Usage record.
"""

from __future__ import annotations

from typing import Any

import httpx

from mnemoseed.llm.registry import LLM_DRIVERS, register
from mnemoseed.llm.types import (
    ChatResult,
    HealthReport,
    LLMDriverInfo,
    LLMUnavailable,
    Usage,
)

DEFAULT_ANTHROPIC_VERSION = "2023-06-01"


@register(LLM_DRIVERS)
class AnthropicLLM:
    """Claude Messages API over raw HTTP (x-api-key + anthropic-version)."""

    info = LLMDriverInfo(
        name="anthropic",
        description="Anthropic Messages API via raw HTTP (x-api-key + anthropic-version)",
    )

    def __init__(
        self,
        base_url: str = "https://api.anthropic.com",
        api_key: str = "",
        model: str = "",
        max_tokens: int = 2048,
        timeout: float = 30.0,
        anthropic_version: str = DEFAULT_ANTHROPIC_VERSION,
        **kwargs: Any,
    ) -> None:
        if not base_url:
            raise ValueError("anthropic requires a non-empty 'base_url'")
        if not model:
            raise ValueError("anthropic requires a non-empty 'model'")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.max_tokens = int(max_tokens)
        self.timeout = float(timeout)
        self.version = anthropic_version
        self.params: dict[str, Any] = kwargs
        # Auth/version ride on each request (not the client): tests swap
        # ``_client`` for a MockTransport one, and the headers must survive.
        self._headers = {
            "x-api-key": api_key,
            "anthropic-version": anthropic_version,
        }
        self._client = httpx.Client(base_url=self.base_url, timeout=self.timeout)

    def chat(self, *, system: str, user: str) -> ChatResult:
        payload = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        try:
            response = self._client.post("/v1/messages", json=payload, headers=self._headers)
            response.raise_for_status()
            body = response.json()
        except httpx.HTTPStatusError as exc:
            raise LLMUnavailable(f"anthropic chat failed: HTTP {exc.response.status_code}") from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise LLMUnavailable(f"anthropic chat failed: {exc}") from exc
        return ChatResult(
            text=_joined_text(body),
            usage=_usage_from(body),
            model=self.model,
            driver="anthropic",
        )

    def check(self) -> HealthReport:
        try:
            response = self._client.get("/v1/models", headers=self._headers)
            if response.status_code != 200:
                return HealthReport(
                    ok=False,
                    detail={"error": f"GET /v1/models returned HTTP {response.status_code}"},
                )
            models = [m.get("id") for m in response.json().get("data") or [] if isinstance(m, dict)]
            return HealthReport(ok=True, detail={"models": models})
        except (httpx.HTTPError, ValueError) as exc:
            return HealthReport(ok=False, detail={"error": str(exc)})


def _joined_text(body: Any) -> str:
    """Concatenate all text blocks in a Messages API response content list."""
    parts: list[str] = []
    for block in body.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            value = block.get("text")
            if isinstance(value, str):
                parts.append(value)
    return "".join(parts)


def _usage_from(body: Any) -> Usage | None:
    data = body.get("usage")
    if not isinstance(data, dict):
        return None
    return Usage(
        prompt_tokens=data.get("input_tokens"),
        completion_tokens=data.get("output_tokens"),
        cache_read_input_tokens=data.get("cache_read_input_tokens"),
        cache_creation_input_tokens=data.get("cache_creation_input_tokens"),
    )
