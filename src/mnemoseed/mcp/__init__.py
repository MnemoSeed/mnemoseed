"""MCP stdio memory gateway (PRD-03 FR-3.1).

Exposes the six ``memory.*`` tools over the official MCP SDK; each tool is a
thin HTTP call to the daemon's /memory surface, with input JSON-schemas and
typed errors (missing profile, unreachable daemon) instead of hangs.
"""

from mnemoseed.mcp.server import build_server, run_server

__all__ = ("build_server", "run_server")
