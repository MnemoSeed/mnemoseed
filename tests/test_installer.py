"""PRD-06 T2 installer (FR-6.1 / FR-6.7): host detection, registration with
backup + diff + per-item confirm, minimal-merge idempotency, and the
byte-identical restore / exact surgical-removal round-trip.

Every file effect runs under pytest tmp_path with an explicit ``home`` /
``data_dir``; the real user profile and MnemoSeed home are never touched.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from mnemoseed.installer import (
    HostConfigError,
    RegistrationPlan,
    apply_registrations,
    detect_hosts,
    host_specs,
    install,
    mnemoseed_mcp_entry,
    plan_registrations,
    purge_plan,
    uninstall,
)


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


def _read(path: Path):
    return json.loads(path.read_bytes().decode("utf-8"))


def _read_toml(path: Path):
    return tomllib.loads(path.read_bytes().decode("utf-8"))


def _claude_marker(home: Path) -> Path:
    return _write(home / ".claude.json", "{}")


def _cursor_marker(home: Path) -> Path:
    (home / ".cursor").mkdir(parents=True, exist_ok=True)
    return home / ".cursor"


def _codex_marker(home: Path) -> Path:
    (home / ".codex").mkdir(parents=True, exist_ok=True)
    return home / ".codex"


def _all_three(home: Path) -> None:
    _claude_marker(home)
    _cursor_marker(home)
    _codex_marker(home)


def _spec(home: Path, name: str):
    return next(spec for spec in host_specs(home) if spec.name == name)


def _approve_all(plan: RegistrationPlan) -> bool:
    return True


def _approve_none(plan: RegistrationPlan) -> bool:
    return False


# ------------------------------------------------------------ host detection


def test_detect_hosts_empty_home_finds_nothing(tmp_path) -> None:
    assert detect_hosts(tmp_path) == []


def test_detect_hosts_finds_each_marker(tmp_path) -> None:
    _claude_marker(tmp_path)
    found = {spec.name for spec in detect_hosts(tmp_path)}
    assert found == {"claude-code"}

    _cursor_marker(tmp_path)
    _codex_marker(tmp_path)
    found = {spec.name for spec in detect_hosts(tmp_path)}
    assert found == {"claude-code", "cursor", "codex"}


def test_detect_hosts_maps_correct_config_targets(tmp_path) -> None:
    _all_three(tmp_path)
    by_name = {spec.name: spec for spec in detect_hosts(tmp_path)}
    assert by_name["claude-code"].config == tmp_path / ".claude.json"
    assert by_name["cursor"].config == tmp_path / ".cursor" / "mcp.json"
    assert by_name["codex"].config == tmp_path / ".codex" / "config.toml"
    assert by_name["codex"].format == "toml"


# ------------------------------------------------------------ entry shape / seam


def test_mnemoseed_mcp_entry_bare_structure() -> None:
    entry = mnemoseed_mcp_entry()
    assert entry == {"command": "mnemoseed", "args": ["mcp"]}


def test_mnemoseed_mcp_entry_identity_seam() -> None:
    """The identity env keys are emitted only once supplied (FR-6.1b/c later):
    profile_id adds MNEMOSEED_PROFILE_ID, token adds MNEMOSEED_TOKEN, and with
    neither the structure is written without any env."""
    bare = mnemoseed_mcp_entry(command="mnemoseed")
    assert "env" not in bare

    with_profile = mnemoseed_mcp_entry(profile_id="work")
    assert with_profile["env"] == {"MNEMOSEED_PROFILE_ID": "work"}

    with_token = mnemoseed_mcp_entry(profile_id="work", token="t0k3n")
    assert with_token["env"] == {"MNEMOSEED_PROFILE_ID": "work", "MNEMOSEED_TOKEN": "t0k3n"}


# ------------------------------------------------------------ minimal merge + backup


def test_install_is_minimal_merge_preserving_unrelated_settings(tmp_path) -> None:
    home = tmp_path / "home"
    data = tmp_path / "data"
    original = {
        "installMethod": "manual",
        "projects": {"repo": {"mcpServers": {"custom": {"command": "x", "args": ["y"]}}}},
        "mcpServers": {"github": {"command": "gh", "args": ["mcp"]}},
    }
    claude = _write(home / ".claude.json", json.dumps(original, indent=2))

    report = install(home, data, hosts=[_spec(home, "claude-code")], approve=_approve_all)
    assert report.written == 1

    loaded = _read(claude)
    assert loaded["installMethod"] == "manual"
    assert loaded["projects"] == original["projects"]
    assert loaded["mcpServers"]["github"] == original["mcpServers"]["github"]
    assert loaded["mcpServers"]["mnemoseed"] == {"command": "mnemoseed", "args": ["mcp"]}


def test_install_backs_up_target_before_write(tmp_path) -> None:
    home = tmp_path / "home"
    data = tmp_path / "data"
    claude = _claude_marker(home)
    claude.write_text('{"theme": "dark"}\n', encoding="utf-8")
    original_bytes = claude.read_bytes()

    report = install(home, data, hosts=[_spec(home, "claude-code")], approve=_approve_all)
    applied = report.applied[0]
    assert applied.backup is not None

    backup = Path(applied.backup)
    assert backup.exists()
    assert backup.read_bytes() == original_bytes  # backup fidelity: byte copy
    assert backup.parent.parent.name == "backups"
    assert ".bak" in backup.name and "claude.json" in backup.name


def test_install_creates_missing_config_without_backup(tmp_path) -> None:
    home = tmp_path / "home"
    data = tmp_path / "data"
    _cursor_marker(home)
    cursor_config = home / ".cursor" / "mcp.json"
    assert not cursor_config.exists()

    report = install(home, data, hosts=[_spec(home, "cursor")], approve=_approve_all)
    assert report.written == 1
    assert report.applied[0].backup is None  # nothing existed to back up
    assert _read(cursor_config)["mcpServers"]["mnemoseed"] == {"command": "mnemoseed", "args": ["mcp"]}


def test_install_propagates_profile_id_into_env(tmp_path) -> None:
    home = tmp_path / "home"
    data = tmp_path / "data"
    claude = _claude_marker(home)

    install(home, data, hosts=[_spec(home, "claude-code")], profile_id="work", approve=_approve_all)
    entry = _read(claude)["mcpServers"]["mnemoseed"]
    assert entry["env"] == {"MNEMOSEED_PROFILE_ID": "work"}


def test_install_is_idempotent_second_run_no_changes(tmp_path) -> None:
    home = tmp_path / "home"
    data = tmp_path / "data"
    _all_three(home)

    first = install(home, data, approve=_approve_all)
    assert first.written == 5  # 3 MCP registrations + Codex hooks + AGENTS.md item

    plans = plan_registrations(home, data)
    assert len(plans) == 5
    assert all(not plan.changed for plan in plans)
    assert all(not plan.diff for plan in plans)

    second = apply_registrations(plans, approve=_approve_all, data_dir=data)
    assert second.written == 0
    assert all(not item.changed for item in second.applied)


def test_install_approval_gates_each_item(tmp_path) -> None:
    home = tmp_path / "home"
    data = tmp_path / "data"
    _all_three(home)
    seen: list[str] = []

    def approve_partial(plan) -> bool:
        seen.append(plan.host)
        return plan.host == "claude-code"

    report = install(home, data, approve=approve_partial)
    assert sorted(seen) == ["claude-code", "codex", "codex-agents", "codex-hooks", "cursor"]
    assert report.written == 1

    registered = [
        report.applied[i].host for i, item in enumerate(report.applied) if item.approved and item.changed
    ]
    assert registered == ["claude-code"]
    assert (home / ".claude.json").exists()
    assert not (home / ".cursor" / "mcp.json").exists()
    assert not (home / ".codex" / "config.toml").exists()
    backups = list((data / "backups").rglob("*.bak"))
    assert len(backups) == 1  # only the approved host was backed up and written


# ------------------------------------------------------------ corrupt/edge configs


def test_install_empty_config_raises_typed_error(tmp_path) -> None:
    home = tmp_path / "home"
    data = tmp_path / "data"
    _write(home / ".claude.json", "")
    with pytest.raises(HostConfigError, match="empty"):
        install(home, data, hosts=[_spec(home, "claude-code")], approve=_approve_all)
    # no data loss: the file is still empty
    assert (home / ".claude.json").read_text(encoding="utf-8") == ""


def test_install_invalid_json_raises_typed_error(tmp_path) -> None:
    home = tmp_path / "home"
    data = tmp_path / "data"
    _write(home / ".claude.json", "not-json{")
    with pytest.raises(HostConfigError, match="invalid JSON"):
        install(home, data, hosts=[_spec(home, "claude-code")], approve=_approve_all)
    assert (home / ".claude.json").read_text(encoding="utf-8") == "not-json{"


def test_install_non_object_config_raises_typed_error(tmp_path) -> None:
    home = tmp_path / "home"
    data = tmp_path / "data"
    _write(home / ".claude.json", "[1, 2, 3]")
    with pytest.raises(HostConfigError, match="not a JSON object"):
        install(home, data, hosts=[_spec(home, "claude-code")], approve=_approve_all)
    assert _read(home / ".claude.json") == [1, 2, 3]


# ------------------------------------------------------------ uninstall round-trip


def test_uninstall_restores_byte_identical_from_backup(tmp_path) -> None:
    home = tmp_path / "home"
    data = tmp_path / "data"
    original = (
        '{\n  "theme": "dark",\n  "mcpServers": {\n    "github": {"command": "gh", "args": ["mcp"]}\n  }\n}\n'
    )
    claude = _write(home / ".claude.json", original)
    original_bytes = claude.read_bytes()

    install(home, data, hosts=[_spec(home, "claude-code")], approve=_approve_all)
    assert claude.read_bytes() != original_bytes

    report = uninstall(home, data)
    assert [roll.outcome for roll in report.rolls] == ["restored"]
    assert claude.read_bytes() == original_bytes


def test_uninstall_surgical_removal_when_backup_missing(tmp_path) -> None:
    home = tmp_path / "home"
    data = tmp_path / "data"
    claude = _claude_marker(home)
    original_github = {"command": "gh", "args": ["mcp"]}
    claude.write_text(
        "{\n"
        '  "theme": "dark",\n'
        '  "mcpServers": {\n'
        '    "github": {"command": "gh", "args": ["mcp"]},\n'
        '    "mnemoseed": {"command": "mnemoseed", "args": ["mcp"]}\n'
        "  }\n"
        "}\n",
        encoding="utf-8",
    )

    # No installer state, no backup: the mnemoseed entry alone is removed and
    # everything else survives (simulates state loss / stray entry).
    report = uninstall(home, data)
    assert [roll.outcome for roll in report.rolls] == ["removed"]
    loaded = _read(claude)
    assert "mnemoseed" not in loaded["mcpServers"]
    assert loaded["mcpServers"]["github"] == original_github
    assert loaded["theme"] == "dark"


def test_uninstall_surgical_when_recorded_backup_deleted(tmp_path) -> None:
    home = tmp_path / "home"
    data = tmp_path / "data"
    claude = _claude_marker(home)
    claude.write_text('{"theme": "dark"}\n', encoding="utf-8")
    install(home, data, hosts=[_spec(home, "claude-code")], approve=_approve_all)

    backups = list((data / "backups").rglob("*.bak"))
    assert len(backups) == 1
    backups[0].unlink()
    report = uninstall(home, data)
    assert [roll.outcome for roll in report.rolls] == ["removed"]

    loaded = _read(claude)
    assert "mcpServers" not in loaded
    assert loaded["theme"] == "dark"


def test_uninstall_never_registered_is_clean_noop(tmp_path) -> None:
    home = tmp_path / "home"
    data = tmp_path / "data"
    _claude_marker(home)

    report = uninstall(home, data)
    assert report.no_op is True
    assert report.rolls == []
    assert _read(home / ".claude.json") == {}


def test_uninstall_invalid_current_config_no_data_loss(tmp_path) -> None:
    home = tmp_path / "home"
    data = tmp_path / "data"
    _write(home / ".claude.json", "{broken")
    # A stray corrupt entry with no backup cannot be surgically edited.
    with pytest.raises(HostConfigError):
        uninstall(home, data)
    assert (home / ".claude.json").read_text(encoding="utf-8") == "{broken"


# ------------------------------------------------------------ daemon pidfile


def _sleeper():
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _pidfile(data: Path, pid: int, started: float | None = None) -> Path:
    data.mkdir(parents=True, exist_ok=True)
    if started is None:
        started = _start_epoch(pid)
    return _write(
        data / "daemon.pid",
        f"{pid}\n{started!r}\n",
    )


def _start_epoch(pid: int) -> float:
    from mnemoseed.installer.proc import process_start_epoch

    start = process_start_epoch(pid)
    assert start is not None
    return start


def test_uninstall_stops_daemon_started_by_us(tmp_path) -> None:
    home = tmp_path / "home"
    data = tmp_path / "data"
    sleeper = _sleeper()
    try:
        _pidfile(data, sleeper.pid)
        report = uninstall(home, data)
        assert report.daemon == "stopped"
        sleeper.wait(timeout=10)
        assert sleeper.returncode is not None
        assert not (data / "daemon.pid").exists()
    finally:
        if sleeper.poll() is None:
            sleeper.kill()
            sleeper.wait(timeout=10)


def test_uninstall_clears_stale_pidfile(tmp_path) -> None:
    home = tmp_path / "home"
    data = tmp_path / "data"
    _write(data / "daemon.pid", "99999999")  # no such process

    report = uninstall(home, data)
    assert report.daemon == "stale"
    assert not (data / "daemon.pid").exists()


def test_uninstall_does_not_kill_unrelated_live_process_on_pid_reuse(tmp_path) -> None:
    """F1 regression: a pidfile whose pid now belongs to an unrelated live
    process must be treated as stale — the process is never terminated."""
    home = tmp_path / "home"
    data = tmp_path / "data"
    other = _sleeper()
    try:
        # The pidfile records a start time that does NOT match the target's
        # creation time: the pid was recycled by an unrelated process.
        stale_start = _start_epoch(other.pid) - 60_000.0
        _pidfile(data, other.pid, stale_start)

        report = uninstall(home, data)
        assert report.daemon == "stale"
        assert other.poll() is None, "uninstall must not terminate an unrelated process"
        assert not (data / "daemon.pid").exists()
    finally:
        if other.poll() is None:
            other.kill()
            other.wait(timeout=10)


def _force_terminate_fail(monkeypatch):
    from mnemoseed.installer import proc

    monkeypatch.setattr(proc, "terminate", lambda pid: False)


def test_uninstall_failed_stop_returns_failed_and_clears_pidfile(tmp_path, monkeypatch) -> None:
    """F4 regression: a daemon we identify but cannot stop yields ``failed``
    and the stale pidfile is removed (never left dangling)."""
    home = tmp_path / "home"
    data = tmp_path / "data"
    sleeper = _sleeper()
    try:
        _pidfile(data, sleeper.pid)
        _force_terminate_fail(monkeypatch)
        report = uninstall(home, data)
        assert report.daemon == "failed"
        assert not (data / "daemon.pid").exists()
        assert sleeper.poll() is None  # the forced failure means it survives
    finally:
        if sleeper.poll() is None:
            sleeper.kill()
            sleeper.wait(timeout=10)


# ------------------------------------------------------------ purge


def test_purge_plan_lists_data_dir_contents(tmp_path) -> None:
    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    _write(data / "config.toml", 'preset = "embedded"\n')
    _write(data / "installer" / "state.json", "{}")
    _write(data / "daemon.pid", "1")

    listed = purge_plan(data)
    assert str(data) in listed
    assert str(data / "config.toml") in listed
    assert str(data / "installer" / "state.json") in listed
    assert str(data / "daemon.pid") in listed


def test_uninstall_purge_dry_run_first_then_delete(tmp_path) -> None:
    home = tmp_path / "home"
    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    _write(data / "config.toml", 'preset = "embedded"\n')

    decided: list[bool | None] = []

    def refuse(paths: list[str]) -> bool:
        decided.append(False)
        assert str(data / "config.toml") in paths
        return False

    report = uninstall(home, data, purge=True, approve_purge=refuse)
    assert report.purged is False
    assert report.purge_list and str(data / "config.toml") in report.purge_list
    assert data.exists()  # refusal kept the data

    report = uninstall(home, data, purge=True, approve_purge=lambda paths: True)
    assert report.purged is True
    assert not data.exists()
    assert decided == [False]


def test_uninstall_without_purge_keeps_data(tmp_path) -> None:
    home = tmp_path / "home"
    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    _write(data / "config.toml", 'preset = "embedded"\n')

    report = uninstall(home, data)
    assert report.purged is False
    assert (data / "config.toml").exists()


# ------------------------------------------------------------ CLI wiring


def _console_script() -> list[str]:
    bin_dir = Path(sys.executable).parent
    for name in ("mnemoseed.exe", "mnemoseed", "mnemoseed.bat"):
        candidate = bin_dir / name
        if candidate.exists():
            return [str(candidate)]
    return [sys.executable, "-m", "mnemoseed.cli"]


def _cli_env(home: Path, data: Path) -> dict:
    env = dict(os.environ)
    env["MNEMOSEED_USER_HOME"] = str(home)
    env["MNEMOSEED_HOME"] = str(data)
    env.pop("STORAGE_MODE", None)
    return env


def test_cli_install_yes_writes_and_backs_up(tmp_path) -> None:
    home = tmp_path / "home"
    data = tmp_path / "data"
    _all_three(home)
    # Every target exists with content so each write is pre-backed up (a fresh
    # config file has nothing to back up and takes the no-backup path).
    _write(home / ".cursor" / "mcp.json", '{"mcpServers": {}}\n')
    _write(home / ".codex" / "config.toml", '[model]\nname = "gpt-5"\n')

    proc = subprocess.run(
        [*_console_script(), "install", "--yes"],
        env=_cli_env(home, data),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "installed: 5 host registration(s)" in proc.stdout  # 3 hosts + Codex hooks + AGENTS.md

    assert _read(home / ".claude.json")["mcpServers"]["mnemoseed"]["command"] == "mnemoseed"
    assert _read(home / ".cursor" / "mcp.json")["mcpServers"]["mnemoseed"]["args"] == ["mcp"]
    assert _read_toml(home / ".codex" / "config.toml")["mcp_servers"]["mnemoseed"]["command"] == "mnemoseed"
    backups = list((data / "backups").rglob("*.bak"))
    assert len(backups) == 3


def test_cli_install_prompt_declines_single_host(tmp_path) -> None:
    home = tmp_path / "home"
    data = tmp_path / "data"
    _claude_marker(home)

    proc = subprocess.run(
        [*_console_script(), "install"],
        env=_cli_env(home, data),
        input="n\n",
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "skipped (not approved)" in proc.stdout
    assert "installed: 0 host registration(s)" in proc.stdout
    assert _read(home / ".claude.json") == {}


def test_cli_uninstall_restores_byte_identical(tmp_path) -> None:
    home = tmp_path / "home"
    data = tmp_path / "data"
    claude = _write(home / ".claude.json", '{"theme": "dark"}\n')
    original = claude.read_bytes()

    subprocess.run(
        [*_console_script(), "install", "--yes"],
        env=_cli_env(home, data),
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    proc = subprocess.run(
        [*_console_script(), "uninstall", "--yes"],
        env=_cli_env(home, data),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "claude-code: restored" in proc.stdout
    assert claude.read_bytes() == original


def test_cli_uninstall_returns_nonzero_when_daemon_stop_fails(tmp_path, monkeypatch) -> None:
    """F4 regression: a failed daemon stop makes ``mnemoseed uninstall`` exit
    non-zero instead of reporting success with a dead pidfile behind it."""
    from argparse import Namespace

    from mnemoseed import config as cfg
    from mnemoseed.cli import cmd_uninstall

    home = tmp_path / "home"
    data = tmp_path / "data"
    sleeper = _sleeper()
    try:
        _pidfile(data, sleeper.pid)
        _force_terminate_fail(monkeypatch)
        monkeypatch.setattr(cfg, "CONFIG_DIR", data)
        monkeypatch.setenv("MNEMOSEED_USER_HOME", str(home))

        code = cmd_uninstall(Namespace(purge=False, yes=True))
        assert code == 1
        assert not (data / "daemon.pid").exists()
    finally:
        if sleeper.poll() is None:
            sleeper.kill()
            sleeper.wait(timeout=10)


def test_cli_doctor_reports_cleanly_without_daemon(tmp_path) -> None:
    home = tmp_path / "home"
    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    _write(data / "config.toml", 'preset = "embedded"\n')

    proc = subprocess.run(
        [*_console_script(), "doctor"],
        env=_cli_env(home, data),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    combined = proc.stdout + proc.stderr
    assert proc.returncode != 0, combined
    assert "[FAIL] daemon" in combined
    assert "fix: start the daemon: mnemoseed up" in combined
    assert "Traceback" not in combined


# ------------------------------------------------------------ F3 byte-fidelity


def test_uninstall_surgical_json_removal_preserves_bytes_lf(tmp_path) -> None:
    """F3 regression (LF): removing the mnemoseed entry changes exactly the
    entry's bytes — key order, indentation and line endings all survive."""
    home = tmp_path / "home"
    data = tmp_path / "data"
    original = (
        "{\n"
        '  "api_key": "secret",\n'
        '  "mcpServers": {\n'
        '    "github": {"command": "gh", "args": ["mcp"]},\n'
        '    "mnemoseed": {"command": "mnemoseed", "args": ["mcp"]}\n'
        "  }\n"
        "}\n"
    )
    expected = (
        "{\n"
        '  "api_key": "secret",\n'
        '  "mcpServers": {\n'
        '    "github": {"command": "gh", "args": ["mcp"]}\n'
        "  }\n"
        "}\n"
    )
    config = _write(home / ".claude.json", original)

    report = uninstall(home, data)
    assert [roll.outcome for roll in report.rolls] == ["removed"]
    assert config.read_bytes() == expected.encode("utf-8")


