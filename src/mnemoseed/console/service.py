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

Writes that M1 owns (``dream_once``, the ``auto_trigger`` toggle) all land in
the append-only audit trail.
"""

from __future__ import annotations

import calendar
import re
import time
from pathlib import Path
from typing import Any

from mnemoseed import __version__
from mnemoseed.config import Config
from mnemoseed.daemon.memory import _trigger_payload
from mnemoseed.dream import DreamTrigger, TokenLedger
from mnemoseed.schema.graph import GraphNode, NodeType
from mnemoseed.schema.stamp import ChunkStamp
from mnemoseed.storage.factory import Stores
from mnemoseed.storage.ports import (
    AuditEntry,
    ChunkFilter,
    DreamRun,
    DreamRunFilter,
    NodeFilter,
    Page,
    TurnRange,
)

# Bounded scan shape: pages are cheap; a client-side overlay over the scan is
# the console's declared read pattern for M1 (10k-row cap keeps a pathological
# filter from starving a local daemon).
_SCAN_PAGE = 500
_SCAN_CAP = 10_000

_AUTO_TRIGGER_LINE = re.compile(r"(\s*)auto_trigger\s*=\s*(?:true|false)(.*)")


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

    def __init__(self, stores: Stores, config: Config, trigger: DreamTrigger) -> None:
        self._stores = stores
        self._config = config
        self._trigger = trigger

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

    def dream_once(self, profile_id: str) -> dict[str, Any]:
        """FR-7.6 manual trigger: reuse the trigger's ``dream_once`` seam."""
        launched = self._trigger.dream_once(profile_id)
        payload = _trigger_payload(self._trigger.status(profile_id))
        payload["launched"] = launched
        self._audit("dream_once", {"profile_id": profile_id, "launched": launched})
        return payload

    def set_auto_trigger(self, enabled: bool) -> dict[str, Any]:
        """FR-7.6 auto-trigger toggle: live flag + config-file persistence."""
        self._trigger.set_auto_trigger(enabled)
        path = self._persist_auto_trigger(enabled)
        self._audit("console.auto_trigger", {"enabled": enabled, "persisted_to": str(path)})
        return {"enabled": enabled, "persisted_to": str(path)}

    def _persist_auto_trigger(self, enabled: bool) -> Path:
        """Write ``[dream] auto_trigger`` back into the config TOML.

        Line-oriented patch (not a full TOML round-trip) so comments and
        unrelated keys survive untouched.
        """
        source = self._config.source
        path = source if source is not None else Path.home() / ".mnemoseed" / "config.toml"
        value = "true" if enabled else "false"
        original = path.read_text(encoding="utf-8") if path.exists() else ""
        lines = original.split("\n")
        in_dream = False
        dream_header: int | None = None
        replaced = False
        out: list[str] = []
        for index, line in enumerate(lines):
            stripped = line.strip()
            is_header = stripped.startswith("[") and stripped.endswith("]")
            if is_header:
                name = stripped[1:-1].strip()
                if name == "dream":
                    in_dream = True
                    dream_header = index
                else:
                    in_dream = False
            if in_dream and not replaced:
                match = _AUTO_TRIGGER_LINE.fullmatch(line)
                if match is not None:
                    out.append(f"{match.group(1)}auto_trigger = {value}{match.group(2)}")
                    replaced = True
                    continue
            out.append(line)
        if not replaced:
            if dream_header is not None:
                out.insert(dream_header + 1, f"auto_trigger = {value}")
            else:
                body = "\n".join(out).rstrip("\n")
                tail = "[dream]\n" + f"auto_trigger = {value}\n"
                out = [body, "", tail.rstrip("\n")] if body else [tail.rstrip("\n")]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(out).strip("\n") + "\n", encoding="utf-8")
        # Keep the in-memory raw mirror consistent for subsequent reads.
        dream_raw = self._config.raw.setdefault("dream", {})
        dream_raw["auto_trigger"] = enabled
        return path

    # ------------------------------------------------------------ plumbing

    def _audit(self, action: str, detail: dict[str, Any]) -> None:
        self._stores.meta.audit_append(
            AuditEntry(actor="console", action=action, detail=detail, at=time.time())
        )

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
