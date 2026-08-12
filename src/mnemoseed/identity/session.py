"""Client-side bearer session persistence (issue #14).

``mnemoseed login`` writes the one-shot profile token to the config dir so the
CLI can attach it as ``Authorization: Bearer`` on later calls. The token is a
bearer secret: the file is written with owner-only permissions, is never
logged, and is deleted by ``mnemoseed logout`` (which also revokes it
server-side). ``MNEMOSEED_TOKEN`` intentionally overrides the file in MCP/hook
child processes so a daemon-hosted agent never needs to read the file.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from mnemoseed.config import CONFIG_DIR

#: Env var that supplies the bearer token to CLI/MCP/hook processes.
MNEMOSEED_TOKEN = "MNEMOSEED_TOKEN"

#: Session file name, resolved under the config dir (~/.mnemoseed).
TOKEN_FILE_NAME = "token.json"
TOKEN_PATH = CONFIG_DIR / TOKEN_FILE_NAME

#: Only the owning user may read the persisted token (0600).
_SESSION_MODE = 0o600


@dataclass(frozen=True)
class AuthSession:
    """The persisted login session (exactly what ``login`` records)."""

    base_url: str
    username: str
    profile_id: str
    token: str
    expires_at: float | None = None


def session_path(path: Path | None = None) -> Path:
    """The session file path, honoring env overrides for isolated tests."""
    return path or TOKEN_PATH


def load_session(path: Path | None = None) -> AuthSession | None:
    """Read a stored session, or None when missing/corrupt (never raises)."""
    target = session_path(path)
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    token = raw.get("token")
    if not isinstance(token, str) or not token:
        return None
    base_url = raw.get("base_url")
    username = raw.get("username")
    profile_id = raw.get("profile_id")
    if not all(isinstance(v, str) and v for v in (base_url, username, profile_id)):
        return None
    expires_at = raw.get("expires_at")
    if expires_at is not None and not isinstance(expires_at, (int, float)):
        return None
    return AuthSession(
        base_url=base_url,
        username=username,
        profile_id=profile_id,
        token=token,
        expires_at=float(expires_at) if expires_at is not None else None,
    )


def save_session(session: AuthSession, path: Path | None = None) -> Path:
    """Persist a session with owner-only permissions; returns the file path."""
    target = session_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _SESSION_MODE)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(_serializable(asdict(session)), handle, sort_keys=True)
    try:
        os.chmod(target, _SESSION_MODE)
    except OSError:  # Windows: best-effort; os.open already narrowed the mode
        pass
    return target


def delete_session(path: Path | None = None) -> bool:
    """Remove a stored session file; False when none existed."""
    target = session_path(path)
    try:
        target.unlink()
    except FileNotFoundError:
        return False
    return True


def file_perms(path: Path) -> int:
    """The owner-read/write bits of a session file (test seam)."""
    return stat.S_IMODE(path.stat().st_mode) & 0o777


def bearer_headers(token: str) -> dict[str, str]:
    """Headers attaching a bearer profile token to an HTTP call."""
    return {"Authorization": f"Bearer {token}"}


def resolve_token(explicit: str | None = None) -> str | None:
    """Token resolution order: explicit arg, env, stored session file."""
    if explicit and explicit.strip():
        return explicit.strip()
    from_env = os.environ.get(MNEMOSEED_TOKEN)
    if from_env and from_env.strip():
        return from_env.strip()
    session = load_session()
    return session.token if session is not None else None


def _serializable(data: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in data.items() if value is not None}
