"""Postgres vector store: chunks table over PostgreSQL + the pgvector extension.

Second-driver for the hippocampus (prd-08 FR-8.4): the full VectorStore surface
(appendix B.1) over a plain ``chunks`` table carrying every appendix A.1 field.
Dense vectors live in a ``vector(dim)`` column indexed with HNSW; the sparse
leg is stored as a structured JSONB ``{"indices": [...], "values": [...]}`` —
never a dense array (A.1). Search is hybrid exactly like lancedb_embedded: a
dense ANN prefilter (cosine ``<=>``) followed by a sparse dot-product re-rank.

snapshot_read maps to Postgres MVCC: the call runs inside a REPEATABLE READ
transaction scoped to the read, so every page of a large snapshot sees the same
committed state. The protocol returns a materialized list of stamps (no version
handle is kept alive after the call), so the snapshot is bounded to the call —
the same "consistent as of call time" contract LanceDB provides via
``table.version``, which is also a call-scoped handle there.

Note on the driver's transaction idiom: psycopg3 nests ``transaction()`` blocks
into savepoints when the connection is already in a transaction. The module
``_transaction`` helper rolls back first, so every mutating method runs as a
real BEGIN/COMMIT unit (the Postgres analog of SQLite's explicit BEGIN
IMMEDIATE).
"""

from __future__ import annotations

import math
import os
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any

import psycopg
from pgvector import Vector
from pgvector.psycopg import register_vector
from psycopg.rows import dict_row
from psycopg.types.json import JsonbDumper, register_default_adapters

from mnemoseed.schema.stamp import ChunkStamp, CognitiveTier, Cues, EmotionCue, Provenance, ProvenanceEvent
from mnemoseed.storage.ports import (
    Capability,
    ChunkFilter,
    DriverInfo,
    Page,
    PageResult,
    SearchHit,
    SparseVector,
    StorageError,
    WeightUpdate,
)
from mnemoseed.storage.registry import VECTOR_DRIVERS, register

_CAPABILITIES = frozenset(
    {
        Capability.VECTOR_HYBRID_SEARCH,
        Capability.VECTOR_METADATA_FILTER,
        Capability.VECTOR_SNAPSHOT,
    }
)

_DENSE_FUSION_WEIGHT = 0.5
_DEFAULT_TABLE = "chunks"


