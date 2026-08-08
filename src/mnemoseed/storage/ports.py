"""Storage port interfaces, capability flags, and the startup gate.

The storage layer is ports-and-adapters: four port interfaces (VectorStore,
GraphStore, MetaStore, Embedder) with a fixed method surface (prd-08 appendix B),
a driver registry behind each port (named multi-instance per layer), and a
capability gate that runs against the resolved config at daemon boot.

Backends are not interchangeable: every driver honestly declares its capability
set, and missing capabilities produce explicit degradations or a refused startup
— never silent failures.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, ClassVar, Protocol

from mnemoseed.schema.graph import Edge, GraphNode, NodeType
from mnemoseed.schema.stamp import ChunkStamp

# ---------------------------------------------------------------- data types


@dataclass(frozen=True)
class SparseVector:
    """Structured sparse vector: parallel index/value pairs (never a dense array).

    The bge-m3 sparse output is ~250k dimensions with few non-zero entries.
    """

    indices: tuple[int, ...]
    values: tuple[float, ...]

    def __post_init__(self) -> None:
        if len(self.indices) != len(self.values):
            raise ValueError("sparse indices and values must be parallel")


@dataclass(frozen=True)
class Page:
    """Pagination cursor for filtered list reads."""

    offset: int = 0
    limit: int = 50


@dataclass(frozen=True)
class PageResult[T]:
    """One page of a filtered list read."""

    items: list[T]
    total: int
    offset: int
    limit: int


@dataclass(frozen=True)
class WeightUpdate:
    """Bulk vector-weight update (decay / reinforcement write-back)."""

    chunk_id: str
    decay_weight: float | None = None
    last_reinforced: float | None = None
    reinforce_count: int | None = None


@dataclass(frozen=True)
class ChunkFilter:
    """Metadata filter for vector reads. profile_id is always explicit."""

    profile_id: str
    min_decay: float = 0.0
    ingested_after: float | None = None
    ingested_before: float | None = None
    session_id: str | None = None
    turn_start: int | None = None
    turn_end: int | None = None
    entities: tuple[str, ...] = ()
    consolidated: bool | None = None


@dataclass(frozen=True)
class SearchHit:
    """One hybrid-search result (similarity feeds downstream scoring)."""

    chunk: ChunkStamp
    similarity: float


@dataclass(frozen=True)
class NodeFilter:
    """Filter for graph reads. profile_id is always explicit."""

    profile_id: str
    node_type: NodeType | None = None
    entities: tuple[str, ...] = ()
    min_decay: float = 0.0


@dataclass(frozen=True)
class GraphWeightUpdate:
    """One entry in a batch decay recompute."""

    node_id: str
    decay_weight: float


@dataclass(frozen=True)
class TimelineEvent:
    """One entry in a version-chain timeline playback."""

    when: float
    version: int
    summary: str


@dataclass(frozen=True)
class TurnRange:
    """Structured turn boundary (snapshot scoping, safe purge, pool events)."""

    start: int
    end: int


@dataclass(frozen=True)
class PoolState:
    """Score-pool balance and the current watermark."""

    balance: float
    watermark: TurnRange | None = None


@dataclass(frozen=True)
class StoredProfile:
    """Profile record (identity namespace for D5 isolation)."""

    profile_id: str
    display_name: str = ""
    created_at: float = 0.0


@dataclass(frozen=True)
class Token:
    """Issued credential for a profile."""

    token_id: str
    profile_id: str
    scopes: Sequence[str] = ()
    issued_at: float = 0.0
    expires_at: float | None = None
    revoked: bool = False


@dataclass(frozen=True)
class AuditEntry:
    """One append-only audit record."""

    actor: str
    action: str
    detail: dict[str, Any] = field(default_factory=dict)
    at: float = 0.0
    id: int | None = None


@dataclass(frozen=True)
class AuditFilter:
    """Filter for the counted, paginated audit read."""

    actor: str | None = None
    action: str | None = None
    since: float | None = None
    until: float | None = None


@dataclass(frozen=True)
class ConfigEntry:
    """One versioned config value."""

    key: str
    value: dict[str, Any]
    version: int
    updated_at: float


@dataclass(frozen=True)
class DreamRun:
    """Dream-engine run record (console panel + idempotent recovery)."""

    run_id: str = ""
    session_id: str | None = None
    turn_range: TurnRange | None = None
    model_id: str = ""
    started_at: float = 0.0
    finished_at: float | None = None
    tokens: int = 0
    cost: float = 0.0
    interrupted: bool = False
    dropped_count: int = 0


@dataclass(frozen=True)
class DreamRunFilter:
    """Filter for dream-run history reads."""

    session_id: str | None = None
    since: float | None = None
    until: float | None = None
    interrupted: bool | None = None


@dataclass(frozen=True)
class EmbeddingResult:
    """Embedder output; sparse is absent when the driver lacks the capability."""

    dense: Sequence[float]
    sparse: SparseVector | None = None


@dataclass(frozen=True)
class DriverInfo:
    """Driver identity and static capability declaration (registry entry)."""

    name: str
    capabilities: frozenset[Capability]
    description: str = ""


# ---------------------------------------------------------------- capabilities


class Capability(StrEnum):
    """Minimal declared capability set (prd-08 FR-8.6, frozen exact list).

    The list is extensible; the validation mechanism is what is frozen.
    """

    VECTOR_HYBRID_SEARCH = "vector.hybrid_search"
    VECTOR_METADATA_FILTER = "vector.metadata_filter"
    VECTOR_SNAPSHOT = "vector.snapshot"
    GRAPH_TRAVERSE_2HOP = "graph.traverse_2hop"
    GRAPH_VERSION_CHAIN = "graph.version_chain"
    GRAPH_COOCCURRENCE_EDGES = "graph.cooccurrence_edges"
    META_TRANSACTION = "meta.transaction"
    META_CONCURRENT_READERS = "meta.concurrent_readers"
    EMBED_LOCAL_INFERENCE = "embed.local_inference"
    EMBED_BATCH = "embed.batch"
    EMBED_SPARSE_OUTPUT = "embed.sparse_output"

    @property
    def layer(self) -> str:
        """Owning layer ("vector" / "graph" / "meta" / "embed")."""
        return self.value.split(".", 1)[0]


class ValidationSeverity(StrEnum):
    """Startup-gate severity from the prd-08 degradation table."""

    HARD = "hard"  # refuse startup, list the missing capabilities
    DEGRADE = "degrade"  # pass startup with an explicit logged warning


@dataclass(frozen=True)
class CapabilityPolicy:
    """How a missing capability behaves at the startup gate (appendix C)."""

    capability: Capability
    severity: ValidationSeverity
    feature: str
    behavior: str


@dataclass(frozen=True)
class CapabilityIssue:
    """One concrete gate finding, bound to a resolved driver instance."""

    capability: Capability
    severity: ValidationSeverity
    layer: str
    instance: str
    driver: str
    feature: str
    behavior: str


@dataclass
class ValidationReport:
    """Startup-gate result for the resolved storage stack."""

    ok: bool
    hard_missing: list[CapabilityIssue] = field(default_factory=list)
    degradations: list[CapabilityIssue] = field(default_factory=list)

    @property
    def missing(self) -> list[CapabilityIssue]:
        """Every gated issue, hard first — the daemon logs over this list."""
        return [*self.hard_missing, *self.degradations]


DEGRADATION_TABLE: tuple[CapabilityPolicy, ...] = (
    # hard requirements — refuse startup (appendix C)
    CapabilityPolicy(
        capability=Capability.META_TRANSACTION,
        severity=ValidationSeverity.HARD,
        feature="score pool and watermark atomicity",
        behavior="atomic pool_add / advance_watermark are a hard dependency; startup refused",
    ),
    CapabilityPolicy(
        capability=Capability.GRAPH_VERSION_CHAIN,
        severity=ValidationSeverity.HARD,
        feature="reconcile and as_of bi-temporal queries",
        behavior="version-chain replay and as_of are a hard dependency; startup refused",
    ),
    CapabilityPolicy(
        capability=Capability.VECTOR_METADATA_FILTER,
        severity=ValidationSeverity.HARD,
        feature="profile isolation and freshness guard",
        behavior="profile_id isolation and ingested_at filtering are a hard dependency; startup refused",
    ),
    # degradations — pass startup with an explicit warning (appendix C)
    CapabilityPolicy(
        capability=Capability.EMBED_SPARSE_OUTPUT,
        severity=ValidationSeverity.DEGRADE,
        feature="hybrid retrieval sparse path",
        behavior="no sparse vectors produced; retrieval degrades to dense-only with a quality warning",
    ),
    CapabilityPolicy(
        capability=Capability.VECTOR_HYBRID_SEARCH,
        severity=ValidationSeverity.DEGRADE,
        feature="hybrid retrieval",
        behavior="hybrid retrieval degrades to dense-only, retrieval quality warning",
    ),
    CapabilityPolicy(
        capability=Capability.VECTOR_SNAPSHOT,
        severity=ValidationSeverity.DEGRADE,
        feature="dream-engine snapshot isolation",
        behavior="dream snapshot degrades to turn-range logical isolation, isolation strength warning",
    ),
    CapabilityPolicy(
        capability=Capability.GRAPH_COOCCURRENCE_EDGES,
        severity=ValidationSeverity.DEGRADE,
        feature="rerank co-occurrence term",
        behavior="rerank drops the epsilon co-occurrence term, retrieval quality warning",
    ),
    CapabilityPolicy(
        capability=Capability.META_CONCURRENT_READERS,
        severity=ValidationSeverity.DEGRADE,
        feature="console concurrent reads",
        behavior="console reads serialize on a single reader, concurrency performance warning",
    ),
    CapabilityPolicy(
        capability=Capability.EMBED_BATCH,
        severity=ValidationSeverity.DEGRADE,
        feature="batch vectorization",
        behavior="embedding runs one text at a time, throughput warning",
    ),
)


# ---------------------------------------------------------------- errors


class StorageError(Exception):
    """Base storage-layer error."""


class UnknownDriverError(StorageError):
    """A driver name that no registered driver provides."""

    def __init__(self, layer: str, driver: str, available: Sequence[str]) -> None:
        if available:
            message = f"unknown {layer} driver {driver!r} (available: {', '.join(available)})"
        else:
            message = f"unknown {layer} driver {driver!r} (no {layer} drivers registered)"
        super().__init__(message)


class CapabilityStartupError(StorageError):
    """The startup gate refused to boot because hard capabilities are missing."""

    def __init__(self, missing: Sequence[CapabilityIssue]) -> None:
        entries = [
            f"  - {issue.layer}.{issue.instance} driver {issue.driver!r} lacks "
            f"{issue.capability.value} ({issue.feature}): {issue.behavior}"
            for issue in missing
        ]
        super().__init__("storage capability gate failed; missing capabilities:\n" + "\n".join(entries))


# ---------------------------------------------------------------- ports


class VectorStore(Protocol):
    """Hippocampus: verbatim shard storage plus metadata-filtered search."""

    info: ClassVar[DriverInfo]

    def capabilities(self) -> frozenset[Capability]:
        raise NotImplementedError

    def upsert_chunk(
        self,
        chunk: ChunkStamp,
        dense: Sequence[float],
        sparse: SparseVector | None = None,
    ) -> None:
        raise NotImplementedError

    def get_chunk(self, chunk_id: str) -> ChunkStamp | None:
        raise NotImplementedError

    def delete_chunk(self, chunk_id: str) -> None:
        raise NotImplementedError

    def search(
        self,
        dense: Sequence[float],
        sparse: SparseVector | None,
        filter: ChunkFilter,
        top_k: int,
    ) -> list[SearchHit]:
        raise NotImplementedError

    def near_duplicate(self, vector: Sequence[float], threshold: float) -> list[ChunkStamp]:
        raise NotImplementedError

    def snapshot_read(self, filter: ChunkFilter) -> list[ChunkStamp]:
        raise NotImplementedError

    def mark_consolidated(self, chunk_ids: Sequence[str]) -> None:
        raise NotImplementedError

    def purge_range(self, session_id: str, turn_start: int, turn_end: int) -> int:
        raise NotImplementedError

    def update_weights(self, updates: Sequence[WeightUpdate]) -> None:
        raise NotImplementedError

    def list_chunks(self, filter: ChunkFilter, page: Page) -> PageResult[ChunkStamp]:
        raise NotImplementedError


class GraphStore(Protocol):
    """Cortex: consolidated structured long-term memory with version chains."""

    info: ClassVar[DriverInfo]

    def capabilities(self) -> frozenset[Capability]:
        raise NotImplementedError

    def upsert_node(self, node: GraphNode) -> None:
        raise NotImplementedError

    def get_node(self, node_id: str) -> GraphNode | None:
        raise NotImplementedError

    def list_nodes(self, filter: NodeFilter, page: Page) -> PageResult[GraphNode]:
        raise NotImplementedError

    def add_edge(self, edge: Edge) -> None:
        raise NotImplementedError

    def bump_cooccurrence(self, node_a: str, node_b: str, profile_id: str) -> None:
        raise NotImplementedError

    def traverse(self, node_id: str, depth: int = 2, filter: NodeFilter | None = None) -> list[GraphNode]:
        raise NotImplementedError

    def find_same_predicate(self, subject: str, predicate: str, profile_id: str) -> list[GraphNode]:
        raise NotImplementedError

    def set_flags(self, nodes: Sequence[str], flags: Sequence[GraphFlag]) -> None:
        raise NotImplementedError

    def clear_flags(self, nodes: Sequence[str], flags: Sequence[GraphFlag]) -> None:
        raise NotImplementedError

    def invalidate(self, node_id: str, valid_to: float) -> None:
        raise NotImplementedError

    def append_version(self, node: GraphNode) -> None:
        raise NotImplementedError

    def versions(self, node_id: str) -> list[GraphNode]:
        raise NotImplementedError

    def diff(self, version_a: str, version_b: str) -> dict[str, Any]:
        raise NotImplementedError

    def timeline(self, node_id: str) -> list[TimelineEvent]:
        raise NotImplementedError

    def as_of(self, timestamp: float, filter: NodeFilter) -> list[GraphNode]:
        raise NotImplementedError

    def batch_update_weights(self, updates: Sequence[GraphWeightUpdate]) -> None:
        raise NotImplementedError

    def query_intentions(self, status: IntentionStatus, due_before: float) -> list[GraphNode]:
        raise NotImplementedError


class MetaStore(Protocol):
    """Metadata: profiles, tokens, score pool, watermarks, config, audit."""

    info: ClassVar[DriverInfo]

    def capabilities(self) -> frozenset[Capability]:
        raise NotImplementedError

    def pool_add(self, points: float, turn_range: TurnRange) -> None:
        raise NotImplementedError

    def pool_state(self) -> PoolState:
        raise NotImplementedError

    def advance_watermark(self, turn_range: TurnRange) -> None:
        raise NotImplementedError

    def upsert_profile(self, profile: StoredProfile) -> None:
        raise NotImplementedError

    def get_profile(self, profile_id: str) -> StoredProfile | None:
        raise NotImplementedError

    def delete_profile(self, profile_id: str) -> None:
        raise NotImplementedError

    def list_profiles(self) -> list[StoredProfile]:
        raise NotImplementedError

    def issue_token(
        self,
        profile_id: str,
        scopes: Sequence[str],
        expires_at: float | None = None,
    ) -> Token:
        raise NotImplementedError

    def revoke_token(self, token_id: str) -> None:
        raise NotImplementedError

    def get_config(self, key: str, version: int | None = None) -> ConfigEntry | None:
        raise NotImplementedError

    def set_config(self, key: str, value: dict[str, Any]) -> int:
        raise NotImplementedError

    def rollback_config(self, key: str, version: int) -> None:
        raise NotImplementedError

    def audit_append(self, entry: AuditEntry) -> None:
        raise NotImplementedError

    def audit_query(self, filter: AuditFilter, page: Page) -> PageResult[AuditEntry]:
        raise NotImplementedError

    def record_dream_run(self, run: DreamRun) -> str:
        raise NotImplementedError

    def list_dream_runs(self, filter: DreamRunFilter, page: Page) -> PageResult[DreamRun]:
        raise NotImplementedError

    def schema_version(self) -> int:
        raise NotImplementedError

    def migrate(self, target: int | None = None) -> int:
        raise NotImplementedError


class Embedder(Protocol):
    """Vectorization provider."""

    info: ClassVar[DriverInfo]
    dimension: int

    def capabilities(self) -> frozenset[Capability]:
        raise NotImplementedError

    def embed(self, text: str) -> EmbeddingResult:
        raise NotImplementedError

    def embed_batch(self, texts: Sequence[str]) -> list[EmbeddingResult]:
        raise NotImplementedError


class GraphFlag(StrEnum):
    """Updatable graph workflow flags (prd-08 appendix A.2)."""

    NEEDS_RECONCILE = "needs_reconcile"
    PENDING_CONSOLIDATION = "pending_consolidation"
    CONFLICT_GROUP = "conflict_group"
    PERIPHERAL_GAPS = "peripheral_gaps"


class IntentionStatus(StrEnum):
    """Prospective-memory node lifecycle."""

    PENDING = "pending"
    FIRED = "fired"
    CANCELLED = "cancelled"


# Any resolved driver instance is one of the four ports.
Store = VectorStore | GraphStore | MetaStore | Embedder


# ---------------------------------------------------------------- validation


def validate_capabilities(instances: Mapping[str, Mapping[str, Store]]) -> ValidationReport:
    """Run the appendix C gate over resolved driver instances.

    Only capabilities present in DEGRADATION_TABLE are gated; the remaining
    declared flags (graph.traverse_2hop, embed.local_inference) are not part of
    the startup criteria. HARD findings refuse startup, DEGRADE findings log a
    warning. No path is silent.
    """
    issues: list[CapabilityIssue] = []
    for layer, named in instances.items():
        for instance_name, store in named.items():
            declared = store.capabilities()
            for policy in DEGRADATION_TABLE:
                if policy.capability.layer != layer:
                    continue
                if policy.capability not in declared:
                    issues.append(
                        CapabilityIssue(
                            capability=policy.capability,
                            severity=policy.severity,
                            layer=layer,
                            instance=instance_name,
                            driver=store.info.name,
                            feature=policy.feature,
                            behavior=policy.behavior,
                        )
                    )
    hard = [i for i in issues if i.severity is ValidationSeverity.HARD]
    degradable = [i for i in issues if i.severity is ValidationSeverity.DEGRADE]
    return ValidationReport(ok=not hard, hard_missing=hard, degradations=degradable)
