"""SecretStore port + FileSecretStore backend (T2-1).

The port is deliberately tiny: ``get`` / ``set`` / ``delete`` / ``exists`` /
``masked_tail``. Consumers (the role router, the admin key endpoints) never
see a value beyond the last four characters, so a response or audit payload
cannot leak a whole secret through the public surface.

The file backend stores one secret per name as ``<CONFIG_DIR>/secrets/
<sanitized>.key`` where the name is sanitized to ``[a-z0-9_.-]`` (the
``/`` in ``mnemoseed/dream/<role>`` becomes ``.``). Writes are atomic
(tmp + replace) so a crash never leaves a torn secret. Permissions follow the
user-profile boundary: POSIX gets an explicit 0700 directory + 0600 files;
on Windows the user-profile ACL is the enforcement boundary and no chmod is
attempted. Values are never logged anywhere in this module.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Protocol, runtime_checkable

#: The subdirectory under the config home that holds one file per secret.
SECRETS_DIR_NAME = "secrets"

#: Per-secret file suffix.
SECRET_FILE_SUFFIX = ".key"

#: The one-time write suffix (atomic replace target).
_TMP_SUFFIX = ".tmp"


class SecretsError(Exception):
    """Typed failure on the secret store surface."""


@runtime_checkable
class SecretStore(Protocol):
    """The key-custody port (T2-1): name-addressed secret values."""

    def get(self, name: str) -> str | None: ...
    def set(self, name: str, value: str) -> None: ...
    def delete(self, name: str) -> None: ...
    def exists(self, name: str) -> bool: ...
    def masked_tail(self, name: str) -> str | None: ...


def sanitize_name(name: str) -> str:
    """Map a secret name to a safe filename stem: ``[a-z0-9_.-]`` only, with
    any other character (e.g. the ``/`` in ``mnemoseed/dream/<role>``) mapped
    to ``.``. The result never contains a path separator."""
    return re.sub(r"[^a-z0-9_.-]", ".", name.lower())


class FileSecretStore:
    """One file per name under ``<directory>/secrets/<sanitized>.key``."""

    def __init__(self, directory: Path | str) -> None:
        self._root = Path(directory).expanduser()
        self._secrets_dir = self._root / SECRETS_DIR_NAME

    def _path(self, name: str) -> Path:
        return self._secrets_dir / f"{sanitize_name(name)}{SECRET_FILE_SUFFIX}"

    def get(self, name: str) -> str | None:
        try:
            return self._path(name).read_text(encoding="utf-8")
        except FileNotFoundError:
            return None

    def set(self, name: str, value: str) -> None:
        self._secrets_dir.mkdir(parents=True, exist_ok=True)
        self._harden_dir()
        target = self._path(name)
        tmp = target.with_name(f"{target.name}{_TMP_SUFFIX}")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, value.encode("utf-8"))
        finally:
            os.close(fd)
        os.replace(tmp, target)
        self._harden_file(target)

    def delete(self, name: str) -> None:
        try:
            self._path(name).unlink()
        except FileNotFoundError:
            pass

    def exists(self, name: str) -> bool:
        return self._path(name).exists()

    def masked_tail(self, name: str) -> str | None:
        value = self.get(name)
        if value is None:
            return None
        return value[-4:] or None

    # ------------------------------------------------------------ permissions

    def _harden_dir(self) -> None:
        if os.name == "nt":
            return  # user-profile ACL is the enforcement boundary on Windows
        try:
            os.chmod(self._secrets_dir, 0o700)
        except OSError:
            raise SecretsError(f"cannot secure the secrets directory {self._secrets_dir}") from None

    def _harden_file(self, path: Path) -> None:
        if os.name == "nt":
            return
        try:
            os.chmod(path, 0o600)
        except OSError:
            raise SecretsError(f"cannot secure the secret file {path}") from None
