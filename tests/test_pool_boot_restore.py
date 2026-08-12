"""NFR-2.3 pool boot-recovery e2e (issue #6, recovery verification).

Proves the score pool + watermark survive a real daemon restart: ingest durable
turns through the public HTTP surface, settle the session, read the pool from
/api/v1/status, bring the daemon fully down, boot it again over the SAME data
directory, and assert the pool row came back intact -- then credit another turn
and watch the balance grow (the restored ledger is live, not frozen).

Reasoning for the assertion shape: /api/v1/status exposes ``pool.balance`` and
``pool.watermark`` sourced from the meta store, which is exactly the database
the pool re-seeds from at boot (daemon/app.py ``_build_capture``), so the
pre/post-restart equality is the NFR claim made visible end-to-end.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from _identity_helpers import OWNER_PASSWORD, OWNER_USERNAME

from mnemoseed.daemon.app import create_app
from mnemoseed.daemon.runner import MnemoseedServer

_SESSION_A = "sess-restore-a"
_SESSION_B = "sess-restore-b"
_PROFILE = "p-restore"

_DURABLE_A = "我决定以后都用 pnpm 管理依赖"
_DURABLE_B = "我打算把日志系统完整迁移到时序数据库存储"
_DURABLE_C = "我坚持每次提交前都跑一遍完整的测试套件"


@asynccontextmanager
async def _boot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[dict[str, Any]]:
    """Boot one real daemon over tmp_path (embedded preset, synthetic embedder).

    Dies cleanly on exit: stores close via the lifespan teardown, so the data
    directory is safe to reopen by a second boot."""
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        'preset = "embedded"\n'
        f'[storage.vector]\nuri = "{(tmp_path / "chunks.lance").as_posix()}"\ndimensions = 64\n'
        f'[storage.graph]\npath = "{(tmp_path / "cortex.db").as_posix()}"\n'
        f'[storage.meta]\npath = "{(tmp_path / "meta.db").as_posix()}"\n'
        f'[storage.embed]\ndriver = "synthetic"\ndimension = 64\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    monkeypatch.setattr("mnemoseed.config.CONFIG_PATH", cfg)
    monkeypatch.setattr("mnemoseed.dream.snapshot.CONFIG_DIR", tmp_path)
    config = uvicorn.Config(
        create_app(),
        host="127.0.0.1",
        port=0,
        log_level="warning",
        lifespan="on",
        access_log=False,
    )
    server = MnemoseedServer(config)
    task = asyncio.create_task(server.serve())
    for _ in range(200):
        if server.started:
            break
        await asyncio.sleep(0.01)
    assert server.started, "daemon never started its run loop"
    port = server.servers[0].sockets[0].getsockname()[1]
    base_url = f"http://127.0.0.1:{port}"
    announcer = threading.Thread(
        target=server.announce_ready,
        args=("127.0.0.1", port),
        daemon=True,
        name="restore-announce",
    )
    announcer.start()
    try:
        await asyncio.wait_for(asyncio.to_thread(server.ready.wait), timeout=20)
    except TimeoutError:
        raise RuntimeError("daemon did not become ready") from None

    async with httpx.AsyncClient(timeout=30) as client:
        setup = await client.post(
            f"{base_url}/api/v1/setup",
            json={"username": OWNER_USERNAME, "password": OWNER_PASSWORD},
        )
        assert setup.status_code in (201, 410), setup.text
        login = await client.post(
            f"{base_url}/api/v1/auth/login",
            json={"username": OWNER_USERNAME, "password": OWNER_PASSWORD},
        )
        assert login.status_code == 200, login.text
        token = login.json()["token"]
    try:
        yield {"base_url": base_url, "token": token}
    finally:
        server.request_shutdown()
        try:
            await asyncio.wait_for(task, timeout=10)
        except TimeoutError:
            task.cancel()


async def _ingest_turn(base_url: str, session: str, text: str, ts: float) -> None:
    """POST one user-prompt turn with a low importance hint (FR-1.9)."""
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            f"{base_url}/ingest",
            json={
                "host": "claude_code",
                "event": "user_prompt",
                "session_id": session,
                "profile_id": _PROFILE,
                "ts": ts,
                "content": {"text": text},
                "importance_hint": 0.15,
            },
        )
        assert response.status_code == 202, response.text


async def _settle(base_url: str, session: str) -> None:
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(
            f"{base_url}/session/end", json={"session_id": session, "profile_id": _PROFILE}
        )
        assert response.status_code == 200, response.text


async def _pool_status(base_url: str, token: str) -> dict[str, Any]:
    """The dashboards pool row: balance + watermark for the test profile."""
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(
            f"{base_url}/api/v1/status",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 200, response.text
    for row in response.json()["profiles"]:
        if row["profile_id"] == _PROFILE:
            return dict(row["pool"])
    raise AssertionError(f"profile {_PROFILE!r} missing from /api/v1/status")


@pytest.mark.anyio
async def test_pool_and_watermark_survive_daemon_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # boot 1: write two durable turns, settle -> the pool credits S and the
    # watermark merges into a non-zero range (a single turn ends at index 0,
    # which the meta store reports as "no watermark advanced yet")
    async with _boot(tmp_path, monkeypatch) as daemon1:
        await _ingest_turn(daemon1["base_url"], _SESSION_A, _DURABLE_A, ts=1.0)
        await _ingest_turn(daemon1["base_url"], _SESSION_A, _DURABLE_B, ts=2.0)
        await _settle(daemon1["base_url"], _SESSION_A)
        before = await _pool_status(daemon1["base_url"], daemon1["token"])
    assert before["balance"] > 0  # the turns actually scored points
    assert before["watermark"] == {"start": 0, "end": 1}

    # boot 2 over the SAME data dir: the pool must come back identical
    async with _boot(tmp_path, monkeypatch) as daemon2:
        after = await _pool_status(daemon2["base_url"], daemon2["token"])
        assert after == before

        # the restored ledger is live, not frozen: a fresh turn grows the balance
        await _ingest_turn(daemon2["base_url"], _SESSION_B, _DURABLE_C, ts=1.0)
        await _settle(daemon2["base_url"], _SESSION_B)
        grown = await _pool_status(daemon2["base_url"], daemon2["token"])
        assert grown["balance"] > after["balance"]
