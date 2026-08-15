"""LLM admin REST router (issue #23; FR-6.9 wizard + design/07 section 8).

The console Models & Routing page and the dataset dream-model setup step both
talk HTTP (the CLI reuses the same service offline); every route sits behind the
identity gate like the console surface:

- GET    /api/v1/llm/routes             per-role route (env-var NAMES only) +
                                        cached connectivity + driver catalog.
- GET    /api/v1/llm/oauth-availability Codex / Grok host-login presence +
                                        expiry for the wizard's quick options.
- POST   /api/v1/llm/routes/{role}      validate + persist a route change to the
                                        config TOML (surgical patch), audited;
                                        typed LLMAdminError -> 422.
- POST   /api/v1/llm/test               run a proposed route's check() so the
                                        console test buttons probe before writing
                                        anything (FR-6.9); a failed probe is a
                                        typed result, never a 422.
- POST   /api/v1/llm/key                 {role, key} -> store the key under the
                                        config dir + pin the ``secrets:``
                                        reference (versioned + audited); answers
                                        {ok, masked_tail, restart_required:false}.
- DELETE /api/v1/llm/key                 {role} -> remove the stored key + clear
                                        the reference (the role falls back to
                                        the env chain). Loopback-only like the
                                        config writes.

Credential hygiene is the router's hard rule: responses carry env-var NAMES or
``secrets:`` references, a literal key value anywhere is a failure of every
test on this surface.
"""

from __future__ import annotations

from typing import Any, cast
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from mnemoseed.config import Config
from mnemoseed.identity.actor import resolve_actor
from mnemoseed.identity.gate import require_identity
from mnemoseed.llm.admin import LLMAdminError, LLMAdminService, LLMTestRequiredError

router = APIRouter(
    prefix="/api/v1",
    tags=["llm"],
    dependencies=[Depends(require_identity)],
)

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def _is_loopback(host: str | None) -> bool:
    """Loopback hosts, including IPv4-mapped IPv6 (the local client's address)."""
    if host is None:
        return False
    return host in _LOOPBACK_HOSTS or host.startswith("127.")


def _reject_remote_writes(request: Request) -> None:
    """Key writes are loopback-only: a remote baseurl is refused (403), the
    same gate config writes run behind."""
    config = cast(Config, request.app.state.config)
    host = urlparse(config.baseurl).hostname
    if not _is_loopback(host):
        raise HTTPException(
            status_code=403,
            detail="key writes are rejected when the daemon baseurl is non-loopback",
        )


class RouteUpdateRequest(BaseModel):
    """One role-route update: None keeps the current value, "" clears an optional
    param, and a non-empty driver/model cannot be cleared (validated in the
    service, which returns the same typed error the CLI prints)."""

    model_config = {"extra": "forbid"}

    driver: str | None = None
    model: str | None = None
    base_url: str | None = None
    api_key_env: str | None = None
    provider: str | None = None


class RouteTestRequest(BaseModel):
    """A proposed route to probe -- never written, so the test buttons answer
    before a change is persisted (FR-6.9). Omitted fields are merged against
    the current route server-side, so a partial probe arms the exact signature
    a partial persist will be checked against."""

    model_config = {"extra": "forbid"}

    role: str
    driver: str | None = None
    model: str | None = None
    base_url: str | None = None
    api_key_env: str | None = None
    provider: str | None = None


class KeySetRequest(BaseModel):
    """T2-3: one role's API key VALUE to store. The value is consumed
    server-side only: it is never echoed, logged, or persisted anywhere but
    the restricted secrets directory."""

    model_config = {"extra": "forbid"}

    role: str
    key: str


class KeyDeleteRequest(BaseModel):
    """T2-3: the role whose stored key + reference should be removed."""

    model_config = {"extra": "forbid"}

    role: str


def _service(request: Request) -> LLMAdminService:
    return cast(LLMAdminService, request.app.state.llm_admin)


@router.get("/llm/routes")
def llm_routes(request: Request) -> dict[str, Any]:
    """FR-6.9: route table + connectivity + driver catalog for the console page."""
    return _service(request).routes()


@router.get("/llm/oauth-availability")
def llm_oauth_availability(request: Request) -> dict[str, Any]:
    """FR-6.9: host Codex / Grok login state (presence + expiry, no token values)."""
    return _service(request).oauth_availability()


@router.post("/llm/routes/{role}")
def llm_set_role(request: Request, role: str, body: RouteUpdateRequest) -> dict[str, Any]:
    """FR-6.9: validate + persist one role-route change (audited, surgical TOML)."""
    actor = resolve_actor(request)
    try:
        return _service(request).set_role(
            role,
            driver=body.driver,
            model=body.model,
            base_url=body.base_url,
            api_key_env=body.api_key_env,
            provider=body.provider,
            actor=actor,
        )
    except LLMTestRequiredError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except LLMAdminError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/llm/test")
def llm_test_route(request: Request, body: RouteTestRequest) -> dict[str, Any]:
    """FR-6.9: probe a proposed route's connectivity; a failure is typed inline."""
    report = _service(request).test_config(
        role=body.role,
        driver=body.driver,
        model=body.model,
        base_url=body.base_url,
        api_key_env=body.api_key_env,
        provider=body.provider,
    )
    return {
        "role": body.role,
        "driver": body.driver,
        "model": body.model,
        "ok": report.ok,
        "detail": report.detail,
    }


@router.post("/llm/key")
def llm_set_key(request: Request, body: KeySetRequest) -> dict[str, Any]:
    """T2-3: store one role's key + pin the secrets: reference (audited,
    versioned, hot-applied to the next dream run; the value never leaves the
    restricted secrets directory)."""
    actor = resolve_actor(request)
    _reject_remote_writes(request)
    try:
        return _service(request).set_key(body.role, body.key, actor=actor)
    except LLMAdminError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.delete("/llm/key")
def llm_delete_key(request: Request, body: KeyDeleteRequest) -> dict[str, Any]:
    """T2-3: remove a stored key + clear the reference; the role falls back
    to its env-var chain."""
    actor = resolve_actor(request)
    _reject_remote_writes(request)
    try:
        return _service(request).delete_key(body.role, actor=actor)
    except LLMAdminError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