@register(VECTOR_DRIVERS)
class PgVectorStore:
    """Vector store over PostgreSQL + pgvector (dense) and JSONB (sparse)."""

    info = DriverInfo(
        name="pgvector",
        capabilities=_CAPABILITIES,
        description="PostgreSQL chunks table with pgvector HNSW dense + JSONB sparse hybrid search",
    )

    def __init__(
        self,
        dsn: str | None = None,
        conn: Any | None = None,
        table_name: str = _DEFAULT_TABLE,
        dimensions: int | None = None,
        schema: str = "public",
        hnsw: bool = True,
        **kwargs: Any,
    ) -> None:
        self.params: dict[str, Any] = kwargs
        if dimensions is None:
            dimensions = kwargs.get("dimensions")
        if isinstance(dimensions, bool) or not isinstance(dimensions, int) or dimensions <= 0:
            raise ValueError("pgvector requires a positive integer 'dimensions'")
        if dsn is None:
            dsn = kwargs.get("dsn")
        if schema == "public":
            schema = kwargs.get("schema", schema)
        self.dimensions = dimensions
        self.table_name = table_name
        self._schema = schema or "public"
        self._hnsw = bool(hnsw)
        if dsn is None:
            dsn = os.environ.get("MNEMOSEED_PG_DSN")
        if conn is None:
            if dsn is None:
                raise StorageError("pgvector requires a 'dsn' connection string (or a 'conn' connection)")
            conn = psycopg.connect(dsn)
            self._owns_conn = True
        else:
            self._owns_conn = False
        self._conn = conn
        self._conn.row_factory = dict_row
        register_default_adapters(self._conn)
        # psycopg3 dumps dict/list to JSON only when wrapped; this connection
        # dumps bare dicts as JSONB so chunk rows (cues/provenance/score/sparse
        # structs) go through unwrapped. Lists only ever appear nested inside a
        # dict here, so no list-level dumper is registered (that would break
        # `= ANY(%s)` array binds elsewhere).
        self._conn.adapters.register_dumper(dict, JsonbDumper)
        self._init_schema()
        # the 'vector' pg type only exists after CREATE EXTENSION ran above
        register_vector(self._conn)

    def capabilities(self) -> frozenset[Capability]:
        return self.info.capabilities

    async def close(self) -> None:
        if self._owns_conn:
            self._conn.close()

    # ------------------------------------------------------------ schema

    def _init_schema(self) -> None:
        # pgvector installs its `vector` type into the schema that is first on
        # the search path at extension-creation time — `public` on a fresh
        # database, but an arbitrary already-existing schema for later runs
        # (CREATE EXTENSION is once-per-database, so IF NOT EXISTS never moves
        # it). Discover the type's actual namespace and keep it on the search
        # path so the unqualified type name resolves, while this instance's
        # schema stays first so tables and tracking land there (D6 isolation).
        with _transaction(self._conn):
            self._conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            self._conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{self._schema}"')
            type_schema = self._conn.execute(
                "SELECT n.nspname FROM pg_type t "
                "JOIN pg_namespace n ON t.typnamespace = n.oid "
                "WHERE t.typname = 'vector'"
            ).fetchone()
            if type_schema is None:
                vector_schema = "public"
            else:
                vector_schema = (
                    str(type_schema["nspname"]) if isinstance(type_schema, dict) else str(type_schema[0])
                )
            self._conn.execute(f'SET search_path TO "{self._schema}", "{vector_schema}", public')
            self._conn.execute(chunks_ddl(self.table_name, self.dimensions))
            if self._hnsw:
                self._conn.execute(hnsw_index_ddl(self.table_name))

    # ------------------------------------------------------------ helpers

    def _exec_rows(self, sql: str, params: Sequence[Any] | None = None) -> list[Any]:
        return self._conn.execute(sql, params).fetchall()

    def _exec_row(self, sql: str, params: Sequence[Any] | None = None) -> Any | None:
        return self._conn.execute(sql, params).fetchone()

    # ------------------------------------------------------------ writes

    def upsert_chunk(
        self,
        chunk: ChunkStamp,
        dense: Sequence[float],
        sparse: SparseVector | None = None,
    ) -> None:
        row = dict(_to_row(chunk, dense, sparse))
        row["vector_dense"] = Vector([float(value) for value in dense])
        columns = list(row)
        updates = ", ".join(f"{column} = EXCLUDED.{column}" for column in columns)
        placeholders = ", ".join(["%s"] * len(columns))
        with _transaction(self._conn):
            self._conn.execute(
                f"INSERT INTO {self.table_name} ({', '.join(columns)}) VALUES ({placeholders}) "
                f"ON CONFLICT (chunk_id) DO UPDATE SET {updates}",
                [row[column] for column in columns],
            )

    def delete_chunk(self, chunk_id: str) -> None:
        with _transaction(self._conn):
            self._conn.execute(f"DELETE FROM {self.table_name} WHERE chunk_id = %s", (chunk_id,))

    def mark_consolidated(self, chunk_ids: Sequence[str]) -> None:
        ids = list(chunk_ids)
        if not ids:
            return
        with _transaction(self._conn):
            self._conn.execute(
                f"UPDATE {self.table_name} SET consolidated = TRUE WHERE chunk_id = ANY(%s)", (ids,)
            )

    def purge_range(self, session_id: str, turn_start: int, turn_end: int) -> int:
        with _transaction(self._conn):
            rows = self._conn.execute(
                f"DELETE FROM {self.table_name} WHERE session_id = %s "
                "AND turn_start IS NOT NULL AND turn_end IS NOT NULL "
                "AND turn_start <= %s AND turn_end >= %s RETURNING chunk_id",
                (session_id, turn_end, turn_start),
            ).fetchall()
            return len(rows)

    def update_weights(self, updates: Sequence[WeightUpdate]) -> None:
        for update in updates:
            sets: list[str] = []
            params: list[Any] = []
            if update.decay_weight is not None:
                sets.append("decay_weight = %s")
                params.append(float(update.decay_weight))
            if update.last_reinforced is not None:
                sets.append("last_reinforced = %s")
                params.append(float(update.last_reinforced))
            if update.reinforce_count is not None:
                sets.append("reinforce_count = %s")
                params.append(int(update.reinforce_count))
            if not sets:
                continue
            with _transaction(self._conn):
                self._conn.execute(
                    f"UPDATE {self.table_name} SET {', '.join(sets)} WHERE chunk_id = %s",
                    [*params, update.chunk_id],
                )

    def update_chunk_state(
        self,
        chunk_ids: Sequence[str],
        hit_increment: int | None = None,
        needs_reconcile: bool | None = None,
    ) -> None:
        """Port update_chunk_state over one batched UPDATE per action."""
        ids = list(chunk_ids)
        if not ids:
            return
        if hit_increment is None and needs_reconcile is None:
            return
        with _transaction(self._conn):
            if hit_increment is not None and hit_increment != 0:
                if hit_increment > 0:
                    self._conn.execute(
                        f"UPDATE {self.table_name} SET hit_count = hit_count + %s, last_hit_at = %s "
                        "WHERE chunk_id = ANY(%s)",
                        (int(hit_increment), time.time(), ids),
                    )
                else:
                    self._conn.execute(
                        f"UPDATE {self.table_name} SET hit_count = hit_count + %s WHERE chunk_id = ANY(%s)",
                        (int(hit_increment), ids),
                    )
            if needs_reconcile is not None:
                self._conn.execute(
                    f"UPDATE {self.table_name} SET needs_reconcile = %s WHERE chunk_id = ANY(%s)",
                    (bool(needs_reconcile), ids),
                )

    # ------------------------------------------------------------ reads

    def get_chunk(self, chunk_id: str) -> ChunkStamp | None:
        row = self._exec_row(f"SELECT * FROM {self.table_name} WHERE chunk_id = %s", (chunk_id,))
        return _to_stamp(row) if row is not None else None

    def search(
        self,
        dense: Sequence[float],
        sparse: SparseVector | None,
        filter: ChunkFilter,
        top_k: int,
    ) -> list[SearchHit]:
        clauses, params = _filter_clauses(filter)
        where = " AND ".join(clauses)
        candidate_k = max(top_k * 4, top_k + 50)
        rows = self._exec_rows(
            f"SELECT *, vector_dense <=> %s AS _dist FROM {self.table_name} "
            f"WHERE {where} ORDER BY _dist LIMIT %s",
            [Vector([float(value) for value in dense]), *params, candidate_k],
        )
        hits: list[SearchHit] = []
        for row in rows:
            dense_sim = 1.0 - float(row["_dist"])
            sparse_sim = _sparse_similarity(sparse, row["vector_sparse"]) if sparse is not None else 0.0
            score = (
                _DENSE_FUSION_WEIGHT * dense_sim + (1.0 - _DENSE_FUSION_WEIGHT) * sparse_sim
                if sparse is not None
                else dense_sim
            )
            hits.append(SearchHit(chunk=_to_stamp(row), similarity=float(score)))
        hits.sort(key=lambda hit: hit.similarity, reverse=True)
        return hits[:top_k]

    def near_duplicate(
        self, vector: Sequence[float], threshold: float, profile_id: str | None = None
    ) -> list[ChunkStamp]:
        """Exact full-scan cosine probe (dense only), mirroring lancedb_embedded.
        ``profile_id`` scopes the probe to one profile (D5); omitted scans the
        whole table."""
        query = [float(value) for value in vector]
        query_norm = math.sqrt(sum(value * value for value in query)) or 1.0
        where = "true"
        params: list[Any] = []
        if profile_id not in (None, "", "*"):
            where = "profile_id = %s"
            params.append(profile_id)
        rows = self._exec_rows(f"SELECT chunk_id, vector_dense FROM {self.table_name} WHERE {where}", params)
        matches: list[tuple[float, str]] = []
        for row in rows:
            row_vector = _dense_to_list(row["vector_dense"])
            if row_vector is None:
                continue
            row_norm = math.sqrt(sum(v * v for v in row_vector)) or 1.0
            dot = sum(q * v for q, v in zip(query, row_vector, strict=False))
            similarity = dot / (query_norm * row_norm)
            if similarity >= threshold:
                matches.append((similarity, str(row["chunk_id"])))
        if not matches:
            return []
        matches.sort(key=lambda entry: entry[0], reverse=True)
        chunks: list[ChunkStamp] = []
        for _similarity, chunk_id in matches:
            stamp = self.get_chunk(chunk_id)
            if stamp is not None:
                chunks.append(stamp)
        return chunks

    def snapshot_read(self, filter: ChunkFilter) -> list[ChunkStamp]:
        """Consistent read as of call time under REPEATABLE READ (MVCC)."""
        clauses, params = _filter_clauses(filter)
        where = " AND ".join(clauses)
        with _transaction(self._conn):
            self._conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            rows = self._exec_rows(
                f"SELECT * FROM {self.table_name} WHERE {where} ORDER BY ingested_at", params
            )
            return [_to_stamp(row) for row in rows]

    def list_chunks(self, filter: ChunkFilter, page: Page) -> PageResult[ChunkStamp]:
        clauses, params = _filter_clauses(filter)
        where = " AND ".join(clauses)
        count_row = self._exec_row(f"SELECT COUNT(*) FROM {self.table_name} WHERE {where}", params)
        total = _count_value(count_row)
        rows = self._exec_rows(
            f"SELECT * FROM {self.table_name} WHERE {where} ORDER BY ingested_at DESC LIMIT %s OFFSET %s",
            [*params, page.limit, page.offset],
        )
        return PageResult(
            items=[_to_stamp(row) for row in rows],
            total=total,
            offset=page.offset,
            limit=page.limit,
        )


