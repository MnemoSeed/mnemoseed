"""PRD-06 T3 daemon endpoint additions for the Claude Code plugin:

- ``POST /flush`` (design/06 section 4: PreCompact rescue): closes the open
  turn without settling the session, then drains the capture funnel, so a
  mid-session context compaction does not lose the in-flight turn. The session
  remains ingestable and the later ``/session/end`` still settles it.
- ``POST /memory/dream_once`` / ``POST /memory/dream_status`` (FR-2.8 manual
  ``/dream`` command): run exactly one manual dream cycle and read the trigger
  status through the HTTP surface the plugin's commands talk to.

Every assertion runs against a real embedded boot whose ``embed`` layer is the
deterministic synthetic driver (same preset as test_memory_endpoints).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from _identity_helpers import attach_token
from fastapi.testclient import TestClient

from mnemoseed.daemon.app import create_app
from mnemoseed.dream import DreamState
from mnemoseed.storage.drivers import lancedb_embedded, sqlite_graph, sqlite_meta
from mnemoseed.storage.drivers.synthetic_embedder import SyntheticEmbedder
from mnemoseed.storage.ports import ChunkFilter, NodeFilter, Page
from mnemoseed.storage.registry import (
    EMBED_DRIVERS,
    GRAPH_DRIVERS,
    META_DRIVERS,
    VECTOR_DRIVERS,
    register,
)

_SESSION = "sess-plugin-routes"
_PROFILE = "prof-plugin"


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


def _config_toml(tmp_path: Path) -> Path:
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        'preset = "embedded"\n'
        f'[storage.vector]\nuri = "{(tmp_path / "chunks.lance").as_posix()}"\ndimensions = 64\n'
        f'[storage.graph]\npath = "{(tmp_path / "cortex.db").as_posix()}"\n'
        f'[storage.meta]\npath = "{(tmp_path / "meta.db").as_posix()}"\n'
        f'[storage.embed]\ndriver = "synthetic"\ndimension = 64\n'
        # test-only: the deep_reflection role runs the deterministic offline
        # StubLLM driver so the manual dream chain stays network-free (issue #4)
        '[dream.llm.deep_reflection]\ndriver = "stub"\nmodel = "stub"\n',
        encoding="utf-8",
    )
    return cfg


@contextmanager
def _client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """Boot the real daemon (synthetic embedder) with a throwaway config.

    Context manager: enters the TestClient (runs lifespan), finishes first-run
    setup and stamps the profile token onto default headers (issue #14: the
    /memory/* routes resolve identity through the profile-token gate; the
    hook-facing /ingest /flush /session/end capture surface stays open).
    """
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    monkeypatch.setattr("mnemoseed.config.CONFIG_PATH", _config_toml(tmp_path))
    monkeypatch.setattr("mnemoseed.dream.snapshot.CONFIG_DIR", tmp_path)
    with TestClient(create_app()) as client:
        attach_token(client)
        yield client


def _user(text: str, ts: float, importance_hint: float | None = None) -> dict:
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


def _chunks(client: TestClient) -> int:
    return client.app.state.stores.vector.list_chunks(ChunkFilter(profile_id=_PROFILE), Page(limit=100)).total


def _graph_rows(path: Path) -> int:
    """Read the graph through a connection bound to the current (test) thread."""
    import asyncio

    driver = sqlite_graph.SqliteGraphDriver(path=path)
    try:
        return driver.list_nodes(NodeFilter(profile_id=_PROFILE), Page(limit=10)).total
    finally:
        asyncio.run(driver.close())


# ------------------------------------------------------------ /flush


def test_flush_rescues_open_turn_without_settling(tmp_path, monkeypatch) -> None:
    """A mid-session /flush takes the in-flight user turn through the drain
    (the store sees it) while the session stays open: later /ingest is still
    accepted and /session/end still settles normally."""
    with _client(tmp_path, monkeypatch) as client:
        assert client.post("/ingest", json=_user(text="我决定以后都用 pnpm", ts=1.0)).status_code == 202
        assert _chunks(client) == 0  # submit-only before the flush

        response = client.post("/flush", json={"session_id": _SESSION, "profile_id": _PROFILE})
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "flushed"
        assert body["closed_turns"] == 1
        assert _chunks(client) == 1  # the rescue drained the open turn

        # the session is NOT settled: the same session keeps accepting input
        assert client.post("/ingest", json=_user(text="我决定改用 vite 打包", ts=2.0)).status_code == 202

        settle = client.post("/session/end", json={"session_id": _SESSION, "profile_id": _PROFILE})
        assert settle.status_code == 200
        assert settle.json()["turns"] == 2
        assert _chunks(client) == 2  # the second turn drained once, no double write


def test_flush_twice_second_call_closes_nothing(tmp_path, monkeypatch) -> None:
    """A flush with no open turn in flight closes zero turns — the rescue path
    is a no-op when the previous flush already drained the in-flight turn."""
    with _client(tmp_path, monkeypatch) as client:
        assert client.post("/ingest", json=_user(text="一个普通句子", ts=1.0)).status_code == 202
        first = client.post("/flush", json={"session_id": _SESSION, "profile_id": _PROFILE})
        assert first.status_code == 200
        assert first.json()["closed_turns"] == 1
        response = client.post("/flush", json={"session_id": _SESSION, "profile_id": _PROFILE})
        assert response.status_code == 200
        assert response.json()["closed_turns"] == 0


def test_flush_unknown_session_returns_404(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        response = client.post("/flush", json={"session_id": "nope", "profile_id": _PROFILE})
        assert response.status_code == 404


def test_flush_profile_mismatch_returns_409(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        client.post("/ingest", json=_user(text="我的偏好", ts=1.0))
        response = client.post("/flush", json={"session_id": _SESSION, "profile_id": "other-profile"})
        assert response.status_code == 409


def test_flush_after_settle_is_a_noop_never_resurrects(tmp_path, monkeypatch) -> None:
    """After /session/end a /flush is a no-op: the session stays settled and
    the turn range is never re-written."""
    with _client(tmp_path, monkeypatch) as client:
        client.post("/ingest", json=_user(text="我决定以后都用 pnpm", ts=1.0))
        settle = client.post("/session/end", json={"session_id": _SESSION, "profile_id": _PROFILE})
        assert settle.status_code == 200
        response = client.post("/flush", json={"session_id": _SESSION, "profile_id": _PROFILE})
        assert response.status_code == 200
        assert response.json()["closed_turns"] == 0
        # still settled: ingest refused, exactly as before the flush
        assert client.post("/ingest", json=_user(text="不该进来", ts=3.0)).status_code == 409
        assert _chunks(client) == 1


# ------------------------------------------------------------ /memory/dream_once


_DURABLE = (
    "我决定以后都用 pnpm 管理依赖来构建前端项目",
    "我以后统一用 vite 来做前端打包方案",
    "我打算把日志系统迁移到时序数据库存储",
    "我认为代码 review 必须关注注释和可读性",
    "我坚持每次提交前都跑一遍完整的测试",
)


def test_dream_once_consumes_one_pending_manual_and_runs_chain(tmp_path, monkeypatch) -> None:
    """Manual-first (FR-2.8): the /dream once command endpoint launches exactly
    one manual dream over the daemon's real trigger wiring — snapshot, reflect,
    merge, safe-clear — reporting the trigger status back over HTTP."""
    with _client(tmp_path, monkeypatch) as client:
        trigger = client.app.state.dream
        for index, text in enumerate(_DURABLE):
            # full-score durable turns reach the 50.0 forced-cap event (the
            # state is driven through real HTTP, where the daemon's sqlite
            # connections are bound to the app-loop thread)
            resp = client.post(
                "/ingest",
                json=_user(text=text, ts=float(index + 1), importance_hint=1.0),
            )
            assert resp.status_code == 202
        settle = client.post("/session/end", json={"session_id": _SESSION, "profile_id": _PROFILE})
        assert settle.status_code == 200
        assert trigger.status(_PROFILE).pending_manual == 1
        assert trigger.status(_PROFILE).state is DreamState.IDLE

        once = client.post("/memory/dream_once", json={"profile_id": _PROFILE})
        assert once.status_code == 200
        body = once.json()
        assert body["launched"] is True
        assert body["state"] == "idle"
        assert body["pending_manual"] == 0
        # D3: the /dream command displays last_event.fired_at, so the status
        # payload must carry the (truthful) injected-clock timestamp
        assert body["last_event"] is not None
        assert body["last_event"]["kind"] == "forced_consolidation"
        assert isinstance(body["last_event"]["fired_at"], float)
        assert (
            client.app.state.stores.vector.list_chunks(ChunkFilter(profile_id=_PROFILE), Page(limit=10)).total
            == 0
        )  # the consumed dream safe-cleared its source chunks
        status = client.post("/memory/dream_status", json={"profile_id": _PROFILE})
        assert status.status_code == 200
        assert isinstance(status.json()["last_event"]["fired_at"], float)
    assert _graph_rows(tmp_path / "cortex.db") >= 1  # the reflected facts landed


def test_dream_status_reports_idle_when_nothing_pending(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        response = client.post("/memory/dream_status", json={"profile_id": _PROFILE})
        assert response.status_code == 200
        body = response.json()
        assert body["state"] == "idle"
        assert body["pending_manual"] == 0
        assert body["pending_queue"] == 0


def test_dream_once_with_nothing_pending_is_a_noop(tmp_path, monkeypatch) -> None:
    """dream_once on an idle profile with no pool events launches nothing and
    reports it honestly (launched=false), never blocking the response."""
    with _client(tmp_path, monkeypatch) as client:
        response = client.post("/memory/dream_once", json={"profile_id": _PROFILE})
        assert response.status_code == 200
        body = response.json()
        assert body["launched"] is False
        assert body["state"] == "idle"


def test_dream_endpoints_reject_blank_profile(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        for path in ("/memory/dream_once", "/memory/dream_status"):
            response = client.post(path, json={"profile_id": "   "})
            assert response.status_code == 422