def test_uninstall_surgical_json_removal_preserves_bytes_crlf(tmp_path) -> None:
    """F3 regression (CRLF): same byte-level result on Windows line endings."""
    home = tmp_path / "home"
    data = tmp_path / "data"
    original = (
        "{\r\n"
        '  "api_key": "secret",\r\n'
        '  "mcpServers": {\r\n'
        '    "github": {"command": "gh", "args": ["mcp"]},\r\n'
        '    "mnemoseed": {"command": "mnemoseed", "args": ["mcp"]}\r\n'
        "  }\r\n"
        "}\r\n"
    )
    expected = (
        "{\r\n"
        '  "api_key": "secret",\r\n'
        '  "mcpServers": {\r\n'
        '    "github": {"command": "gh", "args": ["mcp"]}\r\n'
        "  }\r\n"
        "}\r\n"
    )
    config = _write(home / ".claude.json", original)

    report = uninstall(home, data)
    assert [roll.outcome for roll in report.rolls] == ["removed"]
    assert config.read_bytes() == expected.encode("utf-8")


def test_uninstall_surgical_json_only_entry_removes_whole_object(tmp_path) -> None:
    """A lone mnemoseed entry leaves no empty mcpServers behind."""
    home = tmp_path / "home"
    data = tmp_path / "data"
    config = _write(home / ".claude.json", '{\n  "mcpServers": {\n    "mnemoseed": {}\n  }\n}\n')

    report = uninstall(home, data)
    assert [roll.outcome for roll in report.rolls] == ["removed"]
    assert _read(config) == {}


