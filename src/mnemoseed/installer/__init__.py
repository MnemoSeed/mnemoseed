"""PRD-06 T2 installer surface (FR-6.1 / FR-6.6 / FR-6.7).

Host detection, registration with backup + diff + per-item confirm, the doctor
checklist, and uninstall. Token issuance is FR-6.1b/c (a later task): the
written MCP entry structure is ready but the identity env keys are only emitted
once a profile_id and token are supplied.
"""

from mnemoseed.installer.doctor import Check, DoctorReport, run_doctor
from mnemoseed.installer.hosts import (
    HostConfigError,
    HostSpec,
    detect_hosts,
    host_specs,
    mnemoseed_mcp_entry,
)
from mnemoseed.installer.registration import (
    AppliedRegistration,
    Approval,
    InstallReport,
    RegistrationPlan,
    apply_registrations,
    install,
    plan_registrations,
)
from mnemoseed.installer.state import PIDFILE_NAME, State
from mnemoseed.installer.uninstall import HostRollback, UninstallReport, purge_plan, uninstall

__all__ = [
    "AppliedRegistration",
    "Approval",
    "Check",
    "DoctorReport",
    "HostConfigError",
    "HostRollback",
    "HostSpec",
    "InstallReport",
    "PIDFILE_NAME",
    "RegistrationPlan",
    "State",
    "UninstallReport",
    "apply_registrations",
    "detect_hosts",
    "host_specs",
    "install",
    "mnemoseed_mcp_entry",
    "plan_registrations",
    "purge_plan",
    "run_doctor",
    "uninstall",
]
