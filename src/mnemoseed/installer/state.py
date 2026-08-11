"""Installer state manifest and backup layout.

One JSON manifest records which hosts are registered, each one's config path
and its install-time backup. It lives under the MnemoSeed data dir together
with the timestamped backups and the daemon pidfile, so uninstall can roll
every write back and ``--purge`` removes a single directory.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

STATE_RELATIVE = "installer/state.json"
BACKUPS_RELATIVE = "backups"
PIDFILE_NAME = "daemon.pid"


@dataclass(frozen=True)
class RegistrationRecord:
    """One host registration as recorded by the installer."""

    config: str
    backup: str | None
    installed_at: str


@dataclass
class State:
    """The installer's own bookkeeping, persisted under the data dir."""

    registrations: dict[str, RegistrationRecord] = field(default_factory=dict)

    @classmethod
    def from_data(cls, data: dict[str, Any]) -> State:
        registrations: dict[str, RegistrationRecord] = {}
        raw_hosts = data.get("registrations", {})
        for host, raw in raw_hosts.items() if isinstance(raw_hosts, dict) else ():
            if not isinstance(raw, dict):
                continue
            backup = raw.get("backup")
            registrations[str(host)] = RegistrationRecord(
                config=str(raw.get("config", "")),
                backup=str(backup) if backup else None,
                installed_at=str(raw.get("installed_at", "")),
            )
        return cls(registrations=registrations)

    def to_data(self) -> dict[str, Any]:
        return {
            "version": 1,
            "registrations": {
                host: {
                    "config": record.config,
                    "backup": record.backup,
                    "installed_at": record.installed_at,
                }
                for host, record in sorted(self.registrations.items())
            },
        }

    def record(self, host: str, config: Path, backup: Path | None) -> None:
        """Record a completed registration and where its pre-write backup sits."""
        self.registrations[host] = RegistrationRecord(
            config=str(config.resolve()),
            backup=str(backup.resolve()) if backup is not None else None,
            installed_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )

    def remove(self, host: str) -> None:
        self.registrations.pop(host, None)


def state_path(data_dir: Path) -> Path:
    return data_dir / STATE_RELATIVE


def load_state(data_dir: Path) -> State:
    """Read the manifest; a corrupt or absent manifest is treated as empty so
    uninstall still performs surgical cleanup of every detected entry."""
    path = state_path(data_dir)
    if not path.exists():
        return State()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, ValueError):
        return State()
    if not isinstance(data, dict):
        return State()
    return State.from_data(data)


def save_state(data_dir: Path, state: State) -> None:
    path = state_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state.to_data(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def backup_path_for(data_dir: Path, host: str, config: Path) -> Path:
    """A fresh timestamped backup path for the target config file."""
    directory = data_dir / BACKUPS_RELATIVE / host
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    suffix = 2
    candidate = directory / f"{config.name}.{stamp}.bak"
    while candidate.exists():
        candidate = directory / f"{config.name}.{stamp}-{suffix}.bak"
        suffix += 1
    return candidate
