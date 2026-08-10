"""MCP stdio gateway (PRD-03 T4, FR-3.1 / FR-3.11): the six memory tools, the
initialize ``instructions`` string, input -> environment config resolution, the
typed daemon-unreachable error, and the CLI entrypoint.

Two layers are exercised here: the protocol layer (in-memory MCP client against
the real server, so authorize/list_tools/call_tool round-trip goes through the
official adapter) and the transport layer (the server wired to a real HTTP
daemon stub over a loopback socket so base-url resolution and the unreachable
path are proven on the wire, not mocked).
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from mnemoseed.mcp.client import (
    MemoryDaemonClient,
    MemoryDaemonUnreachableError,
    ProfileRequiredError,
    resolve_base_url,
    resolve_profile_id,
)
from mnemoseed.mcp.server import INSTRUCTIONS, build_server

_TOOLS = (
    "memory.recall",
    "memory.remember",
    "memory.audit",
    "memory.timeline",
    "memory.export",
    "memory.forget_this",
)


@pytest.fixture
def records() -> list[dict]:
    """Capture bucket for the argument-forwarding seam."""
    return []


# ---------------------------------------------------------------- protocol layer


@pytest.mark.anyio
async def test_server_instructions_carry_fr311_boot_context() -> None:
    server = build_server()
    assert len(INSTRUCTIONS) <= 512
    assert server.instructions == INSTRUCTIONS
    assert "memory.recall" in INSTRUCTIONS
    assert "memory.remember" in INSTRUCTIONS


@pytest.mark.anyio
async def test_server_exposes_the_six_dotted_tools() -> None:
    server = build_server()
    from mcp import Client

    async with Client(server=server) as client:
        listed = await client.list_tools()
        tools = {tool.name: tool for tool in listed.tools}
        assert set(tools) == set(_TOOLS)
        for tool in tools.values():
            schema = tool.input_schema
            assert schema.get("type") == "object"
            assert "properties" in schema
            properties = schema["properties"]
            # every tool accepts a profile_id; it is honored even though the
            # SDK models every parameter as optional (env fallback also valid)
            assert "profile_id" in properties


@pytest.mark.anyio
async def test_call_tool_forwards_args_to_daemon(
    monkeypatch: pytest.MonkeyPatch, records: list[dict]
) -> None:
    from mcp import Client
    from mcp.types import TextContent

    server = build_server()

    async def fake_recall(**kwargs) -> dict:
        records.append(kwargs)
        return {"entries": [], "coverage": {"profile_chunks": 0, "pool_size": 0}}

    monkeypatch.setattr("mnemoseed.mcp.server._call_daemon", fake_recall)
    async with Client(server=server) as client:
        result = await client.call_tool(
            "memory.recall",
            {"profile_id": "prof-x", "query": "anything", "top_k": 3},
        )
    assert result.is_error is False
    block = result.content[0]
    assert isinstance(block, TextContent)
    assert json.loads(block.text)["coverage"]["profile_chunks"] == 0
    # The SDK validates every argument the model declares, so the forwarded
    # payload carries every handler parameter (optional ones as None); the
    # hand-authored values must land on the right keys.
    forwarded = records[0]
    assert forwarded["path"] == "/memory/recall"
    assert forwarded["profile_id"] == "prof-x"
    assert forwarded["query"] == "anything"
    assert forwarded["top_k"] == 3


@pytest.mark.anyio
async def test_missing_profile_id_tool_error_mentions_env_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MNEMOSEED_PROFILE_ID", raising=False)
    from mcp import Client

    server = build_server()
    async with Client(server=server) as client:
        result = await client.call_tool("memory.recall", {"query": "anything"})
    assert result.is_error is True
    block = result.content[0]
    assert "MNEMOSEED_PROFILE_ID" in block.text


@pytest.mark.anyio
async def test_unroutable_daemon_becomes_typed_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MNEMOSEED_PROFILE_ID", raising=False)
    monkeypatch.setenv("MNEMOSEED_BASE_URL", "http://127.0.0.1:1")  # port 1: nothing listens
    from mcp import Client

    server = build_server()
    async with Client(server=server) as client:
        result = await client.call_tool("memory.recall", {"profile_id": "p", "query": "x"})
    assert result.is_error is True
    assert "memory daemon unreachable" in result.content[0].text


# ---------------------------------------------------------------- transport layer


class _DaemonHandler(BaseHTTPRequestHandler):
    """Minimal loopback daemon stub: answers the six endpoints with a canned
    JSON body so the MCP server's HTTP plumbing is proven without a real daemon."""

    def _reply(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)  # drain the request body
        payload = json.dumps({"route": self.path, "profile_sent": True}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:  # noqa: N802
        self._reply()

    def log_message(self, *args) -> None:
        pass


@pytest.fixture
def daemon_stub() -> tuple[str, threading.Thread]:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _DaemonHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield base_url, httpd
    httpd.shutdown()


@pytest.mark.anyio
@pytest.mark.parametrize("tool_name", _TOOLS)
async def test_tool_round_trips_over_real_http(
    tool_name: str, daemon_stub: tuple[str, threading.Thread], monkeypatch: pytest.MonkeyPatch
) -> None:
    base_url, _ = daemon_stub
    monkeypatch.setenv("MNEMOSEED_BASE_URL", base_url)
    monkeypatch.setenv("MNEMOSEED_PROFILE_ID", "prof-http")
    from mcp import Client
    from mcp.types import TextContent

    server = build_server()
    async with Client(server=server) as client:
        base_kwargs = _tool_defaults(tool_name)
        result = await client.call_tool(tool_name, base_kwargs)
    assert result.is_error is False
    block = result.content[0]
    assert isinstance(block, TextContent)
    assert json.loads(block.text)["route"].startswith("/memory/")


def _tool_defaults(tool_name: str) -> dict:
    defaults: dict[str, dict] = {
        "memory.recall": {"query": "q"},
        "memory.remember": {"text": "t"},
        "memory.audit": {"chunk_id": "c1"},
        "memory.timeline": {"node_id": "n1"},
        "memory.export": {},
        "memory.forget_this": {"chunk_id": "c1"},
    }
    return defaults[tool_name]


# ---------------------------------------------------------------- client helpers


def test_resolve_profile_id_arg_then_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MNEMOSEED_PROFILE_ID", "from-env")
    assert resolve_profile_id("from-arg") == "from-arg"
    assert resolve_profile_id(None) == "from-env"
    monkeypatch.delenv("MNEMOSEED_PROFILE_ID", raising=False)
    with pytest.raises(ProfileRequiredError):
        resolve_profile_id(None)


def test_resolve_base_url_default_then_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MNEMOSEED_BASE_URL", raising=False)
    assert resolve_base_url(None) == "http://localhost:7788"
    monkeypatch.setenv("MNEMOSEED_BASE_URL", "http://example.test:9999")
    assert resolve_base_url(None) == "http://example.test:9999"
    assert resolve_base_url("http://override:1") == "http://override:1"


def test_client_unreachable_maps_connection_error() -> None:
    client = MemoryDaemonClient(base_url="http://127.0.0.1:1")
    with pytest.raises(MemoryDaemonUnreachableError):
        client.recall(profile_id="p", query="x")


def test_client_posts_json_to_endpoint(daemon_stub: tuple[str, threading.Thread]) -> None:
    base_url, _ = daemon_stub
    client = MemoryDaemonClient(base_url=base_url)
    body = client.recall(profile_id="prof-http", query="anything")
    assert body["route"] == "/memory/recall"
    assert body["profile_sent"] is True


# ---------------------------------------------------------------- CLI entrypoint


def _console_script() -> list[str]:
    """Prefer the installed console script, else python -m."""
    script = Path(sys.executable).with_name("mnemoseed.exe")
    if script.exists():
        return [str(script), "mcp"]
    return [sys.executable, "-m", "mnemoseed.cli", "mcp"]


def test_cli_mcp_subcommand_prints_help() -> None:
    proc = subprocess.run(
        [*_console_script(), "--help"],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parents[1],
    )
    assert proc.returncode == 0
    assert "stdio" in proc.stdout
