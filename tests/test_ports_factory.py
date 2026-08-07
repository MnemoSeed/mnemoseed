"""Capability gate tests: degradation must be explicit, never silent."""

from mnemoseed.config import Config, StorageConfig
from mnemoseed.storage.factory import build_stores, validate_capabilities
from mnemoseed.storage.ports import Capability


def test_embedded_gate_degrades_dream_snapshot(tmp_path, monkeypatch):
    """Embedded preset: sqlite graph has no SNAPSHOT -> one explicit degradation,
    while unknown capability regressions stay visible."""
    monkeypatch.setenv("MNEMOSEED_HOME", str(tmp_path))
    from mnemoseed import config as config_mod

    monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path)

    from mnemoseed.storage.graph.sqlite_graph import SqliteGraph
    from mnemoseed.storage.meta.sqlite_meta import SqliteMeta
    from mnemoseed.storage.ports import DriverInfo, Embedder

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

    from mnemoseed.storage.factory import Stores

    stores = Stores(
        vector=FakeVector(),
        graph=SqliteGraph(path=str(tmp_path / "g.db")),
        meta=SqliteMeta(path=str(tmp_path / "m.db")),
        embed=FakeEmbed(),
    )
    report = validate_capabilities(stores)
    assert not report.ok
    features = {d.feature for d in report.missing}
    assert features == {"dream_snapshot"}  # only this one degrades in embedded


def test_build_stores_unknown_driver_errors(tmp_path, monkeypatch):
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    cfg = Config(preset="embedded", storage={"vector": StorageConfig(driver="nope")})
    import pytest

    from mnemoseed.storage.ports import StorageError

    with pytest.raises(StorageError, match="unknown vector driver"):
        build_stores(cfg)
