"""Identity chain (issue #14): first-run setup wizard + owner account + tokens.

Owns the owner model, password hashing, and the auth gate that every memory and
console surface depends on:

- ``passwords``   — argon2id hash/verify (FR-6.1e: never a plaintext at rest).
- ``service``     — IdentityService: setup-owner (typed 409 on a second owner),
                    login, token validation, local password rotation.
- ``gate``        — require_identity FastAPI dependency: 503 setup pointer
                    pre-setup, 401 without a valid profile token post-setup.
- ``routes``      — the open /api/v1/setup + /api/v1/auth surface.
"""

from mnemoseed.identity.service import (
    AuthIdentity,
    IdentityService,
    InvalidCredentialsError,
    OwnerExistsError,
)

__all__ = [
    "AuthIdentity",
    "IdentityService",
    "InvalidCredentialsError",
    "OwnerExistsError",
]
