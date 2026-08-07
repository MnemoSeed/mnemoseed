"""ChromaDB embedded VectorStore driver (default; optional extra).

One collection per profile (named mnemo_{profile_id}). Metadata carries the
flat ChunkStamp.metadata_filter_view() fields, which back the ingested_after
filter used by freshness checks.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mnemoseed.config import CONFIG_DIR
from mnemoseed.schema.stamp import ChunkStamp
from mnemoseed.storage.ports import VECTOR_DRIVERS, Capability, DriverInfo, VectorStore, register


def _collection_name(profile_id: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in profile_id)
    return f"mnemo_{safe}"[:63]


@register(VECTOR_DRIVERS)
class ChromaEmbedded(VectorStore):
    info = DriverInfo(
        name="chroma_embedded",
        capabilities=frozenset(
            {
                Capability.METADATA_FILTER,
                Capability.TIME_RANGE_FILTER,
                Capability.PERSIST,
                Capability.LOCAL_OFFLINE,
            }
        ),
        description="Embedded ChromaDB vector store (default)",
    )

    def __init__(self, path: str | None = None) -> None:
        import chromadb  # lazy import: optional dependency

        db_path = Path(path).expanduser() if path else CONFIG_DIR / "chroma"
        db_path.mkdir(parents=True, exist_ok=True)
        self._client = chromadb.PersistentClient(path=str(db_path))

    def _collection(self, profile_id: str):
        return self._client.get_or_create_collection(
            name=_collection_name(profile_id),
            metadata={"hnsw:space": "cosine"},
        )

    async def upsert(self, stamp: ChunkStamp, embedding: list[float]) -> None:
        col = self._collection(stamp.profile_id)
        col.upsert(
            ids=[stamp.chunk_id],
            embeddings=[embedding],
            documents=[stamp.text],
            metadatas=[stamp.metadata_filter_view()],
        )

    async def query(
        self,
        profile_id: str,
        embedding: list[float],
        top_k: int = 20,
        filters: dict[str, Any] | None = None,
        ingested_after: float | None = None,
    ) -> list[tuple[ChunkStamp, float]]:
        col = self._collection(profile_id)
        clauses: list[dict[str, Any]] = []
        for k, v in (filters or {}).items():
            clauses.append({k: {"$eq": v}})
        if ingested_after is not None:
            clauses.append({"ingested_at": {"$gt": ingested_after}})
        where: dict[str, Any] = {}
        if len(clauses) == 1:
            where = clauses[0]
        elif clauses:
            where = {"$and": clauses}

        kwargs: dict[str, Any] = dict(
            query_embeddings=[embedding],
            n_results=top_k,
            include=["documents", "metadatas", "distances"],
        )
        if where:
            kwargs["where"] = where
        res = col.query(**kwargs)

        out: list[tuple[ChunkStamp, float]] = []
        ids = res.get("ids", [[]])[0]
        docs = res.get("documents", [[]])[0]
        metas = res.get("metadatas", [[]])[0]
        dists = res.get("distances", [[]])[0]
        for cid, doc, meta, dist in zip(ids, docs, metas, dists, strict=False):
            stamp = ChunkStamp.from_filter_view(cid, doc or "", meta or {})
            out.append((stamp, float(dist)))
        return out

    async def get(self, profile_id: str, chunk_id: str) -> ChunkStamp | None:
        col = self._collection(profile_id)
        res = col.get(ids=[chunk_id], include=["documents", "metadatas"])
        if not res["ids"]:
            return None
        return ChunkStamp.from_filter_view(chunk_id, res["documents"][0] or "", res["metadatas"][0] or {})

    async def delete(self, profile_id: str, chunk_id: str) -> None:
        self._collection(profile_id).delete(ids=[chunk_id])

    async def count(self, profile_id: str) -> int:
        return self._collection(profile_id).count()

    async def close(self) -> None:
        pass  # PersistentClient has no explicit close
