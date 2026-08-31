"""/healthz behaviour (prd-08 FR-8.8 + NFR-8.1, AC-2):

- responds well under 100ms on the hot path;
- reports the store assembly status (per-layer drivers), the meta schema
  version (proof the migrations ran), and the appendix C capability findings.

Two routes to a response are exercised: a real embedded boot through the app
lifespan (integration shaped) and a fake-driver stack that injects a degraded
capability report (unit shaped, same technique as test_daemon.py).
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mnemoseed.config import Config, LayerSpec
from mnemoseed.daemon.app import (
    HealthSnapshot,
    _issue_payload,
    _migrations_payload,
    _stores_payload,
    create_app,
)
from mnemoseed.storage.drivers import bge_m3_onnx, lancedb_embedded, sqlite_graph, sqlite_meta
from mnemoseed.storage.factory import build_stores
from mnemoseed.storage.ports import (
    Capability,
    DriverInfo,
    Embedder,
    GraphStore,
    MetaStore,
    VectorStore,
)
from mnemoseed.storage.registry import (
    EMBED_DRIVERS,
    GRAPH_DRIVERS,
    META_DRIVERS,
    VECTOR_DRIVERS,
    register,
)

_REAL_DRIVERS = (
    (VECTOR_DRIVERS, lancedb_embedded.LanceDbEmbeddedStore),
    (GRAPH_DRIVERS, sqlite_graph.SqliteGraphDriver),
    (META_DRIVERS, sqlite_meta.SqliteMetaDriver),
    (EMBED_DRIVERS, bge_m3_onnx.BgeM3OnnxEmbedder),
)


@pytest.fixture(autouse=True)
def _ensure_real_drivers():
    """Earlier test modules clear the driver registries; re-register the real
    M0 drivers so the real embedded boot below always sees them."""
    for registry, cls in _REAL_DRIVERS:
        if not registry.contains(cls.info.name):
            register(registry)(cls)
    yield


FULL_VECTOR = frozenset(
    {Capability.VECTOR_HYBRID_SEARCH, Capability.VECTOR_METADATA_FILTER, Capability.VECTOR_SNAPSHOT}
)
FULL_GRAPH = frozenset(
    {
        Capability.GRAPH_VERSION_CHAIN,
        Capability.GRAPH_COOCCURRENCE_EDGES,
        Capability.GRAPH_TRAVERSE_2HOP,
        Capability.GRAPH_EDGE_LIST,
    }
)
FULL_META = frozenset({Capability.META_TRANSACTION, Capability.META_CONCURRENT_READERS})
FULL_EMBED = frozenset(
    {Capability.EMBED_LOCAL_INFERENCE, Capability.EMBED_BATCH, Capability.EMBED_SPARSE_OUTPUT}
)


def _embedded_config_toml(tmp_path: Path) -> Path:
    # as_posix(): Windows backslashes are invalid escapes in TOML strings
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        'preset = "embedded"\n'
        f'[storage.vector]\nuri = "{(tmp_path / "chunks.lance").as_posix()}"\ndimensions = 64\n'
        f'[storage.graph]\npath = "{(tmp_path / "cortex.db").as_posix()}"\n'
        f'[storage.meta]\npath = "{(tmp_path / "meta.db").as_posix()}"\n'
        f'[storage.embed]\nmodel_dir = "{(tmp_path / "models").as_posix()}"\n',
        encoding="utf-8",
    )
    return cfg


def test_healthz_via_real_embedded_boot(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    monkeypatch.setattr("mnemoseed.config.CONFIG_PATH", _embedded_config_toml(tmp_path))
    with TestClient(create_app()) as client:
        started = time.perf_counter()
        response = client.get("/healthz")
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        assert response.status_code == 200
        body = response.json()
        assert elapsed_ms < 100.0, f"/healthz took {elapsed_ms:.1f}ms (NFR-8.1 < 100ms)"
        assert body["status"] == "ok"
        assert body["preset"] == "embedded"
        assert body["stores"]["vector"]["main"] == "lancedb_embedded"
        assert body["stores"]["graph"]["main"] == "sqlite_graph"
        assert body["stores"]["meta"]["main"] == "sqlite_meta"
        assert body["stores"]["embed"]["main"] == "bge_m3_onnx"
        # migrations ran at construction: meta schema version is v1 or later
        assert body["migrations"]["main"] >= 1
        assert body["gate"]["ok"] is True
        assert body["gate"]["degradations"] == []
        assert body["gate"]["hard_missing"] == []


def _client_with(stores, config) -> TestClient:
    app = create_app()

    @asynccontextmanager
    async def fake_lifespan(application):
        application.state.config = config
        application.state.stores = stores
        application.state.health = HealthSnapshot(
            started_at=time.perf_counter(),
            preset=config.preset,
            stores=_stores_payload(stores),
            migrations=_migrations_payload(stores),
            gate_ok=stores.report.ok,
            degradations=[_issue_payload(i) for i in stores.report.degradations],
            hard_missing=[_issue_payload(i) for i in stores.report.hard_missing],
        )
        yield

    # fake_lifespan is already wrapped by @asynccontextmanager above; FastAPI
    # does the same wrapping at build time, so assigning it straight in works.
    app.router.lifespan_context = fake_lifespan
    return TestClient(app)


class FakeVector(VectorStore):
    info = DriverInfo(name="fake_vector", capabilities=FULL_VECTOR)

    def capabilities(self):
        return self.info.capabilities


class FakeGraph(GraphStore):
    info = DriverInfo(name="fake_graph", capabilities=FULL_GRAPH)

    def capabilities(self):
        return self.info.capabilities


class FakeMeta(MetaStore):
    info = DriverInfo(name="fake_meta", capabilities=FULL_META)

    def capabilities(self):
        return self.info.capabilities

    def schema_version(self) -> int:
        return 3


class FakeEmbed(Embedder):
    info = DriverInfo(name="fake_embed", capabilities=FULL_EMBED)
    dimension = 8

    def capabilities(self):
        return self.info.capabilities


def _register_all():
    register(VECTOR_DRIVERS)(FakeVector)
    register(GRAPH_DRIVERS)(FakeGraph)
    register(META_DRIVERS)(FakeMeta)
    register(EMBED_DRIVERS)(FakeEmbed)


def _full_config():
    return Config(
        preset="embedded",
        storage={
            "vector": LayerSpec(layer="vector", driver="fake_vector"),
            "graph": LayerSpec(layer="graph", driver="fake_graph"),
            "meta": LayerSpec(layer="meta", driver="fake_meta"),
            "embed": LayerSpec(layer="embed", driver="fake_embed"),
        },
    )


def test_healthz_reports_gate_degradations_but_stays_green() -> None:
    _register_all()

    class NoSparseEmbed(FakeEmbed):
        info = DriverInfo(
            name="fake_embed_nosparse",
            capabilities=FULL_EMBED - {Capability.EMBED_SPARSE_OUTPUT},
        )

    register(EMBED_DRIVERS)(NoSparseEmbed)
    cfg = _full_config()
    cfg.storage["embed"] = LayerSpec(layer="embed", driver="fake_embed_nosparse")
    stores = build_stores(cfg)  # sparse_output is degradable, so this boots

    with _client_with(stores, cfg) as client:
        body = client.get("/healthz").json()
        assert body["status"] == "ok"
        assert body["gate"]["ok"] is True
        features = {d["feature"] for d in body["gate"]["degradations"]}
        assert features == {"hybrid retrieval sparse path"}
        assert body["gate"]["degradations"][0]["severity"] == "degrade"
        assert body["gate"]["hard_missing"] == []
        assert body["stores"]["embed"]["main"] == "fake_embed_nosparse"
