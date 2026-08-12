"""Cross-platform daemon autostart (issue #6, ``mnemoseed startup``).

Every system-side effect sits behind an injected seam, so all three platform
paths are exercised on any machine: the Linux/macOS writers are asserted on the
FILES they produce plus the command lists they would run, and the Windows path
never touches the real registry.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import mnemoseed.cli as cli
from mnemoseed.installer import startup
from mnemoseed.installer.startup import (
    LAUNCHD_LABEL,
    LAUNCHD_PLIST_REL,
    RUN_VALUE_NAME,
    SYSTEMD_UNIT_NAME,
    SYSTEMD_UNIT_REL,
    WIN_RUN_SUBKEY,
)


class _FakeRegistry:
    """In-memory Registry seam: values stay visible until deleted."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.deleted: list[str] = []

    def read(self, name: str) -> str | None:
        return self.values.get(name)

    def write(self, name: str, command: str) -> None:
        self.values[name] = command

    def delete(self, name: str) -> None:
        self.values.pop(name, None)
        self.deleted.append(name)


class _FakeRunner:
    """CommandRunner seam that records argv lists without running anything."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def run(self, argv: list[str]) -> None:
        self.calls.append(list(argv))


# --------------------------------------------------------- platform + command


def test_current_platform_is_normalized() -> None:
    assert startup.current_platform() in ("windows", "macos", "linux")


def test_daemon_argv_from_own_argv0(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(startup.sys, "argv", ["mnemoseed"])
    argv = startup.daemon_argv()
    assert argv[1:] == ["up"]
    assert Path(argv[0]).name.lower() == "mnemoseed"


def test_daemon_argv_prefers_path_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(startup.sys, "argv", ["pytest"])
    monkeypatch.setattr(startup.shutil, "which", lambda _name: "/usr/local/bin/mnemoseed")
    argv = startup.daemon_argv()
    assert argv[1:] == ["up"]
    assert Path(argv[0]).name == "mnemoseed"
    # the PATH hit is resolved to an absolute path, not echoed verbatim
    assert Path(argv[0]).is_absolute()


def test_daemon_argv_falls_back_to_python_module(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(startup.sys, "argv", ["pytest"])
    monkeypatch.setattr(startup.shutil, "which", lambda _name: None)
    argv = startup.daemon_argv()
    assert argv == [startup.sys.executable, "-m", "mnemoseed.cli", "up"]


def test_daemon_command_quotes_spaces_keep_up() -> None:
    command = startup.daemon_command()
    assert "up" in command
    assert ('"' in command) == (" " in command)


# ------------------------------------------------------ unit / plist builders


def test_systemd_unit_text_builder() -> None:
    text = startup.systemd_unit_text('"/usr/local/bin/mnemoseed" up')
    assert "[Unit]" in text and "[Service]" in text and "[Install]" in text
    assert 'ExecStart="/usr/local/bin/mnemoseed" up' in text
    assert "Type=simple" in text
    assert "Restart=on-failure" in text
    assert "RestartSec=2" in text
    assert "WantedBy=default.target" in text


def test_launchd_plist_text_builder() -> None:
    argv = ["/opt/mnemoseed/bin/mnemoseed", "up"]
    text = startup.launchd_plist_text(argv, home=Path("fakehome"))
    assert "<key>Label</key>" in text and f"<string>{LAUNCHD_LABEL}</string>" in text
    assert f"<string>{argv[0]}</string>" in text
    assert "<key>RunAtLoad</key>" in text and "<key>KeepAlive</key>" in text
    assert "daemon.log" in text and "daemon-error.log" in text
    # logs land under ~/.mnemoseed regardless of the host path style
    assert "fakehome/.mnemoseed/daemon.log" in text


def test_launchd_plist_escapes_xml_specials() -> None:
    text = startup.launchd_plist_text([r"C:\Program Files\a&b<x>", "up"], home=Path("h"))
    assert "&amp;" in text
    assert "&lt;" in text
    assert "&gt;" in text


# ------------------------------------------------------------ linux seam path


def test_enable_linux_writes_unit_and_runs_systemctl(tmp_path: Path) -> None:
    runner = _FakeRunner()
    lines = startup.enable("linux", runner=runner, home=tmp_path)
    unit = tmp_path / SYSTEMD_UNIT_REL
    assert unit.exists()
    assert "ExecStart=" in unit.read_text(encoding="utf-8")
    assert runner.calls == [["systemctl", "--user", "enable", "--now", SYSTEMD_UNIT_NAME]]
    assert any("wrote" in line for line in lines)


def test_disable_linux_removes_unit_and_disables(tmp_path: Path) -> None:
    runner = _FakeRunner()
    startup.enable("linux", runner=runner, home=tmp_path)
    lines = startup.disable("linux", runner=runner, home=tmp_path)
    assert not (tmp_path / SYSTEMD_UNIT_REL).exists()
    assert runner.calls[-1] == ["systemctl", "--user", "disable", "--now", SYSTEMD_UNIT_NAME]
    assert any("removed" in line for line in lines)


# ------------------------------------------------------------- macos seam path


def test_enable_macos_writes_plist_and_bootstraps(tmp_path: Path) -> None:
    runner = _FakeRunner()
    lines = startup.enable("macos", runner=runner, home=tmp_path, domain="gui/501")
    plist = tmp_path / LAUNCHD_PLIST_REL
    assert plist.exists()
    assert f"<string>{LAUNCHD_LABEL}</string>" in plist.read_text(encoding="utf-8")
    assert runner.calls == [["launchctl", "bootstrap", "gui/501", str(plist)]]
    assert any("wrote" in line for line in lines)


def test_disable_macos_bootouts_and_removes_plist(tmp_path: Path) -> None:
    runner = _FakeRunner()
    startup.enable("macos", runner=runner, home=tmp_path, domain="gui/501")
    lines = startup.disable("macos", runner=runner, home=tmp_path, domain="gui/501")
    assert not (tmp_path / LAUNCHD_PLIST_REL).exists()
    assert runner.calls[-1] == ["launchctl", "bootout", "gui/501", LAUNCHD_LABEL]
    assert any("removed" in line for line in lines)


# ------------------------------------------------------------ windows seam path


def test_enable_windows_writes_the_run_key() -> None:
    registry = _FakeRegistry()
    lines = startup.enable("windows", registry=registry)
    assert registry.values == {RUN_VALUE_NAME: startup.daemon_command()}
    assert "up" in registry.values[RUN_VALUE_NAME]
    assert any("registered" in line for line in lines)


def test_disable_windows_deletes_the_run_key() -> None:
    registry = _FakeRegistry()
    startup.enable("windows", registry=registry)
    lines = startup.disable("windows", registry=registry)
    assert registry.values == {}
    assert registry.deleted == [RUN_VALUE_NAME]
    assert any("removed" in line for line in lines)


def test_registration_target_locations(tmp_path: Path) -> None:
    assert startup.registration_target("windows") == f"HKCU\\{WIN_RUN_SUBKEY}\\{RUN_VALUE_NAME}"
    assert startup.registration_target("linux", home=tmp_path) == str(tmp_path / SYSTEMD_UNIT_REL)
    assert startup.registration_target("macos", home=tmp_path) == str(tmp_path / LAUNCHD_PLIST_REL)


# ---------------------------------------------------------------------- status


def _pidfile(config_dir: Path, pid: int) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / startup.PIDFILE_NAME).write_text(f"{pid}\n", encoding="utf-8")


def test_status_windows_reports_registered_running(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    registry = _FakeRegistry()
    registry.values[RUN_VALUE_NAME] = startup.daemon_command()
    _pidfile(tmp_path, 4242)
    monkeypatch.setattr(startup, "pid_alive", lambda pid: pid == 4242)
    monkeypatch.setattr(startup, "_healthz_ok", lambda _baseurl: True)
    result = startup.status(
        "windows", registry=registry, config_dir=tmp_path, baseurl="http://127.0.0.1:7788"
    )
    assert result.platform == "windows"
    assert result.registered is True
    assert result.pid_alive is True
    assert result.healthz_ok is True
    assert result.running is True
    assert result.daemon_pid == 4242
    assert result.change_command == "mnemoseed startup disable"


def test_status_pid_alive_false_but_healthz_true_means_running(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _pidfile(tmp_path, 999999)
    # a freshly-restored pidfile may point at a dead pid; the healthz probe is
    # the authoritative running signal
    monkeypatch.setattr(startup, "pid_alive", lambda _pid: False)
    monkeypatch.setattr(startup, "_healthz_ok", lambda _baseurl: True)
    result = startup.status("linux", home=tmp_path, config_dir=tmp_path, baseurl="http://127.0.0.1:7788")
    assert result.registered is False
    assert result.running is True
    assert result.pid_alive is False
    assert result.change_command == "mnemoseed startup enable"


def test_status_linux_registered_by_file_presence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / SYSTEMD_UNIT_REL).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / SYSTEMD_UNIT_REL).write_text("[Unit]\n", encoding="utf-8")
    monkeypatch.setattr(startup, "_healthz_ok", lambda _baseurl: False)
    result = startup.status("linux", home=tmp_path, config_dir=tmp_path, baseurl="http://127.0.0.1:7788")
    assert result.registered is True
    assert result.running is False
    assert result.change_command == "mnemoseed startup disable"


# ------------------------------------------------------------------- CLI wiring


def test_cli_startup_enable_prints_registration_report(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    captured: list[tuple] = []

    def _fake_enable(platform: str | None = None, **kw: object) -> tuple[str, ...]:
        del platform, kw
        captured.append(("enable",))
        return ("registered: sample",)

    monkeypatch.setattr(startup, "enable", _fake_enable)
    status_code = cli.main(["startup", "enable"])
    output = capsys.readouterr().out
    assert status_code == 0
    assert "registered: sample" in output
    assert captured == [("enable",)]


def test_cli_startup_disable_prints_report(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(startup, "disable", lambda platform=None, **kw: ("removed: sample",))
    status_code = cli.main(["startup", "disable"])
    assert status_code == 0
    assert "removed: sample" in capsys.readouterr().out


def test_cli_startup_status_prints_all_report_fields(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = startup.StartupStatus(
        platform="windows",
        registered=True,
        running=True,
        pid_alive=True,
        healthz_ok=True,
        daemon_pid=12,
        target=f"HKCU\\{WIN_RUN_SUBKEY}\\{RUN_VALUE_NAME}",
        baseurl="http://127.0.0.1:7788",
        change_command="mnemoseed startup disable",
    )
    monkeypatch.setattr(startup, "status", lambda platform=None, **kw: fake)
    status_code = cli.main(["startup", "status"])
    output = capsys.readouterr().out
    assert status_code == 0
    by_label = {
        label.strip(): value.strip()
        for label, _, value in (line.partition(":") for line in output.splitlines())
    }
    assert by_label["platform"] == "windows"
    assert by_label["registered"] == "True"
    assert by_label["running"] == "True"
    assert by_label["pid"] == "12"
    assert by_label["baseurl"] == "http://127.0.0.1:7788"
    assert by_label["to change"] == "mnemoseed startup disable"


@pytest.mark.parametrize("subcommand", ["enable", "disable", "status"])
def test_startup_parser_accepts_each_subcommand(monkeypatch: pytest.MonkeyPatch, subcommand: str) -> None:
    """The parser validates the subcommand word before any seam runs."""

    def _noop(*_args: object, **_kwargs: object) -> tuple[str, ...]:
        return ()

    fake_status = startup.StartupStatus(
        platform="linux",
        registered=False,
        running=False,
        pid_alive=False,
        healthz_ok=False,
        daemon_pid=None,
        target="/tmp/none",
        baseurl="http://127.0.0.1:7788",
        change_command="mnemoseed startup enable",
    )
    replacement: object
    if subcommand == "status":
        replacement = lambda platform=None, **kw: fake_status  # noqa: E731
    else:
        replacement = _noop
    monkeypatch.setattr(startup, subcommand, replacement)
    assert cli.main(["startup", subcommand]) == 0
