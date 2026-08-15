"""Daemon smoke test: /health and /capabilities against a Stores stack built
from the registered fake drivers, plus the boot-refusal path.

The real daemon lifespan loads config from disk and builds real drivers, so the
tests inject a prebuilt Stores stack directly (same technique as the previous
skeleton test)."""

from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient

from mnemoseed.config import Config, LayerSpec
from mnemoseed.daemon.app import create_app
from mnemoseed.storage.factory import build_stores
from mnemoseed.storage.ports import (
    Capability,
    CapabilityStartupError,
    DriverInfo,
    Embedder,
    GraphStore,
    MetaStore,
    VectorStore,
)
from mnemoseed.storage.registry import (
    DRIVER_REGISTRIES,
    EMBED_DRIVERS,
    GRAPH_DRIVERS,
    META_DRIVERS,
    VECTOR_DRIVERS,
    register,
)

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


@pytest.fixture(autouse=True)
def _clear_registries():
    for registry in DRIVER_REGISTRIES.values():
        registry.clear()
    yield
    for registry in DRIVER_REGISTRIES.values():
        registry.clear()


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


def _client_with(stores, config):
    app = create_app()

    @asynccontextmanager
    async def fake_lifespan(application):
        application.state.config = config
        application.state.stores = stores
        yield

    app.router.lifespan_context = fake_lifespan
    return TestClient(app)


def test_health_and_capabilities_ok():
    _register_all()
    stores = build_stores(_full_config())

    with _client_with(stores, _full_config()) as client:
        health = client.get("/health").json()
        assert health["status"] == "ok"
        assert health["drivers"] == {
            "vector": "fake_vector",
            "graph": "fake_graph",
            "meta": "fake_meta",
            "embed": "fake_embed",
        }

        caps = client.get("/capabilities").json()
        assert caps["ok"] is True
        assert caps["degradations"] == []


def test_capabilities_reports_degradations():
    _register_all()

    class NoSparseEmbed(FakeEmbed):
        info = DriverInfo(
            name="fake_embed_nosparse",
            capabilities=FULL_EMBED - {Capability.EMBED_SPARSE_OUTPUT},
        )

    register(EMBED_DRIVERS)(NoSparseEmbed)
    cfg = _full_config()
    cfg.storage["embed"] = LayerSpec(layer="embed", driver="fake_embed_nosparse")
    stores = build_stores(cfg)

    with _client_with(stores, cfg) as client:
        caps = client.get("/capabilities").json()
        assert caps["ok"] is True  # sparse_output is degradable, not hard
        features = {d["feature"] for d in caps["degradations"]}
        assert features == {"hybrid retrieval sparse path"}
        assert caps["drivers"]["embed"]["name"] == "fake_embed_nosparse"


def test_hard_missing_refuses_boot():
    _register_all()

    class NoTransactionMeta(MetaStore):
        info = DriverInfo(
            name="fake_meta_notx",
            capabilities=frozenset({Capability.META_CONCURRENT_READERS}),
        )

        def capabilities(self):
            return self.info.capabilities

    register(META_DRIVERS)(NoTransactionMeta)
    cfg = _full_config()
    cfg.storage["meta"] = LayerSpec(layer="meta", driver="fake_meta_notx")

    with pytest.raises(CapabilityStartupError, match="meta.transaction"):
        build_stores(cfg)
