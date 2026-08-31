"""Decay engine daemon wiring: boot starts the sweep task only when enabled,
and shutdown cancels it cleanly (resumable cursor keeps the next boot honest).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mnemoseed.daemon.app import create_app
from mnemoseed.decay import DecaySweeper
from mnemoseed.storage.drivers import lancedb_embedded, sqlite_graph, sqlite_meta
from mnemoseed.storage.drivers.synthetic_embedder import SyntheticEmbedder
from mnemoseed.storage.registry import (
    EMBED_DRIVERS,
    GRAPH_DRIVERS,
    META_DRIVERS,
    VECTOR_DRIVERS,
    register,
)


@pytest.fixture(autouse=True)
def _ensure_real_drivers() -> None:
    """test_daemon clears the shared registries; re-register the real drivers."""
    for registry, cls in (
        (VECTOR_DRIVERS, lancedb_embedded.LanceDbEmbeddedStore),
        (GRAPH_DRIVERS, sqlite_graph.SqliteGraphDriver),
        (META_DRIVERS, sqlite_meta.SqliteMetaDriver),
        (EMBED_DRIVERS, SyntheticEmbedder),
    ):
        if not registry.contains(cls.info.name):
            register(registry)(cls)


def _daemon_config_toml(tmp_path: Path, *, decay_enabled: bool = True) -> Path:
    cfg = tmp_path / "config.toml"
    body = (
        'preset = "embedded"\n'
        f'[storage.vector]\nuri = "{(tmp_path / "chunks.lance").as_posix()}"\ndimensions = 64\n'
        f'[storage.graph]\npath = "{(tmp_path / "cortex.db").as_posix()}"\n'
        f'[storage.meta]\npath = "{(tmp_path / "meta.db").as_posix()}"\n'
        f'[storage.embed]\ndriver = "synthetic"\ndimension = 64\n'
        # test-only: the deep_reflection role runs the deterministic offline
        # StubLLM driver so the full dream chain stays network-free
        '[dream.llm.deep_reflection]\ndriver = "stub"\nmodel = "stub"\n'
    )
    if not decay_enabled:
        body += "[decay]\nenabled = false\n"
    cfg.write_text(body, encoding="utf-8")
    return cfg


def _shim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, decay_enabled: bool = True) -> None:
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    monkeypatch.setattr(
        "mnemoseed.config.CONFIG_PATH", _daemon_config_toml(tmp_path, decay_enabled=decay_enabled)
    )
    monkeypatch.setattr("mnemoseed.dream.snapshot.CONFIG_DIR", tmp_path)


def test_daemon_boot_with_sweep_disabled(tmp_path, monkeypatch) -> None:
    """decay.enabled=false at boot: the daemon still owns the sweep task (it
    no-ops each tick) so a configwrite enable goes live WITHOUT a restart."""
    _shim(tmp_path, monkeypatch, decay_enabled=False)
    with TestClient(create_app()) as client:
        assert client.app.state.health.gate_ok is True
        sweeper = client.app.state.decay
        assert isinstance(sweeper, DecaySweeper)
        assert sweeper.enabled is False
        assert sweeper.run_once() == []
        task = client.app.state.decay_task
        assert task is not None  # task exists, gated by the live flag each tick
        # enable flips live through configwrite — no restart needed
        client.app.state.configwrite.set("decay.enabled", True, actor="test")
        assert sweeper.enabled is True
        assert client.app.state.decay.enabled is True
    assert task.cancelled() is True  # teardown still cancels cleanly


def test_daemon_boot_with_sweep_enabled_starts_and_stops_task(tmp_path, monkeypatch) -> None:
    """Default config (decay.enabled=true): the daemon owns a live DecaySweeper
    and a background sweep task that shutdown cancels cleanly."""
    _shim(tmp_path, monkeypatch)
    with TestClient(create_app()) as client:
        sweeper = client.app.state.decay
        assert isinstance(sweeper, DecaySweeper)
        assert sweeper.enabled is True
        task = client.app.state.decay_task
        assert task is not None
        assert not task.done()
        assert client.app.state.health.gate_ok is True
    assert task.cancelled() is True  # teardown cancelled the loop cleanly
