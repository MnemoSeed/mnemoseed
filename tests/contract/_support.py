"""Shared support for the driver-agnostic contract suite (prd-08 FR-8.5 / AC-3).

One parametrized stack (embedded or live pg) backs every contract test, so the
same behavioral assertions run against both driver families. The stack holds
one driver per layer plus helpers to reach the raw rows that the public ports
deliberately do not expose (turn bounds on chunks, usage counters).

The pg parametrization skips cleanly offline / in CI (NFR-8.2); nothing here
ever downloads a model — the embedder is the deterministic synthetic driver
(prd-08 D7).
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from pgvector import Vector

from mnemoseed.schema.graph import Edge, GraphNode, NodeType, RelType
from mnemoseed.schema.stamp import ChunkStamp, CognitiveTier, Cues, Provenance
from mnemoseed.storage.drivers.lancedb_embedded import LanceDbEmbeddedStore
from mnemoseed.storage.drivers.lancedb_embedded import _escape as _lance_escape
from mnemoseed.storage.drivers.pg_graph import PgGraphDriver
from mnemoseed.storage.drivers.pg_meta import PgMetaDriver
from mnemoseed.storage.drivers.pgvector import PgVectorStore
from mnemoseed.storage.drivers.pgvector import _to_row as _pg_to_row
from mnemoseed.storage.drivers.sqlite_graph import SqliteGraphDriver
from mnemoseed.storage.drivers.sqlite_meta import SqliteMetaDriver
from mnemoseed.storage.drivers.synthetic_embedder import SyntheticEmbedder
from mnemoseed.storage.ports import SparseVector

PG_DSN = os.environ.get("MNEMOSEED_TEST_PG_DSN") or ""


def require_pg() -> str:
    """Skip cleanly when no live Postgres is configured (CI / offline)."""
    if not PG_DSN:
        pytest.skip("MNEMOSEED_TEST_PG_DSN not set (live-Postgres check skips offline / in CI)")
    return PG_DSN


PROFILE = "alice"
DIMENSION = 64

_PREF_PROPS: dict[str, Any] = {
    "domain": "coding",
    "statement": "dark mode",
    "valence": 0.8,
    "prior_width": 0.3,
    "trait_anchor": "anima-1",
    "evidence_chain": [{"event": "created", "at": 123.0}],
}


@dataclass
class ContractStack:
    """One fully-wired driver family under contract test."""

    backend: str
    vector: Any
    graph: Any
    meta: Any
    embed: SyntheticEmbedder
    dimension: int = DIMENSION
    profile: str = PROFILE

    def text_vector(self, text: str) -> list[float]:
        return self.embed.embed(text).dense

    async def close(self) -> None:
        for store in (self.vector, self.graph, self.meta):
            closer = getattr(store, "close", None)
            if closer is not None:
                await closer()


def build_embedded(tmp_path: Path) -> ContractStack:
    """Embedded stack: lancedb chunks + sqlite graph + sqlite meta + synthetic."""
    return ContractStack(
        backend="embedded",
        vector=LanceDbEmbeddedStore(uri=tmp_path / "chunks.lance", dimensions=DIMENSION),
        graph=SqliteGraphDriver(path=tmp_path / "cortex.db"),
        meta=SqliteMetaDriver(path=tmp_path / "meta.db"),
        embed=SyntheticEmbedder(dimension=DIMENSION),
    )


def build_pg(dsn: str) -> ContractStack:
    """Postgres stack.

    Each store gets its own random schema (D6: named instances are separate
    schemas). The migration tracker is per-schema and store-tagged: graph and
    meta must not share one ``schema_version`` table, otherwise the second store
    to initialize would see the other's version rows and skip its own tables.
    """
    vector = PgVectorStore(dsn=dsn, dimensions=DIMENSION, schema=f"contract_v_{uuid.uuid4().hex[:8]}")
    graph = PgGraphDriver(dsn=dsn, schema=f"contract_g_{uuid.uuid4().hex[:8]}")
    meta = PgMetaDriver(dsn=dsn, schema=f"contract_m_{uuid.uuid4().hex[:8]}")
    return ContractStack(
        backend="pg",
        vector=vector,
        graph=graph,
        meta=meta,
        embed=SyntheticEmbedder(dimension=DIMENSION),
    )


# ---------------------------------------------------------------- stamp makers


def make_stamp(
    chunk_id: str,
    text: str,
    *,
    profile_id: str = PROFILE,
    session: str | None = "s1",
    score: float = 0.0,
    decay: float = 1.0,
    entities: tuple[str, ...] = (),
    consolidated: bool = False,
    ingested_at: float = 1.0,
) -> ChunkStamp:
    return ChunkStamp(
        chunk_id=chunk_id,
        profile_id=profile_id,
        text=text,
        cognitive_tier=CognitiveTier.TIER_1,
        model_id="contract-model",
        persona_id="p1",
        cues=Cues(
            project="contract-suite",
            tools_used=["pytest"],
            time_bucket="diurnal",
            entities=list(entities),
        ),
        provenance=Provenance(
            asserted_by="contract-model",
            session_id=session,
            source="manual",
            confidence=0.8,
            asserted_at=100.0,
        ),
        decay_weight=decay,
        score=score,
        consolidated=consolidated,
        ingested_at=ingested_at,
    )


def make_prov(**over: object) -> Provenance:
    base: dict[str, object] = dict(asserted_by="contract-agent", source="session://s-contract")
    base.update(over)
    return Provenance(**base)


def make_pref(**over: object) -> GraphNode:
    base: dict[str, object] = dict(
        profile_id=PROFILE,
        node_type=NodeType.PREFERENCE,
        entities=["ui"],
        props=dict(_PREF_PROPS),
        provenance=make_prov(),
        valid_from=time.time() - 100.0,
    )
    base.update(over)
    return GraphNode(**base)


def make_edge(
    src: str,
    dst: str,
    *,
    rel: RelType = RelType.EVIDENCED_BY,
    profile_id: str = PROFILE,
    weight: float = 1.0,
    created_at: float | None = None,
) -> Edge:
    """Edge helper (profile scopes traversal, not node membership)."""
    return Edge(
        src=src,
        dst=dst,
        rel=rel,
        profile_id=profile_id,
        weight=weight,
        created_at=time.time() if created_at is None else created_at,
    )


def make_intention(over: dict[str, Any] | None = None, **kw: object) -> GraphNode:
    props: dict[str, Any] = {"trigger_condition": "when", "action": "act", "status": "pending"}
    if over:
        props.update(over)
    return GraphNode(
        profile_id=PROFILE,
        node_type=NodeType.INTENTION,
        props=props,
        provenance=make_prov(),
        **kw,
    )


# ---------------------------------------------------------------- raw-layer helpers

# Turn bounds and usage counters are stored per-row but are deliberately not
# settable/readable through the public ports, so contract tests reach the raw
# row on both drivers. Identical semantics are what is under test here, not the
# bytes of either backend.


def write_turn_chunk(
    stack: ContractStack,
    chunk_id: str,
    text: str,
    session: str,
    start: int,
    end: int,
    *,
    profile_id: str = PROFILE,
    dense: list[float] | None = None,
    sparse: SparseVector | None = None,
) -> None:
    """Insert a chunk whose session/turn bounds survive the write path."""
    stamp = make_stamp(chunk_id, text, profile_id=profile_id, session=session)
    if dense is None:
        dense = list(stack.text_vector(text))
    if stack.backend == "embedded":
        row = stack.vector._to_row(stamp, dense, sparse)
        row["session_id"] = session
        row["turn_start"] = int(start)
        row["turn_end"] = int(end)
        stack.vector._table.merge_insert("chunk_id").when_not_matched_insert_all().execute([row])
    else:
        row = dict(_pg_to_row(stamp, dense, sparse))
        row["session_id"] = session
        row["turn_start"] = int(start)
        row["turn_end"] = int(end)
        row["vector_dense"] = Vector(list(dense))
        columns = list(row)
        placeholders = ", ".join(["%s"] * len(columns))
        stack.vector._conn.execute(
            f"INSERT INTO {stack.vector.table_name} ({', '.join(columns)}) VALUES ({placeholders}) "
            "ON CONFLICT (chunk_id) DO NOTHING",
            [row[column] for column in columns],
        )
        # psycopg3 stays in an implicit transaction: the driver's next read
        # path begins with a rollback() that would silently discard this row,
        # so the raw helper must commit its own write (the embedded arm's
        # lance merge_insert is already immediately durable).
        stack.vector._conn.commit()


def raw_chunk(stack: ContractStack, chunk_id: str) -> dict[str, Any]:
    """Raw chunks row for usage counters / turn bounds ({} when absent)."""
    if stack.backend == "embedded":
        rows = stack.vector._table.search().where(f"chunk_id = {_lance_escape(chunk_id)}").limit(1).to_list()
        return rows[0] if rows else {}
    rows = stack.vector._conn.execute(
        f"SELECT * FROM {stack.vector.table_name} WHERE chunk_id = %s", (chunk_id,)
    ).fetchall()
    return dict(rows[0]) if rows else {}


def raw_meta_row(stack: ContractStack, table: str, where_column: str, value: Any) -> dict[str, Any]:
    """Raw meta row for columns the ports do not expose (token revocation)."""
    if stack.backend == "embedded":
        row = stack.meta._conn.execute(f"SELECT * FROM {table} WHERE {where_column} = ?", (value,)).fetchone()
        return dict(row) if row is not None else {}
    rows = stack.meta._conn.execute(f"SELECT * FROM {table} WHERE {where_column} = %s", (value,)).fetchall()
    return dict(rows[0]) if rows else {}


def run(coro: Any) -> Any:
    """Run an async closer in a plain-test context."""
    return asyncio.run(coro)
