"""Uninstall (FR-6.7): per-host rollback, daemon stop, optional purge.

Each registered host prefers restoration from its install-time backup
(byte-identical); without a backup the mnemoseed entry is surgically removed
and nothing else is touched. The daemon is stopped only when a pidfile under
the data dir was written by us AND the recorded process start time still
matches the live target — a stale pidfile whose pid was recycled is discarded
without touching the unrelated process. Data survives by default -- purge is
explicit, exposes a dry-run list first, and is gated by an approval callback.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from mnemoseed.installer import proc
from mnemoseed.installer.hosts import (
    HostSpec,
    host_specs,
    resolve_data_dir,
    resolve_home,
    surgical_remove_mnemoseed,
)
from mnemoseed.installer.state import PIDFILE_NAME, RegistrationRecord, load_state, save_state
from mnemoseed.installer.tomlhost import load_host_toml, remove_codex


@dataclass(frozen=True)
class HostRollback:
    """One host's uninstall outcome."""

    host: str
    outcome: str  # "restored" | "removed" | "no-op"
    config: str
    detail: str = ""


@dataclass(frozen=True)
class UninstallReport:
    """Aggregate uninstall result."""

    rolls: list[HostRollback]
    daemon: str  # "stopped" | "stale" | "not-started" | "failed"
    data_dir: str
    purged: bool
    purge_list: list[str] = field(default_factory=list)

    @property
    def no_op(self) -> bool:
        return not self.rolls and self.daemon in ("not-started", "stale")


def purge_plan(data_dir: Path | None = None) -> list[str]:
    """What ``--purge`` would delete: every path under the data dir."""
    data_dir = resolve_data_dir(data_dir)
    if not data_dir.exists():
        return []
    return [str(path) for path in sorted(data_dir.rglob("*"))] + [str(data_dir)]


def _remove_mnemoseed_entry(spec: HostSpec, config: Path) -> HostRollback | None:
    """Surgically drop only the mnemoseed entry; None when there is none."""
    if not config.exists():
        return None
    original = config.read_bytes().decode("utf-8")
    if spec.format == "toml":
        servers = load_host_toml(config, missing_ok=False).get("mcp_servers")
        if not isinstance(servers, dict) or "mnemoseed" not in servers:
            return None
        cleaned = remove_codex(original)
    else:
        removed = surgical_remove_mnemoseed(original, config)
        if removed is None:
            return None
        cleaned = removed
    config.write_bytes(cleaned.encode("utf-8"))
    return HostRollback(
        host=spec.name, outcome="removed", config=str(config.resolve()), detail="exact removal (no backup)"
    )


def _rollback_artifact_file(host: str, config: Path) -> None:
    """Remove a freshly-created artifact file (no install-time backup).

    Special case: the Codex AGENTS.md registration appends a guidance fragment
    to a file that may predate the installer, so only the appended fragment is
    stripped -- the file is deleted only when the stripped text is empty and the
    user's own content is never removed.
    """
    if host == "codex-agents":
        from mnemoseed.installer.codexfiles import (
            AGENTS_REL,
            adapter_templates_dir,
            artifact_texts,
            strip_agents_fragment,
        )

        if not config.exists():
            return
        fragment = artifact_texts(adapter_templates_dir())[AGENTS_REL]
        stripped = strip_agents_fragment(config.read_text(encoding="utf-8"), fragment)
        if stripped is None:
            return  # fragment absent (user-edited file); leave it alone
        if stripped:
            config.write_text(stripped, encoding="utf-8")
        else:
            config.unlink(missing_ok=True)
        return
    config.unlink(missing_ok=True)


def _rollback_artifact(host: str, record: RegistrationRecord) -> HostRollback:
    """Remove one artifact registration (Cursor hooks/rules, Codex files).

    Restores the main file and every recorded companion from their install-time
    backups when present; a file that was freshly created by the installer (no
    backup) is removed outright, except the Codex AGENTS.md fragment which is
    stripped instead of unlinked. Nothing outside the recorded paths is touched.
    """
    config = Path(record.config)
    restored = False
    if record.backup and Path(record.backup).exists():
        config.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(Path(record.backup), config)
        restored = True
    else:
        _rollback_artifact_file(host, config)
    for path, backup in record.files:
        target = Path(path)
        if backup and Path(backup).exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(Path(backup), target)
        else:
            target.unlink(missing_ok=True)
    detail = f"restored from backup {record.backup}" if restored else "exact removal (created by installer)"
    return HostRollback(
        host=host, outcome="restored" if restored else "removed", config=str(config.resolve()), detail=detail
    )


def _pid_alive(pid: int) -> bool:
    return proc.pid_alive(pid)


def _pid_matches(pid: int, recorded_start: float | None) -> bool:
    """Whether the live process at ``pid`` is the daemon we started, by
    comparing the recorded start time with the target's creation time. A pid
    whose start cannot be read or does not match (typically a recycled pid) is
    NOT the daemon and must never be terminated."""
    if recorded_start is None:
        return False
    actual = proc.process_start_epoch(pid)
    if actual is None:
        return False
    return abs(actual - recorded_start) <= 5.0


