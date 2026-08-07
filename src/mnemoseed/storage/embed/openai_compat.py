"""OpenAI-compatible embedding driver (cloud or self-hosted endpoints).

Works with any service implementing /v1/embeddings: OpenAI, vLLM, Ollama, or
an enclave-hosted inference node. No LOCAL_OFFLINE capability — the startup
gate flags this explicitly when the driver is selected offline.
"""

from __future__ import annotations

from mnemoseed.storage.ports import EMBED_DRIVERS, DriverInfo, Embedder, register


@register(EMBED_DRIVERS)
class OpenAICompatEmbedder(Embedder):
    info = DriverInfo(
        name="openai_compat",
        capabilities=frozenset(),  # not offline-capable
        description="OpenAI-compatible /v1/embeddings endpoint",
    )

    def __init__(
        self,
        base_url: str = "https://api.openai.com/v1",
        api_key: str = "",
        model: str = "text-embedding-3-small",
        dimension: int = 1536,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._dim = dimension

    @property
    def dimension(self) -> int:
        return self._dim

    async def embed(self, texts: list[str]) -> list[list[float]]:
        import httpx  # lazy import

        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{self._base_url}/embeddings",
                headers=headers,
                json={"model": self._model, "input": texts},
            )
            resp.raise_for_status()
            data = resp.json()["data"]
        data.sort(key=lambda d: d["index"])
        return [d["embedding"] for d in data]

    async def close(self) -> None:
        pass
