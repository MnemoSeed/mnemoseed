"""The docker preset resolves every layer to a Postgres/openai driver (task 5
FR-8.4), and the appendix C gate surfaces the openai_compatible sparse
degradation as a warn-level finding — the preset boots only with that explicit
yellow flag.
"""

import asyncio

import pytest

from mnemoseed.config import Config, LayerSpec
from mnemoseed.storage.drivers import openai_compatible, pg_graph, pg_meta, pgvector
from mnemoseed.storage.factory import build_stores
from mnemoseed.storage.ports import Capability
from mnemoseed.storage.registry import (
    EMBED_DRIVERS,
    GRAPH_DRIVERS,
    META_DRIVERS,
    VECTOR_DRIVERS,
    register,
)

_DRIVERS = (
    (VECTOR_DRIVERS, pgvector.PgVectorStore),
    (GRAPH_DRIVERS, pg_graph.PgGraphDriver),
    (META_DRIVERS, pg_meta.PgMetaDriver),
    (EMBED_DRIVERS, openai_compatible.OpenAICompatibleEmbedder),
)


@pytest.fixture(autouse=True)
def _ensure_registered():
    for registry, cls in _DRIVERS:
        if not registry.contains(cls.info.name):
            register(registry)(cls)
    yield


def _docker_config() -> Config:
    # embed needs its endpoint params; the pg drivers are stubbed per-test
    return Config(
        preset="docker",
        storage={
            "embed": LayerSpec(
                layer="embed",
                params={
                    "base_url": "http://127.0.0.1:9999/v1",
                    "api_key": "test-key",
                    "model": "text-embedding-test",
                },
            ),
        },
    )


def test_docker_preset_resolves_to_pg_drivers():
    config = Config(preset="docker")
    assert config.layer_instances("vector")["main"].driver == "pgvector"
    assert config.layer_instances("graph")["main"].driver == "pg_graph"
    assert config.layer_instances("meta")["main"].driver == "pg_meta"
    assert config.layer_instances("embed")["main"].driver == "openai_compatible"


def test_docker_preset_builds_and_reports_sparse_degradation(monkeypatch, caplog):
    # the pg drivers need a live server; the gate test stubs their constructors
    for driver_cls in (pgvector.PgVectorStore, pg_graph.PgGraphDriver, pg_meta.PgMetaDriver):
        monkeypatch.setattr(driver_cls, "__init__", lambda self, **kwargs: setattr(self, "_owns_conn", False))

    stores = build_stores(_docker_config())
    assert stores.report.ok is True
    assert stores.report.hard_missing == []
    issues = {i.capability: i for i in stores.report.degradations}
    assert Capability.EMBED_SPARSE_OUTPUT in issues
    assert issues[Capability.EMBED_SPARSE_OUTPUT].driver == "openai_compatible"
    assert Capability.EMBED_BATCH not in issues
    assert stores.vector.info.name == "pgvector"
    assert stores.graph.info.name == "pg_graph"
    assert stores.meta.info.name == "pg_meta"
    assert stores.embed.info.name == "openai_compatible"

    # the degradation is a logged warning naming the driver and the gap
    messages = [r.message for r in caplog.records]
    assert any("capability degradation" in m for m in messages)
    assert any("openai_compatible" in m and "sparse" in m for m in messages)

    asyncio.run(stores.close())
