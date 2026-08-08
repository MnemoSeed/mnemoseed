"""OpenAI-compatible embedder: any /v1/embeddings endpoint (docker preset).

Second embed driver (prd-08 FR-8.4): talks to any OpenAI-compatible API
(OpenAI, a self-hosted vLLM, localai, ...) over HTTP. Declares only
``embed.batch`` — no ``embed.sparse_output`` (this endpoint never produces a
sparse leg, so the capability gate surfaces the dense-only degradation at
startup) and no ``embed.local_inference`` (inference happens remotely, so the
frontend knows generation is external).

Construction performs no network I/O: httpx clients are lazy and only bind the
transport when the first request opens the socket, so a misset base_url never
blocks daemon boot. A small ``connectivity()`` probe is provided for install
timeouts and diagnostics.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import httpx

from mnemoseed.storage.ports import (
    Capability,
    DriverInfo,
    EmbeddingResult,
)
from mnemoseed.storage.registry import EMBED_DRIVERS, register

_CAPABILITIES = frozenset({Capability.EMBED_BATCH})


@register(EMBED_DRIVERS)
class OpenAICompatibleEmbedder:
    """Dense-only embedder over any OpenAI-compatible /embeddings endpoint."""

    info = DriverInfo(
        name="openai_compatible",
        capabilities=_CAPABILITIES,
        description="dense embeddings via an OpenAI-compatible HTTP endpoint (no sparse)",
    )

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 30.0,
        **kwargs: Any,
    ) -> None:
        self.params: dict[str, Any] = kwargs
        if not base_url:
            raise ValueError("openai_compatible requires a non-empty 'base_url'")
        if not model:
            raise ValueError("openai_compatible requires a non-empty 'model'")
        timeout_value = kwargs.get("timeout", timeout)
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = float(timeout_value)
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = httpx.Client(base_url=self.base_url, headers=headers, timeout=self.timeout)
        self.dimension = 0  # unknown until the first embed call reports it

    def capabilities(self) -> frozenset[Capability]:
        return self.info.capabilities

    async def close(self) -> None:
        self._client.close()

    # ------------------------------------------------------------ embed

    def embed(self, text: str) -> EmbeddingResult:
        results = self.embed_batch([text])
        return results[0]

    def embed_batch(self, texts: Sequence[str]) -> list[EmbeddingResult]:
        payload = {"model": self.model, "input": list(texts)}
        response = self._client.post("/embeddings", json=payload)
        response.raise_for_status()
        data = response.json()
        entries = data.get("data", [])
        ordered = sorted(entries, key=lambda entry: int(entry.get("index", 0)))
        dense_vectors = [self._dense_list(entry["embedding"]) for entry in ordered]
        return [EmbeddingResult(dense=vector, sparse=None) for vector in dense_vectors]

    # ------------------------------------------------------------ diagnostics

    def connectivity(self) -> dict[str, Any]:
        """Small self-test: list the models the endpoint exposes.

        Returns {"reachable": bool, "models": [...]} or the error payload under
        "error". Performs real HTTP I/O — call outside the boot hot path.
        """
        try:
            response = self._client.get("/models")
            if response.status_code != 200:
                return {
                    "reachable": False,
                    "error": f"GET /models returned HTTP {response.status_code}",
                }
        except httpx.HTTPError as exc:
            return {"reachable": False, "error": str(exc)}
        body = response.json()
        models = [m.get("id") for m in body.get("data", []) if isinstance(m, dict)]
        return {"reachable": True, "models": models}

    # ------------------------------------------------------------ internals

    def _dense_list(self, embedding: Any) -> list[float]:
        if not isinstance(embedding, (list, tuple)):
            raise ValueError("embedding endpoint returned a non-list embedding vector")
        vector = [float(value) for value in embedding]
        if self.dimension == 0:
            self.dimension = len(vector)
        return vector
