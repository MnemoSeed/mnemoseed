"""SecretStore port + file backend (T2-1): restart-free key custody.

Dream LLM keys never live in the config file — config stores only a
REFERENCE (``secrets:mnemoseed/dream/<role>``) and the key value lives in a
per-user restricted file under ``<CONFIG_DIR>/secrets/``. The port is the
whole seam: routing resolves through it, the admin/key endpoints write
through it, and a future Keychain backend is a documented port implementation
without touching any consumer.
"""

from __future__ import annotations

from mnemoseed.secrets.refs import is_secrets_ref, secret_name_from_ref
from mnemoseed.secrets.store import FileSecretStore, SecretsError, SecretStore

__all__ = ["FileSecretStore", "SecretStore", "SecretsError", "is_secrets_ref", "secret_name_from_ref"]
