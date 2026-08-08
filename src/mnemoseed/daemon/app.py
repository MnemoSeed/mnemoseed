"""MnemoSeed daemon — FastAPI core, single source of truth.

Boot sequence: load config -> build stores -> capability gate -> serve.
Memory APIs are mounted in later milestones; M0 ships health/capabilities only.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from mnemoseed import __version__
from mnemoseed.config import load_config
from mnemoseed.storage.factory import Stores, build_stores

logger = logging.getLogger("mnemoseed.daemon")


@asynccontextmanager
async def lifespan(app: FastAPI):
    config = load_config()
    stores = build_stores(config)
    app.state.config = config
    app.state.stores = stores
    if stores.report.ok:
        logger.info("storage stack ready, all required capabilities present")
    else:
        for deg in stores.report.missing:
            logger.warning("degraded: %s - %s", deg.feature, deg.behavior)
    yield
    await stores.close()


def create_app() -> FastAPI:
    app = FastAPI(title="MnemoSeed", version=__version__, lifespan=lifespan)

    @app.get("/health")
    async def health() -> dict:
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
    async def capabilities() -> dict:
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
