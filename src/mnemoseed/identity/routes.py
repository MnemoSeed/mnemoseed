"""Identity REST surface (issue #14): the open setup + login routes.

Resides outside the require_identity gate so they answer in setup mode:

- POST   /api/v1/setup          exact-once owner creation (410 permanently after)
- GET    /api/v1/setup/status   setup mode probe for the console wizard/login
- POST   /api/v1/auth/login     password -> one-shot profile token
- GET    /api/v1/auth/me        identity of the presented token (gated)
- POST   /api/v1/auth/logout    revoke the presented token (gated)
"""

from __future__ import annotations

from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from mnemoseed.identity.gate import SETUP_POINTER, require_identity
from mnemoseed.identity.service import AuthIdentity, IdentityService, OwnerExistsError

router = APIRouter(prefix="/api/v1", tags=["identity"])


class SetupRequest(BaseModel):
    username: str = Field(min_length=1, pattern=r".*\S.*")
    password: str = Field(min_length=1, pattern=r".*\S.*")


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, pattern=r".*\S.*")
    password: str = Field(min_length=1, pattern=r".*\S.*")


def _service(request: Request) -> IdentityService:
    return cast(IdentityService, request.app.state.identity)


def _bearer_secret(request: Request) -> str:
    authorization = request.headers.get("authorization", "")
    return authorization[len("bearer ") :].strip() if authorization.lower().startswith("bearer ") else ""


@router.post("/setup", status_code=status.HTTP_201_CREATED)
def setup_owner(body: SetupRequest, request: Request) -> dict[str, Any]:
    """FR-6.1a: create the single owner exactly once; a repeated call is a
    permanent 410 (the endpoint stays closed once setup has run)."""
    identity = _service(request)
    if identity.owner_exists():
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="setup already completed: the setup endpoint is permanently closed",
        )
    try:
        owner = identity.setup_owner(body.username, body.password)
    except OwnerExistsError as exc:
        # A concurrent request already won the single-owner race: the same typed
        # 410 a late sequential setup gets, never a 500 / IntegrityError.
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="setup already completed: the setup endpoint is permanently closed",
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return {
        "username": owner.username,
        "profile_id": owner.profile_id,
        "role": owner.role,
        "setup_required": False,
    }


@router.get("/setup/status")
def setup_status(request: Request) -> dict[str, Any]:
    """Setup-mode probe used by the console to pick wizard vs login."""
    identity = _service(request)
    owner_exists = identity.owner_exists()
    return {"setup_required": not owner_exists, "owner_exists": owner_exists}


@router.post("/auth/login")
def login(body: LoginRequest, request: Request) -> dict[str, Any]:
    """Password login. Pre-setup this is 503 (nothing to log into yet);
    a wrong password is one identical 401 regardless of whether the user exists."""
    identity = _service(request)
    if not identity.owner_exists():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=SETUP_POINTER,
        )
    try:
        token = identity.authenticate(body.username, body.password)
    except Exception as exc:
        from mnemoseed.identity.service import InvalidCredentialsError

        if isinstance(exc, InvalidCredentialsError):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid username or password",
                headers={"WWW-Authenticate": "Bearer"},
            ) from exc
        raise
    return {
        "token": token.token_secret,
        "token_type": "bearer",
        "username": body.username,
        "profile_id": token.profile_id,
        "expires_at": token.expires_at,
    }


@router.get("/auth/me", dependencies=[Depends(require_identity)])
def me(request: Request) -> dict[str, Any]:
    """Resolve the presented token to the proven owner identity."""
    proven: AuthIdentity = request.state.identity
    return {
        "user_id": proven.user_id,
        "username": proven.username,
        "profile_id": proven.profile_id,
        "role": proven.role,
    }


@router.post("/auth/logout", dependencies=[Depends(require_identity)])
def logout(request: Request) -> dict[str, Any]:
    """Revoke the presented token (idempotent: a revoked token 401s on the gate,
    so this route only succeeds while the token is still live)."""
    identity = _service(request)
    secret = _bearer_secret(request)
    identity.revoke_presented(secret)
    return {"status": "logged_out"}
