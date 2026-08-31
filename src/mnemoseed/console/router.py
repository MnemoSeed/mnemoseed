"""Console REST router (PRD-07): /api/v1 reads + FR-7.9 write ops.

Every route sits behind the console auth gate; validation lives on the Query
bounds (422 code covers a malformed filter), and a missing/foreign-profile
memory target becomes a typed 404. The service methods this router calls are
the whole surface -- no storage port is touched from the ASGI layer. Writes
(forget / pin / weight adjust / profiles / tokens) flow through the audit
trail via the service.
"""

from __future__ import annotations

from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from mnemoseed.console.service import (
    CONFLICT_BRANCHES,
    REVIEW_ROUTES,
    REVIEW_VERDICTS,
    ConsoleNotFoundError,
    ConsoleService,
)
from mnemoseed.identity.actor import resolve_actor
from mnemoseed.identity.gate import require_identity
from mnemoseed.schema.graph import NodeType

# Identity gate (issue #14): /api/v1 was previously behind the console admin
# token (localhost implicit trust); it now runs the shared profile-token gate —
# 503 with a setup pointer until the owner exists, then Bearer profile token
# (auth ends loopback implicit trust once setup has run). The console admin
# token still guards only the /console static mount (console/auth.py).
router = APIRouter(
    prefix="/api/v1",
    tags=["console"],
    dependencies=[Depends(require_identity)],
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


@router.get("/graph/subgraph")
def graph_subgraph(
    request: Request,
    profile_id: str = Query(..., min_length=1),
    node_type: Annotated[list[NodeType] | None, Query()] = None,
    time_after: float | None = Query(default=None),
    time_before: float | None = Query(default=None),
    tier: int | None = Query(default=None, ge=1, le=3),
    min_weight: float = Query(default=0.0, ge=0.0, le=1.0),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=2000, ge=1, le=10_000),
) -> dict[str, Any]:
    """FR-7.8 Graph View subgraph: current nodes + one paginated edge page
    (bulk ``list_edges`` when the driver declares GRAPH_EDGE_LIST; otherwise
    the explicit appendix-C per-node adjacency degrade)."""
    return _service(request).graph_subgraph(
        profile_id=profile_id,
        node_types=tuple(node_type) if node_type else (),
        time_after=time_after,
        time_before=time_before,
        tier=tier,
        min_weight=min_weight,
        offset=offset,
        limit=limit,
    )


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
    actor = resolve_actor(request)
    profile_id = body.get("profile_id")
    if not isinstance(profile_id, str) or not profile_id:
        raise HTTPException(status_code=422, detail="body.profile_id must be a non-empty string")
    return _service(request).dream_once(profile_id, actor=actor)


