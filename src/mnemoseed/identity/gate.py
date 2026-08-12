"""Auth enforcement seam (issue #14): the require_identity dependency.

Applied to every /memory/* and /api/v1/* read/write surface (not the public
/healthz|/health|/capabilities liveness routes, and not /ingest which stays the
hook-facing open capture path).

State machine:
- ``app.state.identity is None`` (a bare app that never ran lifespan) -> allow.
  This is the unit-test seam; the real daemon always installs an IdentityService
  in lifespan, so a served request can never take this branch.
- No owner account yet -> 503 with a setup pointer. Only the setup wizard and
  the login routes (which live outside this dependency) answer in setup mode.
- Owner exists -> a valid Bearer profile token is required. Loopback carries no
  implicit trust once setup has run (issue #14); the console resolves identity
  through the same login route a remote client uses.

The token is parsed from the ``Authorization: Bearer <secret>`` header directly
(never via ``HTTPBearer``): a security-scheme dependency declared at the router
level makes FastAPI mis-embed endpoint request-body models (the /memory and
/api/v1 POST surfaces all carry pydantic bodies), so this dependency stays a
plain header read.
"""

from __future__ import annotations

from typing import cast

from fastapi import HTTPException, Request, status
from starlette.datastructures import Headers

from mnemoseed.identity.service import AuthIdentity, IdentityService

#: 503 payload body ("setup pointer"): tells an agent/hook consumer the daemon is
#: in setup mode and where to finish first-run registration.
SETUP_POINTER = {
    "detail": "setup required: no owner account exists yet",
    "setup_url": "/console/#/setup",
}


def _identity(request: Request) -> IdentityService | None:
    value = getattr(request.app.state, "identity", None)
    if value is None:
        return None
    return cast(IdentityService, value)


def _credentials_to_secret(headers: Headers) -> str | None:
    authorization = headers.get("authorization")
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    value = authorization[len("bearer ") :].strip()
    return value or None


def require_identity(request: Request) -> AuthIdentity | None:
    """FastAPI dependency guarding every memory/console route.

    Attaches the proven owner identity to ``request.state.identity`` and returns
    it (endpoints may declare the dependency to receive it directly).
    """
    identity = _identity(request)
    if identity is None:
        # Fake-lifespan / construction-time seam: no identity service yet.
        return None
    if not identity.owner_exists():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=SETUP_POINTER,
        )
    secret = _credentials_to_secret(request.headers)
    if secret is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="profile token required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    identity_ = identity.validate_token(secret)
    if identity_ is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or expired profile token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    request.state.identity = identity_
    return identity_
