r"""Cross-platform daemon autostart (issue #6): ``mnemoseed startup``.

``enable`` registers the daemon to launch at login/boot; ``disable`` removes
that registration; ``status`` reports whether it is registered and whether the
daemon is currently running (pidfile + /healthz). Each platform has one native
registration surface:

=========  ============================================================  =========================
platform   registration                                                 control
=========  ============================================================  =========================
windows    HKCU\Software\Microsoft\Windows\CurrentVersion\Run\MnemoSeed  (none -- fires at login)
linux      ~/.config/systemd/user/mnemoseed.service                     systemctl --user
macos      ~/Library/LaunchAgents/ai.mnemoseed.daemon.plist             launchctl
=========  ============================================================  =========================

The Windows Run key is the simplest reliable per-user mechanism for v1 (no
scheduler policies, no elevated rights); systemd user units and launchd agents
are the native desktop-login autostart on their platforms.

Every system-side effect (the registry, systemctl, launchctl) sits behind an
injected seam, so all three platform paths are exercised on any machine: the
Linux/macOS writers are asserted on the FILES they produce plus the command
lists they would run, and the Windows path never touches the real registry.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from mnemoseed.config import CONFIG_DIR, ConfigError, load_config
from mnemoseed.installer.proc import pid_alive
from mnemoseed.installer.state import PIDFILE_NAME

RUN_VALUE_NAME = "MnemoSeed"
WIN_RUN_SUBKEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
SYSTEMD_UNIT_NAME = "mnemoseed.service"
LAUNCHD_LABEL = "ai.mnemoseed.daemon"
SYSTEMD_UNIT_REL = Path(".config") / "systemd" / "user" / SYSTEMD_UNIT_NAME
LAUNCHD_PLIST_REL = Path("Library") / "LaunchAgents" / "ai.mnemoseed.daemon.plist"

_HEALTHZ_TIMEOUT_S = 1.0


def current_platform() -> str:
    """Normalized platform name: ``windows`` | ``macos`` | ``linux``."""
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


# ------------------------------------------------------------------ seams


class Registry(Protocol):
    """Windows Run-key surface (HKCU\\...\\CurrentVersion\\Run)."""

    def read(self, name: str) -> str | None: ...
    def write(self, name: str, command: str) -> None: ...
    def delete(self, name: str) -> None: ...


class CommandRunner(Protocol):
    """Runs a systemctl / launchctl invocation (system side effect)."""

    def run(self, argv: list[str]) -> None: ...


def _default_registry() -> Registry:
    """The real HKCU Run key. Windows-only: ``import winreg`` raises elsewhere,
    and this factory is only reached when the current platform is `windows`."""
    import winreg

    class _WinregRegistry:
        def __init__(self) -> None:
            self._hive = winreg.HKEY_CURRENT_USER
            self._subkey = WIN_RUN_SUBKEY

        def read(self, name: str) -> str | None:
            try:
                with winreg.OpenKey(self._hive, self._subkey, 0, winreg.KEY_READ) as key:
                    value, _ = winreg.QueryValueEx(key, name)
                    return str(value)
            except OSError:
                return None

        def write(self, name: str, command: str) -> None:
            with winreg.CreateKeyEx(self._hive, self._subkey, 0, winreg.KEY_WRITE) as key:
                winreg.SetValueEx(key, name, 0, winreg.REG_SZ, command)

        def delete(self, name: str) -> None:
            try:
                with winreg.OpenKey(self._hive, self._subkey, 0, winreg.KEY_WRITE) as key:
                    winreg.DeleteValue(key, name)
            except OSError:
                pass

    return _WinregRegistry()


class _SubprocessRunner:
    """Runs control commands with a bounded wait (never ``check=True``: the CLI
    still reports failure to the user even for a nonzero exit)."""

    def run(self, argv: list[str]) -> None:
        subprocess.run(argv, check=False)


def _default_runner() -> CommandRunner:
    return _SubprocessRunner()


# ------------------------------------------------------------ daemon command


def daemon_argv() -> list[str]:
    """``[executable, "up"]`` -- the boot-time program and its flag.

    Resolution order (issue #6, "same executable resolution as
    ``sys.argv[0]`` / ``shutil.which``"): the current invocation when it IS
    the mnemoseed console script, then ``mnemoseed`` on PATH, then
    ``python -m mnemoseed.cli``.
    """
    if sys.argv and sys.argv[0]:
        name = Path(sys.argv[0]).name.lower()
        if name in ("mnemoseed", "mnemoseed.exe", "mnemoseed.bat"):
            return [str(Path(sys.argv[0]).resolve()), "up"]
    found = shutil.which("mnemoseed")
    if found:
        return [str(Path(found).resolve()), "up"]
    return [sys.executable, "-m", "mnemoseed.cli", "up"]


def daemon_command() -> str:
    """Single-string form for the Windows Run key and the systemd ``ExecStart``."""
    return " ".join(_shell_quote(part) for part in daemon_argv())


def _shell_quote(part: str) -> str:
    return f'"{part}"' if (" " in part or not part) else part


# ---------------------------------------------------------- unit/plist texts


def systemd_unit_text(command: str) -> str:
    """systemd user unit: keep the daemon alive across crashes and start it at
    every user login (default.target is the desktop-login target)."""
    return (
        "[Unit]\n"
        "Description=MnemoSeed daemon\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={command}\n"
        "Restart=on-failure\n"
        "RestartSec=2\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def launchd_plist_text(argv: list[str], *, home: Path | None = None) -> str:
    """launchd user LaunchAgent: run at load, keep alive, logs under ~/.mnemoseed."""
    log_dir = (Path(home) if home is not None else Path.home()) / ".mnemoseed"
    entries = "\n    ".join(f"<string>{_escape_xml(part)}</string>" for part in argv)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        "<dict>\n"
        f"    <key>Label</key>\n    <string>{LAUNCHD_LABEL}</string>\n"
        "    <key>ProgramArguments</key>\n    <array>\n"
        f"    {entries}\n"
        "    </array>\n"
        "    <key>RunAtLoad</key>\n    <true/>\n"
        "    <key>KeepAlive</key>\n    <true/>\n"
        f"    <key>StandardOutPath</key>\n    <string>{(log_dir / 'daemon.log').as_posix()}</string>\n"
        f"    <key>StandardErrorPath</key>\n"
        f"    <string>{(log_dir / 'daemon-error.log').as_posix()}</string>\n"
        "</dict>\n"
        "</plist>\n"
    )


def _escape_xml(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def _launchd_domain() -> str:
    uid = getattr(os, "getuid", lambda: -1)()
    if uid < 0:
        raise RuntimeError("cannot determine the launchd user domain (os.getuid unavailable)")
    return f"gui/{int(uid)}"


def registration_target(platform: str, *, home: Path | None = None) -> str:
    """Where this platform's registration lives (for reporting)."""
    if platform == "windows":
        return f"HKCU\\{WIN_RUN_SUBKEY}\\{RUN_VALUE_NAME}"
    base = Path(home) if home is not None else Path.home()
    if platform == "macos":
        return str(base / LAUNCHD_PLIST_REL)
    return str(base / SYSTEMD_UNIT_REL)


# ------------------------------------------------------------------ actions


def enable(
    platform: str | None = None,
    *,
    registry: Registry | None = None,
    runner: CommandRunner | None = None,
    home: Path | None = None,
    domain: str | None = None,
) -> tuple[str, ...]:
    """Register the daemon to start at login/boot; returns lines to print.

    ``home`` overrides the home directory (Linux: ``.config/systemd/user``,
    macOS: ``Library/LaunchAgents``) for tests; ``registry`` / ``runner`` /
    ``domain`` are the system-side seams.
    """
    platform = platform or current_platform()
    if platform == "windows":
        reg = registry if registry is not None else _default_registry()
        command = daemon_command()
        reg.write(RUN_VALUE_NAME, command)
        return (f"registered {RUN_VALUE_NAME} = {command} under HKCU\\{WIN_RUN_SUBKEY}",)
    if platform == "macos":
        return _enable_macos(home=home, runner=runner, domain=domain)
    return _enable_linux(home=home, runner=runner)


def disable(
    platform: str | None = None,
    *,
    registry: Registry | None = None,
    runner: CommandRunner | None = None,
    home: Path | None = None,
    domain: str | None = None,
) -> tuple[str, ...]:
    """Remove the boot-time registration; returns lines to print."""
    platform = platform or current_platform()
    if platform == "windows":
        reg = registry if registry is not None else _default_registry()
        reg.delete(RUN_VALUE_NAME)
        return (f"removed {RUN_VALUE_NAME} from HKCU\\{WIN_RUN_SUBKEY}",)
    if platform == "macos":
        return _disable_macos(home=home, runner=runner, domain=domain)
    return _disable_linux(home=home, runner=runner)


def _enable_linux(home: Path | None, runner: CommandRunner | None) -> tuple[str, ...]:
    target = (Path(home) if home is not None else Path.home()) / SYSTEMD_UNIT_REL
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(systemd_unit_text(daemon_command()), encoding="utf-8")
    run = runner if runner is not None else _default_runner()
    run.run(["systemctl", "--user", "enable", "--now", SYSTEMD_UNIT_NAME])
    return (f"wrote {target}", "systemctl --user enable --now " + SYSTEMD_UNIT_NAME)


def _disable_linux(home: Path | None, runner: CommandRunner | None) -> tuple[str, ...]:
    run = runner if runner is not None else _default_runner()
    run.run(["systemctl", "--user", "disable", "--now", SYSTEMD_UNIT_NAME])
    target = (Path(home) if home is not None else Path.home()) / SYSTEMD_UNIT_REL
    target.unlink(missing_ok=True)
    return ("systemctl --user disable --now " + SYSTEMD_UNIT_NAME, f"removed {target}")


def _enable_macos(home: Path | None, runner: CommandRunner | None, domain: str | None) -> tuple[str, ...]:
    target = (Path(home) if home is not None else Path.home()) / LAUNCHD_PLIST_REL
    target.parent.mkdir(parents=True, exist_ok=True)
    log_dir = (Path(home) if home is not None else Path.home()) / ".mnemoseed"
    log_dir.mkdir(parents=True, exist_ok=True)
    target.write_text(launchd_plist_text(daemon_argv(), home=home), encoding="utf-8")
    run = runner if runner is not None else _default_runner()
    bootstrap_domain = domain if domain is not None else _launchd_domain()
    run.run(["launchctl", "bootstrap", bootstrap_domain, str(target)])
    return (f"wrote {target}", f"launchctl bootstrap {bootstrap_domain} {target}")


def _disable_macos(home: Path | None, runner: CommandRunner | None, domain: str | None) -> tuple[str, ...]:
    run = runner if runner is not None else _default_runner()
    bootout_domain = domain if domain is not None else _launchd_domain()
    run.run(["launchctl", "bootout", bootout_domain, LAUNCHD_LABEL])
    target = (Path(home) if home is not None else Path.home()) / LAUNCHD_PLIST_REL
    target.unlink(missing_ok=True)
    return (f"launchctl bootout {bootout_domain} {LAUNCHD_LABEL}", f"removed {target}")


# ------------------------------------------------------------------- status


@dataclass(frozen=True)
class StartupStatus:
    """Status report for ``mnemoseed startup status``."""

    platform: str
    registered: bool
    running: bool
    pid_alive: bool
    healthz_ok: bool
    daemon_pid: int | None
    target: str
    baseurl: str
    change_command: str


def status(
    platform: str | None = None,
    *,
    registry: Registry | None = None,
    home: Path | None = None,
    config_dir: Path | None = None,
    baseurl: str | None = None,
) -> StartupStatus:
    """Report registration + current running state."""
    platform = platform or current_platform()
    target = registration_target(platform, home=home)
    if platform == "windows":
        reg = registry if registry is not None else _default_registry()
        registered = reg.read(RUN_VALUE_NAME) is not None
    else:
        registered = Path(target).exists()

    pid = _daemon_pid(config_dir if config_dir is not None else CONFIG_DIR)
    alive = pid_alive(pid) if pid is not None else False
    resolved_baseurl = _resolve_baseurl(baseurl)
    healthy = _healthz_ok(resolved_baseurl)
    return StartupStatus(
        platform=platform,
        registered=registered,
        running=alive or healthy,
        pid_alive=alive,
        healthz_ok=healthy,
        daemon_pid=pid,
        target=target,
        baseurl=resolved_baseurl,
        change_command="mnemoseed startup enable" if not registered else "mnemoseed startup disable",
    )


def _daemon_pid(config_dir: Path) -> int | None:
    pidfile = config_dir / PIDFILE_NAME
    if not pidfile.exists():
        return None
    try:
        raw = pidfile.read_text(encoding="utf-8").splitlines()[0].strip()
        pid = int(raw)
        return pid if pid > 0 else None
    except (OSError, ValueError, IndexError):
        return None


def _resolve_baseurl(baseurl: str | None) -> str:
    if baseurl:
        return baseurl.rstrip("/")
    try:
        return load_config().baseurl.rstrip("/")
    except ConfigError:
        return "http://127.0.0.1:7788"


def _healthz_ok(baseurl: str) -> bool:
    import httpx

    try:
        response = httpx.get(f"{baseurl.rstrip('/')}/healthz", timeout=_HEALTHZ_TIMEOUT_S)
        return response.status_code == 200
    except Exception:
        return False
