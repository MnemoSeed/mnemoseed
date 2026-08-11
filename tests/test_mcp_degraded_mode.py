"""PRD-06 T6 -- Tier-2 degraded-mode (MCP-only, no hooks) end-to-end (FR-6.5).

A Tier-2 host (desktop Chat class) has no lifecycle hooks and no transcript
capture: the model alone drives memory through the MCP tool surface. This module
pins that floor in three parts:

  1. instructions audit -- the initialize ``instructions`` literal must stay a
     self-contained <=512-char guide (Codex's hard cap, FR-6.5) that tells a
     hook-less model to (a) call memory.recall before answering anything
     memory-dependent, (b) call memory.remember for durable facts and
     preferences, and (c) expect a repeated pin to reinforce in place rather
     than duplicate (the remember-idempotency pairing of FR-6.5). The length is
     asserted against the real ``INSTRUCTIONS`` constant, never a fixture.
  2. a Tier-2 host loop over ONE real daemon using ONLY the MCP surface
     (the ``mnemoseed mcp`` stdio subprocess): initialize -> read instructions
     -> remember -> close -> fresh MCP session on the same daemon -> recall
     -> re-remember the same fact -> reinforced (same chunk, no duplicate)
     -> forget_this -> honest-empty recall. Every assertion rides the public MCP
     tool surface; no hook path and no direct /ingest or /memory HTTP call.
  3. remember idempotency pinned through the MCP tool surface specifically:
     the same fact remembered twice returns ``reinforced`` with the same
     chunk_id and the profile export still holds exactly one chunk.

The daemon boots on the caller's event loop (same harness as
``test_e2e_dual_client``); the MCP child is managed by the SDK's stdio
transport. A module-scoped guard re-checks for leaked ``mnemoseed cli mcp``
processes.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import subprocess
import sys
import threading
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import CallToolResult, InitializeResult, TextContent

from mnemoseed.daemon.app import create_app
from mnemoseed.daemon.runner import MnemoseedServer
from mnemoseed.mcp.server import INSTRUCTIONS
from mnemoseed.storage.drivers import lancedb_embedded, sqlite_graph, sqlite_meta
from mnemoseed.storage.drivers.synthetic_embedder import SyntheticEmbedder
from mnemoseed.storage.registry import (
    EMBED_DRIVERS,
    GRAPH_DRIVERS,
    META_DRIVERS,
    VECTOR_DRIVERS,
    register,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_MCP_CHILD_MARKER = "mnemoseed.cli mcp"

_TOOLS = (
    "memory.recall",
    "memory.remember",
    "memory.audit",
    "memory.timeline",
    "memory.export",
    "memory.forget_this",
)


@pytest.fixture(autouse=True)
def _ensure_real_drivers() -> None:
    """Restore the real drivers if a previous suite cleared the registries."""
    for registry, cls in (
        (VECTOR_DRIVERS, lancedb_embedded.LanceDbEmbeddedStore),
        (GRAPH_DRIVERS, sqlite_graph.SqliteGraphDriver),
        (META_DRIVERS, sqlite_meta.SqliteMetaDriver),
        (EMBED_DRIVERS, SyntheticEmbedder),
    ):
        if not registry.contains(cls.info.name):
            register(registry)(cls)


# ------------------------------------------------------------ instructions audit


def test_fr65_instructions_self_contained_under_512_chars() -> None:
    """FR-6.5 cap + Codex's 512-char hard limit: the guidance asserted against
    the real constant stays under the bound and remains self-contained for a
    Tier-2 host -- all six tools and both configuration keys survive any edit."""
    assert len(INSTRUCTIONS) <= 512
    for tool in _TOOLS:
        assert tool in INSTRUCTIONS
    assert "MNEMOSEED_PROFILE_ID" in INSTRUCTIONS
    assert "MNEMOSEED_BASE_URL" in INSTRUCTIONS


def test_fr65_instructions_direct_recall_before_answering() -> None:
    """FR-6.5 (a): a hook-less model is explicitly told to call memory.recall
    before answering anything memory-dependent -- the passive-capture ceiling is
    stated as a directive, not just a tool listing."""
    lower = INSTRUCTIONS.lower()
    assert "call memory.recall" in lower
    assert "before answering" in lower


def test_fr65_instructions_require_remember_for_durable_facts() -> None:
    """FR-6.5 (b): the model is told to pin durable user facts and preferences
    with memory.remember -- the write half of the degraded loop."""
    lower = INSTRUCTIONS.lower()
    assert "call memory.remember" in lower
    assert "durable" in lower


def test_fr65_instructions_state_repin_idempotency() -> None:
    """FR-6.5 (c): the guidance pairs the degraded write path with remember
    idempotency -- a repeated pin reinforces in place, never duplicates."""
    lower = INSTRUCTIONS.lower()
    assert "reinforce" in lower
    assert "duplicat" in lower


# ------------------------------------------------------------- daemon harness


def _serving_config_toml(tmp_path: Path) -> Path:
    """Embedded-preset config: lance/sqlite under tmp_path, synthetic embedder."""
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


class _DaemonHarness:
    """A real daemon booted on the caller's event loop, driven over HTTP."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self._tmp = tmp_path
        self._monkeypatch = monkeypatch
        self._server: MnemoseedServer | None = None
        self._task: asyncio.Task[Any] | None = None
        self.base_url: str = ""

    async def __aenter__(self) -> _DaemonHarness:
        self._monkeypatch.delenv("STORAGE_MODE", raising=False)
        self._monkeypatch.setattr("mnemoseed.config.CONFIG_PATH", _serving_config_toml(self._tmp))
        self._monkeypatch.setattr("mnemoseed.dream.snapshot.CONFIG_DIR", self._tmp)
        config = uvicorn.Config(
            create_app(),
            host="127.0.0.1",
            port=0,
            log_level="warning",
            lifespan="on",
            access_log=False,
        )
        server = MnemoseedServer(config)
        self._server = server
        self._task = asyncio.create_task(server.serve())
        for _ in range(200):
            if server.started:
                break
            await asyncio.sleep(0.01)
        assert server.started, "daemon never started its run loop"
        port = server.servers[0].sockets[0].getsockname()[1]
        self.base_url = f"http://127.0.0.1:{port}"
        announcer = threading.Thread(
            target=server.announce_ready,
            args=("127.0.0.1", port),
            daemon=True,
            name="degraded-announce",
        )
        announcer.start()
        try:
            await asyncio.wait_for(asyncio.to_thread(server.ready.wait), timeout=20)
        except TimeoutError:
            raise RuntimeError("daemon did not become ready") from None
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        assert self._server is not None and self._task is not None
        self._server.request_shutdown()
        try:
            await asyncio.wait_for(self._task, timeout=10)
        except TimeoutError:
            self._task.cancel()