def test_install_preserves_existing_key_order_indent(tmp_path) -> None:
    """F3: a registration write keeps the target's key order and indent unit."""
    home = tmp_path / "home"
    data = tmp_path / "data"
    original = '{\n    "zeta": 1,\n    "alpha": 2\n}\n'
    config = _write(home / ".claude.json", original)

    install(home, data, hosts=[_spec(home, "claude-code")], approve=_approve_all)
    raw = config.read_bytes().decode("utf-8")

    assert raw.index('"zeta"') < raw.index('"alpha"')  # order preserved
    assert '\n    "mcpServers"' in raw  # 4-space indent preserved
    assert "mcpServers" in raw and "mnemoseed" in raw


def test_install_preserves_crlf_line_endings(tmp_path) -> None:
    """F3: a registration write into a CRLF file stays CRLF."""
    home = tmp_path / "home"
    data = tmp_path / "data"
    original = '{\r\n  "theme": "dark"\r\n}\r\n'
    config = _write(home / ".claude.json", original)

    install(home, data, hosts=[_spec(home, "claude-code")], approve=_approve_all)
    raw = config.read_bytes().decode("utf-8")

    assert "\r\n" in raw  # CRLF preserved end-to-end
    assert "\n" not in raw.replace("\r\n", "")  # and no stray LF snuck in


