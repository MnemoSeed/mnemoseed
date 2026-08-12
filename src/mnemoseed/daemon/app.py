"""MnemoSeed daemon — FastAPI core, single source of truth.

Boot sequence: load config -> build stores (drivers run their schema
migrations at construction) -> capability gate -> serve. Memory APIs land in
later milestones; M0 ships /healthz, /health and /capabilities only.

``/healthz`` is the liveness probe: it reports store assembly status (per-layer
drivers), the meta schema version (proof the migrations ran), and the appendix
C capability findings, and responds well under the 100ms NFR-8.1 budget.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, cast

from fastapi import FastAPI

from mnemoseed import __version__
from mnemoseed.capture import ScoringPipeline, StrippingPipeline, TurnScorer, TurnSegmenter, WritingPipeline
from mnemoseed.capture.pool import PoolEvent, ScorePool
from mnemoseed.capture.stamper import WriteContext
from mnemoseed.config import Config, load_config
from mnemoseed.console import ConsoleService, GuardedStaticFiles
from mnemoseed.console import router as console_router
from mnemoseed.daemon.ingest import router as ingest_router
from mnemoseed.daemon.memory import MemoryService
from mnemoseed.daemon.memory import router as memory_router
from mnemoseed.dream import (
    DreamPipeline,
    DreamTrigger,
    FileSnapshotter,
    Merger,
    ReflectOrchestrator,
    TokenLedger,
    resume_boundary,
)
from mnemoseed.identity import IdentityService
from mnemoseed.identity.routes import router as identity_router
from mnemoseed.llm import RoleRouter
from mnemoseed.llm.admin import LLMAdminService
from mnemoseed.llm.admin_routes import router as llm_admin_router
from mnemoseed.llm.types import (
    ChatResult,
    DreamLLM,
    HealthReport,
    LLMDriverInfo,
    LLMError,
    LLMUnavailable,
)
from mnemoseed.schema.stamp import CognitiveTier
from mnemoseed.schema.turn import Turn
from mnemoseed.storage.factory import Stores, build_stores
from mnemoseed.storage.ports import CapabilityIssue, GraphStore, MetaStore

logger = logging.getLogger("mnemoseed.daemon")

# Console SPA shell directory, served under /console by GuardedStaticFiles.
# Path: <pkg>/console/static (sibling of the daemon package).
_CONSOLE_STATIC_DIR = Path(__file__).resolve().parent.parent / "console" / "static"

# The dream role the reflect boundary runs (FR-2.14): the long-background deep
# reflection digest driven by ReflectOrchestrator (design/02 section 6).
_REFLECT_ROLE = "deep_reflection"


class _UnavailableLLM:
    """Boot-safe deferred dream LLM (FR-2.6).

    Used when the configured deep_reflection route cannot be materialized at
    boot (unknown driver name, or a driver construction failure): boot must
    never crash on a broken route. Dreams stay capture-only — ``chat`` degrades
    through the typed ``LLMUnavailable`` path the reflect boundary already
    handles, and ``check`` reports the reason without raising.
    """

    info: ClassVar[LLMDriverInfo] = LLMDriverInfo(
        name="unavailable",
        description="deferred route: the configured deep_reflection driver failed to build",
    )

    def __init__(self, reason: str) -> None:
        self._reason = reason

    def chat(self, *, system: str, user: str) -> ChatResult:
        del system
        raise LLMUnavailable(self._reason)

    def check(self) -> HealthReport:
        return HealthReport(ok=False, detail={"error": self._reason})


def _build_dream_llm(config: Config, meta: MetaStore) -> DreamLLM:
    """Materialize the dream-pipeline LLM from the configured routes.

    The reflect role is resolved through the RoleRouter exactly like any other
    consumer: the route's own driver+model+params build the instance, the
    api-key env-var chain is resolved at materialization time, and the role is
    audited. Resolution performs no network I/O (drivers construct lazy HTTP
    clients), so boot stays fast with or without keys. A route that cannot be
    built (unknown driver, bad params) degrades typed to a deferred LLM instead
    of crashing boot; the reflect boundary then reports ``llm_unavailable`` and
    the snapshot stays journaled (FR-2.6).
    """
    router = RoleRouter(routes=config.llm, audit=meta)
    try:
        return router.resolve(_REFLECT_ROLE)
    except LLMError as exc:
        logger.warning(
            "deep_reflection route unavailable at boot (%s); dreams degrade to capture-only "
            "until the route is fixed",
            exc,
        )
        return _UnavailableLLM(str(exc))


def _reflect_unavailable(reason: str) -> None:
    """FR-2.6: log each typed provider outage the reflect boundary refuses."""
    logger.warning("dream reflect model unavailable: %s", reason)


@dataclass(frozen=True)
class HealthSnapshot:
    """Boot-time snapshot served by /healthz (captured once, served many)."""

    started_at: float
    preset: str
    stores: dict[str, dict[str, str]]
    migrations: dict[str, int]
    gate_ok: bool
    degradations: list[dict[str, str]]
    hard_missing: list[dict[str, str]]


def _issue_payload(issue: CapabilityIssue) -> dict[str, str]:
    return {
        "capability": issue.capability.value,
        "severity": issue.severity.value,
        "layer": issue.layer,
        "instance": issue.instance,
        "driver": issue.driver,
        "feature": issue.feature,
        "behavior": issue.behavior,
    }


def _stores_payload(stores: Stores) -> dict[str, dict[str, str]]:
    return {
        kind: {name: store.info.name for name, store in named.items()}
        for kind, named in stores.instances.items()
    }


def _migrations_payload(stores: Stores) -> dict[str, int]:
    """Meta schema version per named instance — proof the migrations ran."""
    versions: dict[str, int] = {}
    for name, store in stores.instances.get("meta", {}).items():
        getter = getattr(store, "schema_version", None)
        if getter is not None:
            versions[name] = int(getter())
    return versions


def _daemon_write_context(turn: Turn) -> WriteContext:
    """Per-write encoding context on the serving path (FR-1.6).

    Every default is explicit because the wire model carries no situational
    fields today:

    - profile_id: the turn's identity (required, never guessed).
    - host: the producing host label; free encoding-specificity context.
    - cognitive_tier: TIER_1 until a model-tier config exists; Tier-1 hosts
      route to the core graph by default.
    - agent_label: None until the anima system exists; capture must stay
      neutral, and the daemon is the only party allowed to supply the label.
    - project / task: None; the /ingest wire model carries no such fields yet.
    - time_bucket / entities: dataclass defaults.
    """
    return WriteContext(
        profile_id=turn.profile_id,
        host=turn.host.value,
        cognitive_tier=CognitiveTier.TIER_1,
    )


class _DreamRelay:
    """Deferred dream-event delivery off the scoring hot path.

    The ScorePool fires dream events while the ScoringPipeline is still scoring
    a drained session — before the WritingPipeline has persisted that session's
    chunks to the vector store. A dream launched at that instant would capture
    an empty snapshot and its safe-clear would purge nothing. The relay instead
    collects the fired events and, once the drain wrote the chunks (the daemon
    flushes after ``WritingPipeline.drain``), hands them to the trigger in
    order. Manual-first (FR-2.8) is untouched: with auto_trigger=False the relay
    simply delivers events the trigger records as pending-manual.
    """

    def __init__(self, trigger: DreamTrigger) -> None:
        self._trigger = trigger
        self._pending: deque[PoolEvent] = deque()

    def handle(self, event: PoolEvent) -> None:
        """ScorePool sink seam: buffer a fired dream event during the drain."""
        self._pending.append(event)

    def flush(self) -> None:
        """Deliver buffered events to the trigger, in fire order."""
        while self._pending:
            self._trigger.handle_event(self._pending.popleft())


def _build_capture(
    stores: Stores, config: Config
) -> tuple[WritingPipeline, DreamTrigger, DreamPipeline, _DreamRelay]:
    """Serving capture funnel: strip -> score -> pool -> stamp/write over the
    resolved storage stack. /ingest stays submit-only; the funnel drains on
    /session/end (v1 drain trigger, off the /ingest hot path).

    The ScorePool binds the meta store as its persistence backend, is restored
    at boot from the persisted per-profile ledgers (so a daemon restart keeps
    un-triggered balances), and sinks its dream events into the DreamTrigger.
    The trigger binds the real snapshotter (T2): a frozen capture written under
    the config directory and registered in dream_runs, whose completion seam
    runs the T4 dream pipeline (reflect -> merge -> safe-clear commit) off the
    ingest hot path. The reflect LLM is the configured deep_reflection route
    (FR-2.14) resolved through the RoleRouter — drivers construct lazy HTTP
    clients, so boot resolves no network, and an unbuildable route degrades
    typed to a deferred LLM instead of crashing boot (FR-2.6). Boot recovery
    (NFR-2.3) resumes interrupted dreams at their exact phase boundary —
    reflect for a fresh snapshot, merge ONLY for one that already ran reflect —
    synchronously before serving starts. The safe-clear purger fires exactly
    once, on merge-commit. FR-2.8 manual-first ``[dream] auto_trigger`` stays
    honoured.
    """
    snapshotter = FileSnapshotter(store=stores.vector, meta=stores.meta)
    trigger = DreamTrigger(
        snapshotter=snapshotter,
        auto_trigger=config.dream.auto_trigger,
        purger=snapshotter.purge_snapshot,
    )
    graph_isolated = cast(GraphStore | None, stores.instances.get("graph", {}).get("isolated"))
    if graph_isolated is None:
        logger.warning(
            "no 'isolated' graph instance configured; tier-3 output is stranded "
            "(the salvage review channel still captures the entry)"
        )
    reflector = ReflectOrchestrator(
        llm=_build_dream_llm(config, stores.meta),
        directory=snapshotter.directory,
        on_done=trigger.on_reflect_complete,
        # FR-2.6: log every typed provider outage the reflect boundary refuses
        # (capture-only degradation), off the ingest hot path.
        on_unavailable=_reflect_unavailable,
        # FR-2.5b: the monthly token ledger binds the real meta store and the
        # config's USD cap, so the budget gate and the meter run on the serving
        # path (capture-only once the projected month spend exceeds the cap).
        ledger=TokenLedger(meta=stores.meta, budget_usd=config.dream.token_budget_usd),
    )
    merger = Merger(
        graph_main=stores.graph,
        graph_isolated=graph_isolated,
        meta=stores.meta,
        on_committed=trigger.on_merge_committed,
    )
    pipeline = DreamPipeline(trigger=trigger, snapshotter=snapshotter, reflector=reflector, merger=merger)
    relay = _DreamRelay(trigger)
    snapshotter.on_ready = pipeline.on_snapshot_ready
    for snapshot in snapshotter.recover():
        snapshotter.adopt(snapshot)
        boundary = resume_boundary(snapshot)
        if boundary == "reflect":
            trigger.resume(snapshot.profile_id, snapshot.turn_range)
        elif boundary == "merge":
            # reflect already wrote back; resume straight at the merge stage so
            # the merge-commit seam fires the safe-clear once and never re-runs
            # reflect (would duplicate the committed graph writes)
            trigger.resume_merge(snapshot.profile_id, snapshot.turn_range)
        pipeline.run(snapshot)
    pool = ScorePool(clock=time.monotonic, backend=stores.meta, sink=relay.handle)
    for profile_id, state in stores.meta.pool_states().items():
        pool.restore(profile_id, state.balance, state.watermark)
    scoring = ScoringPipeline(
        scorer=TurnScorer(embedder=stores.embed),
        pool=pool,
    )
    return (
        WritingPipeline(
            store=stores.vector,
            inner=scoring,
            embedder=stores.embed,
            context=_daemon_write_context,
        ),
        trigger,
        pipeline,
        relay,
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    config = load_config()
    stores = build_stores(config)
    app.state.config = config
    app.state.stores = stores
    # The serving funnel is bound to the resolved stack here, not at app
    # construction: the VectorStore/Embedder instances only exist after boot.
    (
        app.state.capture,
        app.state.dream,
        app.state.dream_pipeline,
        app.state.dream_relay,
    ) = _build_capture(stores, config)
    app.state.segmenter = TurnSegmenter(app.state.capture)
    # The memory surface (T4) owns one retrieval engine whose track executor is
    # shut down in teardown, before the stores close (no worker outlives boot).
    app.state.memory = MemoryService(stores, config)
    # Console surface (PRD-07): the live dream trigger is bound here so the
    # /api/v1 routers observe the same trigger instance the /memory and /ingest
    # surfaces drive.
    app.state.console = ConsoleService(stores, config, app.state.dream)
    # Identity chain (issue #14): the owner account + token surface. Every
    # memory and console route depends on require_identity, which reads this
    # state; the setup wizard 503s those routes until setup_owner has run.
    app.state.identity = IdentityService(stores.meta)
    # LLM admin surface (issue #23 / FR-6.9): per-role route reads/writes,
    # OAuth availability, and the pre-write connectivity probe. It audits
    # through the same meta store the console writes do and persists surgical
    # TOML patches into the live config.
    app.state.llm_admin = LLMAdminService(config, stores.meta)
    app.state.health = HealthSnapshot(
        started_at=time.perf_counter(),
        preset=config.preset,
        stores=_stores_payload(stores),
        migrations=_migrations_payload(stores),
        gate_ok=stores.report.ok,
        degradations=[_issue_payload(i) for i in stores.report.degradations],
        hard_missing=[_issue_payload(i) for i in stores.report.hard_missing],
    )
    if stores.report.ok:
        logger.info("storage stack ready, all required capabilities present")
    else:
        for deg in stores.report.missing:
            logger.warning("degraded: %s - %s", deg.feature, deg.behavior)
    yield
    # Close the memory engine before the stores: the retrieval executor's
    # worker threads own sqlite handles and must join first (lifecycle fix).
    app.state.memory.close()
    await stores.close()


def create_app() -> FastAPI:
    app = FastAPI(title="MnemoSeed", version=__version__, lifespan=lifespan)
    # Capture intake lives per-app state; F1 (StrippingPipeline) drains the
    # same pipeline instance the /ingest router hands turns to, on the
    # consumer side of the seam so the HTTP path stays O(1).
    app.state.capture = StrippingPipeline()
    app.state.segmenter = TurnSegmenter(app.state.capture)
    app.include_router(ingest_router)
    app.include_router(identity_router)
    app.include_router(memory_router)
    app.include_router(console_router)
    app.include_router(llm_admin_router)
    # Console SPA shell (PRD-07 T1): served from its own static directory,
    # mounted behind the same localhost/admin-token gate as /api/v1.
    app.mount("/console", GuardedStaticFiles(directory=_CONSOLE_STATIC_DIR), name="console")

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        snap: HealthSnapshot = app.state.health
        elapsed_ms = (time.perf_counter() - snap.started_at) * 1000.0
        return {
            "status": "ok",
            "uptime_ms": round(elapsed_ms, 3),
            "preset": snap.preset,
            "stores": snap.stores,
            "migrations": snap.migrations,
            "gate": {
                "ok": snap.gate_ok,
                "degradations": snap.degradations,
                "hard_missing": snap.hard_missing,
            },
        }

    @app.get("/health")
    async def health() -> dict[str, Any]:
        stores: Stores = app.state.stores
        return {
            "status": "ok",
            "version": __version__,
            "preset": app.state.config.preset,
            "drivers": {
                "vector": stores.vector.info.name,
                "graph": stores.graph.info.name,
                "meta": stores.meta.info.name,
                "embed": stores.embed.info.name,
            },
        }

    @app.get("/capabilities")
    async def capabilities() -> dict[str, Any]:
        stores: Stores = app.state.stores
        return {
            "ok": stores.report.ok,
            "degradations": [
                {
                    "capability": d.capability.value,
                    "feature": d.feature,
                    "behavior": d.behavior,
                }
                for d in stores.report.missing
            ],
            "drivers": {
                kind: {
                    "name": store.info.name,
                    "capabilities": sorted(c.value for c in store.info.capabilities),
                }
                for kind, store in (
                    ("vector", stores.vector),
                    ("graph", stores.graph),
                    ("meta", stores.meta),
                    ("embed", stores.embed),
                )
            },
        }

    return app


app = create_app()
