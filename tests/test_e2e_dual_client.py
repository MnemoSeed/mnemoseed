"""PRD-03 task 5 -- dual-client end-to-end integration (AC-1).

Brings up ONE real daemon (uvicorn over ``daemon/app.py`` with embedded stores
and the synthetic embedder, in-process) and drives BOTH client surfaces at the
same time:

  * the MCP stdio gateway -- the real ``mnemoseed mcp`` subprocess spawned via
    the MCP SDK's stdio client, and
  * the daemon's own ``/memory/*`` HTTP surface.

Covered scenarios:

  1. cross-visibility (AC-1): a pin written through MCP ``memory.remember`` is
     recalled through HTTP ``/memory/recall``, and the reverse direction too.
  2. profile isolation: a pin under one profile is never readable under another
     from either surface, and the foreign profile sees honest-empty coverage.
  3. concurrent dual-client load: 2 surfaces x 8 workers x 25 interleaved
     remember/recall ops, asserting exact counts, no corruption and no errors,
     on a combined run so the surfaces provoke each other.
  4. forget_this cross-surface: chunk = hard delete (recall goes empty on both
     surfaces); node = tombstone (audit/timeline still show the version history
     from either surface).
  5. usage/audit coherence (FR-3.7 seam): operations from both surfaces land in
     the same append-only audit trail with the correct profile attribution, and
     recall registers hits from either surface.

The daemon boots on the caller's event loop, so the test must never block the
loop (no ``threading.join`` from async code). The MCP child process is managed
by the SDK's stdio transport, which terminates it on session exit; a module
scoped guard re-checks for leaked ``mnemoseed cli mcp`` processes.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import subprocess
import sys
import threading
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from _identity_helpers import OWNER_PASSWORD, OWNER_USERNAME
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import CallToolResult, TextContent

from mnemoseed.daemon.app import create_app
from mnemoseed.daemon.runner import MnemoseedServer
from mnemoseed.schema.graph import GraphNode, NodeType
from mnemoseed.schema.stamp import Provenance
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

# Mandated concurrent-load shape: surfaces x workers x ops.
CONCURRENT_SURFACES = 2
CONCURRENT_WORKERS = 8
CONCURRENT_OPS = 25


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
        self.token: str = ""

    async def _attach_owner(self) -> str:
        """Finish first-run setup and return a profile token (issue #14): the
        /memory/* surface this suite drives is gated on the owner."""
        async with httpx.AsyncClient(timeout=30) as client:
            setup = await client.post(
                f"{self.base_url}/api/v1/setup",
                json={"username": OWNER_USERNAME, "password": OWNER_PASSWORD},
            )
            assert setup.status_code == 201, setup.text
            login = await client.post(
                f"{self.base_url}/api/v1/auth/login",
                json={"username": OWNER_USERNAME, "password": OWNER_PASSWORD},
            )
            assert login.status_code == 200, login.text
            return login.json()["token"]

    @property
    def auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

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
        # The bound socket appears during uvicorn's startup pass; a bounded poll
        # on the started event is enough to learn the advertised port before
        # handing it to the announce thread.
        for _ in range(200):
            if server.started:
                break
            await asyncio.sleep(0.01)
        assert server.started, "daemon never started its run loop"
        port = server.servers[0].sockets[0].getsockname()[1]
        self.base_url = f"http://127.0.0.1:{port}"
        # Readiness goes through the SAME announce thread the production runner
        # uses (runner.announce_ready -> server.ready), polled off-loop so the
        # uvicorn run loop is never blocked by this test's activity.
        announcer = threading.Thread(
            target=server.announce_ready,
            args=("127.0.0.1", port),
            daemon=True,
            name="e2e-announce",
        )
        announcer.start()
        try:
            await asyncio.wait_for(asyncio.to_thread(server.ready.wait), timeout=20)
        except TimeoutError:
            raise RuntimeError("daemon did not become ready") from None
        self.token = await self._attach_owner()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        assert self._server is not None and self._task is not None
        self._server.request_shutdown()
        try:
            await asyncio.wait_for(self._task, timeout=10)
        except TimeoutError:
            self._task.cancel()

    async def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(f"{self.base_url}{path}", json=payload, headers=self.auth_headers)
            return {"status": response.status_code, "json": response.json()}


@contextlib.asynccontextmanager
async def _mcp_session(base_url: str, token: str) -> AsyncIterator[ClientSession]:
    """One real ``mnemoseed mcp`` subprocess session against the daemon.

    The gateway holds the profile token via MNEMOSEED_TOKEN (issue #14): the
    MCP client bearer-attaches it to every /memory/* call, and returns a typed
    error when no token is configured."""
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "mnemoseed.cli", "mcp"],
        env={"MNEMOSEED_BASE_URL": base_url, "MNEMOSEED_TOKEN": token},
        cwd=_REPO_ROOT,
    )
    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            yield session


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


async def _mcp_audit(
    session: ClientSession, profile_id: str, *, chunk_id: str | None = None, node_id: str | None = None
) -> dict[str, Any]:
    args: dict[str, Any] = {"profile_id": profile_id}
    if chunk_id:
        args["chunk_id"] = chunk_id
    if node_id:
        args["node_id"] = node_id
    return _result_json(await session.call_tool("memory.audit", args))


async def _mcp_timeline(
    session: ClientSession, profile_id: str, node_id: str | None = None
) -> dict[str, Any]:
    args: dict[str, Any] = {"profile_id": profile_id}
    if node_id:
        args["node_id"] = node_id
    return _result_json(await session.call_tool("memory.timeline", args))


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


# ------------------------------------------------- 1. cross-visibility (AC-1)


@pytest.mark.anyio
async def test_cross_surface_mcp_pin_readable_via_http_and_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-1 cross-client inheritance: a pin written on one client surface is
    recalled on the other, for both directions."""
    profile = "prof-ac1"
    http_text = "cursor prefers dependency injection everywhere"
    mcp_text = "cline remembers the sprint demo is on fridays"
    async with _DaemonHarness(tmp_path, monkeypatch) as daemon:
        async with _mcp_session(daemon.base_url, daemon.token) as session:
            pinned = await _mcp_remember(session, profile, mcp_text)
            assert pinned["outcome"] == "new_chunk"
            assert pinned["chunk_id"]
            await daemon.post("/memory/remember", {"profile_id": profile, "text": http_text})
            via_http = await daemon.post("/memory/recall", {"profile_id": profile, "query": mcp_text})
            entries = via_http["json"]["memory"]["entries"]
            assert entries and entries[0]["text"] == mcp_text
            via_mcp_after_http_write = await _mcp_recall(session, profile, http_text)
            mcp_entries = via_mcp_after_http_write["memory"]["entries"]
            assert mcp_entries and mcp_entries[0]["text"] == http_text


# ------------------------------------------------- 2. profile isolation


@pytest.mark.anyio
async def test_profile_isolation_honest_empty_on_both_surfaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A write under P1 is invisible under P2 from both surfaces, which report
    honest-empty coverage instead of leaking foreign rows (FR-3.13/D5)."""
    private_text = "keystore rotation happens tuesday"
    async with _DaemonHarness(tmp_path, monkeypatch) as daemon:
        async with _mcp_session(daemon.base_url, daemon.token) as session:
            await _mcp_remember(session, "p1", private_text)
            via_http_p2 = await daemon.post("/memory/recall", {"profile_id": "p2", "query": private_text})
            http_entries = via_http_p2["json"]["memory"]["entries"]
            assert http_entries == []
            assert via_http_p2["json"]["memory"]["coverage"]["profile_chunks"] == 0
            via_mcp_p2 = await _mcp_recall(session, "p2", private_text)
            assert via_mcp_p2["memory"]["entries"] == []
            assert via_mcp_p2["memory"]["coverage"]["profile_chunks"] == 0
            via_mcp_p1 = await _mcp_recall(session, "p1", private_text)
            assert via_mcp_p1["memory"]["entries"][0]["text"] == private_text
            export_p2 = await daemon.post("/memory/export", {"profile_id": "p2"})
            assert export_p2["json"]["profile_id"] == "p2"
            assert export_p2["json"]["paging"]["chunk_total"] == 0


# ------------------------------------------------- 3. forget_this cross-surface


@pytest.mark.anyio
async def test_forget_this_chunk_hard_delete_seen_by_both_surfaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """forget_this on a chunk is a hard delete: recall from either surface goes
    honest-empty, and both surfaces report the same 404 for the deleted chunk."""
    text = "temporary wifi password was winter-2026"
    async with _DaemonHarness(tmp_path, monkeypatch) as daemon:
        async with _mcp_session(daemon.base_url, daemon.token) as session:
            pinned = await _mcp_remember(session, "p-forget", text)
            chunk_id = pinned["chunk_id"]
            removed = await daemon.post(
                "/memory/forget_this", {"profile_id": "p-forget", "chunk_id": chunk_id}
            )
            assert removed["json"]["removed"]["chunks"] == [chunk_id]
            via_http = await daemon.post("/memory/recall", {"profile_id": "p-forget", "query": text})
            assert via_http["json"]["memory"]["entries"] == []
            via_mcp = await _mcp_recall(session, "p-forget", text)
            assert via_mcp["memory"]["entries"] == []
            timeline_http = await daemon.post("/memory/timeline", {"profile_id": "p-forget"})
            assert all(entry["id"] != chunk_id for entry in timeline_http["json"]["events"])
            audit_http = await daemon.post("/memory/audit", {"profile_id": "p-forget", "chunk_id": chunk_id})
            assert audit_http["status"] == 404
            audit_mcp = await session.call_tool(
                "memory.forget_this", {"profile_id": "p-forget", "chunk_id": chunk_id}
            )
            assert audit_mcp.is_error
            assert "404" in audit_mcp.content[0].text  # type: ignore[union-attr]


async def _seed_graph_node(tmp_path: Path, profile_id: str, node_id: str, statement: str) -> None:
    """Pre-boot setup: insert one graph node into the sqlite file the daemon's
    config reserves, so node tombstoning is exercised through the public
    /memory surface. Same setup precedent as test_dream_boot seeding."""
    driver = sqlite_graph.SqliteGraphDriver(path=tmp_path / "cortex.db")
    try:
        driver.upsert_node(
            GraphNode(
                node_id=node_id,
                profile_id=profile_id,
                node_type=NodeType.PREFERENCE,
                entities=["ui"],
                props={
                    "domain": "coding",
                    "statement": statement,
                    "valence": 0.8,
                    "prior_width": 0.3,
                    "trait_anchor": "anima-1",
                    "evidence_chain": [{"event": "created", "at": 123.0}],
                },
                provenance=Provenance(asserted_by="user", source="memory.remember", confidence=1.0),
                valid_from=time.time() - 100.0,
            )
        )
    finally:
        await driver.close()


@pytest.mark.anyio
async def test_forget_this_node_tombstone_keeps_history_on_both_surfaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """forget_this on a node tombstones it: recall stops surfacing it, while
    audit/timeline keep the full version history reachable from either client
    surface (design/03 storage-layer erasure)."""
    statement = "prefers explicit interface types in python"
    profile = "p-node"
    node_id = "node-e2e-tombstone"
    await _seed_graph_node(tmp_path, profile, node_id, statement)
    async with _DaemonHarness(tmp_path, monkeypatch) as daemon:
        async with _mcp_session(daemon.base_url, daemon.token) as session:
            timeline_before_http = await daemon.post(
                "/memory/timeline", {"profile_id": profile, "node_id": node_id}
            )
            assert timeline_before_http["json"]["events"]
            removed = await daemon.post("/memory/forget_this", {"profile_id": profile, "node_id": node_id})
            assert removed["json"]["removed"]["nodes"] == [node_id]
            timeline_http = await daemon.post("/memory/timeline", {"profile_id": profile, "node_id": node_id})
            assert timeline_http["json"]["events"]
            timeline_mcp = await _mcp_timeline(session, profile, node_id=node_id)
            assert timeline_mcp["events"]
            audit_http = await daemon.post("/memory/audit", {"profile_id": profile, "node_id": node_id})
            assert audit_http["json"]["target"]["id"] == node_id
            assert audit_http["json"]["versions"]
            audit_mcp = await _mcp_audit(session, profile, node_id=node_id)
            assert audit_mcp["versions"]
            via_http = await daemon.post("/memory/recall", {"profile_id": profile, "query": statement})
            assert via_http["json"]["memory"]["entries"] == []
            via_mcp = await _mcp_recall(session, profile, statement)
            assert via_mcp["memory"]["entries"] == []


# ------------------------------------------------- 5. usage/audit coherence


@pytest.mark.anyio
async def test_usage_audit_coherence_both_surfaces_same_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FR-3.7 seam: operations from either surface land in the same profile-
    scoped audit trail, and recall from either surface registers a usage hit.
    A foreign profile sees an honest-empty audit for the same chunk."""
    profile = "prof-audit"
    http_text = "deploy freeze starts on the 15th"
    mcp_text = "code owners must review all prs"
    async with _DaemonHarness(tmp_path, monkeypatch) as daemon:
        async with _mcp_session(daemon.base_url, daemon.token) as session:
            http_pin = await daemon.post("/memory/remember", {"profile_id": profile, "text": http_text})
            http_chunk = http_pin["json"]["chunk_id"]
            mcp_pin = await _mcp_remember(session, profile, mcp_text)
            mcp_chunk = mcp_pin["chunk_id"]
            audit_mcp_http_chunk = await _mcp_audit(session, profile, chunk_id=http_chunk)
            assert any(row["action"] == "remember" for row in audit_mcp_http_chunk["audit"])
            assert all(
                row["detail"].get("profile_id") in (None, profile) for row in audit_mcp_http_chunk["audit"]
            )
            audit_http_mcp_chunk = await daemon.post(
                "/memory/audit", {"profile_id": profile, "chunk_id": mcp_chunk}
            )
            assert any(row["action"] == "remember" for row in audit_http_mcp_chunk["json"]["audit"])
            foreign = await daemon.post("/memory/audit", {"profile_id": "prof-other", "chunk_id": http_chunk})
            assert foreign["status"] == 200
            assert foreign["json"]["audit"] == []
            recall_http = await daemon.post("/memory/recall", {"profile_id": profile, "query": http_text})
            assert recall_http["json"]["memory"]["coverage"]["vector_hits"] >= 1
            recall_mcp = await _mcp_recall(session, profile, mcp_text)
            assert recall_mcp["memory"]["coverage"]["vector_hits"] >= 1


# ------------------------------------------------- 4. concurrent dual-client load


@pytest.mark.anyio
async def test_concurrent_dual_surface_load_exact_counts_no_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Convincing, non-lossy concurrency: 2 surfaces x 8 workers x 25 interleaved
    remember/recall ops, all in flight together. Failure of this test is the T5
    concurrency probe: it must show zero tool errors / zero 5xx and exact chunk
    counts, proving the two clients share one consistent store."""
    profile = "prof-load"
    http_texts = {
        f"h{w:02d}-{i:03d}": f"http worker {w} op {i}"
        for w in range(CONCURRENT_WORKERS)
        for i in range(CONCURRENT_OPS)
    }
    mcp_texts = {
        f"m{w:02d}-{i:03d}": f"mcp worker {w} op {i}"
        for w in range(CONCURRENT_WORKERS)
        for i in range(CONCURRENT_OPS)
    }
    bag: dict[str, Any] = {"errors": [], "http_written": [], "mcp_written": []}

    async def http_worker(base_url: str, token: str, w: int) -> None:
        headers = {"Authorization": f"Bearer {token}"}
        async with httpx.AsyncClient(timeout=60) as client:
            for i in range(CONCURRENT_OPS):
                text = http_texts[f"h{w:02d}-{i:03d}"]
                response = await client.post(
                    base_url + "/memory/remember",
                    json={"profile_id": profile, "text": text},
                    headers=headers,
                    timeout=60,
                )
                if response.status_code >= 500:
                    bag["errors"].append(("http-5xx", response.status_code, text))
                else:
                    bag["http_written"].append(text)
                recall = await client.post(
                    base_url + "/memory/recall",
                    json={"profile_id": profile, "query": text},
                    headers=headers,
                    timeout=60,
                )
                if recall.status_code >= 500:
                    bag["errors"].append(("http-5xx", recall.status_code, text))

    async def mcp_worker(session: ClientSession, w: int) -> None:
        for i in range(CONCURRENT_OPS):
            text = mcp_texts[f"m{w:02d}-{i:03d}"]
            remember = await session.call_tool("memory.remember", {"profile_id": profile, "text": text})
            if remember.is_error:
                bag["errors"].append(("mcp-tool-error", remember.content[0].text, text))
            else:
                bag["mcp_written"].append(text)
            recall = await session.call_tool("memory.recall", {"profile_id": profile, "query": text})
            if recall.is_error:
                bag["errors"].append(("mcp-tool-error", recall.content[0].text, text))

    async with _DaemonHarness(tmp_path, monkeypatch) as daemon:
        base_url = daemon.base_url
        async with _mcp_session(base_url, daemon.token) as session:
            tasks = [
                asyncio.create_task(http_worker(base_url, daemon.token, w)) for w in range(CONCURRENT_WORKERS)
            ]
            tasks += [asyncio.create_task(mcp_worker(session, w)) for w in range(CONCURRENT_WORKERS)]
            await asyncio.wait_for(asyncio.gather(*tasks), timeout=240)
            export = await daemon.post("/memory/export", {"profile_id": profile, "limit": 500})
        body = export["json"]
        errors = bag["errors"]
        assert errors == [], f"{len(errors)} errors under dual-surface load: {errors[:5]}"
        assert len(bag["http_written"]) == CONCURRENT_WORKERS * CONCURRENT_OPS
        assert len(bag["mcp_written"]) == CONCURRENT_WORKERS * CONCURRENT_OPS
        assert body["profile_id"] == profile
        assert body["paging"]["chunk_total"] == CONCURRENT_WORKERS * CONCURRENT_OPS * CONCURRENT_SURFACES
        exported = {chunk["text"] for chunk in body["chunks"]}
        expected = set(http_texts.values()) | set(mcp_texts.values())
        assert exported == expected
