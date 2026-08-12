"""argon2id password hashing (FR-6.1e).

The owner password is stored exclusively as an argon2id PHC string — never the
plaintext, and never a fast reversible encoding. OWASP-friendly parameters are
compressed for an embedded single-user daemon (the verify path runs inside the
request loop, so work factor stays on the interactive-login tier rather than the
key-derivation tier).
"""

from __future__ import annotations

import argon2

# OWASP recommended argon2id parameters (memory 19 MiB, iterations 2, lanes 1).
# Kept as module constants so tests can pin the algorithm prefix ($argon2id$).
_TIME_COST = 2
_MEMORY_COST = 19 * 1024  # KiB
_PARALLELISM = 1

_HASHER = argon2.PasswordHasher(
    time_cost=_TIME_COST,
    memory_cost=_MEMORY_COST,
    parallelism=_PARALLELISM,
    hash_len=32,
    salt_len=16,
)


def hash_password(password: str) -> str:
    """Hash one password to an argon2id PHC string (or ``$argon2id$`` phc)."""
    return _HASHER.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Constant-time verify; a malformed/foreign hash is simply invalid.

    Note the argument order: ``PasswordHasher.verify(hash, password)`` takes the
    PHC hash first -- passing them swapped turns every valid password into an
    invalid hash.
    """
    try:
        return _HASHER.verify(password_hash, password)
    except (
        argon2.exceptions.VerificationError,
        argon2.exceptions.InvalidHashError,
        argon2.exceptions.VerifyMismatchError,
    ):
        return False


def needs_rehash(password_hash: str) -> bool:
    """True when the stored hash predates the current parameter set."""
    try:
        return _HASHER.check_needs_rehash(password_hash)
    except argon2.exceptions.InvalidHashError:
        return True
