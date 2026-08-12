"""IdentityService: owner setup, login, token validation, local password reset.

The open-source edition is hard-limited to one user (design/06 2.7): exactly one
owner account and one default profile. The service layer keeps the typed hard
limit (``OwnerExistsError``); the HTTP route surfaces it as a permanent 410 once
setup has run.

Token contract (FR-6.1b / PRD-06): ``issue_token`` returns a one-shot bearer
secret; only its sha256 digest is persisted in ``tokens.token_hash``. Every read
path except login returns the secret empty, so the value materializes exactly
once and is never written to disk or logs.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

from mnemoseed.identity.passwords import hash_password, verify_password
from mnemoseed.storage.ports import (
    AuditEntry,
    MetaStore,
    OwnerConflictError,
    StoredProfile,
    StoredUser,
    Token,
)

# The one profile every owner account owns (design/06: owner -> profiles ->
# agent tokens). The daemon never guesses identity (D5); profile_id stays the
# explicit per-request key, this constant only names the setup-created default.
DEFAULT_PROFILE_ID = "default"
_OWNER_ROLE = "owner"
# Profile tokens live 30 days; expiry/revocation are enforced on every read by
# the storage driver's authenticate_token.
TOKEN_TTL_SECONDS = 30 * 24 * 3600
# Every token issued by the owner login carries the same full-memory scope set.
# Scope enforcement is a later milestone; the vocabulary is reserved now.
_PROFILE_SCOPES = ("memory:read", "memory:write")


class OwnerExistsError(Exception):
    """A second owner account was attempted (single-user hard limit)."""


class InvalidCredentialsError(Exception):
    """Login with an unknown user or wrong password, or auth-reset with no owner."""


@dataclass(frozen=True)
class AuthIdentity:
    """The proven caller after token validation (attached to request.state)."""

    user_id: str
    username: str
    profile_id: str
    role: str


class IdentityService:
    """Persistence-backed identity operations over the MetaStore port."""

    def __init__(self, meta: MetaStore) -> None:
        self._meta = meta

    # ------------------------------------------------------------ queries

    def owner_exists(self) -> bool:
        return self._meta.count_users() > 0

    def _owner(self) -> StoredUser | None:
        for user in self._meta.list_users():
            if user.role == _OWNER_ROLE:
                return user
        return None

    # ------------------------------------------------------------ setup

    def setup_owner(self, username: str, password: str) -> AuthIdentity:
        """Create the single owner + default profile. Exact-once even under
        concurrent requests: the whole setup commits in one storage transaction
        (``MetaStore.create_owner`` does the check inside its write lock), so a
        losing caller gets the typed 409 (the route translates it to a permanent
        410) instead of a second owner row or a naked IntegrityError."""
        if not username or not password:
            raise ValueError("username and password are required")
        now = time.time()
        user_id = uuid.uuid4().hex
        user = StoredUser(
            user_id=user_id,
            username=username,
            password_hash=hash_password(password),
            role=_OWNER_ROLE,
            created_at=now,
        )
        profile = StoredProfile(profile_id=DEFAULT_PROFILE_ID, display_name=username, created_at=now)
        audit = AuditEntry(
            actor="setup",
            action="owner_created",
            detail={"username": username, "user_id": user_id},
            at=now,
        )
        try:
            self._meta.create_owner(user, profile, audit)
        except OwnerConflictError as exc:
            raise OwnerExistsError("owner account already exists: setup is permanently closed") from exc
        return AuthIdentity(
            user_id=user_id,
            username=username,
            profile_id=DEFAULT_PROFILE_ID,
            role=_OWNER_ROLE,
        )

    # ------------------------------------------------------------ authentication

    def authenticate(self, username: str, password: str) -> Token:
        """Verify credentials against the argon2 hash and issue a profile token.

        A failed attempt is audited and answered with one identical 401 for both
        an unknown user and a wrong password (no user-enumeration oracle).
        """
        user = self._meta.get_user_by_username(username)
        if user is None or not verify_password(password, user.password_hash):
            self._meta.audit_append(
                AuditEntry(
                    actor="auth",
                    action="login_failed",
                    detail={"username": username, "reason": "bad_credentials"},
                    at=time.time(),
                )
            )
            raise InvalidCredentialsError("invalid username or password")
        token = self._meta.issue_token(
            DEFAULT_PROFILE_ID,
            _PROFILE_SCOPES,
            expires_at=time.time() + TOKEN_TTL_SECONDS,
        )
        self._meta.audit_append(
            AuditEntry(
                actor=user.username,
                action="login_succeeded",
                detail={"user_id": user.user_id, "token_id": token.token_id},
                at=time.time(),
            )
        )
        return token

    def validate_token(self, secret: str) -> AuthIdentity | None:
        """Resolve a bearer secret to the proven owner identity, or None."""
        token = self._meta.authenticate_token(secret)
        if token is None:
            return None
        owner = self._owner()
        if owner is None:
            return None
        return AuthIdentity(
            user_id=owner.user_id,
            username=owner.username,
            profile_id=token.profile_id,
            role=owner.role,
        )

    def revoke_presented(self, secret: str) -> bool:
        """Revoke the token a client presented (logout). False when unknown."""
        token = self._meta.authenticate_token(secret)
        if token is None:
            return False
        self._meta.revoke_token(token.token_id)
        return True

    # ------------------------------------------------------------ local reset

    def set_owner_password(self, password: str) -> None:
        """Local-only password rotation (``mnemoseed auth reset``). Requires
        direct meta-store access — physical machine access is the authority."""
        if not password:
            raise ValueError("password is required")
        owner = self._owner()
        if owner is None:
            raise InvalidCredentialsError("no owner account exists")
        self._meta.update_user_password(owner.user_id, hash_password(password))
        self._meta.audit_append(
            AuditEntry(
                actor="auth-reset",
                action="password_reset",
                detail={"user_id": owner.user_id},
                at=time.time(),
            )
        )