# ---------------------------------------------------------------- module helpers


def _count_value(row: Any) -> int:
    """Extract a COUNT(*) scalar regardless of the connection row factory."""
    if row is None:
        return 0
    return int(row["count"]) if isinstance(row, dict) else int(row[0])


@contextmanager
def _transaction(conn: Any) -> Iterator[None]:
    """Begin a real outer transaction; roll back any lingering read transaction
    first so psycopg3 does not nest into a savepoint."""
    conn.rollback()
    with conn.transaction():
        yield


def chunks_ddl(table: str, dim: int) -> str:
    """DDL for the chunks table carrying every appendix A.1 field."""
    return (
        f"CREATE TABLE IF NOT EXISTS {table} ("
        "chunk_id TEXT PRIMARY KEY, "
        "text TEXT NOT NULL, "  # verbatim channel: never post-processed
        f"vector_dense vector({dim}) NOT NULL, "
        "vector_sparse JSONB NOT NULL, "  # struct {indices int[], values float[]}
        "profile_id TEXT NOT NULL, "
        "session_id TEXT, "
        "turn_start INTEGER, "
        "turn_end INTEGER, "
        "cognitive_tier INTEGER NOT NULL DEFAULT 3, "
        "model_id TEXT NOT NULL, "
        "anima_id TEXT, "
        "cues JSONB NOT NULL, "
        "provenance JSONB NOT NULL, "
        "score JSONB NOT NULL, "
        "decay_weight DOUBLE PRECISION NOT NULL DEFAULT 1.0, "
        "last_reinforced DOUBLE PRECISION, "
        "consolidated BOOLEAN NOT NULL DEFAULT FALSE, "
        "ingested_at DOUBLE PRECISION NOT NULL, "
        "peripheral_gaps BOOLEAN NOT NULL DEFAULT FALSE, "
        "needs_reconcile BOOLEAN NOT NULL DEFAULT FALSE, "
        "hit_count INTEGER NOT NULL DEFAULT 0, "
        "last_hit_at DOUBLE PRECISION, "
        "reinforce_count INTEGER NOT NULL DEFAULT 0"
        ")"
    )


