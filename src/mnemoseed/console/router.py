"""Console REST router (PRD-07 T1): /api/v1, read-only-first + M1 writes.

Every route sits behind the console auth gate; validation lives on the Query
bounds (422 code covers a malformed filter), and a missing/foreign-profile
memory target becomes a typed 404. The service methods this router calls are
the whole surface -- no storage port is touched from the ASGI layer.
"""

from __future__ import annotations

from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from mnemoseed.console.auth import require_console_auth
from mnemoseed.console.service import ConsoleNotFoundError, ConsoleService
from mnemoseed.schema.graph import NodeType

router = APIRouter(
    prefix="/api/v1",
    tags=["console"],
    dependencies=[Depends(require_console_auth)],
)


def _service(request: Request) -> ConsoleService:
    return cast(ConsoleService, request.app.state.console)


# ---------------------------------------------------------------- dashboard


@router.get("/status")
def status(request: Request) -> dict[str, Any]:
    """FR-7.2: daemon health, driver backends, per-profile dream/pool/tokens."""
    return _service(request).status()


# ---------------------------------------------------------------- memory browse


@router.get("/chunks")
def list_chunks(
    request: Request,
    profile_id: str = Query(..., min_length=1),
    time_after: float | None = Query(default=None),
    time_before: float | None = Query(default=None),
    project: str | None = Query(default=None, min_length=1),
    host: str | None = Query(default=None, min_length=1),
    entity: Annotated[list[str] | None, Query()] = None,
    tier: int | None = Query(default=None, ge=1, le=3),
    min_decay: float = Query(default=0.0, ge=0.0, le=1.0),
    max_decay: float | None = Query(default=None, ge=0.0, le=1.0),
    consolidated: bool | None = Query(default=None),
    needs_reconcile: bool | None = Query(default=None),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=500),
) -> dict[str, Any]:
    """FR-7.4 short-term memory page (newest first)."""
    return _service(request).list_chunks(
        profile_id=profile_id,
        time_after=time_after,
        time_before=time_before,
        project=project,
        host=host,
        entity=tuple(entity) if entity else (),
        tier=tier,
        min_decay=min_decay,
        max_decay=max_decay,
        consolidated=consolidated,
        needs_reconcile=needs_reconcile,
        offset=offset,
        limit=limit,
    )


@router.get("/chunks/{chunk_id}")
def get_chunk(request: Request, chunk_id: str, profile_id: str = Query(..., min_length=1)) -> dict[str, Any]:
    """FR-7.5 chunk dossier (verbatim channel, provenance history, weights)."""
    try:
        return _service(request).get_chunk(profile_id=profile_id, chunk_id=chunk_id)
    except ConsoleNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/nodes")
def list_nodes(
    request: Request,
    profile_id: str = Query(..., min_length=1),
    node_type: Annotated[NodeType | None, Query()] = None,
    entity: Annotated[list[str] | None, Query()] = None,
    min_decay: float = Query(default=0.0, ge=0.0, le=1.0),
    max_decay: float | None = Query(default=None, ge=0.0, le=1.0),
    tier: int | None = Query(default=None, ge=1, le=3),
    updated_after: float | None = Query(default=None),
    updated_before: float | None = Query(default=None),
    needs_reconcile: bool | None = Query(default=None),
    pending_consolidation: bool | None = Query(default=None),
    conflict: bool | None = Query(default=None),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=500),
) -> dict[str, Any]:
    """FR-7.4 long-term memory page (node type / tier / decay range)."""
    return _service(request).list_nodes(
        profile_id=profile_id,
        node_type=node_type,
        entity=tuple(entity) if entity else (),
        min_decay=min_decay,
        max_decay=max_decay,
        tier=tier,
        updated_after=updated_after,
        updated_before=updated_before,
        needs_reconcile=needs_reconcile,
        pending_consolidation=pending_consolidation,
        conflict=conflict,
        offset=offset,
        limit=limit,
    )


@router.get("/nodes/{node_id}")
def get_node(request: Request, node_id: str, profile_id: str = Query(..., min_length=1)) -> dict[str, Any]:
    """FR-7.5 node dossier (triple, version chain, weights, flags, usage)."""
    try:
        return _service(request).get_node(profile_id=profile_id, node_id=node_id)
    except ConsoleNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# ---------------------------------------------------------------- dream panel


@router.get("/dream/status")
def dream_status(request: Request, profile_id: str = Query(..., min_length=1)) -> dict[str, Any]:
    """FR-7.6 trigger state + pending queue for one profile."""
    return _service(request).dream_status(profile_id)


@router.get("/dream/runs")
def dream_runs(
    request: Request,
    since: float | None = Query(default=None),
    until: float | None = Query(default=None),
    interrupted: bool | None = Query(default=None),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=500),
) -> dict[str, Any]:
    """FR-7.6 run history (global -- the run journal has no profile column)."""
    return _service(request).dream_runs(
        since=since,
        until=until,
        interrupted=interrupted,
        offset=offset,
        limit=limit,
    )


# ---------------------------------------------------------------- dream writes


@router.post("/dream/once")
def dream_once(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    """FR-7.6 manual dream trigger, audited."""
    profile_id = body.get("profile_id")
    if not isinstance(profile_id, str) or not profile_id:
        raise HTTPException(status_code=422, detail="body.profile_id must be a non-empty string")
    return _service(request).dream_once(profile_id)


@router.post("/dream/auto_trigger")
def dream_auto_trigger(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    """FR-7.6 auto-trigger toggle (persisted to the config file), audited."""
    enabled = body.get("enabled")
    if not isinstance(enabled, bool):
        raise HTTPException(status_code=422, detail="body.enabled must be a boolean")
    return _service(request).set_auto_trigger(enabled)
