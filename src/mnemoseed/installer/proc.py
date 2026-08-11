"""Cross-platform process introspection for the installer daemon-stop path.

uninstall must never terminate an unrelated process: a stale pidfile whose pid
was later reused by another program belongs to a process we do not know. The
identity check compares the creation time recorded in the pidfile (written when
``up`` starts the daemon) with the live process's creation time, so a reused
pid fails the check and is left alone.
"""

from __future__ import annotations

import os
import signal
import time
from pathlib import Path

try:
    import ctypes
    from ctypes import wintypes
except ImportError:  # pragma: no cover - windows only
    ctypes = None  # type: ignore[assignment]
    wintypes = None  # type: ignore[assignment]

# 100-nanosecond offset between the Windows FILETIME epoch (1601) and unix.
_WIN_EPOCH_DELTA = 116_444_736_00_000_000


def pid_alive(pid: int) -> bool:
    """Whether a pid currently exists (no identity implied)."""
    if pid <= 0:
        return False
    if os.name == "nt" and ctypes is not None:
        return _win_pid_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def process_start_epoch(pid: int) -> float | None:
    """Unix epoch second when ``pid`` was created, or None if unreadable."""
    if pid <= 0:
        return None
    if os.name == "nt":
        return _win_start_epoch(pid)
    return _posix_start_epoch(pid)


def terminate(pid: int) -> bool:
    """Force-stop a pid (SIGTERM then SIGKILL, or TerminateProcess on Windows)."""
    if pid <= 0:
        return False
    if os.name == "nt" and ctypes is not None:
        return _win_terminate(pid)
    try:
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and pid_alive(pid):
            time.sleep(0.1)
        kill = getattr(signal, "SIGKILL", None)
        if kill is not None and pid_alive(pid):
            os.kill(pid, kill)
        return True
    except OSError:
        return False


def _win_pid_alive(pid: int) -> bool:
    assert ctypes is not None and wintypes is not None
    process_query_limited = 0x1000
    handle = ctypes.windll.kernel32.OpenProcess(process_query_limited, False, pid)  # type: ignore[attr-defined]
    if not handle:
        return False
    ctypes.windll.kernel32.CloseHandle(handle)  # type: ignore[attr-defined]
    return True


def _win_start_epoch(pid: int) -> float | None:
    assert ctypes is not None and wintypes is not None
    process_query_limited = 0x1000
    handle = ctypes.windll.kernel32.OpenProcess(process_query_limited, False, pid)  # type: ignore[attr-defined]
    if not handle:
        return None
    try:
        creation = wintypes.FILETIME()
        exit_time = wintypes.FILETIME()
        kernel_time = wintypes.FILETIME()
        user_time = wintypes.FILETIME()
        ok = ctypes.windll.kernel32.GetProcessTimes(  # type: ignore[attr-defined]
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel_time),
            ctypes.byref(user_time),
        )
        if not ok:
            return None
        raw = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
        return float(raw - _WIN_EPOCH_DELTA) / 10_000_000.0
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)  # type: ignore[attr-defined]


def _win_terminate(pid: int) -> bool:
    assert ctypes is not None and wintypes is not None
    process_terminate = 0x0001
    handle = ctypes.windll.kernel32.OpenProcess(  # type: ignore[attr-defined]
        process_terminate,
        False,
        pid,
    )
    if not handle:
        return False
    try:
        result = ctypes.windll.kernel32.TerminateProcess(  # type: ignore[attr-defined]
            handle,
            1,
        )
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)  # type: ignore[attr-defined]
    return bool(result) or not pid_alive(pid)


def _posix_start_epoch(pid: int) -> float | None:
    proc_stat = Path(f"/proc/{pid}/stat")
    if not proc_stat.exists():
        return None
    try:
        stat = proc_stat.read_text(encoding="ascii", errors="replace")
    except OSError:
        return None
    # comm may contain spaces / parens: everything after the last ")" is field 3+.
    tail = stat.rsplit(")", 1)
    if len(tail) != 2:
        return None
    parts = tail[1].split()
    if len(parts) < 20:
        return None
    try:
        start_ticks = int(parts[19])
    except ValueError:
        return None
    boot = _posix_boot_epoch()
    if boot is None:
        return None

    def _ticks_per_sec() -> int:
        sysconf = getattr(os, "sysconf", None)
        if sysconf is None:
            return 100
        return int(sysconf("SC_CLK_TCK"))

    return boot + start_ticks / max(float(_ticks_per_sec()), 1.0)


def _posix_boot_epoch() -> float | None:
    proc_stat = Path("/proc/stat")
    try:
        lines = proc_stat.read_text(encoding="ascii", errors="replace").splitlines()
    except OSError:
        return None
    for line in lines:
        if line.startswith("btime "):
            try:
                return float(line.split()[1])
            except (ValueError, IndexError):
                return None
    return None
