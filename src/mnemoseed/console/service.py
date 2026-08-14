"""Console read service (PRD-07 T1): the watchtower over the storage ports.

Every console read resolves through the existing port methods
(``list_chunks`` / ``list_nodes`` / ``get_chunk`` / ``get_node`` /
``list_dream_runs`` / ``pool_state`` ...), so the surface is driver-agnostic.
Filters the vector SQL index already supports (time, entity, decay floor,
consolidated, needs_reconcile) are pushed into ``ChunkFilter``; the remaining
browser filters of the PRD-07 Memory Browser (project / host / tier / decay
ceiling for chunks, node overlay flags) are applied client-side over bounded
scans -- a console read, not the retrieval hot path, so a full scan is an
honest trade for M1 and avoids a driver-specific SQL extension.

Deviations carried by the current schema (reported, not papered over):

- Chunk usage counters (``hit_count`` / ``last_hit_at`` / ``reinforce_count`` /
  ``needs_reconcile``) are deliberately hidden by the vector ports (the
  embedded test suite pins this), so chunk dossiers expose ``None`` there.
- ``DreamRun`` has no profile_id column and does not record token/cost
  breakdowns yet, so ``/dream/runs`` is a global run history and split counts
  stay out of scope until the run record grows them.

Writes that M1 owns (``dream_once``, the ``auto_trigger`` toggle, conflict
resolutions, review verdicts, and the FR-7.9 console write ops -- forget /
pin / weight adjust / profile create-rename-archive / token issue-revoke) all
land in the append-only audit trail.
"""

from __future__ import annotations

import calendar
import re
import time
from pathlib import Path
from typing import Any

from mnemoseed import __version__
from mnemoseed.config import Config
from mnemoseed.configwrite.service import ConfigWriteService
from mnemoseed.daemon.memory import _trigger_payload
from mnemoseed.dream import DreamTrigger, TokenLedger
from mnemoseed.dream import snapshot as _dream_snapshot
from mnemoseed.dream.reflect import result_from_payload
from mnemoseed.dream.snapshot import Snapshot, load_snapshot_file
from mnemoseed.schema.graph import GraphNode, NodeType
from mnemoseed.schema.stamp import ChunkStamp, ProvenanceEvent
from mnemoseed.storage.factory import Stores
from mnemoseed.storage.ports import (
    AuditEntry,
    AuditFilter,
    ChunkFilter,
    DreamRun,
    DreamRunFilter,
    GraphFlag,
    GraphWeightUpdate,
    NodeFilter,
    Page,
    StoredProfile,
    TurnRange,
    WeightUpdate,
)

# Bounded scan shape: pages are cheap; a client-side overlay over the scan is
# the console's declared read pattern for M1 (10k-row cap keeps a pathological
# filter from starving a local daemon).
_SCAN_PAGE = 500
_SCAN_CAP = 10_000

# FR-7.6 quality-review verdicts and FR-7.7 Reconcile branches (design/01
# section 4a semantics) live as closed vocabulary on the console surface.
REVIEW_VERDICTS: frozenset[str] = frozenset({"accept", "reject", "hallucination"})
REVIEW_ROUTES: frozenset[str] = frozenset({"core", "isolated", "salvage"})
CONFLICT_BRANCHES: frozenset[str] = frozenset({"reinforce", "coexist", "invalidate", "pending"})

_REVIEW_ACTION = "review_verdict"
_CONFLICT_RESOLVE_ACTION = "conflict.resolve"

# Journal filenames are "<run_id>.json"; run_id is a whitelist-restricted slug
# (uuid hex for real runs) so a crafted value ("../x", absolute paths, dots)
# can never escape the journal dir through path concatenation.
_RUN_ID_CHARS = re.compile(r"[A-Za-z0-9_-]+")


class ConsoleNotFoundError(Exception):
    """The requested memory target does not exist for this profile."""


def _range_payload(turn_range: TurnRange | None) -> dict[str, int] | None:
    if turn_range is None:
        return None
    return {"start": turn_range.start, "end": turn_range.end}


def _utc_day_start(timestamp: float) -> float:
    return float(calendar.timegm(time.gmtime(timestamp)[:3] + (0, 0, 0)))


def _utc_week_start(timestamp: float) -> float:
    clock = time.gmtime(timestamp)
    # tm_wday counts Monday == 0; a negative day number rolls back cleanly in
    # calendar.timegm, so the UTC week always starts on Monday.
    return float(calendar.timegm((clock.tm_year, clock.tm_mon, clock.tm_mday - clock.tm_wday, 0, 0, 0)))


