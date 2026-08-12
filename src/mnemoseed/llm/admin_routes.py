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

Credential hygiene is the router's hard rule: responses carry env-var NAMES, a
literal key value anywhere is a failure of every test on this surface.
"""

from __future__ import annotations

from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from mnemoseed.identity.gate import require_identity
from mnemoseed.llm.admin import LLMAdminError, LLMAdminService

router = APIRouter(
    prefix="/api/v1",
    tags=["llm"],
    dependencies=[Depends(require_identity)],
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
    before a change is persisted (FR-6.9)."""

    model_config = {"extra": "forbid"}

    role: str
    driver: str
    model: str
    base_url: str = ""
    api_key_env: str | None = None
    provider: str | None = None


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
    try:
        return _service(request).set_role(
            role,
            driver=body.driver,
            model=body.model,
            base_url=body.base_url,
            api_key_env=body.api_key_env,
            provider=body.provider,
        )
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
