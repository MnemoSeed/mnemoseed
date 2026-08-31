"""stdio MCP server exposing the six memory tools (PRD-03 FR-3.1 / FR-3.11).

Every tool is a thin HTTP call: arguments are validated into a JSON schema by
the SDK, forwarded to the matching daemon /memory endpoint, and the JSON body
is returned as the tool result. Configuration comes from MNEMOSEED_BASE_URL /
MNEMOSEED_PROFILE_ID; a missing profile and an unreachable daemon both surface
as typed tool errors (never a hang — the HTTP call has a bounded timeout).

The server is built per-run (:func:`build_server`) so tool registration never
leaks across tests or processes; ``run_server`` is the CLI entry's blocking
stdio loop.
"""

from __future__ import annotations

import json
from typing import Any

import anyio
from mcp.server.mcpserver import MCPServer

from mnemoseed import __version__
from mnemoseed.mcp.client import (
    MemoryDaemonClient,
    resolve_base_url,
    resolve_profile_id,
)

# Initialize instructions (FR-3.11 / FR-6.5): self-contained boot context for
# the host, naming the six tools and the environment configuration keys, plus
# the Tier-2 degraded-mode guidance a hook-less host needs -- recall before
# answering, remember for durable facts, and the repin-idempotency expectation
# (a repeated pin reinforces, never duplicates). Bilingual: the tool names and
# the memory behavior are English, the two primary verbs carry the Chinese cue
# so a Chinese-speaking host picks it up immediately. Kept <=512 chars (Codex's
# hard cap; test_mcp_degraded_mode pins the bound against this constant).
INSTRUCTIONS = (
    "MnemoSeed memory (FR-3.1) - MCP-only host, no hooks: the model drives memory. "
    "Call memory.recall (检索) before answering anything memory-dependent; "
    "call memory.remember (钉记) for durable facts and preferences; "
    "re-remembering the same fact reinforces the stored pin, never duplicates. "
    "Also memory.audit, memory.timeline, memory.export, memory.forget_this (GDPR). "
    "Pass profile_id per call or set MNEMOSEED_PROFILE_ID; daemon at "
    "MNEMOSEED_BASE_URL (default http://localhost:7788)."
)

_SERVER_NAME = "mnemoseed-memory"


async def _dispatch(path: str, payload: dict[str, Any]) -> str:
    """Resolve the profile, then forward the (now complete) payload to the
    daemon and serialize the JSON body for the tool result."""
    payload = dict(payload)
    payload["profile_id"] = resolve_profile_id(payload.get("profile_id") or None)
    return json.dumps(await _call_daemon(path=path, **payload))


async def _call_daemon(path: str, **payload: Any) -> dict[str, Any]:
    """Post one memory request off the event loop (bounded timeout).

    This function is the monkeypatch seam the transport tests use to prove
    argument forwarding; the real body issues one bounded HTTP POST.
    """
    client = MemoryDaemonClient(base_url=resolve_base_url(None))
    return await anyio.to_thread.run_sync(client.post, path, payload)


def build_server() -> MCPServer:
    """Build a fresh MCP server with the six memory tools registered."""
    server = MCPServer(name=_SERVER_NAME, version=__version__, instructions=INSTRUCTIONS)

    @server.tool(
        name="memory.recall",
        description=(
            "Retrieve relevant memory for a query: dual-track retrieval, "
            "budgeted assembly, conflict pairing and honest coverage."
        ),
    )
    async def memory_recall(
        query: str = "",
        profile_id: str = "",
        host: str | None = None,
        project: str | None = None,
        time_bucket: str | None = None,
        top_k: int | None = None,
        budget: int | None = None,
        as_of: float | None = None,
    ) -> str:
        """Run the full recall path over the daemon /memory/recall surface."""
        return await _dispatch(
            "/memory/recall",
            {
                "query": query,
                "profile_id": profile_id,
                "host": host,
                "project": project,
                "time_bucket": time_bucket,
                "top_k": top_k,
                "budget": budget,
                "as_of": as_of,
            },
        )

    @server.tool(
        name="memory.remember",
        description="Store an explicit user pin; an identical re-pin reinforces instead of duplicating.",
    )
    async def memory_remember(text: str = "", profile_id: str = "") -> str:
        """Pin one explicitly asserted memory as a durable chunk."""
        return await _dispatch("/memory/remember", {"text": text, "profile_id": profile_id})

    @server.tool(
        name="memory.audit",
        description="Read the provenance and version history of one chunk or node.",
    )
    async def memory_audit(
        node_id: str | None = None, chunk_id: str | None = None, profile_id: str = ""
    ) -> str:
        """Audit a target's provenance, version chain, and relevant audit rows."""
        return await _dispatch(
            "/memory/audit",
            {"node_id": node_id, "chunk_id": chunk_id, "profile_id": profile_id},
        )

    @server.tool(
        name="memory.timeline",
        description="Replay a node's version timeline, or list the profile's recent activity.",
    )
    async def memory_timeline(node_id: str | None = None, profile_id: str = "") -> str:
        """Return one node's versions, or the profile-wide recent-first events."""
        return await _dispatch("/memory/timeline", {"node_id": node_id, "profile_id": profile_id})

    @server.tool(
        name="memory.export",
        description="Export the whole profile in a stable, paginated JSON shape (provenance included).",
    )
    async def memory_export(profile_id: str = "", offset: int = 0, limit: int = 50) -> str:
        """Dump the profile's chunks and nodes in the export/1 schema."""
        return await _dispatch(
            "/memory/export",
            {"profile_id": profile_id, "offset": offset, "limit": limit},
        )

    @server.tool(
        name="memory.forget_this",
        description=(
            "Permanently forget memory: delete a chunk, tombstone a node, or "
            "sweep by entity (GDPR, design/03 storage-layer erasure)."
        ),
    )
    async def memory_forget_this(
        chunk_id: str | None = None,
        node_id: str | None = None,
        entity: str | None = None,
        profile_id: str = "",
    ) -> str:
        """Remove memory by chunk_id, node_id, or entity; audit exactly what went."""
        return await _dispatch(
            "/memory/forget_this",
            {
                "chunk_id": chunk_id,
                "node_id": node_id,
                "entity": entity,
                "profile_id": profile_id,
            },
        )

    return server


def run_server() -> int:
    """Run the stdio MCP gateway (blocking); returns the process exit code."""
    build_server().run(transport="stdio")
    return 0
