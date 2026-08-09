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
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI

from mnemoseed import __version__
from mnemoseed.capture import ScoringPipeline, StrippingPipeline, TurnScorer, TurnSegmenter, WritingPipeline
from mnemoseed.capture.pool import ScorePool
from mnemoseed.capture.stamper import WriteContext
from mnemoseed.config import Config, load_config
from mnemoseed.daemon.ingest import router as ingest_router
from mnemoseed.dream import DreamTrigger, NullSnapshotter
from mnemoseed.schema.stamp import CognitiveTier
from mnemoseed.schema.turn import Turn
from mnemoseed.storage.factory import Stores, build_stores
from mnemoseed.storage.ports import CapabilityIssue

logger = logging.getLogger("mnemoseed.daemon")


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


def _build_capture(stores: Stores, config: Config) -> tuple[WritingPipeline, DreamTrigger]:
    """Serving capture funnel: strip -> score -> pool -> stamp/write over the
    resolved storage stack. /ingest stays submit-only; the funnel drains on
    /session/end (v1 drain trigger, off the /ingest hot path).

    The ScorePool binds the meta store as its persistence backend, is restored
    at boot from the persisted per-profile ledgers (so a daemon restart keeps
    un-triggered balances), and sinks its dream events into the DreamTrigger.
    The trigger binds the void snapshot seam until T2 supplies the real one and
    honours the FR-2.8 manual-first ``[dream] auto_trigger`` flag. The trigger
    is returned for the console panel (PRD-07) and the future /ingest
    ``notify_activity`` wiring.
    """
    trigger = DreamTrigger(snapshotter=NullSnapshotter(), auto_trigger=config.dream.auto_trigger)
    pool = ScorePool(clock=time.monotonic, backend=stores.meta, sink=trigger.handle_event)
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
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    config = load_config()
    stores = build_stores(config)
    app.state.config = config
    app.state.stores = stores
    # The serving funnel is bound to the resolved stack here, not at app
    # construction: the VectorStore/Embedder instances only exist after boot.
    app.state.capture, app.state.dream = _build_capture(stores, config)
    app.state.segmenter = TurnSegmenter(app.state.capture)
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
    await stores.close()


def create_app() -> FastAPI:
    app = FastAPI(title="MnemoSeed", version=__version__, lifespan=lifespan)
    # Capture intake lives per-app state; F1 (StrippingPipeline) drains the
    # same pipeline instance the /ingest router hands turns to, on the
    # consumer side of the seam so the HTTP path stays O(1).
    app.state.capture = StrippingPipeline()
    app.state.segmenter = TurnSegmenter(app.state.capture)
    app.include_router(ingest_router)

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
