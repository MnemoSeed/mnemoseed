"""HTTP client and config resolution for the MCP memory gateway.

The MCP server is a thin transport: each tool resolves configuration, forwards
its JSON-schema-validated arguments to the matching daemon /memory endpoint,
and maps transport-level failure (connect refused, timeout, DNS, non-2xx) into
typed errors that surface to the MCP client as tool errors instead of hangs.
Configuration: ``MNEMOSEED_BASE_URL`` (default http://localhost:7788) and
``MNEMOSEED_PROFILE_ID`` (required per call or via env).
"""

from __future__ import annotations

import os
from typing import Any, cast

import httpx

DEFAULT_BASE_URL = "http://localhost:7788"
REQUEST_TIMEOUT_SECONDS = 3.0

ENV_BASE_URL = "MNEMOSEED_BASE_URL"
ENV_PROFILE_ID = "MNEMOSEED_PROFILE_ID"


class MemoryDaemonError(Exception):
    """Base class for the typed daemon connectivity / config errors."""


class MemoryDaemonUnreachableError(MemoryDaemonError):
    """The daemon did not answer (connect refused, timeout, DNS...)."""


class MemoryDaemonStatusError(MemoryDaemonError):
    """The daemon answered with a non-2xx status."""


class ProfileRequiredError(MemoryDaemonError):
    """No profile was given and MNEMOSEED_PROFILE_ID is not set."""


def resolve_base_url(explicit: str | None) -> str:
    """Base URL resolution order: explicit arg, env, built-in default."""
    return (explicit or os.environ.get(ENV_BASE_URL) or DEFAULT_BASE_URL).rstrip("/")


def resolve_profile_id(explicit: str | None) -> str:
    """Profile resolution order: explicit arg, env; a blank value is missing."""
    if explicit and explicit.strip():
        return explicit.strip()
    from_env = os.environ.get(ENV_PROFILE_ID)
    if from_env and from_env.strip():
        return from_env.strip()
    raise ProfileRequiredError(f"no profile_id given and {ENV_PROFILE_ID} is not set")


class MemoryDaemonClient:
    """Thin JSON client for the daemon /memory endpoints (bounded timeouts)."""

    def __init__(self, base_url: str, timeout: float = REQUEST_TIMEOUT_SECONDS) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            response = httpx.post(f"{self.base_url}{path}", json=payload, timeout=self.timeout)
        except httpx.HTTPError as exc:
            raise MemoryDaemonUnreachableError(f"memory daemon unreachable: {exc}") from exc
        if response.status_code >= 400:
            raise MemoryDaemonStatusError(
                f"memory daemon error {response.status_code}: {response.text[:400]}"
            )
        return cast(dict[str, Any], response.json())

    def recall(self, profile_id: str, query: str, **extra: Any) -> dict[str, Any]:
        """Convenience seam used by direct HTTP tests / scripts."""
        return self.post("/memory/recall", {"profile_id": profile_id, "query": query, **extra})