class ConsoleService:
    """Daemon-owned read/write surface behind the /api/v1 console router."""

    def __init__(
        self,
        stores: Stores,
        config: Config,
        trigger: DreamTrigger,
        journal_dir: Path | None = None,
        configwrite: ConfigWriteService | None = None,
    ) -> None:
        self._stores = stores
        self._config = config
        self._trigger = trigger
        # The single config writer (PRD-07 FR-7.11): every console settings
        # change funnels through its registry -> validate -> patch -> record ->
        # audit flow instead of a hand-rolled TOML patch.
        self._configwrite = (
            configwrite if configwrite is not None else ConfigWriteService(config, stores.meta)
        )
        # The dream snapshot journal (the same directory the snapshotter and
        # reflect pass write) is the review view's source of triples + source
        # chunks. Read the module attribute at construction so a test patch of
        # ``mnemoseed.dream.snapshot.CONFIG_DIR`` is honoured exactly like the
        # snapshotter/reflector's own default.
        self._journal_dir = (
            Path(journal_dir) if journal_dir is not None else _dream_snapshot.CONFIG_DIR / "dreams"
        )

    # ------------------------------------------------------------ dashboard

    def status(self) -> dict[str, Any]:
        """FR-7.2 dashboard: daemon health + one row per known profile."""
        stores = self._stores
        daemon: dict[str, Any] = {
            "version": __version__,
            "preset": self._config.preset,
            "drivers": {
                "vector": stores.vector.info.name,
                "graph": stores.graph.info.name,
                "meta": stores.meta.info.name,
                "embed": stores.embed.info.name,
            },
            "gate": {"ok": stores.report.ok},
        }
        known: set[str] = set(stores.meta.pool_states())
        for profile in stores.meta.list_profiles():
            known.add(profile.profile_id)
        return {
            "daemon": daemon,
            "profiles": [self._profile_status(profile_id) for profile_id in sorted(known)],
        }

    def _profile_status(self, profile_id: str) -> dict[str, Any]:
        """One profile row: dream state, pool, counts, token usage."""
        stores = self._stores
        stored = stores.meta.get_profile(profile_id)
        trigger = self._trigger.status(profile_id)
        pool = stores.meta.pool_state(profile_id)
        ledger = self._ledger().status(profile_id)
        now = time.time()
        chunk_filter = ChunkFilter(profile_id=profile_id)
        node_filter = NodeFilter(profile_id=profile_id)
        needs_chunks = stores.vector.list_chunks(
            ChunkFilter(profile_id=profile_id, needs_reconcile=True), Page(limit=1)
        ).total
        nodes = self._scan_nodes(node_filter)
        return {
            "profile_id": profile_id,
            "display_name": stored.display_name if stored is not None else "",
            "archived": stored.archived if stored is not None else False,
            "dream": {
                "state": trigger.state.value,
                "pending_queue": trigger.pending_queue,
                "pending_manual": trigger.pending_manual,
                "current_range": _range_payload(trigger.current_range),
                "auto_trigger": self._trigger.auto_trigger_enabled,
            },
            "pool": {
                "balance": pool.balance,
                "watermark": _range_payload(pool.watermark),
            },
            "counts": {
                "chunks": stores.vector.list_chunks(chunk_filter, Page(limit=1)).total,
                "nodes": stores.graph.list_nodes(node_filter, Page(limit=1)).total,
                "needs_reconcile": needs_chunks + sum(1 for node in nodes if node.needs_reconcile),
                "pending_consolidation": sum(1 for node in nodes if node.pending_consolidation),
            },
            "tokens": {
                # The monthly ledger is the authoritative meter (reflect records
                # delta+output tokens per UTC month); the daily/weekly rows are a
                # temporal projection over the run journal (0 until a future
                # task records per-run tokens).
                "today": self._dream_tokens_since(profile_id, _utc_day_start(now)),
                "this_week": self._dream_tokens_since(profile_id, _utc_week_start(now)),
                "ledger": {
                    "year_month": ledger.year_month,
                    "used_tokens": ledger.used_tokens,
                    "used_usd": ledger.used_usd,
                    "budget_usd": ledger.budget_usd,
                    "remaining_usd": ledger.remaining_usd,
                },
            },
        }

    def _ledger(self) -> TokenLedger:
        return TokenLedger(meta=self._stores.meta, budget_usd=self._config.dream.token_budget_usd)

    def _dream_tokens_since(self, profile_id: str, since: float) -> int:
        # The run journal has no profile column today, so the temporal rows are
        # profile-worth when the local daemon runs one profile (the common
        # case); with several profiles they aggregate. (Schema limitation.)
        del profile_id
        page = self._stores.meta.list_dream_runs(DreamRunFilter(since=since), Page(limit=_SCAN_CAP))
        return sum(run.tokens for run in page.items)

    # ------------------------------------------------------------ memory browse

    def list_chunks(
        self,
        *,
        profile_id: str,
        time_after: float | None = None,
        time_before: float | None = None,
        project: str | None = None,
        host: str | None = None,
        entity: tuple[str, ...] = (),
        tier: int | None = None,
        min_decay: float = 0.0,
        max_decay: float | None = None,
        consolidated: bool | None = None,
        needs_reconcile: bool | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        """FR-7.4 short-term memory page, newest first."""
        store_filter = ChunkFilter(
            profile_id=profile_id,
            min_decay=min_decay,
            ingested_after=time_after,
            ingested_before=time_before,
            entities=entity,
            consolidated=consolidated,
            needs_reconcile=needs_reconcile,
        )
        overlay = project is not None or host is not None or tier is not None or max_decay is not None
        if not overlay:
            page = self._stores.vector.list_chunks(store_filter, Page(offset=offset, limit=limit))
            return {
                "items": [self._chunk_summary(chunk) for chunk in page.items],
                "paging": {"total": page.total, "offset": offset, "limit": limit},
            }
        filtered = [
            chunk
            for chunk in self._scan_chunks(store_filter)
            if (project is None or chunk.cues.project == project)
            and (host is None or chunk.cues.host == host)
            and (tier is None or int(chunk.cognitive_tier) == tier)
            and (max_decay is None or chunk.decay_weight <= max_decay)
        ]
        filtered.sort(key=lambda chunk: (-chunk.ingested_at, chunk.chunk_id))
        return {
            "items": [self._chunk_summary(chunk) for chunk in filtered[offset : offset + limit]],
            "paging": {"total": len(filtered), "offset": offset, "limit": limit},
        }

    def list_nodes(
        self,
        *,
        profile_id: str,
        node_type: NodeType | None = None,
        entity: tuple[str, ...] = (),
        min_decay: float = 0.0,
        max_decay: float | None = None,
        tier: int | None = None,
        updated_after: float | None = None,
        updated_before: float | None = None,
        needs_reconcile: bool | None = None,
        pending_consolidation: bool | None = None,
        conflict: bool | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        """FR-7.4 long-term memory page (node type / tier / decay range)."""
        store_filter = NodeFilter(
            profile_id=profile_id, node_type=node_type, entities=entity, min_decay=min_decay
        )
        overlay = bool(
            max_decay is not None
            or tier is not None
            or updated_after is not None
            or updated_before is not None
            or needs_reconcile is not None
            or pending_consolidation is not None
            or conflict is not None
        )
        if not overlay:
            page = self._stores.graph.list_nodes(store_filter, Page(offset=offset, limit=limit))
            return {
                "items": [self._node_summary(node) for node in page.items],
                "paging": {"total": page.total, "offset": offset, "limit": limit},
            }
        filtered = [
            node
            for node in self._scan_nodes(store_filter)
            if self._node_overlay_matches(
                node,
                max_decay=max_decay,
                tier=tier,
                updated_after=updated_after,
                updated_before=updated_before,
                needs_reconcile=needs_reconcile,
                pending_consolidation=pending_consolidation,
                conflict=conflict,
            )
        ]
        filtered.sort(key=lambda node: (-node.updated_at, node.node_id))
        return {
            "items": [self._node_summary(node) for node in filtered[offset : offset + limit]],
            "paging": {"total": len(filtered), "offset": offset, "limit": limit},
        }

    def _scan_chunks(self, store_filter: ChunkFilter) -> list[ChunkStamp]:
        items: list[ChunkStamp] = []
        offset = 0
        while True:
            page = self._stores.vector.list_chunks(store_filter, Page(offset=offset, limit=_SCAN_PAGE))
            items.extend(page.items)
            offset += page.limit
            if offset >= page.total or not page.items or len(items) >= _SCAN_CAP:
                break
        return items

    def _scan_nodes(self, store_filter: NodeFilter) -> list[GraphNode]:
        items: list[GraphNode] = []
        offset = 0
        while True:
            page = self._stores.graph.list_nodes(store_filter, Page(offset=offset, limit=_SCAN_PAGE))
            items.extend(page.items)
            offset += page.limit
            if offset >= page.total or not page.items or len(items) >= _SCAN_CAP:
                break
        return items

    @staticmethod
    def _node_overlay_matches(
        node: GraphNode,
        *,
        max_decay: float | None,
        tier: int | None,
        updated_after: float | None,
        updated_before: float | None,
        needs_reconcile: bool | None,
        pending_consolidation: bool | None,
        conflict: bool | None,
    ) -> bool:
        if max_decay is not None and node.decay_weight > max_decay:
            return False
        if tier is not None and int(node.cognitive_tier) != tier:
            return False
        if updated_after is not None and node.updated_at < updated_after:
            return False
        if updated_before is not None and node.updated_at > updated_before:
            return False
        if needs_reconcile is not None and node.needs_reconcile is not needs_reconcile:
            return False
        if pending_consolidation is not None and node.pending_consolidation is not pending_consolidation:
            return False
        if conflict is not None and node.conflict_flag is not conflict:
            return False
        return True

    # ------------------------------------------------------------ memory detail

    def get_chunk(self, *, profile_id: str, chunk_id: str) -> dict[str, Any]:
        """FR-7.5 chunk dossier: verbatim channel + full provenance history."""
        chunk = self._stores.vector.get_chunk(chunk_id)
        if chunk is None or chunk.profile_id != profile_id:
            raise ConsoleNotFoundError(f"chunk {chunk_id!r} not found")
        cues = chunk.cues
        emotion = cues.emotion
        provenance = chunk.provenance
        return {
            "type": "chunk",
            "chunk_id": chunk.chunk_id,
            "profile_id": chunk.profile_id,
            "content": {"verbatim": chunk.text},
            "cues": {
                "project": cues.project,
                "host": cues.host,
                "task": cues.task,
                "tools_used": list(cues.tools_used),
                "time_bucket": cues.time_bucket,
                "entities": list(cues.entities),
                "emotion": (
                    {
                        "valence": emotion.valence,
                        "arousal": emotion.arousal,
                        "peripheral_gaps": emotion.peripheral_gaps,
                    }
                    if emotion is not None
                    else None
                ),
            },
            "provenance": self._provenance_payload(provenance),
            "version_chain": [],
            "weights": {
                "decay_weight": chunk.decay_weight,
                "score": chunk.score,
                "confidence": provenance.confidence,
                "last_reinforced": None,
                "reinforce_count": None,
            },
            "flags": {
                "consolidated": chunk.consolidated,
                "needs_reconcile": None,
                "pending_consolidation": None,
                "conflict_flag": None,
                "peripheral_gaps": bool(emotion.peripheral_gaps) if emotion is not None else False,
            },
            "usage": {"hit_count": None, "last_hit_at": None},
            "metadata": {
                "cognitive_tier": int(chunk.cognitive_tier),
                "model_id": chunk.model_id,
                "persona_id": chunk.persona_id,
                "ingested_at": chunk.ingested_at,
                "turn_start": chunk.turn_start,
                "turn_end": chunk.turn_end,
            },
        }

    def get_node(self, *, profile_id: str, node_id: str) -> dict[str, Any]:
        """FR-7.5 node dossier: triple, version chain, weights, flags, usage."""
        node = self._stores.graph.get_node(node_id)
        if node is None or node.profile_id != profile_id:
            raise ConsoleNotFoundError(f"node {node_id!r} not found")
        versions = self._stores.graph.versions(node_id)
        timeline = self._stores.graph.timeline(node_id)
        return {
            "type": "node",
            "node_id": node.node_id,
            "profile_id": node.profile_id,
            "node_type": node.node_type.value,
            "content": {
                "subject": node.props.get("subject"),
                "predicate": node.props.get("predicate"),
                "object": node.props.get("object"),
                "statement": node.props.get("statement"),
            },
            "entities": list(node.entities),
            "provenance": self._provenance_payload(node.provenance),
            "version_chain": [version.model_dump() for version in versions],
            "weights": {
                "decay_weight": node.decay_weight,
                "confidence": node.confidence,
                "reinforce_count": node.reinforce_count,
                "last_reinforced": node.last_reinforced,
                "never_decay": node.never_decay,
            },
            "usage": {"hit_count": node.hit_count, "last_hit_at": node.last_hit_at},
            "flags": {
                "conflict_flag": node.conflict_flag,
                "conflict_group": node.conflict_group,
                "needs_reconcile": node.needs_reconcile,
                "pending_consolidation": node.pending_consolidation,
                "peripheral_gaps": node.peripheral_gaps,
            },
            "promotion_status": node.promotion_status.value,
            "version": {
                "number": node.version,
                "prev_version_id": node.prev_version_id,
                "valid_from": node.valid_from,
                "valid_to": node.valid_to,
                "current": node.is_current,
            },
            "timeline": [
                {"when": event.when, "version": event.version, "summary": event.summary} for event in timeline
            ],
            "created_at": node.created_at,
            "updated_at": node.updated_at,
        }

    # ------------------------------------------------------------ dream panel

    def dream_status(self, profile_id: str) -> dict[str, Any]:
        """FR-7.6: trigger state + pending queue depths for one profile."""
        status = self._trigger.status(profile_id)
        payload = _trigger_payload(status)
        payload["queue_depth"] = status.pending_queue + status.pending_manual
        return payload

    def dream_runs(
        self,
        *,
        since: float | None = None,
        until: float | None = None,
        interrupted: bool | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        """FR-7.6 run history. Global: the run journal carries no profile column."""
        page = self._stores.meta.list_dream_runs(
            DreamRunFilter(since=since, until=until, interrupted=interrupted),
            Page(offset=offset, limit=limit),
        )
        return {
            "runs": [self._dream_run_payload(run) for run in page.items],
            "paging": {"total": page.total, "offset": offset, "limit": limit},
        }

    # ------------------------------------------------------------ dream writes

    def dream_once(self, profile_id: str, *, actor: str = "console") -> dict[str, Any]:
        """FR-7.6 manual trigger: reuse the trigger's ``dream_once`` seam."""
        launched = self._trigger.dream_once(profile_id)
        payload = _trigger_payload(self._trigger.status(profile_id))
        payload["launched"] = launched
        self._audit("dream_once", {"profile_id": profile_id, "launched": launched}, actor=actor)
        return payload

    def set_auto_trigger(self, enabled: bool, *, actor: str = "console") -> dict[str, Any]:
        """FR-7.6 auto-trigger toggle: live flag + config-file persistence.

        The persistence goes through the single config writer (FR-7.11): the
        registry validates the boolean, the surgical TOML patch lands in
        [dream], the versioned store records it, and the audit trail keeps the
        console-level entry (the writer adds its own config.set entry).
        """
        self._trigger.set_auto_trigger(enabled)
        result = self._configwrite.set("dream.auto_trigger", enabled, actor=actor)
        path = Path(result["persisted_to"])
        self._audit("console.auto_trigger", {"enabled": enabled, "persisted_to": str(path)}, actor=actor)
        return {"enabled": enabled, "persisted_to": str(path)}

    # ------------------------------------------------------------ dream review (FR-7.6)

    def dream_review(self, *, run_id: str, profile_id: str) -> dict[str, Any]:
        """FR-7.6 quality-review view: one run's produced triples with their
        source chunks (diff-style pairing) and any already-recorded verdicts."""
        snap = self._load_run_snapshot(run_id, profile_id)
        result = result_from_payload(snap.reflect_result)
        if result is None:
            return {
                "run_id": run_id,
                "profile_id": profile_id,
                "turn_range": _range_payload(snap.turn_range),
                "reflected": False,
                "triples": [],
            }
        chunks_by_id = {chunk.chunk_id: chunk.text for chunk in snap.chunks}
        verdicts = self._verdicts_for_run(run_id)
        return {
            "run_id": run_id,
            "profile_id": profile_id,
            "turn_range": _range_payload(snap.turn_range),
            "prompt_version": result.prompt_version,
            "reflected": True,
            "triples": [
                {
                    "subject": triple.subject,
                    "predicate": triple.predicate,
                    "object": triple.object,
                    "confidence": triple.confidence,
                    "route": triple.route.value,
                    "polarity": triple.polarity,
                    "preference": triple.preference,
                    "tiers": [int(tier) for tier in triple.tiers],
                    "chunk_ids": list(triple.chunk_ids),
                    "chunks": [
                        {"chunk_id": chunk_id, "text": chunks_by_id[chunk_id]}
                        for chunk_id in triple.chunk_ids
                        if chunk_id in chunks_by_id
                    ],
                    "verdict": verdicts.get(
                        self._triple_key(triple.subject, triple.predicate, triple.object, triple.route.value)
                    ),
                }
                for triple in result.triples
            ],
        }

    def dream_review_verdict(
        self,
        *,
        run_id: str,
        profile_id: str,
        subject: str,
        predicate: str,
        obj: str,
        route: str,
        verdict: str,
        actor: str = "console",
    ) -> dict[str, Any]:
        """FR-7.6 review write: record one per-triple verdict, audit-logged and
        idempotent (re-submitting the same verdict does not duplicate the row)."""
        snap = self._load_run_snapshot(run_id, profile_id)
        result = result_from_payload(snap.reflect_result)
        if result is None:
            raise ConsoleNotFoundError(f"dream run {run_id!r} has no reviewable output")
        key = self._triple_key(subject, predicate, obj, route)
        if not any(
            self._triple_key(t.subject, t.predicate, t.object, t.route.value) == key for t in result.triples
        ):
            raise ConsoleNotFoundError(
                f"triple ({subject}, {predicate}, {obj}) is not part of dream run {run_id!r}"
            )
        existing = self._verdicts_for_run(run_id).get(key)
        if existing is not None and existing["action"] == verdict:
            return {"recorded": False, "verdict": verdict, "at": existing["at"]}
        at = time.time()
        self._stores.meta.audit_append(
            AuditEntry(
                actor=actor,
                action=_REVIEW_ACTION,
                detail={
                    "run_id": run_id,
                    "profile_id": profile_id,
                    "subject": subject,
                    "predicate": predicate,
                    "object": obj,
                    "route": route,
                    "verdict": verdict,
                },
                at=at,
            )
        )
        return {"recorded": True, "verdict": verdict, "at": at}

    def _load_run_snapshot(self, run_id: str, profile_id: str) -> Snapshot:
        """The journaled snapshot behind a dream run; 404 for unknown/foreign."""
        if not _RUN_ID_CHARS.fullmatch(run_id):
            raise ConsoleNotFoundError(f"dream run {run_id!r} not found")
        # Defense in depth on top of the whitelist: anchor the resolved path
        # inside the journal dir so a symlink or exotic spelling cannot escape.
        snapped = (self._journal_dir / f"{run_id}.json").resolve()
        if not snapped.is_relative_to(self._journal_dir.resolve()):
            raise ConsoleNotFoundError(f"dream run {run_id!r} not found")
        snap = load_snapshot_file(snapped)
        if snap is None or snap.profile_id != profile_id:
            raise ConsoleNotFoundError(f"dream run {run_id!r} not found")
        return snap

    def _verdicts_for_run(self, run_id: str) -> dict[tuple[str, str, str, str], dict[str, Any]]:
        """Per-triple verdicts recorded for one run (last write wins)."""
        out: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        page = Page(limit=200)
        while True:
            result = self._stores.meta.audit_query(AuditFilter(action=_REVIEW_ACTION), page)
            for entry in result.items:
                detail = entry.detail
                if str(detail.get("run_id", "")) != run_id:
                    continue
                key = self._triple_key(
                    detail.get("subject"), detail.get("predicate"), detail.get("object"), detail.get("route")
                )
                verdict = str(detail.get("verdict", ""))
                if verdict:
                    out[key] = {"action": verdict, "at": entry.at}
            consumed = page.offset + len(result.items)
            if result.total <= consumed:
                return out
            page = Page(offset=consumed, limit=page.limit)

    @staticmethod
    def _triple_key(subject: Any, predicate: Any, obj: Any, route: Any) -> tuple[str, str, str, str]:
        """The review subject/predicate/object/route dedup key (casefold)."""
        return (
            str(subject or "").casefold(),
            str(predicate or "").casefold(),
            str(obj or "").casefold(),
            str(route or "").casefold(),
        )

    # ------------------------------------------------------------ conflicts inbox (FR-7.7)

    def list_conflicts(self, *, profile_id: str) -> dict[str, Any]:
        """FR-7.7 inbox: flag_conflict pairs, grouped by their shared group id."""
        flagged = [
            node
            for node in self._scan_nodes(NodeFilter(profile_id=profile_id))
            if node.conflict_flag and node.conflict_group is not None
        ]
        groups: dict[str, list[GraphNode]] = {}
        for node in flagged:
            groups.setdefault(str(node.conflict_group), []).append(node)
        return {
            "groups": [
                {"group_id": group_id, "sides": [self._conflict_side(node) for node in members]}
                for group_id, members in sorted(groups.items())
            ]
        }

    @classmethod
    def _conflict_side(cls, node: GraphNode) -> dict[str, Any]:
        """One side of a conflict pair: statement, cues (domain/entities/scope),
        weights, version, and the provenance summary."""
        props = node.props
        statement = props.get("statement")
        if not isinstance(statement, str) or not statement:
            fallback = props.get("object")
            statement = fallback if isinstance(fallback, str) and fallback else node.node_id
        return {
            "node_id": node.node_id,
            "node_type": node.node_type.value,
            "statement": statement,
            "domain": props.get("domain"),
            "scope": props.get("scope"),
            "entities": list(node.entities),
            "decay_weight": node.decay_weight,
            "confidence": node.confidence,
            "reinforce_count": node.reinforce_count,
            "last_reinforced": node.last_reinforced,
            "version": node.version,
            "updated_at": node.updated_at,
            "provenance": {
                "asserted_by": node.provenance.asserted_by,
                "session_id": node.provenance.session_id,
                "source": node.provenance.source,
                "asserted_at": node.provenance.asserted_at,
            },
        }

    def resolve_conflict(
        self,
        *,
        group_id: str,
        profile_id: str,
        branch: str,
        node_id: str | None = None,
        scope: str | None = None,
        actor: str = "console",
    ) -> dict[str, Any]:
        """FR-7.7 resolution: one of the four Reconcile branches (design/01
        section 4a) on a conflict pair, written back to the version chain and
        audit-logged. ``reinforce``/``invalidate`` name one side; ``coexist``
        annotates every side with a scope; ``pending`` records without touching
        the pair (it stays flagged in the inbox). Any resolving branch clears
        the pair's conflict flags. Re-submitting an already-resolved group is
        idempotent: the recorded resolution is returned, never re-written."""
        members = self._conflict_group_nodes(group_id, profile_id)
        prior = self._prior_resolution(group_id, profile_id)

        def _already(detail: dict[str, Any] | None) -> dict[str, Any]:
            return {
                "group_id": group_id,
                "profile_id": profile_id,
                "branch": (detail or {}).get("branch") or branch,
                "already_resolved": True,
                "at": (detail or {}).get("at"),
            }

        if members is None:
            if prior is None:
                raise ConsoleNotFoundError(f"conflict group {group_id!r} not found")
            return _already(prior)
        if branch == "pending" and prior is not None and prior.get("branch") == "pending":
            return _already(prior)

        written: list[str] = []
        reinforced: list[str] = []
        invalidated: list[str] = []
        at = time.time()
        if branch == "reinforce":
            target = self._member_node(members, node_id, group_id)
            if target is None:
                raise ConsoleNotFoundError(f"node {node_id!r} is not part of conflict group {group_id!r}")
            self._stores.graph.append_version(
                self._reinforced_node(target, group_id, at, actor), invalidate_at=at
            )
            written = [node.node_id for node in members]
            reinforced = [target.node_id]
        elif branch == "coexist":
            if scope is None or not scope.strip():
                raise ConsoleNotFoundError("coexist resolution requires a scope annotation")
            for node in members:
                self._stores.graph.append_version(
                    self._scoped_node(node, scope.strip(), group_id, at, actor), invalidate_at=at
                )
                written.append(node.node_id)
        elif branch == "invalidate":
            target = self._member_node(members, node_id, group_id)
            if target is None:
                raise ConsoleNotFoundError(f"node {node_id!r} is not part of conflict group {group_id!r}")
            self._stores.graph.invalidate(target.node_id, at)
            invalidated = [target.node_id]

        if branch != "pending":
            self._stores.graph.clear_flags([node.node_id for node in members], [GraphFlag.CONFLICT_GROUP])
        self._audit(
            _CONFLICT_RESOLVE_ACTION,
            {
                "group_id": group_id,
                "profile_id": profile_id,
                "branch": branch,
                "node_id": node_id,
                "scope": scope.strip() if scope else None,
                "written": written,
                "reinforced": reinforced,
                "invalidated": invalidated,
            },
            actor=actor,
        )
        return {
            "group_id": group_id,
            "profile_id": profile_id,
            "branch": branch,
            "already_resolved": False,
            "written": written,
            "reinforced": reinforced,
            "invalidated": invalidated,
            "scope": scope.strip() if scope else None,
            "at": at,
        }

    def _conflict_group_nodes(self, group_id: str, profile_id: str) -> list[GraphNode] | None:
        """Currently-flagged members of one group; None when the group is gone."""
        members = [
            node
            for node in self._scan_nodes(NodeFilter(profile_id=profile_id))
            if node.conflict_flag and str(node.conflict_group) == group_id
        ]
        return members if members else None

    @staticmethod
    def _member_node(members: list[GraphNode], node_id: str | None, group_id: str) -> GraphNode | None:
        if node_id is None:
            return None
        for node in members:
            if node.node_id == node_id:
                return node
        return None

    def _reinforced_node(self, node: GraphNode, group_id: str, at: float, actor: str) -> GraphNode:
        """Reinforce the kept side (design/01 4a): confidence up, decay_weight
        back to 1.0, last_reinforced/last_reinforce_count refreshed, and the
        resolution pinned into the provenance history (version chain payload)."""
        history = list(node.provenance.history)
        history.append(
            ProvenanceEvent(
                at=at,
                action="conflict_reinforced",
                actor=actor,
                detail={"group_id": group_id, "branch": "reinforce"},
            )
        )
        provenance = node.provenance.model_copy(update={"history": history})
        # A resolution is a chain append, never an in-place rewrite (design/01
        # section 4): bump the version and start the new revision at ``at`` so
        # the old revision stays temporally readable as_of, bitemporal-first.
        return node.model_copy(
            update={
                "version": node.version + 1,
                "valid_from": at,
                "confidence": min(0.95, round(float(node.confidence) + 0.05, 4)),
                "decay_weight": 1.0,
                "last_reinforced": at,
                "reinforce_count": int(node.reinforce_count) + 1,
                "updated_at": at,
                "provenance": provenance,
            }
        )

    def _scoped_node(self, node: GraphNode, scope: str, group_id: str, at: float, actor: str) -> GraphNode:
        """Coexist with scope (design/01 4a): both sides keep their statement,
        the shared scope annotation lands on each node's props and provenance."""
        props = dict(node.props)
        props["scope"] = scope
        history = list(node.provenance.history)
        history.append(
            ProvenanceEvent(
                at=at,
                action="scope_annotated",
                actor=actor,
                detail={"group_id": group_id, "scope": scope, "branch": "coexist"},
            )
        )
        provenance = node.provenance.model_copy(update={"history": history})
        # Chain append semantics: the rewritten node is a new revision that
        # takes over at ``at``; the pre-resolution revision is closed by the
        # caller's append_version(invalidate_at=at) and stays as_of-readable.
        return node.model_copy(
            update={
                "version": node.version + 1,
                "valid_from": at,
                "props": props,
                "updated_at": at,
                "provenance": provenance,
            }
        )

    def _prior_resolution(self, group_id: str, profile_id: str) -> dict[str, Any] | None:
        """The most recent recorded resolution of a group (None when never
        resolved); this is the idempotency ledger for re-submits."""
        latest: dict[str, Any] | None = None
        page = Page(limit=200)
        while True:
            result = self._stores.meta.audit_query(AuditFilter(action=_CONFLICT_RESOLVE_ACTION), page)
            for entry in result.items:
                detail = entry.detail
                if (
                    str(detail.get("group_id", "")) == group_id
                    and str(detail.get("profile_id", "")) == profile_id
                ):
                    latest = {
                        "group_id": group_id,
                        "profile_id": profile_id,
                        "branch": detail.get("branch"),
                        "node_id": detail.get("node_id"),
                        "scope": detail.get("scope"),
                        "at": entry.at,
                    }
            consumed = page.offset + len(result.items)
            if result.total <= consumed:
                return latest
            page = Page(offset=consumed, limit=page.limit)

    # ------------------------------------------------------------ memory writes (FR-7.9)

    def forget(
        self,
        *,
        profile_id: str,
        chunk_id: str | None = None,
        node_id: str | None = None,
        entity: str | None = None,
        actor: str = "console",
    ) -> dict[str, Any]:
        """FR-7.9 forget: mirrors the daemon's ``forget_this`` semantics
        (design/03 storage-layer erasure) -- chunks are physically removed from
        the verbatim channel, graph nodes are tombstoned (the version chain
        survives for as_of replay). Exactly one target kind is required; every
        removal is audit-logged."""
        removed_chunks: list[str] = []
        removed_nodes: list[str] = []
        if chunk_id is not None:
            chunk = self._stores.vector.get_chunk(chunk_id)
            if chunk is None or chunk.profile_id != profile_id:
                raise ConsoleNotFoundError(f"chunk {chunk_id!r} not found")
            self._stores.vector.delete_chunk(chunk_id)
            removed_chunks.append(chunk_id)
        elif node_id is not None:
            node = self._stores.graph.get_node(node_id)
            if node is None or node.profile_id != profile_id:
                raise ConsoleNotFoundError(f"node {node_id!r} not found")
            self._stores.graph.tombstone(node_id)
            removed_nodes.append(node_id)
        elif entity is not None:
            chunk_page = self._stores.vector.list_chunks(
                ChunkFilter(profile_id=profile_id, entities=(entity,)), Page(limit=_SCAN_CAP)
            )
            for chunk in chunk_page.items:
                self._stores.vector.delete_chunk(chunk.chunk_id)
                removed_chunks.append(chunk.chunk_id)
            for node in self._scan_nodes(NodeFilter(profile_id=profile_id, entities=(entity,))):
                self._stores.graph.tombstone(node.node_id)
                removed_nodes.append(node.node_id)
        else:
            raise ValueError("forget requires one of chunk_id, node_id, entity")
        self._audit(
            "forget_this",
            {
                "profile_id": profile_id,
                "entity": entity,
                "chunks": removed_chunks,
                "nodes": removed_nodes,
            },
            actor=actor,
        )
        return {"removed": {"chunks": removed_chunks, "nodes": removed_nodes}}

    def pin_node(
        self, *, profile_id: str, node_id: str, pinned: bool, actor: str = "console"
    ) -> dict[str, Any]:
        """FR-7.9 manual pin: flips ``never_decay`` as a version-chain append
        (never an in-place rewrite) and audit-logs the action. Idempotent when
        the node already carries the requested state."""
        node = self._stores.graph.get_node(node_id)
        if node is None or node.profile_id != profile_id:
            raise ConsoleNotFoundError(f"node {node_id!r} not found")
        if node.never_decay is pinned:
            return {
                "node_id": node_id,
                "profile_id": profile_id,
                "never_decay": pinned,
                "version": node.version,
                "changed": False,
            }
        at = time.time()
        history = list(node.provenance.history)
        history.append(
            ProvenanceEvent(
                at=at,
                action="pinned" if pinned else "unpinned",
                actor=actor,
                detail={"node_id": node_id, "profile_id": profile_id, "pinned": pinned},
            )
        )
        provenance = node.provenance.model_copy(update={"history": history})
        revised = node.model_copy(
            update={
                "version": node.version + 1,
                "valid_from": at,
                "never_decay": pinned,
                "updated_at": at,
                "provenance": provenance,
            }
        )
        self._stores.graph.append_version(revised, invalidate_at=at)
        self._audit(
            "pin",
            {
                "node_id": node_id,
                "profile_id": profile_id,
                "pinned": pinned,
                "version": revised.version,
            },
            actor=actor,
        )
        return {
            "node_id": node_id,
            "profile_id": profile_id,
            "never_decay": pinned,
            "version": revised.version,
            "changed": True,
        }

    def adjust_weight(
        self,
        *,
        profile_id: str,
        kind: str,
        target_id: str,
        decay_weight: float,
        actor: str = "console",
    ) -> dict[str, Any]:
        """FR-7.9 manual decay-weight adjustment for one node or chunk,
        bounded to [0.0, 1.0] and audited with both the old and new values."""
        if kind == "node":
            node = self._stores.graph.get_node(target_id)
            if node is None or node.profile_id != profile_id:
                raise ConsoleNotFoundError(f"node {target_id!r} not found")
            old = node.decay_weight
            self._stores.graph.batch_update_weights(
                [GraphWeightUpdate(node_id=target_id, decay_weight=decay_weight)]
            )
        elif kind == "chunk":
            chunk = self._stores.vector.get_chunk(target_id)
            if chunk is None or chunk.profile_id != profile_id:
                raise ConsoleNotFoundError(f"chunk {target_id!r} not found")
            old = chunk.decay_weight
            self._stores.vector.update_weights([WeightUpdate(chunk_id=target_id, decay_weight=decay_weight)])
        else:
            raise ValueError(f"unknown weight target kind {kind!r}")
        self._audit(
            "weight_adjust",
            {
                "profile_id": profile_id,
                "kind": kind,
                "target_id": target_id,
                "old_decay_weight": old,
                "new_decay_weight": decay_weight,
            },
            actor=actor,
        )
        return {
            "kind": kind,
            "target_id": target_id,
            "profile_id": profile_id,
            "decay_weight": decay_weight,
            "old_decay_weight": old,
        }

    # ------------------------------------------------------------ profiles (FR-7.3)

    def create_profile(self, *, profile_id: str, display_name: str, actor: str = "console") -> dict[str, Any]:
        """FR-7.3 console profile create (upsert into the meta port), audited."""
        at = time.time()
        self._stores.meta.upsert_profile(
            StoredProfile(profile_id=profile_id, display_name=display_name, created_at=at)
        )
        self._audit("profile.create", {"profile_id": profile_id, "display_name": display_name}, actor=actor)
        return {
            "profile_id": profile_id,
            "display_name": display_name,
            "created_at": at,
            "archived": False,
        }

    def rename_profile(self, *, profile_id: str, display_name: str, actor: str = "console") -> dict[str, Any]:
        """FR-7.3 console profile rename: display_name-only upsert that never
        touches created_at or the archived flag; audited with the old name."""
        profile = self._stores.meta.get_profile(profile_id)
        if profile is None:
            raise ConsoleNotFoundError(f"profile {profile_id!r} not found")
        old_name = profile.display_name
        self._stores.meta.upsert_profile(
            StoredProfile(
                profile_id=profile_id,
                display_name=display_name,
                created_at=profile.created_at,
                archived=profile.archived,
            )
        )
        self._audit(
            "profile.rename",
            {
                "profile_id": profile_id,
                "display_name": old_name,
                "new_display_name": display_name,
            },
            actor=actor,
        )
        return {
            "profile_id": profile_id,
            "display_name": display_name,
            "created_at": profile.created_at,
            "archived": profile.archived,
        }

    def archive_profile(self, *, profile_id: str, archived: bool, actor: str = "console") -> dict[str, Any]:
        """FR-7.3 console profile archive flag (reversible), audited."""
        profile = self._stores.meta.get_profile(profile_id)
        if profile is None:
            raise ConsoleNotFoundError(f"profile {profile_id!r} not found")
        self._stores.meta.archive_profile(profile_id, archived)
        self._audit(
            "profile.archive",
            {"profile_id": profile_id, "archived": archived, "display_name": profile.display_name},
            actor=actor,
        )
        return {"profile_id": profile_id, "archived": archived}

    def issue_token(
        self,
        *,
        profile_id: str,
        scopes: tuple[str, ...],
        expires_at: float | None = None,
        actor: str = "console",
    ) -> dict[str, Any]:
        """FR-7.3 console token issue. The bearer secret is returned exactly
        once and never lands in the audit detail (only the token id + shape)."""
        if self._stores.meta.get_profile(profile_id) is None:
            raise ConsoleNotFoundError(f"profile {profile_id!r} not found")
        token = self._stores.meta.issue_token(profile_id, scopes, expires_at)
        self._audit(
            "token.issue",
            {
                "token_id": token.token_id,
                "profile_id": profile_id,
                "scopes": list(token.scopes),
                "expires_at": expires_at,
            },
            actor=actor,
        )
        return {
            "token_id": token.token_id,
            "profile_id": profile_id,
            "scopes": list(token.scopes),
            "issued_at": token.issued_at,
            "expires_at": token.expires_at,
            "token_secret": token.token_secret,
        }

    def revoke_token(self, *, token_id: str, actor: str = "console") -> dict[str, Any]:
        """FR-7.3 console token revoke (idempotent), audited."""
        self._stores.meta.revoke_token(token_id)
        self._audit("token.revoke", {"token_id": token_id}, actor=actor)
        return {"token_id": token_id, "revoked": True}

    # ------------------------------------------------------------ audit (FR-7.9 / G-AC1)

    def audit_log(
        self,
        *,
        actor: str | None = None,
        action: str | None = None,
        since: float | None = None,
        until: float | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        """FR-7.9 audit view: paginated append-only trail with actor/action/
        time filters, ascending (chronological, id-ordered)."""
        page = self._stores.meta.audit_query(
            AuditFilter(actor=actor, action=action, since=since, until=until),
            Page(offset=offset, limit=limit),
        )
        return {
            "items": [
                {
                    "id": entry.id,
                    "actor": entry.actor,
                    "action": entry.action,
                    "detail": entry.detail,
                    "at": entry.at,
                }
                for entry in page.items
            ],
            "paging": {"total": page.total, "offset": offset, "limit": limit},
        }

    # ------------------------------------------------------------ plumbing

    def _audit(self, action: str, detail: dict[str, Any], *, actor: str = "console") -> None:
        self._stores.meta.audit_append(AuditEntry(actor=actor, action=action, detail=detail, at=time.time()))

    # ------------------------------------------------------------ payloads

    @staticmethod
    def _chunk_summary(chunk: ChunkStamp) -> dict[str, Any]:
        cues = chunk.cues
        return {
            "chunk_id": chunk.chunk_id,
            "profile_id": chunk.profile_id,
            "text": chunk.text,
            "cognitive_tier": int(chunk.cognitive_tier),
            "model_id": chunk.model_id,
            "cues": {
                "project": cues.project,
                "host": cues.host,
                "task": cues.task,
                "tools_used": list(cues.tools_used),
                "time_bucket": cues.time_bucket,
                "entities": list(cues.entities),
            },
            "decay_weight": chunk.decay_weight,
            "score": chunk.score,
            "consolidated": chunk.consolidated,
            "ingested_at": chunk.ingested_at,
            "turn_start": chunk.turn_start,
            "turn_end": chunk.turn_end,
        }

    @staticmethod
    def _node_summary(node: GraphNode) -> dict[str, Any]:
        statement = node.props.get("statement")
        if not isinstance(statement, str) or not statement:
            fallback = node.props.get("object")
            statement = fallback if isinstance(fallback, str) and fallback else node.node_id
        return {
            "node_id": node.node_id,
            "profile_id": node.profile_id,
            "node_type": node.node_type.value,
            "statement": statement,
            "entities": list(node.entities),
            "decay_weight": node.decay_weight,
            "confidence": node.confidence,
            "conflict_flag": node.conflict_flag,
            "conflict_group": node.conflict_group,
            "needs_reconcile": node.needs_reconcile,
            "pending_consolidation": node.pending_consolidation,
            "hit_count": node.hit_count,
            "version": node.version,
            "updated_at": node.updated_at,
        }

    @staticmethod
    def _provenance_payload(provenance: Any) -> dict[str, Any]:
        return {
            "asserted_by": provenance.asserted_by,
            "agent_id": provenance.agent_id,
            "session_id": provenance.session_id,
            "source": provenance.source,
            "confidence": provenance.confidence,
            "asserted_at": provenance.asserted_at,
            "history": [
                {"action": event.action, "actor": event.actor, "at": event.at, "detail": event.detail}
                for event in provenance.history
            ],
        }

    @staticmethod
    def _dream_run_payload(run: DreamRun) -> dict[str, Any]:
        duration = (run.finished_at - run.started_at) if run.finished_at is not None else None
        return {
            "run_id": run.run_id,
            "session_id": run.session_id,
            "turn_range": _range_payload(run.turn_range),
            "model_id": run.model_id,
            "started_at": run.started_at,
            "finished_at": run.finished_at,
            "duration_seconds": duration,
            "tokens": run.tokens,
            "cost": run.cost,
            "interrupted": run.interrupted,
            "dropped_count": run.dropped_count,
        }