@router.post("/dream/auto_trigger")
def dream_auto_trigger(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    """FR-7.6 auto-trigger toggle (persisted to the config file), audited."""
    actor = resolve_actor(request)
    enabled = body.get("enabled")
    if not isinstance(enabled, bool):
        raise HTTPException(status_code=422, detail="body.enabled must be a boolean")
    return _service(request).set_auto_trigger(enabled, actor=actor)


# ---------------------------------------------------------------- dream review (FR-7.6)


@router.get("/dream/review/{run_id}")
def dream_review(request: Request, run_id: str, profile_id: str = Query(..., min_length=1)) -> dict[str, Any]:
    """FR-7.6 quality review: one run's triples with their source chunks
    (diff-style pairing) and any already-recorded verdicts."""
    try:
        return _service(request).dream_review(run_id=run_id, profile_id=profile_id)
    except ConsoleNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _string_field(body: dict[str, Any], name: str) -> str:
    value = body.get(name)
    if not isinstance(value, str) or not value:
        raise HTTPException(status_code=422, detail=f"body.{name} must be a non-empty string")
    return value


@router.post("/dream/review")
def dream_review_verdict(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    """FR-7.6 review write: per-triple accept/reject/hallucination verdict,
    audit-logged and idempotent."""
    run_id = _string_field(body, "run_id")
    profile_id = _string_field(body, "profile_id")
    subject = _string_field(body, "subject")
    predicate = _string_field(body, "predicate")
    obj = _string_field(body, "object")
    route = _string_field(body, "route")
    if route not in REVIEW_ROUTES:
        raise HTTPException(status_code=422, detail=f"body.route must be one of {sorted(REVIEW_ROUTES)}")
    verdict = _string_field(body, "verdict")
    if verdict not in REVIEW_VERDICTS:
        raise HTTPException(status_code=422, detail=f"body.verdict must be one of {sorted(REVIEW_VERDICTS)}")
    actor = resolve_actor(request)
    try:
        return _service(request).dream_review_verdict(
            run_id=run_id,
            profile_id=profile_id,
            subject=subject,
            predicate=predicate,
            obj=obj,
            route=route,
            verdict=verdict,
            actor=actor,
        )
    except ConsoleNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# ---------------------------------------------------------------- conflicts inbox (FR-7.7)


@router.get("/conflicts")
def list_conflicts(request: Request, profile_id: str = Query(..., min_length=1)) -> dict[str, Any]:
    """FR-7.7 inbox: flag_conflict pairs, both sides with provenance and cues."""
    return _service(request).list_conflicts(profile_id=profile_id)


@router.post("/conflicts/{group_id}/resolve")
def resolve_conflict(request: Request, group_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """FR-7.7 resolution: reinforce / coexist / invalidate / pending, written
    back to the version chain + audit. Idempotent on re-submit."""
    profile_id = _string_field(body, "profile_id")
    branch = _string_field(body, "branch")
    if branch not in CONFLICT_BRANCHES:
        raise HTTPException(status_code=422, detail=f"body.branch must be one of {sorted(CONFLICT_BRANCHES)}")
    node_id = body.get("node_id")
    if node_id is not None and (not isinstance(node_id, str) or not node_id):
        raise HTTPException(status_code=422, detail="body.node_id must be a non-empty string")
    scope = body.get("scope")
    if scope is not None and not isinstance(scope, str):
        raise HTTPException(status_code=422, detail="body.scope must be a string")
    service = _service(request)
    if branch in ("reinforce", "invalidate") and (node_id is None or not node_id):
        raise HTTPException(status_code=422, detail=f"body.node_id is required for branch {branch!r}")
    if branch == "coexist" and (scope is None or not scope.strip()):
        raise HTTPException(status_code=422, detail="body.scope is required for branch 'coexist'")
    actor = resolve_actor(request)
    try:
        return service.resolve_conflict(
            group_id=group_id,
            profile_id=profile_id,
            branch=branch,
            node_id=node_id,
            scope=scope,
            actor=actor,
        )
    except ConsoleNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# ---------------------------------------------------------------- memory writes (FR-7.9)


@router.post("/forget")
def forget(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    """FR-7.9 forget: chunk / node / entity erasure, mirroring the daemon's
    forget_this semantics (chunks deleted, nodes tombstoned), audit-logged."""
    profile_id = _string_field(body, "profile_id")
    targets = [key for key in ("chunk_id", "node_id", "entity") if body.get(key) is not None]
    if len(targets) != 1:
        raise HTTPException(
            status_code=422,
            detail="body must contain exactly one of chunk_id, node_id, entity",
        )
    for key in targets:
        value = body.get(key)
        if not isinstance(value, str) or not value:
            raise HTTPException(status_code=422, detail=f"body.{key} must be a non-empty string")
    actor = resolve_actor(request)
    try:
        return _service(request).forget(
            profile_id=profile_id,
            chunk_id=body.get("chunk_id"),
            node_id=body.get("node_id"),
            entity=body.get("entity"),
            actor=actor,
        )
    except ConsoleNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/pin")
def pin(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    """FR-7.9 manual pin: flip a node's never_decay as a version-chain append,
    audit-logged and idempotent."""
    profile_id = _string_field(body, "profile_id")
    node_id = _string_field(body, "node_id")
    pinned = body.get("pinned")
    if not isinstance(pinned, bool):
        raise HTTPException(status_code=422, detail="body.pinned must be a boolean")
    actor = resolve_actor(request)
    try:
        return _service(request).pin_node(profile_id=profile_id, node_id=node_id, pinned=pinned, actor=actor)
    except ConsoleNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/weights")
def adjust_weight(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    """FR-7.9 manual decay-weight adjust for a node or chunk, bounded to
    [0.0, 1.0] and audited with the old + new values."""
    profile_id = _string_field(body, "profile_id")
    kind = _string_field(body, "kind")
    if kind not in ("node", "chunk"):
        raise HTTPException(status_code=422, detail="body.kind must be 'node' or 'chunk'")
    target_id = _string_field(body, "target_id")
    weight = body.get("decay_weight")
    if not isinstance(weight, (int, float)) or isinstance(weight, bool):
        raise HTTPException(status_code=422, detail="body.decay_weight must be a number")
    weight = float(weight)
    if not 0.0 <= weight <= 1.0:
        raise HTTPException(status_code=422, detail="body.decay_weight must be within [0.0, 1.0]")
    actor = resolve_actor(request)
    try:
        return _service(request).adjust_weight(
            profile_id=profile_id,
            kind=kind,
            target_id=target_id,
            decay_weight=weight,
            actor=actor,
        )
    except ConsoleNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# ---------------------------------------------------------------- profiles (FR-7.3)


@router.post("/profiles")
def create_profile(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    """FR-7.3 console profile create (upsert), audit-logged."""
    profile_id = _string_field(body, "profile_id")
    display_name = body.get("display_name")
    if display_name is None:
        display_name = ""
    elif not isinstance(display_name, str):
        raise HTTPException(status_code=422, detail="body.display_name must be a string")
    return _service(request).create_profile(
        profile_id=profile_id, display_name=display_name, actor=resolve_actor(request)
    )


@router.post("/profiles/{profile_id}/rename")
def rename_profile(request: Request, profile_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """FR-7.3 console profile rename (display_name-only upsert), audit-logged."""
    display_name = _string_field(body, "display_name")
    actor = resolve_actor(request)
    try:
        return _service(request).rename_profile(profile_id=profile_id, display_name=display_name, actor=actor)
    except ConsoleNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/profiles/{profile_id}/archive")
def archive_profile(request: Request, profile_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """FR-7.3 console profile archive flag (reversible), audit-logged."""
    archived = body.get("archived")
    if not isinstance(archived, bool):
        raise HTTPException(status_code=422, detail="body.archived must be a boolean")
    actor = resolve_actor(request)
    try:
        return _service(request).archive_profile(profile_id=profile_id, archived=archived, actor=actor)
    except ConsoleNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/profiles/{profile_id}/tokens")
def issue_token(request: Request, profile_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """FR-7.3 console token issue: the bearer secret is returned exactly once
    and never written to the audit trail."""
    scopes = body.get("scopes")
    if scopes is None:
        scopes = ()
    elif not isinstance(scopes, list) or not all(isinstance(s, str) and s for s in scopes):
        raise HTTPException(status_code=422, detail="body.scopes must be a list of non-empty strings")
    expires_at = body.get("expires_at")
    if expires_at is not None and not isinstance(expires_at, (int, float)):
        raise HTTPException(status_code=422, detail="body.expires_at must be a number")
    actor = resolve_actor(request)
    try:
        return _service(request).issue_token(
            profile_id=profile_id,
            scopes=tuple(scopes),
            expires_at=float(expires_at) if expires_at is not None else None,
            actor=actor,
        )
    except ConsoleNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/tokens/{token_id}/revoke")
def revoke_token(request: Request, token_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """FR-7.3 console token revoke (idempotent), audit-logged."""
    del body
    return _service(request).revoke_token(token_id=token_id, actor=resolve_actor(request))


# ---------------------------------------------------------------- audit (FR-7.9 / G-AC1)


@router.get("/audit")
def audit_log(
    request: Request,
    actor: str | None = Query(default=None, min_length=1),
    action: str | None = Query(default=None, min_length=1),
    since: float | None = Query(default=None),
    until: float | None = Query(default=None),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=500),
) -> dict[str, Any]:
    """FR-7.9 audit view: paginated append-only trail, filterable by actor /
    action / time window, ascending (chronological, id-ordered)."""
    return _service(request).audit_log(
        actor=actor,
        action=action,
        since=since,
        until=until,
        offset=offset,
        limit=limit,
    )
