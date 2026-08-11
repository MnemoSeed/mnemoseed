"""Console package (PRD-07): daemon-hosted management shell.

T1 ships the REST surface (read-only core + review writes) plus the SPA
shell; the interactive panels land in T2/T4. All routes live under
``/api/v1`` and the placeholder page behind ``/console``, both guarded by the
localhost/admin-token auth gate.
"""

from mnemoseed.console.auth import GuardedStaticFiles, require_console_auth
from mnemoseed.console.router import router
from mnemoseed.console.service import ConsoleNotFoundError, ConsoleService

__all__ = [
    "ConsoleNotFoundError",
    "ConsoleService",
    "GuardedStaticFiles",
    "require_console_auth",
    "router",
]
