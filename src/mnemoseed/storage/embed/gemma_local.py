"""Local Gemma embedding driver (embedded default; the offline capability
source).

Loads embeddinggemma-300m via sentence-transformers (lazy import; weights are
downloaded once at init time). This is the only default source of the
LOCAL_OFFLINE capability — retrieval keeps working with no network.
"""

from __future__ import annotations

from mnemoseed.storage.ports import EMBED_DRIVERS, Capability, DriverInfo, Embedder, register

_MODEL_ID = "google/embeddinggemma-300m"


@register(EMBED_DRIVERS)
class GemmaLocalEmbedder(Embedder):
    info = DriverInfo(
        name="gemma_local",
        capabilities=frozenset({Capability.LOCAL_OFFLINE}),
        description=f"Local embedding {_MODEL_ID} (default, offline-capable)",
    )

    def __init__(self, model_id: str = _MODEL_ID, device: str | None = None) -> None:
        from sentence_transformers import SentenceTransformer  # lazy import: heavy dependency

        kwargs = {"device": device} if device else {}
        self._model = SentenceTransformer(model_id, **kwargs)
        self._dim = int(self._model.get_sentence_embedding_dimension())

    @property
    def dimension(self) -> int:
        return self._dim

    async def embed(self, texts: list[str]) -> list[list[float]]:
        import asyncio

        # sentence-transformers is a sync CPU/GPU call; keep the loop unblocked
        vectors = await asyncio.to_thread(
            self._model.encode, texts, normalize_embeddings=True, convert_to_numpy=True
        )
        return [v.tolist() for v in vectors]

    async def close(self) -> None:
        pass
