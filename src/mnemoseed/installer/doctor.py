"""``mnemoseed doctor`` (FR-6.6): a battery of checks, one actionable fix per
failure, non-zero exit when anything fails.

Checks: daemon reachability on the configured URL, TCP port status, the
embedding load state, a real round-trip write+read+forget probe against the
daemon, and registration presence per detected host. A down daemon degrades the
run without aborting it: every other check still executes and no traceback
escapes (doctor reports, never crashes).
"""

from __future__ import annotations

import socket
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from mnemoseed.config import Config, load_config
from mnemoseed.identity.session import bearer_headers, resolve_token
from mnemoseed.installer.hosts import (
    MCP_SERVERS_KEY,
    MCP_SERVERS_TOML_KEY,
    MNEMOSEED_KEY,
    HostConfigError,
    detect_hosts,
    load_host_json,
    resolve_data_dir,
    resolve_home,
)
from mnemoseed.installer.tomlhost import load_host_toml

_PROBE_TIMEOUT = 3.0
_PROBE_PROFILE = "doctor-probe"


@dataclass(frozen=True)
class Check:
    """One check result with a single-line actionable fix when it failed."""

    name: str
    ok: bool
    detail: str
    fix: str | None = None


@dataclass(frozen=True)
class DoctorReport:
    """The full checklist plus the aggregate exit code."""

    baseurl: str
    checks: list[Check] = field(default_factory=list)

    @property
    def failed(self) -> list[Check]:
        return [check for check in self.checks if not check.ok]

    @property
    def exit_code(self) -> int:
        return 1 if self.failed else 0


HttpFn = Callable[..., httpx.Response]


def _http_request(
    method: str,
    url: str,
    *,
    timeout: float = _PROBE_TIMEOUT,
    json: Any | None = None,
) -> httpx.Response:
    # The profile-token gate (issue #14) closes /api/v1 and /memory once an
    # owner exists; when MNEMOSEED_TOKEN / the stored session holds a token,
    # doctor attaches it so the round-trip probe still works against a set-up
    # daemon. Absent a token it fails open: the probe just reports its 401/503.
    headers: dict[str, str] = {}
    token = resolve_token()
    if token:
        headers.update(bearer_headers(token))
    if method == "GET":
        return httpx.get(url, timeout=timeout, headers=headers)
    return httpx.post(url, json=json, timeout=timeout, headers=headers)


def _tcp_probe(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=_PROBE_TIMEOUT):
            return True
    except OSError:
        return False


def _host_port(baseurl: str) -> tuple[str, int]:
    parts = urlsplit(baseurl)
    host = parts.hostname or "127.0.0.1"
    try:
        port = parts.port
    except ValueError:
        port = None
    if port is None:
        port = 443 if parts.scheme == "https" else 80
    return host, int(port)


def _json_body(response: httpx.Response) -> dict[str, Any] | None:
    """Parse a JSON object response without ever crashing doctor on a malformed
    body (non-JSON 200, HTML error page, truncated replay, ...)."""
    try:
        body = response.json()
    except (ValueError, TypeError, AttributeError):
        return None
    if not isinstance(body, dict):
        return None
    return body


def _setup_pending(response: httpx.Response) -> bool:
    """True when a 503 is the issue-#14 setup pointer (owner account missing)."""
    if response.status_code != 503:
        return False
    body = _json_body(response)
    detail = (body or {}).get("detail")
    return isinstance(detail, dict) and "setup required" in str(detail.get("detail", ""))


def _round_trip(request: HttpFn, baseurl: str) -> tuple[bool, str]:
    """Write a probe pin, read it back, forget it. Returns (ok, detail).

    A reachable daemon that has not been set up yet (503 setup pointer) is not
    broken: the memory surface is intentionally closed until the owner account
    exists, so doctor reports the probe as skipped (ok) with an honest detail
    instead of a restart-worthy failure.
    """
    marker = f"mnemoseed doctor probe {time.time():.9f}"
    try:
        write = request(
            "POST", f"{baseurl}/memory/remember", json={"profile_id": _PROBE_PROFILE, "text": marker}
        )
        if write.status_code != 200:
            if _setup_pending(write):
                return True, "skipped (daemon ready, setup pending: run the console setup wizard)"
            return False, f"write failed (HTTP {write.status_code})"
        write_body = _json_body(write)
        if write_body is None:
            return False, "write ok but the response was not a JSON object"
        chunk_id = write_body.get("chunk_id")
        read = request(
            "POST", f"{baseurl}/memory/recall", json={"profile_id": _PROBE_PROFILE, "query": marker}
        )
        if read.status_code != 200:
            return False, f"read failed (HTTP {read.status_code})"
        body = _json_body(read)
        if body is None:
            return False, "read ok but the response was not a JSON object"
        entries = (body.get("memory") or {}).get("entries") or []
        # Near-duplicate reinforcement means a repeat probe text may not create
        # a new chunk; the write returns the surviving chunk_id either way, so
        # assert on the id landing in recall, not on the marker text.
        if not isinstance(entries, list) or not any(
            chunk_id and chunk_id == entry.get("id") for entry in entries
        ):
            return False, "write ok but recall did not surface the probe"
        if chunk_id:
            cleanup = request(
                "POST",
                f"{baseurl}/memory/forget_this",
                json={"profile_id": _PROBE_PROFILE, "chunk_id": chunk_id},
            )
            if cleanup.status_code != 200:
                return True, "write+read ok (probe cleanup failed)"
        return True, "write+read+forget ok"
    except httpx.HTTPError as exc:
        return False, f"probe failed ({type(exc).__name__})"