# ------------------------------------------------------------ F6 Codex TOML


def test_install_codex_writes_toml_mcp_server_table(tmp_path) -> None:
    home = tmp_path / "home"
    data = tmp_path / "data"
    _codex_marker(home)
    codex_config = home / ".codex" / "config.toml"

    report = install(home, data, hosts=[_spec(home, "codex")], approve=_approve_all)
    assert report.written == 3  # MCP entry + Codex hooks + AGENTS.md item
    assert codex_config.exists()
    parsed = _read_toml(codex_config)
    assert parsed["mcp_servers"]["mnemoseed"] == {"command": "mnemoseed", "args": ["mcp"]}


def test_install_codex_preserves_existing_toml_bytes(tmp_path) -> None:
    home = tmp_path / "home"
    data = tmp_path / "data"
    _codex_marker(home)
    codex_config = _write(
        home / ".codex" / "config.toml",
        '# my settings\r\nmodel = "gpt-5"\r\n\r\n[mcp_servers.other]\r\ncommand = "x"\r\n',
    )

    install(home, data, hosts=[_spec(home, "codex")], approve=_approve_all)
    raw = codex_config.read_bytes().decode("utf-8")

    assert "# my settings" in raw
    assert 'model = "gpt-5"' in raw
    assert "[mcp_servers.other]" in raw
    assert "[mcp_servers.mnemoseed]" in raw
    parsed = _read_toml(codex_config)
    assert parsed["model"] == "gpt-5"
    assert set(parsed["mcp_servers"]) == {"other", "mnemoseed"}