@contextlib.asynccontextmanager
async def _mcp_session(base_url: str) -> AsyncIterator[tuple[InitializeResult, ClientSession]]:
    """One real ``mnemoseed mcp`` subprocess session against the daemon; yields
    the initialize result (instructions on the wire) alongside the session."""
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "mnemoseed.cli", "mcp"],
        env={"MNEMOSEED_BASE_URL": base_url},
        cwd=_REPO_ROOT,
    )
    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            init = await session.initialize()
            yield init, session


def _result_json(result: CallToolResult) -> dict[str, Any]:
    """Extract the daemon JSON body from a tool result; errors raise."""
    content = result.content[0]
    assert isinstance(content, TextContent)
    if result.is_error:
        raise RuntimeError(content.text)
    return json.loads(content.text)


async def _mcp_remember(session: ClientSession, profile_id: str, text: str) -> dict[str, Any]:
    return _result_json(await session.call_tool("memory.remember", {"profile_id": profile_id, "text": text}))


async def _mcp_recall(session: ClientSession, profile_id: str, query: str) -> dict[str, Any]:
    return _result_json(await session.call_tool("memory.recall", {"profile_id": profile_id, "query": query}))


async def _mcp_export(session: ClientSession, profile_id: str, limit: int = 100) -> dict[str, Any]:
    return _result_json(await session.call_tool("memory.export", {"profile_id": profile_id, "limit": limit}))


