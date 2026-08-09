"""Daemon serving funnel (M1 item A): the full strip -> score -> pool ->
stamp/write chain bound to the daemon's resolved VectorStore + Embedder.

/ingest stays submit-only (O(1)); the buffered turns drain when the session
settles, because no scheduled drain exists yet — /session/end is the v1 drain
trigger. Every assertion runs through the HTTP surface against a real embedded
boot whose ``embed`` layer is the deterministic synthetic driver, so nothing
touches the network or a model download.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mnemoseed.capture.pool import PoolEventKind
from mnemoseed.daemon.app import create_app
from mnemoseed.dream import DreamState, DreamTrigger
from mnemoseed.schema.stamp import CognitiveTier
from mnemoseed.storage.drivers import lancedb_embedded, sqlite_graph, sqlite_meta
from mnemoseed.storage.drivers.synthetic_embedder import SyntheticEmbedder
from mnemoseed.storage.ports import ChunkFilter, Page, TurnRange
from mnemoseed.storage.registry import (
    EMBED_DRIVERS,
    GRAPH_DRIVERS,
    META_DRIVERS,
    VECTOR_DRIVERS,
    register,
)

_SESSION = "sess-serve-1"
_PROFILE = "prof-main"


@pytest.fixture(autouse=True)
def _ensure_real_drivers():
    """test_daemon clears the shared registries; re-register the real drivers."""
    for registry, cls in (
        (VECTOR_DRIVERS, lancedb_embedded.LanceDbEmbeddedStore),
        (GRAPH_DRIVERS, sqlite_graph.SqliteGraphDriver),
        (META_DRIVERS, sqlite_meta.SqliteMetaDriver),
        (EMBED_DRIVERS, SyntheticEmbedder),
    ):
        if not registry.contains(cls.info.name):
            register(registry)(cls)
    yield


def _serving_config_toml(tmp_path: Path) -> Path:
    # as_posix(): Windows backslashes are invalid escapes in TOML strings
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        'preset = "embedded"\n'
        f'[storage.vector]\nuri = "{(tmp_path / "chunks.lance").as_posix()}"\ndimensions = 64\n'
        f'[storage.graph]\npath = "{(tmp_path / "cortex.db").as_posix()}"\n'
        f'[storage.meta]\npath = "{(tmp_path / "meta.db").as_posix()}"\n'
        f'[storage.embed]\ndriver = "synthetic"\ndimension = 64\n',
        encoding="utf-8",
    )
    return cfg


def _user(
    text: str = "我决定以后都用 pnpm 管理依赖",
    ts: float = 1.0,
    importance_hint: float | None = None,
) -> dict:
    payload = {
        "host": "claude_code",
        "event": "user_prompt",
        "session_id": _SESSION,
        "profile_id": _PROFILE,
        "ts": ts,
        "content": {"text": text},
    }
    if importance_hint is not None:
        payload["importance_hint"] = importance_hint
    return payload


def _client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    monkeypatch.setattr("mnemoseed.config.CONFIG_PATH", _serving_config_toml(tmp_path))
    # keep the snapshot journal out of the user's ~/.mnemoseed: boot recovery
    # scans CONFIG_DIR/"dreams", so point it at the throwaway home.
    monkeypatch.setattr("mnemoseed.dream.snapshot.CONFIG_DIR", tmp_path)
    return TestClient(create_app())


def _writes(client: TestClient) -> int:
    stores = client.app.state.stores
    return stores.vector.list_chunks(ChunkFilter(profile_id=_PROFILE), Page(limit=10)).total


def test_serving_pipeline_writes_on_session_end_not_on_ingest(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        # a second user prompt closes the first turn on the /ingest path; both
        # stay buffered because submit is O(1) and drain is off the HTTP path
        assert client.post("/ingest", json=_user(text="我决定以后都用 pnpm", ts=1.0)).status_code == 202
        assert client.post("/ingest", json=_user(text="我决定改用 vite 打包", ts=2.0)).status_code == 202
        assert _writes(client) == 0  # nothing written by any /ingest round trip

        response = client.post("/session/end", json={"session_id": _SESSION, "profile_id": _PROFILE})
        assert response.status_code == 200
        assert _writes(client) == 2  # the drain on settlement writes both turns

        chunks = client.app.state.stores.vector.list_chunks(
            ChunkFilter(profile_id=_PROFILE), Page(limit=10)
        ).items
    by_turn = {chunk.turn_start: chunk for chunk in chunks}
    assert set(by_turn) == {0, 1}
    # WriteContext defaults: host from the turn, TIER_1 cognitive tier, no
    # agent_label until an anima exists, and the turn window on the stamp.
    for chunk in by_turn.values():
        assert chunk.cues.host == "claude_code"
        assert chunk.cognitive_tier is CognitiveTier.TIER_1
        assert chunk.persona_id is None
    assert by_turn[0].turn_start == by_turn[0].turn_end == 0
    assert by_turn[1].turn_start == by_turn[1].turn_end == 1


def test_serving_pipeline_single_turn_context(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        assert client.post("/ingest", json=_user()).status_code == 202
        assert _writes(client) == 0
        assert (
            client.post("/session/end", json={"session_id": _SESSION, "profile_id": _PROFILE}).status_code
            == 200
        )
        page = client.app.state.stores.vector.list_chunks(ChunkFilter(profile_id=_PROFILE), Page(limit=10))

        assert page.total == 1
        chunk = page.items[0]
        assert "user:" in chunk.text
        assert chunk.profile_id == _PROFILE
        assert chunk.cues.host == "claude_code"
        assert chunk.cognitive_tier is CognitiveTier.TIER_1
        assert chunk.persona_id is None
        assert chunk.turn_start == 0
        assert chunk.turn_end == 0


def test_serving_boot_restores_persisted_score_pool(tmp_path, monkeypatch) -> None:
    """Per-profile pool balances survive a daemon restart: the ScorePool is
    seeded at boot from the meta store, not from the (lost) in-process ledgers."""
    seed = sqlite_meta.SqliteMetaDriver(path=tmp_path / "meta.db")
    seed.pool_credit(_PROFILE, 13.0, TurnRange(start=2, end=6))
    asyncio.run(seed.close())

    with _client(tmp_path, monkeypatch) as client:
        pool = client.app.state.capture.pool
        assert pool.balances() == {_PROFILE: pytest.approx(13.0)}
        # restore is conservative: no turns pooled, no ledger counters bumped
        ledger = pool.stats(_PROFILE)
        assert ledger is not None
        assert ledger.turns_pooled == 0
        assert ledger.points_added == 0.0


_DURABLE_TEXTS = (
    "我决定以后都用 pnpm 管理依赖来构建前端项目",
    "我以后统一用 vite 来做前端打包方案",
    "我打算把日志系统迁移到时序数据库存储",
    "我认为代码 review 必须关注注释和可读性",
    "我坚持每次提交前都跑一遍完整的测试",
)


def test_serving_boot_wires_dream_trigger_as_manual_sink(tmp_path, monkeypatch) -> None:
    """The ScorePool sinks dream events into the trigger; the default embedded
    config keeps the FR-2.8 manual-first flag (auto_trigger = false), so a
    fired event is recorded as a pending manual run rather than driving.

    Driven through HTTP (the sqlite connection is bound to the app loop
    thread): five full-score durable turns reach the 50.0 hard cap and fire an
    overflow event, which the pool ignores idleness for.
    """
    with _client(tmp_path, monkeypatch) as client:
        trigger = client.app.state.dream
        assert isinstance(trigger, DreamTrigger)
        assert trigger.status(_PROFILE).state is DreamState.IDLE
        for index, text in enumerate(_DURABLE_TEXTS):
            resp = client.post("/ingest", json=_user(text=text, ts=float(index + 1), importance_hint=1.0))
            assert resp.status_code == 202
        resp = client.post("/session/end", json={"session_id": _SESSION, "profile_id": _PROFILE})
        assert resp.status_code == 200
        status = trigger.status(_PROFILE)
        assert status.last_event is not None
        assert status.last_event.kind is PoolEventKind.FORCED_CONSOLIDATION
        # manual-first: the overflow is held as a pending manual run, nothing drives
        assert status.pending_manual == 1
        assert status.state is DreamState.IDLE


def _auto_trigger_config_toml(tmp_path: Path) -> Path:
    cfg = _serving_config_toml(tmp_path)
    text = cfg.read_text(encoding="utf-8") + "[dream]\nauto_trigger = true\n"
    cfg.write_text(text, encoding="utf-8")
    return cfg


def test_serving_boot_honours_dream_auto_trigger_config(tmp_path, monkeypatch) -> None:
    """With [dream] auto_trigger = true, a fired pool event drives the trigger
    into DREAMING through the real snapshot seam, which completes synchronously
    (capture + persist + dream_runs registration) on the request path."""
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    monkeypatch.setattr("mnemoseed.config.CONFIG_PATH", _auto_trigger_config_toml(tmp_path))
    monkeypatch.setattr("mnemoseed.dream.snapshot.CONFIG_DIR", tmp_path)
    with TestClient(create_app()) as client:
        trigger = client.app.state.dream
        assert trigger.status(_PROFILE).state is DreamState.IDLE
        for index, text in enumerate(_DURABLE_TEXTS):
            resp = client.post("/ingest", json=_user(text=text, ts=float(index + 1), importance_hint=1.0))
            assert resp.status_code == 202
        resp = client.post("/session/end", json={"session_id": _SESSION, "profile_id": _PROFILE})
        assert resp.status_code == 200
        status = trigger.status(_PROFILE)
        assert status.last_event is not None
        assert status.last_event.kind is PoolEventKind.FORCED_CONSOLIDATION
        # auto mode: the overflow fires the live dream; the real snapshotter
        # completed inline, so the trigger is already dreaming
        assert status.state is DreamState.DREAMING
        assert status.current_range is not None
        # a snapshot file was persisted and a dream run registered
        assert len(list((tmp_path / "dreams").glob("*.json"))) == 1


def test_serving_boot_recovery_resumes_preseeded_snapshot(tmp_path, monkeypatch) -> None:
    """NFR-2.3: an interrupted dream (crash after snapshot) reboots into
    DREAMING, ready for reflect, with the snapshot's scope, offline and before
    serving starts."""
    from mnemoseed.dream import FileSnapshotter as RealSnapshotter
    from mnemoseed.schema.stamp import ChunkStamp, CognitiveTier, Cues, Provenance
    from mnemoseed.storage.drivers.lancedb_embedded import LanceDbEmbeddedStore
    from mnemoseed.storage.drivers.synthetic_embedder import SyntheticEmbedder
    from mnemoseed.storage.ports import DreamRunFilter

    # seed a snapshot file exactly where the daemon will scan (tmp_path/"dreams")
    seed_store = LanceDbEmbeddedStore(uri=tmp_path / "seed.lance", dimensions=64)
    seed_embed = SyntheticEmbedder(dimension=64)
    embedding = seed_embed.embed("phased 快照测试")
    seed_store.upsert_chunk(
        ChunkStamp(
            chunk_id="seed-1",
            profile_id=_PROFILE,
            text="phased 快照测试",
            cognitive_tier=CognitiveTier.TIER_1,
            model_id="test-model",
            cues=Cues(entities=["test"]),
            provenance=Provenance(asserted_by="test", session_id=_SESSION, source="manual"),
            turn_start=0,
            turn_end=1,
        ),
        embedding.dense,
    )
    seed_meta = sqlite_meta.SqliteMetaDriver(path=tmp_path / "meta.db")
    seeder = RealSnapshotter(store=seed_store, meta=seed_meta, directory=tmp_path / "dreams")
    result = seeder.request(_PROFILE, TurnRange(0, 1))
    assert result.ok
    asyncio.run(seed_store.close())
    asyncio.run(seed_meta.close())

    monkeypatch.delenv("STORAGE_MODE", raising=False)
    monkeypatch.setattr("mnemoseed.config.CONFIG_PATH", _serving_config_toml(tmp_path))
    monkeypatch.setattr("mnemoseed.dream.snapshot.CONFIG_DIR", tmp_path)
    with TestClient(create_app()) as client:
        trigger = client.app.state.dream
        # recovered snapshot resumed at the reflect boundary: DREAMING on the
        # snapshot's scope, no re-capture, no second registration
        assert trigger.status(_PROFILE).state is DreamState.DREAMING
        assert trigger.status(_PROFILE).current_range == TurnRange(0, 1)
        assert len(list((tmp_path / "dreams").glob("*.json"))) == 1
    # the seed's dream_runs row was registered exactly once (recovery never
    # re-registers); read back through a fresh connection after the app closes
    read_meta = sqlite_meta.SqliteMetaDriver(path=tmp_path / "meta.db")
    assert read_meta.list_dream_runs(DreamRunFilter(), Page(limit=10)).total == 1
    asyncio.run(read_meta.close())


def test_serving_boot_survives_corrupt_snapshot_file(tmp_path, monkeypatch) -> None:
    """A malformed snapshot file in the journal must not crash daemon boot:
    boot recovery skips it and serving continues (design/02 section 7)."""
    dreams = tmp_path / "dreams"
    dreams.mkdir()
    bad_type = (
        '{"snapshot_id": "bad", "profile_id": "p",'
        ' "turn_range": {"start": 0, "end": 2}, "chunks": [],'
        ' "created_at": null, "phases": []}'
    )
    (dreams / "bad-type.json").write_text(bad_type, encoding="utf-8")
    (dreams / "broken-syntax.json").write_text('{"snapshot_id": ', encoding="utf-8")
    with _client(tmp_path, monkeypatch) as client:
        assert client.app.state.dream.status(_PROFILE).state is DreamState.IDLE
        assert client.post("/ingest", json=_user(ts=1.0)).status_code == 202


def test_serving_boot_recovers_reflect_done_snapshot_at_merge(tmp_path, monkeypatch) -> None:
    """A snapshot that already ran reflect resumes at the merge boundary: the
    daemon boots the profile into MERGING (never DREAMING, so reflect never
    re-runs), the merge completion fires the safe-clear exactly once with the
    snapshot's scope, and the finished dream is never re-recovered on later
    boots (the journal terminates, no infinite re-recovery)."""
    from mnemoseed.dream import FileSnapshotter as RealSnapshotter
    from mnemoseed.dream import SnapshotPhase, write_snapshot_file
    from mnemoseed.schema.stamp import ChunkStamp, CognitiveTier, Cues, Provenance
    from mnemoseed.storage.drivers.lancedb_embedded import LanceDbEmbeddedStore
    from mnemoseed.storage.drivers.synthetic_embedder import SyntheticEmbedder

    # seed into the exact lance uri the app config reserves, so the daemon's
    # purge seam can be observed to have cleared the snapshot's scope
    seed_store = LanceDbEmbeddedStore(uri=tmp_path / "chunks.lance", dimensions=64)
    seed_embed = SyntheticEmbedder(dimension=64)
    embedding = seed_embed.embed("已经反映过的测试")
    seed_store.upsert_chunk(
        ChunkStamp(
            chunk_id="seed-1",
            profile_id=_PROFILE,
            text="已经反映过的测试",
            cognitive_tier=CognitiveTier.TIER_1,
            model_id="test-model",
            cues=Cues(entities=["test"]),
            provenance=Provenance(asserted_by="test", session_id=_SESSION, source="manual"),
            turn_start=0,
            turn_end=1,
        ),
        embedding.dense,
    )
    seed_meta = sqlite_meta.SqliteMetaDriver(path=tmp_path / "meta.db")
    seeder = RealSnapshotter(store=seed_store, meta=seed_meta, directory=tmp_path / "dreams")
    captured = seeder.request(_PROFILE, TurnRange(0, 1)).snapshot
    assert captured is not None
    # T3 wrote back and marked reflect done; the daemon crashed before merge
    write_snapshot_file(tmp_path / "dreams", captured.with_phase(SnapshotPhase.REFLECT_DONE.value))
    asyncio.run(seed_store.close())
    asyncio.run(seed_meta.close())

    monkeypatch.delenv("STORAGE_MODE", raising=False)
    monkeypatch.setattr("mnemoseed.config.CONFIG_PATH", _serving_config_toml(tmp_path))
    monkeypatch.setattr("mnemoseed.dream.snapshot.CONFIG_DIR", tmp_path)
    with TestClient(create_app()) as client:
        trigger = client.app.state.dream
        # resumed at the merge boundary, positioned to receive the merge commit
        assert trigger.status(_PROFILE).state is DreamState.MERGING
        assert trigger.status(_PROFILE).current_range == TurnRange(0, 1)
        # the T4 Merger seam calls this on completion -> safe-clear fires once
        trigger.on_merge_committed(_PROFILE)
        assert trigger.status(_PROFILE).state is DreamState.IDLE
        assert client.app.state.stores.vector.get_chunk("seed-1") is None
    # terminated: a later boot finds the journal merge-complete, nothing recovers
    with TestClient(create_app()) as client:
        assert client.app.state.dream.status(_PROFILE).state is DreamState.IDLE
    assert len(list((tmp_path / "dreams").glob("*.json"))) == 1
