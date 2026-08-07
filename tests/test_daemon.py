"""Daemon boot smoke test: health + capabilities endpoints with a fully
local stack (sqlite drivers + in-memory fakes for vector/embed)."""

from fastapi.testclient import TestClient

from mnemoseed.daemon.app import create_app
from mnemoseed.storage.factory import Stores
from mnemoseed.storage.ports import Capability, DriverInfo, Embedder


class FakeVector:
    info = DriverInfo(
        name="fake_vector",
        capabilities=frozenset(
            {Capability.METADATA_FILTER, Capability.TIME_RANGE_FILTER, Capability.PERSIST}
        ),
    )

    async def close(self):
        pass


class FakeEmbed(Embedder):
    info = DriverInfo(name="fake_embed", capabilities=frozenset({Capability.LOCAL_OFFLINE}))
    dimension = 8

    async def embed(self, texts):
        return [[0.0] * 8 for _ in texts]


def test_health_and_capabilities(tmp_path, monkeypatch):
    from mnemoseed.config import Config
    from mnemoseed.storage.factory import validate_capabilities
    from mnemoseed.storage.graph.sqlite_graph import SqliteGraph
    from mnemoseed.storage.meta.sqlite_meta import SqliteMeta

    stores = Stores(
        vector=FakeVector(),
        graph=SqliteGraph(path=str(tmp_path / "g.db")),
        meta=SqliteMeta(path=str(tmp_path / "m.db")),
        embed=FakeEmbed(),
    )
    stores.report = validate_capabilities(stores)

    app = create_app()

    # bypass lifespan storage build; inject the test stack directly
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def fake_lifespan(app):
        app.state.config = Config()
        app.state.stores = stores
        yield

    app.router.lifespan_context = fake_lifespan

    with TestClient(app) as client:
        health = client.get("/health").json()
        assert health["status"] == "ok"
        assert health["drivers"]["graph"] == "sqlite_graph"

        caps = client.get("/capabilities").json()
        assert caps["ok"] is False  # sqlite graph lacks SNAPSHOT
        features = {d["feature"] for d in caps["degradations"]}
        assert features == {"dream_snapshot"}
