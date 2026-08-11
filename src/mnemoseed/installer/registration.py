"""Registration planning and application (FR-6.1).

Every write is gated: planning is read-only (merge + diff) and applying walks
the plans one by one through an explicit approval callback. An approved change
first backs the target file up (timestamped copy under the data dir) and records
the backup in the installer state so uninstall can roll it back. The merge is
minimal: only the ``mnemoseed`` entry is added or updated (``mcpServers`` JSON
key or ``mcp_servers`` TOML table); every other key is preserved, a second
identical install is a no-op, and writes keep the target file's original key
order / indentation / line endings.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mnemoseed.installer.hosts import (
    MCP_SERVERS_KEY,
    MCP_SERVERS_TOML_KEY,
    MNEMOSEED_KEY,
    HostConfigError,
    HostSpec,
    detect_hosts,
    diff_json,
    json_file_text,
    load_host_json,
    mnemoseed_mcp_entry,
    resolve_data_dir,
    resolve_home,
)
from mnemoseed.installer.state import backup_path_for, load_state, save_state
from mnemoseed.installer.tomlhost import load_host_toml, merge_codex, parse_toml


def _read_raw_text(path: Path) -> str:
    """The file's exact current bytes as text (line endings preserved)."""
    if not path.exists():
        return ""
    return path.read_bytes().decode("utf-8")


@dataclass(frozen=True)
class RegistrationPlan:
    """One host's planned write: current config, merge, raw diff + write text.

    ``files`` holds companion files written together with ``config`` (used by
    the Cursor project artifacts: hook scripts written alongside hooks.json);
    ``what`` names the verb the plan performs for prompts and reports.
    """

    host: str
    display: str
    config: Path
    format: str
    before: dict[str, Any]
    after: dict[str, Any]
    before_text: str
    write_text: str
    diff: str
    changed: bool
    files: tuple[tuple[Path, str], ...] = ()
    what: str = "register the mnemoseed MCP entry"

    def describe(self) -> str:
        if not self.changed:
            return f"{self.display}: no changes (already registered)"
        suffix = f" (+{len(self.files)} companion files)" if self.files else ""
        return f"{self.display}: {self.what} in {self.config}{suffix}"


@dataclass(frozen=True)
class AppliedRegistration:
    """Outcome of one planned write after the approval gate."""

    host: str
    config: str
    approved: bool
    changed: bool
    backup: str | None


@dataclass(frozen=True)
class InstallReport:
    """What was planned and what was actually applied."""

    planned: list[RegistrationPlan]
    applied: list[AppliedRegistration]

    @property
    def written(self) -> int:
        return sum(1 for item in self.applied if item.approved and item.changed)


Approval = Callable[[RegistrationPlan], bool]


def _merge(spec: HostSpec, entry: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], str, str, bool]:
    """Minimal merge of the mnemoseed MCP entry into a host config.

    Returns ``(current, merged, current_text, write_text, changed)``. For JSON
    hosts the merged text re-serializes with the original key order / indent /
    line ending; for TOML hosts the mnemoseed table is merged in text-surgically.
    ``changed`` is False when the entry already matches exactly, making a
    repeated install a no-op.
    """
    config_path = spec.config
    before_text = _read_raw_text(config_path)
    if spec.format == "toml":
        before = load_host_toml(config_path, missing_ok=True)
        servers = before.get(MCP_SERVERS_TOML_KEY)
        changed = not (isinstance(servers, dict) and servers.get(MNEMOSEED_KEY) == entry)
        merged_text = merge_codex(before_text, entry)
        after = parse_toml(merged_text, config_path) if merged_text else {}
        return before, after, before_text, merged_text, changed

    before = load_host_json(config_path, missing_ok=True)
    after = dict(before)
    servers = after.get(MCP_SERVERS_KEY)
    if servers is None:
        servers = {}
    elif not isinstance(servers, dict):
        raise HostConfigError(config_path, f"{MCP_SERVERS_KEY!r} must be an object")
    else:
        servers = dict(servers)
    if servers.get(MNEMOSEED_KEY) == entry:
        return before, after, before_text, before_text, False
    servers[MNEMOSEED_KEY] = entry
    after[MCP_SERVERS_KEY] = servers
    return before, after, before_text, json_file_text(after, raw=before_text), True


