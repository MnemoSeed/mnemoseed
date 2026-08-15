"""Secrets reference grammar (T2-2).

The ``api_key_env`` field of a dream role accepts either a comma-separated
env-var NAME list (unchanged) OR a single ``secrets:mnemoseed/dream/<role>``
reference. A reference names the secret-store key — never a value — so the
grammar here is the one shared by the config loader, the configwrite registry
validator, and the role router's resolve-time precedence:

    reference  ->  secrets:mnemoseed/dream/<role>
    store name ->  mnemoseed/dream/<role>

The embedded role is shape-checked against the live dream roles by the
callers (LLM_ROLES lives in mnemoseed.config; importing it here would create
a cycle, since config validation consumes this grammar).
"""

from __future__ import annotations

import re

#: The reference prefix that distinguishes a store reference from an env name.
SECRETS_REF_PREFIX = "secrets:"

#: Shape of a reference; the role is validated against the live roles by the
#: callers (LLM_ROLES lives in mnemoseed.config).
SECRETS_REF_RE = re.compile(r"secrets:mnemoseed/dream/([a-z][a-z0-9_]*)")


def is_secrets_ref(value: str) -> bool:
    """True when the value is a secrets-store reference (not an env name)."""
    return value.startswith(SECRETS_REF_PREFIX)


def secret_name_from_ref(ref: str) -> str | None:
    """The store key a reference addresses (``mnemoseed/dream/<role>``), or
    None when the value is not a reference."""
    if not is_secrets_ref(ref):
        return None
    name = ref[len(SECRETS_REF_PREFIX) :]
    return name or None