def _stop_daemon(data_dir: Path) -> str:
    """Stop the daemon only when a pidfile we wrote still refers to a process
    that was started when we recorded it. A stale or foreign pidfile is removed
    without touching the process it points at."""
    pidfile = data_dir / PIDFILE_NAME
    if not pidfile.exists():
        return "not-started"
    try:
        lines = pidfile.read_text(encoding="utf-8").splitlines()
        pid = int(lines[0])
        recorded = float(lines[1]) if len(lines) > 1 else None
    except (OSError, ValueError, IndexError):
        pidfile.unlink(missing_ok=True)
        return "stale"
    if not _pid_alive(pid) or not _pid_matches(pid, recorded):
        pidfile.unlink(missing_ok=True)
        return "stale"
    if proc.terminate(pid):
        pidfile.unlink(missing_ok=True)
        return "stopped"
    # Failed to stop the daemon we own: clear the stale pidfile so the next
    # run retries cleanly instead of inheriting a dead reference.
    pidfile.unlink(missing_ok=True)
    return "failed"


def deregister(home: Path | None = None, data_dir: Path | None = None) -> list[HostRollback]:
    """FR-6.1c unlink: unbind the mnemoseed entry from every detected host.

    Each host config is restored from its install-time backup when present,
    otherwise the mnemoseed entry is surgically removed. The daemon is never
    stopped and data is never purged (unlike :func:`uninstall`); only the host
    registrations are rolled back. No-op hosts are filtered out of the result.
    """
    home = resolve_home(home)
    data_dir = resolve_data_dir(data_dir)
    state = load_state(data_dir)
    rolls: list[HostRollback] = []

    for spec in host_specs(home):
        state_record = state.registrations.get(spec.name)
        config: Path = Path(state_record.config) if state_record and state_record.config else spec.config
        if state_record is not None and state_record.backup:
            backup = Path(state_record.backup)
            if backup.exists():
                config.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(backup, config)
                rolls.append(
                    HostRollback(
                        host=spec.name,
                        outcome="restored",
                        config=str(config.resolve()),
                        detail=f"restored from backup {backup}",
                    )
                )
                state.remove(spec.name)
                continue
        rollback = _remove_mnemoseed_entry(spec, config)
        if rollback is not None:
            rolls.append(rollback)
            state.remove(spec.name)
        else:
            rolls.append(
                HostRollback(
                    host=spec.name,
                    outcome="no-op",
                    config=str(config.resolve()),
                    detail="not registered",
                )
            )
    save_state(data_dir, state)
    return [roll for roll in rolls if roll.outcome != "no-op"]


def uninstall(
    home: Path | None = None,
    data_dir: Path | None = None,
    *,
    purge: bool = False,
    approve_purge: Callable[[list[str]], bool] | None = None,
) -> UninstallReport:
    """Roll back every host registration, stop a daemon we started, and purge
    the data dir only when asked.

    Rollback per detected host (plus registered hosts no longer detected): the
    recorded install-time backup is restored byte-identically when present,
    otherwise the mnemoseed entry is surgically removed. A host that was never
    registered is a clean no-op.
    """
    home = resolve_home(home)
    data_dir = resolve_data_dir(data_dir)
    state = load_state(data_dir)
    rolls: list[HostRollback] = []

    for spec in host_specs(home):
        state_record = state.registrations.get(spec.name)
        config: Path = Path(state_record.config) if state_record and state_record.config else spec.config
        if state_record is not None and state_record.backup:
            backup = Path(state_record.backup)
            if backup.exists():
                config.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(backup, config)
                rolls.append(
                    HostRollback(
                        host=spec.name,
                        outcome="restored",
                        config=str(config.resolve()),
                        detail=f"restored from backup {backup}",
                    )
                )
                state.remove(spec.name)
                continue
        rollback = _remove_mnemoseed_entry(spec, config)
        if rollback is not None:
            rolls.append(rollback)
            state.remove(spec.name)
        else:
            rolls.append(
                HostRollback(
                    host=spec.name,
                    outcome="no-op",
                    config=str(config.resolve()),
                    detail="not registered",
                )
            )

    # Project artifact registrations (Cursor hooks/rules, FR-6.3b) are not host
    # configs: each recorded item is rolled back from its backup or removed.
    covered = {spec.name for spec in host_specs(home)}
    for host, record in list(state.registrations.items()):
        if host in covered:
            continue
        rolls.append(_rollback_artifact(host, record))
        state.remove(host)

    daemon_status = _stop_daemon(data_dir)

    dry_run: list[str] = []
    purged = False
    if purge:
        dry_run = purge_plan(data_dir)
        if dry_run and (approve_purge is None or approve_purge(dry_run)):
            shutil.rmtree(data_dir)
            purged = True
        elif not dry_run:
            purged = True

    if not purged:
        save_state(data_dir, state)
    return UninstallReport(
        rolls=[roll for roll in rolls if roll.outcome != "no-op"],
        daemon=daemon_status,
        data_dir=str(data_dir),
        purged=purged,
        purge_list=dry_run,
    )