def hnsw_index_ddl(table: str) -> str:
    """Approximate-nearest-neighbor index over the dense column (cosine)."""
    return (
        f"CREATE INDEX IF NOT EXISTS {table}_dense_hnsw ON {table} "
        "USING hnsw (vector_dense vector_cosine_ops)"
    )


def _filter_clauses(filter: ChunkFilter) -> tuple[list[str], list[Any]]:
    """WHERE fragments (with params) for one profile, mirroring sqlite_graph's
    json accessors via jsonb arrows."""
    clauses = ["profile_id = %s"]
    params: list[Any] = [filter.profile_id]
    if filter.min_decay > 0.0:
        clauses.append("decay_weight >= %s")
        params.append(filter.min_decay)
    if filter.ingested_after is not None:
        clauses.append("ingested_at >= %s")
        params.append(filter.ingested_after)
    if filter.ingested_before is not None:
        clauses.append("ingested_at <= %s")
        params.append(filter.ingested_before)
    if filter.session_id is not None:
        clauses.append("session_id = %s")
        params.append(filter.session_id)
    if filter.turn_start is not None:
        clauses.append("turn_start IS NOT NULL AND turn_start >= %s")
        params.append(filter.turn_start)
    if filter.turn_end is not None:
        clauses.append("turn_end IS NOT NULL AND turn_end <= %s")
        params.append(filter.turn_end)
    if filter.entities:
        clauses.append(
            "EXISTS (SELECT 1 FROM jsonb_array_elements_text(cues->'entities') e WHERE e = ANY(%s))"
        )
        params.append(list(filter.entities))
    if filter.consolidated is not None:
        clauses.append("consolidated = %s")
        params.append(bool(filter.consolidated))
    if filter.needs_reconcile is not None:
        clauses.append("needs_reconcile = %s")
        params.append(bool(filter.needs_reconcile))
    return clauses, params


