"""Contract-suite fixtures.

Two jobs: re-register the real drivers (test_registry, which shares the module
registries, clears them at teardown — the import-time @register fires only
once), and provide the parametrized `stack` fixture that runs every contract
test against embedded and, when a live Postgres is available, pg.
"""

from __future__ import annotations

import asyncio

import pytest
from _support import PG_DSN, ContractStack, build_embedded, build_pg

from mnemoseed.storage.drivers.lancedb_embedded import LanceDbEmbeddedStore
from mnemoseed.storage.drivers.openai_compatible import OpenAICompatibleEmbedder
from mnemoseed.storage.drivers.pg_graph import PgGraphDriver
from mnemoseed.storage.drivers.pg_meta import PgMetaDriver
from mnemoseed.storage.drivers.pgvector import PgVectorStore
from mnemoseed.storage.drivers.sqlite_graph import SqliteGraphDriver
from mnemoseed.storage.drivers.sqlite_meta import SqliteMetaDriver
from mnemoseed.storage.drivers.synthetic_embedder import SyntheticEmbedder
from mnemoseed.storage.registry import (
    EMBED_DRIVERS,
    GRAPH_DRIVERS,
    META_DRIVERS,
    VECTOR_DRIVERS,
    register,
)

_REAL_DRIVERS: tuple[tuple[object, type], ...] = (
    (VECTOR_DRIVERS, LanceDbEmbeddedStore),
    (VECTOR_DRIVERS, PgVectorStore),
    (GRAPH_DRIVERS, SqliteGraphDriver),
    (GRAPH_DRIVERS, PgGraphDriver),
    (META_DRIVERS, SqliteMetaDriver),
    (META_DRIVERS, PgMetaDriver),
    (EMBED_DRIVERS, SyntheticEmbedder),
    (EMBED_DRIVERS, OpenAICompatibleEmbedder),
)


@pytest.fixture(autouse=True)
def _ensure_real_drivers_registered() -> None:
    """Re-register any driver test_registry cleared from the shared registries."""
    for registry, cls in _REAL_DRIVERS:
        if not registry.contains(cls.info.name):
            register(registry)(cls)
    yield


@pytest.fixture(params=["embedded", "pg"])
def stack(request: pytest.FixtureRequest, tmp_path) -> ContractStack:
    """One contract stack per backend; the pg arm skips cleanly offline."""
    if request.param == "pg":
        if not PG_DSN:
            pytest.skip("MNEMOSEED_TEST_PG_DSN not set (pg contract arm skips offline / in CI)")
        built = build_pg(PG_DSN)
    else:
        built = build_embedded(tmp_path)
    yield built
    asyncio.run(built.close())
