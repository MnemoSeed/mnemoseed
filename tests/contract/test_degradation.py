"""AC-4 degradation demos: every missing-capability path is explicit, never silent.

Three subset combinations from appendix C:
(a) openai_compatible (no embed.sparse_output) -> dense-only retrieval + a
    startup degradation warning;
(b) a vector driver without vector.snapshot -> startup degradation warning and
    snapshot_read degrades to a turn-range logical read, warning again;
(c) a meta driver without meta.transaction -> HARD gate, startup refusal.

The wrapper drivers below are what the gate actually sees; the real backends
underneath are the embedded drivers, so the tests stay offline.
"""

from __future__ import annotations

import logging

import httpx
import pytest
from _support import PROFILE, make_stamp, run

from mnemoseed.config import Config, LayerSpec
from mnemoseed.storage.drivers.lancedb_embedded import LanceDbEmbeddedStore
from mnemoseed.storage.drivers.openai_compatible import OpenAICompatibleEmbedder
from mnemoseed.storage.drivers.sqlite_meta import SqliteMetaDriver
from mnemoseed.storage.factory import CapabilityStartupError, build_stores
from mnemoseed.storage.ports import Capability, ChunkFilter, DriverInfo, Page
from mnemoseed.storage.registry import META_DRIVERS, VECTOR_DRIVERS, register

_DIM = 64


def _dense_handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
    """An OpenAI /embeddings reply I can conjure without any network."""
    vector = [0.0625, 0.0] + [0.0] * (_DIM - 2)
    return httpx.Response(200, json={"data": [{"index": 0, "embedding": vector}]})


def _mock_openai_embedder() -> OpenAICompatibleEmbedder:
    embedder = OpenAICompatibleEmbedder(base_url="http://embed.invalid", api_key="test", model="mock")
    embedder._client = httpx.Client(
        base_url="http://embed.invalid", transport=httpx.MockTransport(_dense_handler), timeout=5.0
    )
    return embedder


# ---------------------------------------------------------- degrade wrapper (a)


def test_embed_dense_only_loudly_degrades_to_dense_retrieval(caplog, tmp_path) -> None:
    """openai_compatible: no sparse leg; gate warns and retrieval stays dense."""
    config = Config(
        preset="custom",
        storage={
            "vector": LayerSpec(
                "vector",
                driver="lancedb_embedded",
                params={"uri": str(tmp_path / "chunks.lance"), "dimensions": _DIM},
            ),
            "graph": LayerSpec("graph", driver="sqlite_graph", params={"path": str(tmp_path / "graph.db")}),
            "meta": LayerSpec("meta", driver="sqlite_meta", params={"path": str(tmp_path / "meta.db")}),
            "embed": LayerSpec(
                "embed",
                driver="openai_compatible",
                params={"base_url": "http://embed.invalid", "api_key": "test", "model": "mock"},
            ),
        },
    )
    with caplog.at_level(logging.WARNING):
        stores = build_stores(config)
    assert any(
        "capability degradation" in r.message and "embed.sparse_output" in r.message for r in caplog.records
    ), "startup must log the sparse-output degradation"

    embedder = stores.embed
    embedder._client = httpx.Client(
        base_url="http://embed.invalid", transport=httpx.MockTransport(_dense_handler), timeout=5.0
    )
    out = embedder.embed("alpha beta gamma")
    assert out.sparse is None, "openai_compatible never produces a sparse leg"
    assert len(out.dense) == _DIM

    # dense-only retrieval is the degraded-but-working path
    stores.vector.upsert_chunk(make_stamp("d1", "alpha beta gamma"), out.dense, None)
    hits = stores.vector.search(out.dense, None, ChunkFilter(profile_id=PROFILE), top_k=5)
    assert {hit.chunk.chunk_id for hit in hits} == {"d1"}
    assert hits[0].chunk.text == "alpha beta gamma"
    run(stores.close())


# ---------------------------------------------------------- degrade wrapper (b)


