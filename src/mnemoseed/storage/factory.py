"""Storage factory: builds drivers from Config and validates capabilities.

Startup gate rule: drivers are not interchangeable. Every driver declares its
capability set; the daemon validates it against REQUIRED_CAPS at boot and
records explicit degradations. Missing capabilities degrade loudly, never
silently.
"""

from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass, field

from mnemoseed.config import Config
from mnemoseed.storage.ports import (
    EMBED_DRIVERS,
    GRAPH_DRIVERS,
    META_DRIVERS,
    REQUIRED_CAPS,
    VECTOR_DRIVERS,
    CapabilityReport,
    Degradation,
    Embedder,
    GraphStore,
    MetaStore,
    StorageError,
    VectorStore,
)

logger = logging.getLogger(__name__)


@dataclass
class Stores:
    """Fully wired storage stack plus its capability report."""

    vector: VectorStore
    graph: GraphStore
    meta: MetaStore
    embed: Embedder
    report: CapabilityReport = field(default_factory=lambda: CapabilityReport(ok=True))

    async def close(self) -> None:
        for store in (self.vector, self.graph, self.meta, self.embed):
            await store.close()


_DRIVER_MODULES = {
    # driver name: module providing it (imported on demand so optional deps stay lazy)
    "chroma_embedded": "mnemoseed.storage.vector.chroma_embedded",
    "pgvector": "mnemoseed.storage.vector.pgvector_store",
    "sqlite_graph": "mnemoseed.storage.graph.sqlite_graph",
    "postgres_graph": "mnemoseed.storage.graph.postgres_graph",
    "sqlite_meta": "mnemoseed.storage.meta.sqlite_meta",
    "postgres_meta": "mnemoseed.storage.meta.postgres_meta",
    "gemma_local": "mnemoseed.storage.embed.gemma_local",
    "openai_compat": "mnemoseed.storage.embed.openai_compat",
}


def _build(table: dict[str, type], kind: str, driver: str, params: dict):
    if driver not in table and driver in _DRIVER_MODULES:
        try:
            importlib.import_module(_DRIVER_MODULES[driver])
        except ImportError as exc:
            raise StorageError(_extra_hint(kind, driver, exc)) from exc
    cls = table.get(driver)
    if cls is None:
        raise StorageError(
            f"unknown {kind} driver: {driver!r} (available: {', '.join(sorted(table)) or 'none'})"
        )
    try:
        return cls(**params)
    except ImportError as exc:
        # driver module imported fine, but its lazy optional dependency did not
        raise StorageError(_extra_hint(kind, driver, exc)) from exc


def _extra_hint(kind: str, driver: str, exc: ImportError) -> str:
    return (
        f"{kind} driver {driver!r} needs an optional dependency: {exc.name}. "
        "Install the matching extra (e.g. mnemoseed[embedded] or mnemoseed[postgres])."
    )


def validate_capabilities(stores: Stores) -> CapabilityReport:
    """Check each REQUIRED_CAPS entry against the wired driver's declared set."""
    by_kind = {
        "vector": stores.vector,
        "graph": stores.graph,
        "meta": stores.meta,
        "embed": stores.embed,
    }
    missing: list[Degradation] = []
    for feature, (kind, cap, behavior) in REQUIRED_CAPS.items():
        if cap not in by_kind[kind].info.capabilities:
            missing.append(
                Degradation(
                    capability=cap,
                    feature=feature,
                    behavior=f"[{kind}:{by_kind[kind].info.name}] {behavior}",
                )
            )
    return CapabilityReport(ok=not missing, missing=missing)


def build_stores(config: Config) -> Stores:
    """Instantiate all four ports from config, then run the capability gate."""
    vec_cfg = config.layer("vector")
    graph_cfg = config.layer("graph")
    meta_cfg = config.layer("meta")
    embed_cfg = config.layer("embed")

    stores = Stores(
        vector=_build(VECTOR_DRIVERS, "vector", vec_cfg.driver, vec_cfg.params),
        graph=_build(GRAPH_DRIVERS, "graph", graph_cfg.driver, graph_cfg.params),
        meta=_build(META_DRIVERS, "meta", meta_cfg.driver, meta_cfg.params),
        embed=_build(EMBED_DRIVERS, "embed", embed_cfg.driver, embed_cfg.params),
    )
    stores.report = validate_capabilities(stores)
    for deg in stores.report.missing:
        logger.warning("capability degradation — %s: %s", deg.feature, deg.behavior)
    return stores
