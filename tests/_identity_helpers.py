"""Shared identity bootstrap (issue #14): finish setup + attach the profile token.

Every pre-issue-#14 test suite drove the daemon's /memory and /api/v1 surfaces
without credentials (console admin token / loopback implicit trust). Once the
owner exists those surfaces require a Bearer profile token via the
``require_identity`` gate. This module gives those suites one helper: run the
exact POST /setup then /auth/login the setup wizard uses, and stamp the token
onto the client's default headers so the existing wire-level test bodies keep
asserting through the public HTTP surface rather than internal helpers.

These are test-only (fixture) credentials — fake values, never real secrets.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi.testclient import TestClient

OWNER_USERNAME = "owner"
OWNER_PASSWORD = "test-owner-password-never-real"


def setup_and_login(client: TestClient) -> str:
    """POST /api/v1/setup then /api/v1/auth/login; returns the profile token.

    A 410 from setup is tolerated (the daemon was already set up -- e.g. a
    second daemon boot over the same meta dir): the helper contract is "an
    authenticated client", so login alone then suffices. First-run exact-once
    behaviour is asserted by the identity tests that POST /setup directly.
    """
    response = client.post("/api/v1/setup", json={"username": OWNER_USERNAME, "password": OWNER_PASSWORD})
    assert response.status_code in (201, 410), response.text
    response = client.post(
        "/api/v1/auth/login", json={"username": OWNER_USERNAME, "password": OWNER_PASSWORD}
    )
    assert response.status_code == 200, response.text
    token = response.json().get("token")
    assert token and isinstance(token, str)
    return token


def attach_token(client: TestClient) -> str:
    """Inside a running TestClient: finish setup and stamp default auth headers."""
    token = setup_and_login(client)
    client.headers["Authorization"] = f"Bearer {token}"
    return token


def auth_headers(token: str) -> dict[str, str]:
    """Explicit Authorization headers for a token (non-default-header paths)."""
    return {"Authorization": f"Bearer {token}"}


def fresh_token(client: TestClient, username: str = OWNER_USERNAME, password: str = OWNER_PASSWORD) -> str:
    """Issue one more profile token (e.g. for an explicit-header request)."""
    response = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    token = response.json().get("token")
    assert token and isinstance(token, str)
    return token


def realistic_payload(profile_id: str = "default") -> dict[str, Any]:
    """A /memory/* payload the gate only needs identity for (seeded by stores)."""
    return {
        "profile_id": profile_id,
        "text": f"seed {time.time()}",
        "source": "test-seed",
    }
