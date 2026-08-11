"""Console auth gate (FR-7.1 / NFR-7.1): localhost implicit trust.

The console is local-first: a loopback request (``127.0.0.1`` / ``::1`` /
``localhost``) is implicitly trusted and needs no credential. A non-loopback
request must present the admin token: ``Authorization: Bearer <token>`` or
``X-Admin-Token: <token>``. The token value is read from the
``MNEMOSEED_CONSOLE_ADMIN_TOKEN`` environment variable; when none is configured
remote access is refused outright (NFR-7.1: remote access must be explicitly
enabled + an admin token).

This is a console-admin gate, distinct from the PRD-06 profile login tokens the
MCP client uses: the daemon validates no token today, so the console defines its
own minimal credential with the same "keys are attestations, values come from
the environment" rule the rest of the repo follows.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

from fastapi import HTTPException, Request, status
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.types import Receive, Scope, Send

ADMIN_TOKEN_ENV = "MNEMOSEED_CONSOLE_ADMIN_TOKEN"

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

AUTHENTICATION_HEADERS = {"WWW-Authenticate": "Bearer"}


def is_loopback(host: str | None) -> bool:
    """True for any IPv4/IPv6 loopback representation, else False."""
    if host is None:
        return False
    if host in _LOOPBACK_HOSTS:
        return True
    return host.startswith("127.") or host in ("::ffff:127.0.0.1", "::1")


def admin_token() -> str:
    """The configured console admin token (empty = remote access disabled)."""
    return os.environ.get(ADMIN_TOKEN_ENV, "")


def check_authorized(request: Request) -> bool:
    """Loopback passes; a non-loopback request needs the matching admin token."""
    client = request.client
    host = client.host if client is not None else None
    if is_loopback(host):
        return True
    expected = admin_token()
    if not expected:
        return False
    supplied: str | None = None
    authorization = request.headers.get("authorization")
    if authorization and authorization.lower().startswith("bearer "):
        supplied = authorization[len("bearer ") :]
    if supplied is None:
        supplied = request.headers.get("x-admin-token")
    if supplied is None:
        return False
    return secrets.compare_digest(supplied, expected)


async def require_console_auth(request: Request) -> None:
    """FastAPI dependency guarding every console route."""
    if not check_authorized(request):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="console admin token required for non-localhost access",
            headers=AUTHENTICATION_HEADERS,
        )


class GuardedStaticFiles:
    """An ASGI app wrapping StaticFiles behind the console auth gate, for the
    /console mount. Implements the ASGI callable directly (no adapter) so
    Starlette's ``mount`` accepts it as a first-class application."""

    def __init__(self, directory: Path) -> None:
        self._inner = StaticFiles(directory=directory, html=True)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not check_authorized(Request(scope)):
            response = JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={"detail": "console admin token required for non-localhost access"},
                headers=AUTHENTICATION_HEADERS,
            )
            await response(scope, receive, send)
            return
        await self._inner(scope, receive, send)
