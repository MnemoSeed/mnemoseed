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
from mnemoseed.capture import InMemoryCapturePipeline, TurnSegmenter
from mnemoseed.config import load_config
from mnemoseed.daemon.ingest import router as ingest_router
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


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    config = load_config()
    stores = build_stores(config)
    app.state.config = config
    app.state.stores = stores
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
    # Capture intake lives per-app state; the F1-F3 funnel consumes the same
    # pipeline instance the /ingest router hands turns to.
    app.state.capture = InMemoryCapturePipeline()
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
