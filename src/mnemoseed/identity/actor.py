"""Audit actor attribution shared by every write surface (design/07 5).

Each REST router resolves the surface actor from the ``X-MnemoSeed-Actor``
header (cli | console | mcp) and threads it into the service layer so no
service ever reads the header itself — the HTTP layer supplies the value. The
header is optional: an untagged request (the console UI's own calls, which
ride the same origin) defaults to ``console``.

This is the reference implementation configwrite originally inlined; keeping it
shared prevents the per-router copies from drifting.
"""

from __future__ import annotations

from fastapi import HTTPException, Request

ACTORS: frozenset[str] = frozenset({"cli", "console", "mcp"})
ACTOR_HEADER = "X-MnemoSeed-Actor"
DEFAULT_ACTOR = "console"


def resolve_actor(request: Request) -> str:
    """The surface actor from the header, else the console default.

    An unrecognised value is a typed 422 so a typo'd client cannot masquerade
    as another surface or silently fall through to a wrong attribution.
    """
    raw = request.headers.get(ACTOR_HEADER)
    if raw is None:
        return DEFAULT_ACTOR
    value = raw.strip()
    if value not in ACTORS:
        raise HTTPException(
            status_code=422,
            detail=f"invalid {ACTOR_HEADER}: {value!r} (expected one of: {', '.join(sorted(ACTORS))})",
        )
    return value