def _to_row(
    chunk: ChunkStamp,
    dense: Sequence[float],
    sparse: SparseVector | None,
) -> dict[str, Any]:
    cues = chunk.cues
    emotion = cues.emotion
    provenance = chunk.provenance
    return {
        "chunk_id": chunk.chunk_id,
        "text": chunk.text,
        "vector_dense": [float(value) for value in dense],
        "vector_sparse": (
            {"indices": [int(i) for i in sparse.indices], "values": [float(v) for v in sparse.values]}
            if sparse is not None
            else {"indices": [], "values": []}
        ),
        "profile_id": chunk.profile_id,
        "session_id": provenance.session_id,
        "turn_start": chunk.turn_start,
        "turn_end": chunk.turn_end,
        "cognitive_tier": int(chunk.cognitive_tier),
        "model_id": chunk.model_id,
        "anima_id": chunk.persona_id,
        "cues": {
            "project": cues.project,
            "host": cues.host,
            "task": cues.task,
            "tools_used": [str(tool) for tool in cues.tools_used],
            "time_bucket": cues.time_bucket,
            "entities": [str(entity) for entity in cues.entities],
            "emotion_valence": float(emotion.valence) if emotion and emotion.valence is not None else None,
        },
        "provenance": {
            "asserted_by": provenance.asserted_by,
            "agent_id": provenance.agent_id,
            "session_id": provenance.session_id,
            "source": provenance.source,
            "confidence": float(provenance.confidence),
            "asserted_at": float(provenance.asserted_at),
            "history": [_event_to_row(event) for event in provenance.history],
        },
        "score": {
            "emotion": 0.0,
            "novelty": 0.0,
            "causal": 0.0,
            "total": float(chunk.score),
        },
        "decay_weight": float(chunk.decay_weight),
        "last_reinforced": float(chunk.ingested_at),
        "consolidated": bool(chunk.consolidated),
        "ingested_at": float(chunk.ingested_at),
        "peripheral_gaps": bool(emotion.peripheral_gaps) if emotion else False,
        "needs_reconcile": False,
        "hit_count": 0,
        "last_hit_at": None,
        "reinforce_count": 0,
    }