def _check_hosts(home: Path) -> list[Check]:
    detected = detect_hosts(home)
    if not detected:
        return [Check("hosts", True, "no hosts detected", None)]
    checks: list[Check] = []
    for spec in detected:
        try:
            if spec.format == "toml":
                data = load_host_toml(spec.config, missing_ok=True)
                servers = data.get(MCP_SERVERS_TOML_KEY)
            else:
                data = load_host_json(spec.config, missing_ok=True)
                servers = data.get(MCP_SERVERS_KEY)
            registered = isinstance(servers, dict) and MNEMOSEED_KEY in servers
        except HostConfigError as exc:
            checks.append(
                Check(
                    f"hosts.{spec.name}",
                    False,
                    f"{spec.display}: config unreadable ({exc})",
                    "repair the config, then rerun: mnemoseed doctor",
                )
            )
            continue
        if registered:
            checks.append(Check(f"hosts.{spec.name}", True, f"{spec.display}: registered", None))
        else:
            checks.append(
                Check(
                    f"hosts.{spec.name}",
                    False,
                    f"{spec.display}: not registered at {spec.config}",
                    "register the host: mnemoseed install",
                )
            )
    return checks


def run_doctor(
    config: Config | None = None,
    *,
    home: Path | None = None,
    data_dir: Path | None = None,
    request: HttpFn | None = None,
) -> DoctorReport:
    """Run the FR-6.6 checklist and return the report.

    ``request`` and the port probe are test seams; defaults talk to the real
    daemon. ``home`` / ``data_dir`` default to the environment-resolved paths.
    """
    cfg = config if config is not None else load_config()
    home = resolve_home(home)
    resolve_data_dir(data_dir)
    http = request if request is not None else _http_request
    baseurl = cfg.baseurl
    checks: list[Check] = []

    daemon_ok = False
    healthz: dict[str, Any] | None = None
    daemon_detail = f"unreachable at {baseurl}"
    try:
        response = http("GET", f"{baseurl}/healthz")
        if response.status_code == 200:
            body = _json_body(response)
            if body is None:
                daemon_detail = f"HTTP 200 without a JSON object at {baseurl}"
            else:
                healthz = body
                daemon_ok = body.get("status") == "ok"
                gate = body.get("gate")
                preset = body.get("preset")
                daemon_detail = (
                    f"reachable at {baseurl} (preset={preset}, "
                    f"gate={gate.get('ok') if isinstance(gate, dict) else gate})"
                )
        else:
            daemon_detail = f"HTTP {response.status_code} at {baseurl}"
    except httpx.HTTPError as exc:
        daemon_detail = f"unreachable at {baseurl} ({type(exc).__name__})"
    daemon_fix = None if daemon_ok else "start the daemon: mnemoseed up"
    checks.append(Check("daemon", daemon_ok, daemon_detail, daemon_fix))

    host, port = _host_port(baseurl)
    port_open = _tcp_probe(host, port)
    checks.append(
        Check(
            "port",
            port_open,
            f"TCP {host}:{port} {'open' if port_open else 'closed'}",
            None if port_open else f"listen on {host}:{port}: mnemoseed up",
        )
    )

    embed_driver: str | None = None
    if healthz is not None:
        stores = healthz.get("stores")
        if isinstance(stores, dict):
            embed = stores.get("embed")
            if isinstance(embed, dict):
                driver = embed.get("main")
                embed_driver = driver if isinstance(driver, str) else None
    if embed_driver:
        checks.append(Check("embedding", True, f"loaded in daemon ({embed_driver})", None))
    else:
        try:
            resolved = cfg.layer_instances("embed")["main"].driver
        except Exception:  # a broken config must not crash doctor
            resolved = None
        detail = (
            f"configured ({resolved}); load state unknown while the daemon is down"
            if resolved
            else "embed driver unresolvable from config"
        )
        checks.append(Check("embedding", False, detail, "start the daemon: mnemoseed up"))

    if daemon_ok and healthz is not None:
        round_ok, round_detail = _round_trip(http, baseurl)
        round_fix = None if round_ok else "restart the daemon: mnemoseed up"
        checks.append(Check("round-trip", round_ok, round_detail, round_fix))
    else:
        checks.append(
            Check("round-trip", False, "skipped (daemon unreachable)", "start the daemon: mnemoseed up")
        )

    checks.extend(_check_hosts(home))
    return DoctorReport(baseurl=baseurl, checks=checks)
