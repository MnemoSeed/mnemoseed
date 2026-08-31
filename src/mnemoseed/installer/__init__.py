"""PRD-06 T2 installer surface (FR-6.1 / FR-6.6 / FR-6.7).

Host detection, registration with backup + diff + per-item confirm, the doctor
checklist, and uninstall. Token issuance is FR-6.1b/c (a later task): the
written MCP entry structure is ready but the identity env keys are only emitted
once a profile_id and token are supplied.
"""

# Issue #6: cross-platform daemon autostart. Imported first so the ordering
# never matters for cycles (startup pulls in proc/state, which must not depend
# on __init__ having finished).
from mnemoseed.installer import startup
from mnemoseed.installer.codexfiles import plan_codex_files, trust_guidance_lines
from mnemoseed.installer.cursorfiles import adapter_templates_dir, artifact_texts, plan_cursor_project
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
from mnemoseed.installer.startup import StartupStatus
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
    "StartupStatus",
    "State",
    "UninstallReport",
    "adapter_templates_dir",
    "apply_registrations",
    "artifact_texts",
    "detect_hosts",
    "host_specs",
    "install",
    "mnemoseed_mcp_entry",
    "plan_codex_files",
    "plan_cursor_project",
    "plan_registrations",
    "purge_plan",
    "run_doctor",
    "startup",
    "trust_guidance_lines",
    "uninstall",
]
