"""Storage port interfaces and the capability-declaration mechanism.

Rule: backends are not interchangeable. Every driver honestly declares its
capability set; the daemon validates it against REQUIRED_CAPS at startup, and
missing capabilities produce explicit degradations — never silent failures.
"""

from __future__ import annotations

import abc
import enum
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from mnemoseed.schema.graph import Edge, GraphNode, NodeType
from mnemoseed.schema.stamp import ChunkStamp


class Capability(enum.StrEnum):
    """Driver capability declaration. Extend when a feature needs more storage."""

    # VectorStore
    METADATA_FILTER = "metadata_filter"  # filter by stamp fields (cues/tier/profile)
    TIME_RANGE_FILTER = "time_range_filter"  # ingested_at range filter (freshness checks)
    PERSIST = "persist"  # durable storage (vs in-memory)

    # GraphStore
    VERSION_CHAIN = "version_chain"  # version chain / bi-temporal (as_of queries)
    TRANSACTIONS = "transactions"  # atomic multi-step writes
    SNAPSHOT = "snapshot"  # MVCC / consistent snapshots (dream-engine isolation)
    TRAVERSAL = "traversal"  # N-hop subgraph traversal (hybrid retrieval)

    # Embedder
    LOCAL_OFFLINE = "local_offline"  # works without network access


@dataclass(frozen=True)
class DriverInfo:
    """Driver registry entry."""

    name: str
    capabilities: frozenset[Capability]
    description: str = ""


@dataclass
class Degradation:
    """One explicit degradation: which capability is missing, which feature is
    affected, and what the fallback behavior is."""

    capability: Capability
    feature: str
    behavior: str


@dataclass
class CapabilityReport:
    """Startup validation result."""

    ok: bool
    missing: list[Degradation] = field(default_factory=list)


class StorageError(Exception):
    """Base storage-layer error."""


class CapabilityMissing(StorageError):
    """A capability the driver never declared was invoked."""


# ---------------------------------------------------------------- ports


class VectorStore(abc.ABC):
    """Hippocampus: vector storage of raw shards. profile_id is always passed
    explicitly — ports never infer identity."""

    info: DriverInfo

    @abc.abstractmethod
    async def upsert(self, stamp: ChunkStamp, embedding: list[float]) -> None: ...

    @abc.abstractmethod
    async def query(
        self,
        profile_id: str,
        embedding: list[float],
        top_k: int = 20,
        filters: dict[str, Any] | None = None,
        ingested_after: float | None = None,  # epoch seconds; freshness checks
    ) -> list[tuple[ChunkStamp, float]]:  # (shard, distance) — distance feeds scoring
        ...

    @abc.abstractmethod
    async def get(self, profile_id: str, chunk_id: str) -> ChunkStamp | None: ...

    @abc.abstractmethod
    async def delete(self, profile_id: str, chunk_id: str) -> None: ...

    @abc.abstractmethod
    async def count(self, profile_id: str) -> int: ...

    async def close(self) -> None:  # noqa: B027 optional hook
        pass


class GraphStore(abc.ABC):
    """Cortex: consolidated structured long-term memory."""

    info: DriverInfo

    @abc.abstractmethod
    async def put_node(self, node: GraphNode) -> None: ...

    @abc.abstractmethod
    async def get_node(self, node_id: str, as_of: float | None = None) -> GraphNode | None:
        """With as_of set, replay the version chain to the version in effect at
        that point in time."""

    @abc.abstractmethod
    async def find_nodes(
        self,
        profile_id: str,
        node_type: NodeType | None = None,
        entity: str | None = None,
        min_decay: float = 0.0,
        limit: int = 50,
    ) -> list[GraphNode]: ...

    @abc.abstractmethod
    async def put_edge(self, edge: Edge) -> None: ...

    @abc.abstractmethod
    async def neighbors(self, node_id: str, hops: int = 2) -> list[GraphNode]: ...

    @abc.abstractmethod
    async def supersede(self, old_id: str, new_node: GraphNode) -> None:
        """Reconcile rewrite: old node pinned with valid_to, new node chained in."""

    @abc.abstractmethod
    async def history(self, node_id: str) -> list[GraphNode]:
        """Full version-chain history."""

    @abc.abstractmethod
    async def close(self) -> None: ...


class MetaStore(abc.ABC):
    """Metadata: accounts/profiles/tokens, pools, watermarks, audit."""

    info: DriverInfo

    @abc.abstractmethod
    async def kv_get(self, ns: str, key: str) -> Any | None: ...

    @abc.abstractmethod
    async def kv_put(self, ns: str, key: str, value: Any) -> None: ...

    @abc.abstractmethod
    async def audit(self, actor: str, action: str, detail: dict[str, Any]) -> None: ...

    @abc.abstractmethod
    async def audit_iter(self, ns: str | None = None) -> AsyncIterator[dict[str, Any]]: ...

    @abc.abstractmethod
    async def close(self) -> None: ...


class Embedder(abc.ABC):
    """Embedding provider."""

    info: DriverInfo
    dimension: int

    @abc.abstractmethod
    async def embed(self, texts: list[str]) -> list[list[float]]: ...

    async def close(self) -> None:  # noqa: B027
        pass


# ---------------------------------------------------------------- registry

Registry = dict[str, type]

VECTOR_DRIVERS: Registry = {}
GRAPH_DRIVERS: Registry = {}
META_DRIVERS: Registry = {}
EMBED_DRIVERS: Registry = {}


def register(table: Registry):
    """Class decorator: register a driver by its info.name."""

    def deco(cls: type) -> type:
        table[cls.info.name] = cls
        return cls

    return deco


# Hard capability requirements per feature; checked at startup, missing entries
# become explicit degradations.
REQUIRED_CAPS: dict[str, tuple[str, Capability, str]] = {
    # feature_key: (driver_kind, capability, fallback behavior)
    "freshness_guard": (
        "vector",
        Capability.TIME_RANGE_FILTER,
        "pending-consolidation annotation disabled: retrieval cannot detect new "
        "evidence ingested after the watermark (feature off, warning logged)",
    ),
    "dream_snapshot": (
        "graph",
        Capability.SNAPSHOT,
        "dream-engine snapshot isolation degrades to logical turn-range isolation "
        "(write-back inside an interrupted window may see newer data; warning logged)",
    ),
    "as_of_query": (
        "graph",
        Capability.VERSION_CHAIN,
        "as_of point-in-time queries unavailable (API returns 501)",
    ),
    "hybrid_graph_path": (
        "graph",
        Capability.TRAVERSAL,
        "hybrid retrieval degrades to the vector path only (graph path off, warning logged)",
    ),
    "local_offline": (
        "embed",
        Capability.LOCAL_OFFLINE,
        "embedded preset requires a network embedding API (breaks the offline promise; startup error)",
    ),
}