class NoSnapshotVector:
    """A vector driver that silently dropped vector.snapshot from its caps."""

    info = DriverInfo(
        name="no_snapshot_vector",
        capabilities=frozenset({Capability.VECTOR_HYBRID_SEARCH, Capability.VECTOR_METADATA_FILTER}),
        description="contract wrapper: lancedb without the snapshot capability",
    )

    def __init__(self, **kwargs) -> None:
        self._inner = LanceDbEmbeddedStore(**kwargs)

    def capabilities(self) -> frozenset[Capability]:
        return self.info.capabilities

    async def close(self) -> None:
        await self._inner.close()

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def snapshot_read(self, filter: ChunkFilter):
        logging.getLogger("mnemoseed.degradation.no_snapshot").warning(
            "capability degradation - vector.main driver %r lacks vector.snapshot "
            "(dream-engine snapshot isolation): snapshot degrades to turn-range "
            "logical isolation",
            self.info.name,
        )
        return self._inner.list_chunks(filter, Page(limit=1 << 20)).items


def test_vector_snapshot_degrade_to_logical_read_at_startup_and_use(caplog, tmp_path) -> None:
    """Dropping vector.snapshot is a DEGRADE: warned at boot, snapshot becomes
    a logical read and still warns when invoked."""
    if not VECTOR_DRIVERS.contains(NoSnapshotVector.info.name):
        register(VECTOR_DRIVERS)(NoSnapshotVector)
    config = Config(
        preset="custom",
        storage={
            "vector": LayerSpec(
                "vector",
                driver="no_snapshot_vector",
                params={"uri": str(tmp_path / "chunks.lance"), "dimensions": _DIM},
            ),
            "graph": LayerSpec("graph", driver="sqlite_graph", params={"path": str(tmp_path / "graph.db")}),
            "meta": LayerSpec("meta", driver="sqlite_meta", params={"path": str(tmp_path / "meta.db")}),
            "embed": LayerSpec("embed", driver="synthetic", params={"dimension": _DIM}),
        },
    )
    with caplog.at_level(logging.WARNING):
        stores = build_stores(config)  # startup passes (degrade, not hard)
    assert any(
        "capability degradation" in r.message and "vector.snapshot" in r.message for r in caplog.records
    ), "startup must log the snapshot degradation"

    stores.vector.upsert_chunk(
        make_stamp("s1", "snap one"),
        stores.embed.embed("snap one").dense,
        stores.embed.embed("snap one").sparse,
    )
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        snapshot = stores.vector.snapshot_read(ChunkFilter(profile_id=PROFILE))
    assert {chunk.chunk_id for chunk in snapshot} == {"s1"}
    assert any(
        "capability degradation" in r.message and "logical isolation" in r.message for r in caplog.records
    ), "the degraded snapshot path itself must warn (no silent fallback)"
    run(stores.close())


# ---------------------------------------------------------- hard gate wrapper (c)


class NoTransactionMeta:
    """A meta driver missing the hard meta.transaction capability."""

    info = DriverInfo(
        name="no_transaction_meta",
        capabilities=frozenset({Capability.META_CONCURRENT_READERS}),
        description="contract wrapper: sqlite_meta without the transaction capability",
    )

    def __init__(self, **kwargs) -> None:
        self._inner = SqliteMetaDriver(**kwargs)

    def capabilities(self) -> frozenset[Capability]:
        return self.info.capabilities

    async def close(self) -> None:
        await self._inner.close()

    def __getattr__(self, name):
        return getattr(self._inner, name)


def test_meta_transaction_missing_refuses_startup(caplog, tmp_path) -> None:
    """Dropping meta.transaction is HARD: build_stores raises, nothing boots."""
    if not META_DRIVERS.contains(NoTransactionMeta.info.name):
        register(META_DRIVERS)(NoTransactionMeta)
    config = Config(
        preset="custom",
        storage={
            "vector": LayerSpec(
                "vector",
                driver="lancedb_embedded",
                params={"uri": str(tmp_path / "chunks.lance"), "dimensions": _DIM},
            ),
            "graph": LayerSpec("graph", driver="sqlite_graph", params={"path": str(tmp_path / "graph.db")}),
            "meta": LayerSpec(
                "meta", driver="no_transaction_meta", params={"path": str(tmp_path / "meta.db")}
            ),
            "embed": LayerSpec("embed", driver="synthetic", params={"dimension": _DIM}),
        },
    )
    with caplog.at_level(logging.WARNING):
        with pytest.raises(CapabilityStartupError, match="meta.transaction"):
            build_stores(config)
    assert not any("capability degradation" in r.message for r in caplog.records), (
        "a HARD miss must not be logged as a degradation; refusing is the only path"
    )