def plan_registrations(
    home: Path | None = None,
    data_dir: Path | None = None,
    *,
    hosts: list[HostSpec] | None = None,
    command: str = "mnemoseed",
    profile_id: str | None = None,
    token: str | None = None,
    cursor_project: Path | None = None,
) -> list[RegistrationPlan]:
    """Read-only plan of what each host registration would write (diff shown).

    ``hosts`` defaults to everything :func:`detect_hosts` finds under ``home``;
    a caller may pass an explicit subset to scope the install. With
    ``cursor_project`` set, the project-scoped Cursor adapter artifacts (hooks
    + rules, FR-6.3b) are planned as additional approvable items. When Codex is
    among the targets, the user-level Codex artifacts (hooks.json + hook
    scripts, AGENTS.md fragment, FR-6.3c) are planned the same way.
    """
    home = resolve_home(home)
    targets = hosts if hosts is not None else detect_hosts(home)
    entry = mnemoseed_mcp_entry(command, profile_id=profile_id, token=token)
    plans: list[RegistrationPlan] = []
    for spec in targets:
        before, after, before_text, write_text, changed = _merge(spec, entry)
        if spec.format == "toml":
            diff = "".join(_text_diff(before_text, write_text, spec.display) if changed else [])
        else:
            diff = diff_json(before, after, spec.display, raw=before_text)
        plans.append(
            RegistrationPlan(
                host=spec.name,
                display=spec.display,
                config=spec.config,
                format=spec.format,
                before=before,
                after=after,
                before_text=before_text,
                write_text=write_text,
                diff=diff,
                changed=changed,
            )
        )
    if cursor_project is not None:
        from mnemoseed.installer.cursorfiles import plan_cursor_project

        plans.extend(plan_cursor_project(cursor_project))
    if any(spec.name == "codex" for spec in targets):
        from mnemoseed.installer.codexfiles import plan_codex_files

        plans.extend(plan_codex_files(home))
    return plans


def _text_diff(before: str, after: str, label: str) -> list[str]:
    from difflib import unified_diff

    return list(
        unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"{label} (current)",
            tofile=f"{label} (planned)",
        )
    )


def _backup_file(config: Path, data_dir: Path, host: str) -> Path:
    """Timestamped byte-copy of the target file under the data dir."""
    target = backup_path_for(data_dir, host, config)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(config, target)
    return target


def apply_registrations(
    plans: list[RegistrationPlan],
    *,
    approve: Approval,
    data_dir: Path | None = None,
) -> InstallReport:
    """Apply the planned writes, one per-item approval at a time.

    A no-change plan (idempotent re-install) passes through the approval
    callback for a uniform report but writes nothing and creates no backup.
    Every approved change is backed up first and recorded in the installer
    state before its merged config is written; companion files in ``plan.files``
    are written and recorded the same way.
    """
    data_dir = resolve_data_dir(data_dir)
    state = load_state(data_dir)
    applied: list[AppliedRegistration] = []
    for plan in plans:
        if not plan.changed:
            approved = approve(plan)
            applied.append(
                AppliedRegistration(
                    host=plan.host,
                    config=str(plan.config.resolve()),
                    approved=approved,
                    changed=False,
                    backup=None,
                )
            )
            continue
        if not approve(plan):
            applied.append(
                AppliedRegistration(
                    host=plan.host,
                    config=str(plan.config.resolve()),
                    approved=False,
                    changed=True,
                    backup=None,
                )
            )
            continue
        backup = _backup_file(plan.config, data_dir, plan.host) if plan.config.exists() else None
        file_ops: list[tuple[Path, str, Path | None]] = []
        for path, text in plan.files:
            resolved = path.resolve()
            file_backup = _backup_file(resolved, data_dir, plan.host) if resolved.exists() else None
            file_ops.append((resolved, text, file_backup))
        plan.config.parent.mkdir(parents=True, exist_ok=True)
        plan.config.write_bytes(plan.write_text.encode("utf-8"))
        for path, text, _file_backup in file_ops:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        state.record(
            plan.host,
            plan.config,
            backup,
            files=tuple(
                (str(path), str(file_backup.resolve()) if file_backup is not None else None)
                for path, _text, file_backup in file_ops
            ),
        )
        applied.append(
            AppliedRegistration(
                host=plan.host,
                config=str(plan.config.resolve()),
                approved=True,
                changed=True,
                backup=str(backup.resolve()) if backup is not None else None,
            )
        )
    save_state(data_dir, state)
    return InstallReport(planned=plans, applied=applied)


def install(
    home: Path | None = None,
    data_dir: Path | None = None,
    *,
    hosts: list[HostSpec] | None = None,
    command: str = "mnemoseed",
    profile_id: str | None = None,
    token: str | None = None,
    cursor_project: Path | None = None,
    approve: Approval,
) -> InstallReport:
    """Full FR-6.1 flow: detect -> plan with diff -> apply per approved item."""
    plans = plan_registrations(
        home,
        data_dir,
        hosts=hosts,
        command=command,
        profile_id=profile_id,
        token=token,
        cursor_project=cursor_project,
    )
    return apply_registrations(plans, approve=approve, data_dir=data_dir)
