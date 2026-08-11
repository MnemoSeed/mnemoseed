"""Daemon memory surface (PRD-03 T4): the six /memory endpoints.

Router + service seam over the retrieval engine and the storage ports:

- POST /memory/recall     - CueExtractor -> HybridRetriever -> Assembler, with
                            envelope cues, top_k / budget / as_of overrides, the
                            honest-empty CoverageReport (FR-3.13), conflict
                            pairing / pending-consolidation / fresh-evidence
                            markers, and fire-and-forget usage events (FR-3.7).
- POST /memory/remember   - explicit user pin; provenance asserts
                            ``asserted_by="user"`` with source
                            ``EXPLICIT_PIN_SOURCE``; identical re-pins reinforce
                            the existing chunk instead of duplicating it.
- POST /memory/audit      - provenance + version chain + relevant audit rows.
- POST /memory/timeline   - per-node version replay, else profile-wide paging.
- POST /memory/export     - stable paged JSON dump including provenance.
- POST /memory/forget_this- GDPR deletion (design/03 2.4): chunk rows are
                            deleted, graph nodes are tombstoned (version chain
                            preserved), the audit trail records exactly what was
                            removed.
- POST /memory/dream_once / dream_status - the /dream command HTTP surface
                            (FR-2.8 manual-first): run exactly one manual dream
                            cycle and read the trigger's observability. Handlers
                            are async so the chain runs on the app event-loop
                            thread (the daemon's sqlite connections are bound to
                            it.

Profiles: the identity in every request is the explicit ``profile_id`` only —
the daemon never guesses identity (D5 isolation). Token auth is explicitly out
of scope for T4 (lands with PRD-06); this module only reserves the shape.

Determinism: no clocks except reading the live timestamp where the semantic
contract is timestamp-based (forget/deletion time, remember `asserted_at`,
usage-event `last_hit_at`); no randomness; no network.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Sequence
from dataclasses import replace
from typing import Annotated, Any, Self, cast

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field, model_validator

from mnemoseed.capture.stamper import ConsistencyVerdict, NearDuplicateChecker, WriteConfig
from mnemoseed.config import Config
from mnemoseed.dream import DreamTrigger, TriggerStatus
from mnemoseed.retrieve.assemble import (
    AssembledContext,
    AssembledEntry,
    Assembler,
)
from mnemoseed.retrieve.cues import CueExtractor
from mnemoseed.retrieve.hybrid import HybridRetriever
from mnemoseed.schema.graph import GraphNode
from mnemoseed.schema.stamp import ChunkStamp, CognitiveTier, Provenance, ProvenanceEvent
from mnemoseed.schema.turn import ProfileRef
from mnemoseed.storage.factory import Stores
from mnemoseed.storage.ports import (
    AuditEntry,
    AuditFilter,
    ChunkFilter,
    GraphStore,
    NodeFilter,
    Page,
    PageResult,
    VectorStore,
    WeightUpdate,
)

logger = logging.getLogger("mnemoseed.daemon.memory")

# Explicit-pin provenance source marker (FR-3.1). A /memory/remember write is
# asserted by the user and never merges into the capture provenance channel.
EXPLICIT_PIN_SOURCE = "memory.remember"


class MemoryNotFoundError(Exception):
    """The requested memory target does not exist for this profile."""


# ---------------------------------------------------------------- wire models

NonBlankText = Annotated[str, Field(min_length=1, pattern=r".*\S.*")]


class RecallRequest(BaseModel):
    profile_id: ProfileRef
    query: NonBlankText
    # Envelope cues ride through as weak rerank context (FR-3.14), never as a
    # candidate filter.
    host: str | None = None
    project: str | None = None
    time_bucket: str | None = None
    top_k: int | None = Field(default=None, ge=1, le=100)
    budget: int | None = Field(default=None, ge=1)
    as_of: float | None = None


class RememberRequest(BaseModel):
    profile_id: ProfileRef
    text: NonBlankText


class AuditRequest(BaseModel):
    profile_id: ProfileRef
    node_id: str | None = None
    chunk_id: str | None = None

    @model_validator(mode="after")
    def _has_target(self) -> Self:
        if self.node_id is None and self.chunk_id is None:
            raise ValueError("audit requires a node_id or a chunk_id target")
        return self


class TimelineRequest(BaseModel):
    profile_id: ProfileRef
    node_id: str | None = None


class ExportRequest(BaseModel):
    profile_id: ProfileRef
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=50, ge=1, le=500)


class ForgetRequest(BaseModel):
    profile_id: ProfileRef
    chunk_id: str | None = None
    node_id: str | None = None
    entity: str | None = None

    @model_validator(mode="after")
    def _has_target(self) -> Self:
        if self.chunk_id is None and self.node_id is None and self.entity is None:
            raise ValueError("forget_this requires a chunk_id, node_id, or entity target")
        return self


class DreamRequest(BaseModel):
    """Request body for the /dream command surface (FR-2.8 manual-first)."""

    profile_id: ProfileRef


# ---------------------------------------------------------------- as_of views


class _AsOfVectorView:
    """VectorStore facade replaying the chunk store as of one instant.

    Every filtered read is bound to ``at`` through ``ChunkFilter.ingested_before``
    (an otherwise current revision whose chunk was ingested later simply does
    not exist yet at ``at``); the rest of the VectorStore surface forwards to
    the live store so the retrieval engine works unchanged on the view.
    """

    def __init__(self, inner: VectorStore, at: float) -> None:
        self._inner = inner
        self._at = at

    def _scoped(self, filter: ChunkFilter) -> ChunkFilter:
        return replace(filter, ingested_before=self._at)

    def search(
        self,
        dense: Sequence[float],
        sparse: Any,
        filter: ChunkFilter,
        top_k: int,
    ) -> list[Any]:
        return self._inner.search(dense, sparse, self._scoped(filter), top_k)

    def snapshot_read(self, filter: ChunkFilter) -> list[Any]:
        return self._inner.snapshot_read(self._scoped(filter))

    def list_chunks(self, filter: ChunkFilter, page: Page) -> PageResult[Any]:
        return self._inner.list_chunks(self._scoped(filter), page)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _AsOfGraphView:
    """GraphStore facade replaying point-in-time node snapshots.

    ``list_nodes`` returns the node valid on ``[valid_from, valid_to)`` at
    ``at``, including superseded or tombstoned revisions. ``traverse`` stays
    anchored on the live store: it walks only nodes with a current revision and
    remaps each through the snapshot to its as-of form, so a superseded neighbor
    is seen as it was then, while a fully tombstoned node (no current revision)
    is never walked and recall's as_of does not resurrect it. Every other
    GraphStore surface (get_node / versions / writes, ...) forwards to the live
    store.
    """

    def __init__(self, inner: GraphStore, at: float) -> None:
        self._inner = inner
        self._at = at
        self._snapshots: dict[str, dict[str, GraphNode]] = {}

    def _snapshot(self, profile_id: str) -> dict[str, GraphNode]:
        cached = self._snapshots.get(profile_id)
        if cached is not None:
            return cached
        snapshot = {
            node.node_id: node for node in self._inner.as_of(self._at, NodeFilter(profile_id=profile_id))
        }
        self._snapshots[profile_id] = snapshot
        return snapshot

    def list_nodes(self, filter: NodeFilter, page: Page) -> PageResult[GraphNode]:
        filtered = [
            node
            for node in self._snapshot(filter.profile_id).values()
            if (filter.node_type is None or node.node_type is filter.node_type)
            and node.decay_weight >= filter.min_decay
            and (not filter.entities or any(entity in node.entities for entity in filter.entities))
        ]
        filtered.sort(key=lambda node: (-node.decay_weight, node.node_id))
        total = len(filtered)
        items = filtered[page.offset : page.offset + page.limit]
        return PageResult(items=items, total=total, offset=page.offset, limit=page.limit)

    def traverse(
        self,
        node_id: str,
        depth: int = 2,
        filter: NodeFilter | None = None,
    ) -> list[GraphNode]:
        if filter is None:
            return self._inner.traverse(node_id, depth=depth, filter=None)
        snapshot = self._snapshot(filter.profile_id)
        remapped: list[GraphNode] = []
        for node in self._inner.traverse(node_id, depth=depth, filter=filter):
            past = snapshot.get(node.node_id)
            if past is None or past.node_id in {seen.node_id for seen in remapped}:
                continue
            remapped.append(past)
        return remapped

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


# ---------------------------------------------------------------- service


class MemoryService:
    """Daemon-owned memory engine: leverages the retrieval + storage ports."""

    def __init__(self, stores: Stores, config: Config) -> None:
        self._stores = stores
        self._config = config
        self._cues = CueExtractor()
        self._retriever = HybridRetriever()
        self._assembler = Assembler()

    @property
    def retriever(self) -> HybridRetriever:
        return self._retriever

    def close(self) -> None:
        """Release the retrieval engine (T4 lifecycle fix): the daemon owns the
        HybridRetriever and shuts its track executor down on teardown so worker
        threads never outlive the process."""
        self._retriever.close()

    # ------------------------------------------------------------ recall

    def recall(
        self,
        *,
        profile_id: str,
        query: str,
        host: str | None = None,
        project: str | None = None,
        time_bucket: str | None = None,
        top_k: int | None = None,
        budget: int | None = None,
        as_of: float | None = None,
    ) -> dict[str, Any]:
        """Full recall path: cues -> dual-track pool -> budgeted context."""
        extracted = self._cues.extract(query, host=host, project=project, time_bucket=time_bucket)
        if as_of is not None:
            vector_store = cast(VectorStore, _AsOfVectorView(self._stores.vector, as_of))
            graph_store = cast(GraphStore, _AsOfGraphView(self._stores.graph, as_of))
        else:
            vector_store = self._stores.vector
            graph_store = self._stores.graph
        recall_result = self._retriever.recall(
            query,
            extracted,
            profile_id=profile_id,
            vector_store=vector_store,
            graph_store=graph_store,
            embedder=self._stores.embed,
        )
        assembler = self._assembler
        if top_k is not None or budget is not None:
            base = assembler.config
            assembler = Assembler(
                replace(
                    base,
                    top_k=top_k if top_k is not None else base.top_k,
                    budget_tokens=budget if budget is not None else base.budget_tokens,
                )
            )
        context = assembler.assemble(
            recall_result,
            profile_id=profile_id,
            meta_store=self._stores.meta,
            vector_store=self._stores.vector,
            graph_store=self._stores.graph,
        )
        self._record_hits(context)
        return {"memory": self._memory_payload(context)}

    def _record_hits(self, context: AssembledContext) -> None:
        """FR-3.7 usage events: fire-and-forget, never failing or blocking recall.

        Only chunks that made the context package are counted (a hit means the
        recalled memory). The raw store write is best-effort by design.
        """
        chunk_ids = [entry.id for entry in context.entries if entry.kind == "chunk"]
        if not chunk_ids:
            return
        try:
            self._stores.vector.update_chunk_state(chunk_ids, hit_increment=1)
        except Exception:  # pragma: no cover - usage accounting must not fail recall
            logger.warning("usage-event write failed; recall proceeds", exc_info=True)

    def _memory_payload(self, context: AssembledContext) -> dict[str, Any]:
        coverage = context.coverage
        watermark = coverage.watermark
        return {
            "entries": [self._entry_payload(entry) for entry in context.entries],
            "dropped_count": context.dropped_count,
            "budget_tokens": context.budget_tokens,
            "tokens_used": context.tokens_used,
            "coverage": {
                "vector_hits": coverage.vector_hits,
                "graph_hits": coverage.graph_hits,
                "pool_size": coverage.pool_size,
                "profile_chunks": coverage.profile_chunks,
                "watermark": (
                    {"start": watermark.start, "end": watermark.end} if watermark is not None else None
                ),
                "fresh_evidence_chunks": coverage.fresh_evidence_chunks,
                "pending_marked": coverage.pending_marked,
            },
        }

    @staticmethod
    def _entry_payload(entry: AssembledEntry) -> dict[str, Any]:
        return {
            "kind": entry.kind,
            "id": entry.id,
            "source": entry.source,
            "text": entry.text,
            "score": entry.score,
            "tokens": entry.tokens,
            "flags": [flag.value for flag in entry.flags],
            "conflict_group": entry.conflict_group,
            "recent_evidence": list(entry.recent_evidence),
        }

    # ------------------------------------------------------------ remember

    def remember(self, *, profile_id: str, text: str) -> dict[str, Any]:
        """Write an explicit user pin, mirroring the StampWriter's dual-branch
        near-duplicate flow: a strong consistent hit reinforces in place, a
        conflict flags needs_reconcile, anything else becomes a new chunk.
        Provenance is append-only; the explicit-pin source is never rewritten.
        """
        now = time.time()
        extracted = self._cues.extract(text)
        vector = self._stores.vector
        embedded = self._stores.embed.embed(text)
        config = WriteConfig()
        stamp = ChunkStamp(
            chunk_id=uuid.uuid4().hex,
            profile_id=profile_id,
            text=text,
            cognitive_tier=CognitiveTier.TIER_1,
            model_id="user",
            cues=extracted.cues,
            provenance=Provenance(
                asserted_by="user",
                session_id=None,
                source=EXPLICIT_PIN_SOURCE,
                confidence=1.0,
                asserted_at=now,
                history=[ProvenanceEvent(action="created", actor="user", at=now)],
            ),
            decay_weight=1.0,
            score=1.0,
            ingested_at=now,
        )
        strong = vector.near_duplicate(embedded.dense, config.reinforce_threshold, profile_id=profile_id)
        band = vector.near_duplicate(embedded.dense, config.conflict_threshold, profile_id=profile_id)
        if not band:
            vector.upsert_chunk(stamp, embedded.dense, embedded.sparse)
            self._audit(profile_id, "remember", {"chunk_id": stamp.chunk_id, "profile_id": profile_id})
            return {"outcome": "new_chunk", "chunk_id": stamp.chunk_id}
        hit = band[0]
        strong_ids = {chunk.chunk_id for chunk in strong}
        verdict = NearDuplicateChecker().check(stamp.text, hit.text)
        if hit.chunk_id in strong_ids and verdict is ConsistencyVerdict.CONSISTENT:
            rebound = min(1.0, hit.decay_weight + config.reinforce_bonus)
            vector.update_weights([WeightUpdate(hit.chunk_id, decay_weight=rebound, last_reinforced=now)])
            self._audit(
                profile_id,
                "remember",
                {"chunk_id": hit.chunk_id, "profile_id": profile_id, "outcome": "reinforced"},
            )
            return {"outcome": "reinforced", "chunk_id": hit.chunk_id}
        if verdict is ConsistencyVerdict.CONFLICT:
            vector.update_chunk_state([hit.chunk_id], needs_reconcile=True)
            self._audit(
                profile_id,
                "remember",
                {"chunk_id": hit.chunk_id, "profile_id": profile_id, "outcome": "needs_reconcile"},
            )
            return {"outcome": "needs_reconcile", "chunk_id": hit.chunk_id}
        vector.upsert_chunk(stamp, embedded.dense, embedded.sparse)
        self._audit(profile_id, "remember", {"chunk_id": stamp.chunk_id, "profile_id": profile_id})
        return {"outcome": "new_chunk", "chunk_id": stamp.chunk_id}

    # ------------------------------------------------------------ audit

    def audit(
        self,
        *,
        profile_id: str,
        node_id: str | None = None,
        chunk_id: str | None = None,
    ) -> dict[str, Any]:
        """Provenance + version chain for one target, plus relevant audit rows."""
        if chunk_id is not None:
            chunk = self._stores.vector.get_chunk(chunk_id)
            if chunk is None:
                raise MemoryNotFoundError(f"chunk {chunk_id!r} not found")
            return {
                "target": {"type": "chunk", "id": chunk_id},
                "provenance": chunk.provenance.model_dump(),
                "versions": [],
                "audit": self._relevant_audit(profile_id, chunk_id),
            }
        chain = self._stores.graph.versions(node_id) if node_id is not None else []
        if not chain:
            raise MemoryNotFoundError(f"node {node_id!r} not found")
        return {
            "target": {"type": "node", "id": node_id},
            "provenance": chain[-1].provenance.model_dump(),
            "versions": [version.model_dump() for version in chain],
            "audit": self._relevant_audit(profile_id, node_id if node_id is not None else "?"),
        }

    def _relevant_audit(self, profile_id: str, target_id: str) -> list[dict[str, Any]]:
        """Audit rows referencing ``target_id`` (client-side filter: the port's
        AuditFilter carries no target or profile dimension)."""
        page = self._stores.meta.audit_query(AuditFilter(), Page(offset=0, limit=200))
        relevant: list[dict[str, Any]] = []
        for entry in page.items:
            detail = entry.detail or {}
            if target_id not in self._audit_targets(detail):
                continue
            if profile_id not in (detail.get("profile_id") or (profile_id,)) and entry.actor != "capture":
                # Keep rows whose detail omits a profile (system-level) but drop
                # rows that name a different profile explicitly (D5 isolation).
                continue
            relevant.append(
                {
                    "actor": entry.actor,
                    "action": entry.action,
                    "detail": detail,
                    "at": entry.at,
                    "id": entry.id,
                }
            )
        return relevant

    @staticmethod
    def _audit_targets(detail: dict[str, Any]) -> tuple[str, ...]:
        targets: list[Any] = []
        for key in ("chunk_id", "node_id"):
            if detail.get(key):
                targets.append(detail[key])
        for key in ("chunks", "nodes"):
            values = detail.get(key)
            if isinstance(values, list):
                targets.extend(values)
        return tuple(str(target) for target in targets)

    # ------------------------------------------------------------ timeline

    def timeline(self, *, profile_id: str, node_id: str | None = None) -> dict[str, Any]:
        """Per-node version replay, else a profile-wide recent-first listing."""
        if node_id is not None:
            version_events = self._stores.graph.timeline(node_id)
            if not version_events:
                raise MemoryNotFoundError(f"node {node_id!r} not found")
            return {
                "events": [
                    {"when": event.when, "version": event.version, "summary": event.summary}
                    for event in version_events
                ]
            }
        chunk_page = self._stores.vector.list_chunks(
            ChunkFilter(profile_id=profile_id), Page(offset=0, limit=100)
        )
        node_page = self._stores.graph.list_nodes(
            NodeFilter(profile_id=profile_id), Page(offset=0, limit=100)
        )
        events: list[dict[str, Any]] = [
            {
                "when": chunk.ingested_at,
                "kind": "chunk",
                "id": chunk.chunk_id,
                "version": None,
                "summary": chunk.text,
            }
            for chunk in chunk_page.items
        ]
        for node in node_page.items:
            summary = node.props.get("statement")
            events.append(
                {
                    "when": node.updated_at,
                    "kind": "node",
                    "id": node.node_id,
                    "version": node.version,
                    "summary": summary if isinstance(summary, str) and summary else node.node_id,
                }
            )
        events.sort(key=lambda event: event["when"], reverse=True)
        return {"events": events}

    # ------------------------------------------------------------ export

    def export(self, *, profile_id: str, offset: int = 0, limit: int = 50) -> dict[str, Any]:
        """Stable paged profile dump including provenance (schema-tagged)."""
        chunk_page = self._stores.vector.list_chunks(
            ChunkFilter(profile_id=profile_id), Page(offset=offset, limit=limit)
        )
        node_page = self._stores.graph.list_nodes(
            NodeFilter(profile_id=profile_id), Page(offset=offset, limit=limit)
        )
        return {
            "schema": "mnemoseed.memory.export/1",
            "profile_id": profile_id,
            "chunks": [chunk.model_dump() for chunk in chunk_page.items],
            "nodes": [node.model_dump() for node in node_page.items],
            "paging": {
                "chunk_total": chunk_page.total,
                "node_total": node_page.total,
                "offset": offset,
                "limit": limit,
            },
        }

    # ------------------------------------------------------------ forget_this

    def forget_this(
        self,
        *,
        profile_id: str,
        chunk_id: str | None = None,
        node_id: str | None = None,
        entity: str | None = None,
    ) -> dict[str, Any]:
        """GDPR deletion (design/03 2.4). Chunks are physically deleted; graph
        nodes are tombstoned so their full version chains stay reachable through
        the store layer (versions / audit / timeline) while every current read
        stops seeing them. recall's as_of does not resurrect a tombstoned node:
        the graph walk stays anchored on live revisions."""
        removed_chunks: list[str] = []
        removed_nodes: list[str] = []
        if chunk_id is not None:
            if self._stores.vector.get_chunk(chunk_id) is None:
                raise MemoryNotFoundError(f"chunk {chunk_id!r} not found")
            self._stores.vector.delete_chunk(chunk_id)
            removed_chunks.append(chunk_id)
        if node_id is not None:
            if self._stores.graph.get_node(node_id) is None:
                raise MemoryNotFoundError(f"node {node_id!r} not found")
            self._stores.graph.tombstone(node_id, deleted_at=time.time())
            removed_nodes.append(node_id)
        if entity is not None:
            for chunk in self._stores.vector.list_chunks(
                ChunkFilter(profile_id=profile_id, entities=(entity,)), Page(offset=0, limit=1000)
            ).items:
                self._stores.vector.delete_chunk(chunk.chunk_id)
                removed_chunks.append(chunk.chunk_id)
            for node in self._stores.graph.list_nodes(
                NodeFilter(profile_id=profile_id, entities=(entity,)), Page(offset=0, limit=1000)
            ).items:
                self._stores.graph.tombstone(node.node_id, deleted_at=time.time())
                removed_nodes.append(node.node_id)
        self._audit(
            profile_id,
            "forget_this",
            {"chunks": removed_chunks, "nodes": removed_nodes, "profile_id": profile_id},
        )
        return {"removed": {"chunks": removed_chunks, "nodes": removed_nodes}}

    # ------------------------------------------------------------ plumbing

    def _audit(self, profile_id: str, action: str, detail: dict[str, Any]) -> None:
        self._stores.meta.audit_append(AuditEntry(actor="user", action=action, detail=detail, at=time.time()))


# ---------------------------------------------------------------- router


router = APIRouter()


def _route_404(exc: MemoryNotFoundError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


@router.post("/memory/recall")
def memory_recall(req: RecallRequest, request: Request) -> dict[str, Any]:
    service: MemoryService = request.app.state.memory
    return service.recall(
        profile_id=req.profile_id,
        query=req.query,
        host=req.host,
        project=req.project,
        time_bucket=req.time_bucket,
        top_k=req.top_k,
        budget=req.budget,
        as_of=req.as_of,
    )


@router.post("/memory/remember")
def memory_remember(req: RememberRequest, request: Request) -> dict[str, Any]:
    service: MemoryService = request.app.state.memory
    return service.remember(profile_id=req.profile_id, text=req.text)


@router.post("/memory/audit")
def memory_audit(req: AuditRequest, request: Request) -> dict[str, Any]:
    service: MemoryService = request.app.state.memory
    try:
        return service.audit(profile_id=req.profile_id, node_id=req.node_id, chunk_id=req.chunk_id)
    except MemoryNotFoundError as exc:
        raise _route_404(exc) from exc


@router.post("/memory/timeline")
def memory_timeline(req: TimelineRequest, request: Request) -> dict[str, Any]:
    service: MemoryService = request.app.state.memory
    try:
        return service.timeline(profile_id=req.profile_id, node_id=req.node_id)
    except MemoryNotFoundError as exc:
        raise _route_404(exc) from exc


@router.post("/memory/export")
def memory_export(req: ExportRequest, request: Request) -> dict[str, Any]:
    service: MemoryService = request.app.state.memory
    return service.export(profile_id=req.profile_id, offset=req.offset, limit=req.limit)


@router.post("/memory/forget_this")
def memory_forget_this(req: ForgetRequest, request: Request) -> dict[str, Any]:
    service: MemoryService = request.app.state.memory
    try:
        return service.forget_this(
            profile_id=req.profile_id,
            chunk_id=req.chunk_id,
            node_id=req.node_id,
            entity=req.entity,
        )
    except MemoryNotFoundError as exc:
        raise _route_404(exc) from exc


# ------------------------------------------------------------ /dream surface


def _trigger_payload(status: TriggerStatus) -> dict[str, Any]:
    """Serialized trigger observability (state, pending depths, ranges).

    PoolEvent.last_event is reduced to its semantic fields; the injected-clock
    ``fired_at`` timestamp is part of the event the /dream command displays
    (FR-2.8), so it stays on the wire as an epoch float.
    """
    last = status.last_event
    current = status.current_range
    return {
        "profile_id": status.profile_id,
        "state": status.state.value,
        "pending_queue": status.pending_queue,
        "pending_manual": status.pending_manual,
        "last_event": (
            {
                "kind": last.kind.value,
                "profile_id": last.profile_id,
                "turn_range": {"start": last.turn_range.start, "end": last.turn_range.end},
                "fired_at": last.fired_at,
            }
            if last is not None
            else None
        ),
        "current_range": ({"start": current.start, "end": current.end} if current is not None else None),
    }


@router.post("/memory/dream_once")
async def memory_dream_once(req: DreamRequest, request: Request) -> dict[str, Any]:
    """One manual dream cycle (FR-2.8 ``/dream once``).

    ``async def`` so the whole snapshot -> reflect -> merge -> safe-clear chain
    runs on the app event-loop thread: the daemon's sqlite connections are bound
    to that thread and refuse cross-thread use.
    """
    trigger: DreamTrigger = request.app.state.dream
    launched = trigger.dream_once(req.profile_id)
    status = trigger.status(req.profile_id)
    payload = _trigger_payload(status)
    payload["launched"] = launched
    return payload


@router.post("/memory/dream_status")
async def memory_dream_status(req: DreamRequest, request: Request) -> dict[str, Any]:
    trigger: DreamTrigger = request.app.state.dream
    return _trigger_payload(trigger.status(req.profile_id))
