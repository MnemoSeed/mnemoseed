"""pgvector VectorStore driver (Postgres-family alternative).

Proves the VectorStore port is portable: the same ChunkStamp /
metadata_filter_view lands on a pgvector table, metadata as JSONB, and
ingested_after filters on a plain double-precision column.
"""

from __future__ import annotations

import json
from typing import Any

from mnemoseed.schema.stamp import ChunkStamp
from mnemoseed.storage.ports import VECTOR_DRIVERS, Capability, DriverInfo, VectorStore, register

_DDL = """
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS mnemo_vectors (
    chunk_id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL,
    embedding vector({dim}) NOT NULL,
    document TEXT NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{{}}',
    ingested_at DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mnemo_vectors_profile ON mnemo_vectors(profile_id);
CREATE INDEX IF NOT EXISTS idx_mnemo_vectors_ingested ON mnemo_vectors(profile_id, ingested_at);
"""


@register(VECTOR_DRIVERS)
class PgVectorStore(VectorStore):
    info = DriverInfo(
        name="pgvector",
        capabilities=frozenset(
            {
                Capability.METADATA_FILTER,
                Capability.TIME_RANGE_FILTER,
                Capability.PERSIST,
                Capability.TRANSACTIONS,
                Capability.VERSION_CHAIN,
            }
        ),
        description="Postgres + pgvector vector store",
    )

    def __init__(self, dsn: str, dimension: int = 768) -> None:
        import psycopg  # lazy import: optional dependency

        self._dim = dimension
        self._conn = psycopg.connect(dsn, autocommit=True)
        with self._conn.cursor() as cur:
            cur.execute(_DDL.format(dim=dimension))

    async def upsert(self, stamp: ChunkStamp, embedding: list[float]) -> None:
        cols = "chunk_id, profile_id, embedding, document, metadata, ingested_at"
        with self._conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO mnemo_vectors ({cols}) "
                "VALUES (%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (chunk_id) DO UPDATE SET embedding=EXCLUDED.embedding, "
                "document=EXCLUDED.document, metadata=EXCLUDED.metadata, ingested_at=EXCLUDED.ingested_at",
                (
                    stamp.chunk_id,
                    stamp.profile_id,
                    embedding,
                    stamp.text,
                    json.dumps(stamp.metadata_filter_view(), ensure_ascii=False),
                    stamp.ingested_at,
                ),
            )

    async def query(
        self,
        profile_id: str,
        embedding: list[float],
        top_k: int = 20,
        filters: dict[str, Any] | None = None,
        ingested_after: float | None = None,
    ) -> list[tuple[ChunkStamp, float]]:
        sql = (
            "SELECT chunk_id, document, metadata, embedding <=> %s AS dist "
            "FROM mnemo_vectors WHERE profile_id = %s"
        )
        params: list[Any] = [embedding, profile_id]
        if ingested_after is not None:
            sql += " AND ingested_at > %s"
            params.append(ingested_after)
        for k, v in (filters or {}).items():
            sql += " AND metadata->>%s = %s"
            params.extend([k, str(v)])
        sql += " ORDER BY embedding <=> %s LIMIT %s"
        params.extend([embedding, top_k])

        out: list[tuple[ChunkStamp, float]] = []
        with self._conn.cursor() as cur:
            cur.execute(sql, params)
            for chunk_id, doc, meta, dist in cur.fetchall():
                stamp = ChunkStamp.from_filter_view(chunk_id, doc or "", meta or {})
                out.append((stamp, float(dist)))
        return out

    async def get(self, profile_id: str, chunk_id: str) -> ChunkStamp | None:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT document, metadata FROM mnemo_vectors WHERE chunk_id=%s AND profile_id=%s",
                (chunk_id, profile_id),
            )
            row = cur.fetchone()
        if not row:
            return None
        return ChunkStamp.from_filter_view(chunk_id, row[0] or "", row[1] or {})

    async def delete(self, profile_id: str, chunk_id: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "DELETE FROM mnemo_vectors WHERE chunk_id=%s AND profile_id=%s",
                (chunk_id, profile_id),
            )

    async def count(self, profile_id: str) -> int:
        with self._conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM mnemo_vectors WHERE profile_id=%s", (profile_id,))
            return int(cur.fetchone()[0])

    async def close(self) -> None:
        self._conn.close()