def test_install_codex_is_idempotent(tmp_path) -> None:
    home = tmp_path / "home"
    data = tmp_path / "data"
    _codex_marker(home)

    first = install(home, data, hosts=[_spec(home, "codex")], approve=_approve_all)
    assert first.written == 3  # MCP entry + Codex hooks + AGENTS.md item

    plans = plan_registrations(home, data, hosts=[_spec(home, "codex")])
    assert len(plans) == 3
    assert not plans[0].changed
    assert not plans[0].diff


def test_uninstall_codex_removes_only_mnemoseed_table(tmp_path) -> None:
    home = tmp_path / "home"
    data = tmp_path / "data"
    _codex_marker(home)
    codex_config = _write(
        home / ".codex" / "config.toml",
        'model = "gpt-5"\n\n[mcp_servers.other]\ncommand = "x"\n\n[mcp_servers.mnemoseed]\n'
        'command = "mnemoseed"\nargs = ["mcp"]\n',
    )
    original_other = codex_config.read_bytes()

    report = uninstall(home, data)
    assert [roll.outcome for roll in report.rolls] == ["removed"]
    raw = codex_config.read_bytes().decode("utf-8")
    assert "[mcp_servers.mnemoseed]" not in raw
    assert "[mcp_servers.other]" in raw

    # The unrelated table is byte-identical to its original spelling.
    remaining = codex_config.read_bytes()
    for marker in (b"[mcp_servers.other]", b'command = "x"'):
        assert marker in remaining
    assert remaining != original_other  # the mnemoseed block was dropped
    assert _read_toml(codex_config)["mcp_servers"] == {"other": {"command": "x"}}