def _to_stamp(row: dict[str, Any]) -> ChunkStamp:
    cues_row = row["cues"] or {}
    prov_row = row["provenance"] or {}
    score_row = row["score"] or {}
    valence_raw = cues_row.get("emotion_valence")
    valence = float(valence_raw) if isinstance(valence_raw, (int, float)) else None
    peripheral_gaps = bool(row["peripheral_gaps"])
    has_emotion = valence is not None or peripheral_gaps
    return ChunkStamp(
        chunk_id=str(row["chunk_id"]),
        profile_id=str(row["profile_id"]),
        text=str(row["text"]),
        cognitive_tier=CognitiveTier(int(row["cognitive_tier"])),
        model_id=str(row["model_id"]),
        persona_id=row.get("anima_id"),
        cues=Cues(
            project=cues_row.get("project"),
            host=cues_row.get("host"),
            task=cues_row.get("task"),
            tools_used=[str(tool) for tool in (cues_row.get("tools_used") or [])],
            time_bucket=cues_row.get("time_bucket"),
            entities=[str(entity) for entity in (cues_row.get("entities") or [])],
            emotion=(EmotionCue(valence=valence, peripheral_gaps=peripheral_gaps) if has_emotion else None),
        ),
        provenance=Provenance(
            asserted_by=str(prov_row.get("asserted_by", "")),
            agent_id=prov_row.get("agent_id"),
            session_id=prov_row.get("session_id"),
            source=str(prov_row.get("source", "")),
            confidence=float(prov_row.get("confidence", 0.5)),
            asserted_at=float(prov_row.get("asserted_at", 0.0)),
            history=[ProvenanceEvent(**_event_from_row(event)) for event in (prov_row.get("history") or [])],
        ),
        decay_weight=float(row["decay_weight"]),
        score=float(score_row.get("total", 0.0)),
        consolidated=bool(row["consolidated"]),
        ingested_at=float(row["ingested_at"]),
        turn_start=int(row["turn_start"]) if row.get("turn_start") is not None else None,
        turn_end=int(row["turn_end"]) if row.get("turn_end") is not None else None,
    )


def _sparse_similarity(query: SparseVector, stored: Any) -> float:
    if not isinstance(stored, dict):
        return 0.0
    indices = stored.get("indices") or []
    values = stored.get("values") or []
    if not indices or not values:
        return 0.0
    left = dict(zip(query.indices, query.values, strict=False))
    right = dict(zip((int(i) for i in indices), (float(v) for v in values), strict=False))
    query_norm = math.sqrt(sum(v * v for v in query.values)) or 1.0
    stored_norm = math.sqrt(sum(v * v for v in right.values())) or 1.0
    dot = sum(weight * right[index] for index, weight in left.items() if index in right)
    return float(dot / (query_norm * stored_norm))


def _dense_to_list(value: Any) -> list[float] | None:
    if value is None:
        return None
    if isinstance(value, Vector) or hasattr(value, "to_list"):
        return [float(v) for v in value.to_list()]
    if isinstance(value, (list, tuple)):
        return [float(v) for v in value]
    return None


def _event_to_row(event: ProvenanceEvent) -> dict[str, Any]:
    return {
        "at": float(event.at),
        "action": event.action,
        "actor": event.actor,
        "detail": dict(event.detail or {}),
    }


def _event_from_row(event: dict[str, Any]) -> dict[str, Any]:
    detail = event.get("detail") or {}
    return {
        "at": float(event.get("at", 0.0)),
        "action": str(event.get("action", "")),
        "actor": str(event.get("actor", "")),
        "detail": dict(detail) if isinstance(detail, dict) else {},
    }