async def _mcp_forget(session: ClientSession, profile_id: str, chunk_id: str) -> dict[str, Any]:
    return _result_json(
        await session.call_tool("memory.forget_this", {"profile_id": profile_id, "chunk_id": chunk_id})
    )


# ---------------------------------------------------------------- leak guard


def _mcp_child_pids() -> set[int]:
    """PIDs of python processes running the real MCP stdio gateway.

    Uses the Windows CIM query; on other platforms the stdio transport's own
    process-tree cleanup is trusted and the guard degrades to a no-op.
    """
    if sys.platform != "win32":
        return set()
    script = (
        "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
        f"Where-Object {{ $_.CommandLine -like '*{_MCP_CHILD_MARKER}*' }} | "
        "Select-Object -Expand ProcessId"
    )
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True, check=False
    )
    out = (proc.stdout or "").strip()
    pids: set[int] = set()
    for line in out.splitlines():
        value = line.strip()
        if value.isdigit():
            pids.add(int(value))
    return pids


@pytest.fixture(scope="module", autouse=True)
def _mcp_leak_guard() -> Any:
    """No spawned ``mnemoseed mcp`` subprocess may outlive the module."""
    before = _mcp_child_pids()
    yield
    leaked = _mcp_child_pids() - before
    assert not leaked, f"leaked MCP gateway child processes: {sorted(leaked)}"


# ------------------------------------------------------- degraded host loop


@pytest.mark.anyio
async def test_tier2_host_loop_remember_recall_reinforce_forget_only_mcp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FR-6.5 / AC-9 Tier-2 floor, exercised through the MCP surface only.

    One daemon, two independent MCP sessions (a later "session" per the FR):
    initialize reads the degraded-mode instructions; a first remember writes a
    new chunk; a fresh session recalls it, re-remembering the SAME fact returns
    ``reinforced`` with the same chunk_id and the export still holds exactly one
    chunk (idempotent pin, no duplicate); forget_this then recall goes
    honest-empty. No hook path and no direct daemon HTTP call participates."""
    fact = "the user keeps fridays light for deep-focus writing"
    profile = "prof-tier2"
    async with _DaemonHarness(tmp_path, monkeypatch) as daemon:
        # "session" one: initialize (read the on-wire degraded-mode guidance),
        # then remember a durable preference.
        pinned: dict[str, Any]
        async with _mcp_session(daemon.base_url) as (init, session1):
            assert init.instructions is not None
            assert init.instructions == INSTRUCTIONS
            assert "call memory.recall" in init.instructions.lower()
            assert "call memory.remember" in init.instructions.lower()
            pinned = await _mcp_remember(session1, profile, fact)
        assert pinned["outcome"] == "new_chunk"
        first_chunk = pinned["chunk_id"]
        assert first_chunk

        # Later "session": a fresh MCP connection against the same daemon.
        async with _mcp_session(daemon.base_url) as (_init, session2):
            # The model follows the instructions: recall first.
            recalled = await _mcp_recall(session2, profile, fact)
            assert recalled["memory"]["entries"]
            assert recalled["memory"]["entries"][0]["text"] == fact
            assert recalled["memory"]["coverage"]["vector_hits"] >= 1

            # Over-eager re-remember of the SAME fact: reinforced in place, the
            # dual-write idempotency the instructions promise, over MCP.
            repinned = await _mcp_remember(session2, profile, fact)
            assert repinned["outcome"] == "reinforced"
            assert repinned["chunk_id"] == first_chunk

            # No duplicate anywhere on the MCP surface.
            exported = await _mcp_export(session2, profile)
            assert exported["paging"]["chunk_total"] == 1
            assert [chunk["text"] for chunk in exported["chunks"]] == [fact]
            again = await _mcp_recall(session2, profile, fact)
            assert [entry["text"] for entry in again["memory"]["entries"]] == [fact]

            # GDPR forget then honest-empty recall (never a missing/stale answer).
            removed = await _mcp_forget(session2, profile, first_chunk)
            assert removed["removed"]["chunks"] == [first_chunk]
            emptied = await _mcp_recall(session2, profile, fact)
            assert emptied["memory"]["entries"] == []
            assert emptied["memory"]["coverage"]["profile_chunks"] == 0
