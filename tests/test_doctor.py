"""PRD-06 T2 doctor (FR-6.6): each failed check prints one actionable fix, the
exit code aggregates failures, and a down daemon degrades without aborting.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from mnemoseed.config import Config
from mnemoseed.installer.doctor import run_doctor


@dataclass
class FakeResponse:
    status_code: int
    payload: object

    def json(self):
        return self.payload


class FakeDaemon:
    """A scriptable daemon seam: replies to healthz and the round-trip surface,
    echoes back the remembered probe text on recall, and can simulate a down
    daemon or a recall that misses the probe."""

    def __init__(self, *, down: bool = False, recall_matches: bool = True, reinforced: bool = False) -> None:
        self.down = down
        self.recall_matches = recall_matches
        self.reinforced = reinforced
        self.remembered: str | None = None
        self.calls: list[tuple[str, str]] = []

    def __call__(self, method: str, url: str, json: object = None) -> FakeResponse:
        self.calls.append((method, url))
        if self.down:
            raise httpx.ConnectError("connection refused")
        if url.endswith("/healthz"):
            return FakeResponse(
                200,
                {
                    "status": "ok",
                    "preset": "embedded",
                    "stores": {"embed": {"main": "synthetic"}},
                    "gate": {"ok": True},
                },
            )
        if url.endswith("/memory/remember"):
            self.remembered = (json or {}).get("text")
            if self.reinforced:
                # near-duplicate hit: no new chunk; the surviving id differs
                # from what a fresh write would have produced
                return FakeResponse(200, {"outcome": "reinforced", "chunk_id": "probe-old"})
            return FakeResponse(200, {"outcome": "new_chunk", "chunk_id": "probe-1"})
        if url.endswith("/memory/recall"):
            entries = []
            if self.recall_matches and self.remembered:
                if self.reinforced:
                    # the stored text is the earlier probe's, not this marker's
                    entries = [{"kind": "chunk", "id": "probe-old", "text": "older probe text"}]
                else:
                    entries = [{"kind": "chunk", "id": "probe-1", "text": self.remembered}]
            return FakeResponse(200, {"memory": {"entries": entries}})
        if url.endswith("/memory/forget_this"):
            return FakeResponse(200, {"removed": {"chunks": ["probe-1"], "nodes": []}})
        return FakeResponse(404, {"detail": "not found"})


def _mark_ports(monkeypatch, open_port: bool) -> None:
    monkeypatch.setattr(
        "mnemoseed.installer.doctor._tcp_probe",
        lambda host, port: open_port,
    )


def test_doctor_all_green_with_daemon(tmp_path, monkeypatch) -> None:
    daemon = FakeDaemon()
    _mark_ports(monkeypatch, open_port=True)
    report = run_doctor(Config(baseurl="http://127.0.0.1:7788"), home=tmp_path, request=daemon)

    assert report.exit_code == 0, report.checks
    assert all(check.ok for check in report.checks)

    by_name = {check.name: check for check in report.checks}
    assert by_name["daemon"].ok
    assert "embedded" in by_name["daemon"].detail
    assert by_name["port"].ok
    assert by_name["embedding"].ok and "synthetic" in by_name["embedding"].detail
    assert by_name["round-trip"].ok
    assert by_name["hosts"].ok

    # the round-trip really ran: remember -> recall -> forget against the daemon
    assert ("POST", "http://127.0.0.1:7788/memory/remember") in daemon.calls
    assert ("POST", "http://127.0.0.1:7788/memory/recall") in daemon.calls
    assert ("POST", "http://127.0.0.1:7788/memory/forget_this") in daemon.calls


def test_doctor_daemon_down_still_runs_every_other_check(tmp_path, monkeypatch) -> None:
    daemon = FakeDaemon(down=True)
    _mark_ports(monkeypatch, open_port=False)
    report = run_doctor(Config(baseurl="http://127.0.0.1:7788"), home=tmp_path, request=daemon)

    assert report.exit_code != 0
    failed = {check.name: check for check in report.failed}
    assert "daemon" in failed
    assert "port" in failed
    assert "embedding" in failed
    assert "round-trip" in failed

    # hosts still ran and passed (nothing detected)
    hosts = next(check for check in report.checks if check.name == "hosts")
    assert hosts.ok

    # every failed check carries a single-line actionable fix
    for check in report.failed:
        assert check.fix and "mnemoseed" in check.fix

    # a degraded daemon never escapes a traceback
    assert not any("Traceback" in check.detail for check in report.checks)


def test_doctor_round_trip_surfaces_probe_write(tmp_path, monkeypatch) -> None:
    daemon = FakeDaemon(recall_matches=False)
    _mark_ports(monkeypatch, open_port=True)
    report = run_doctor(Config(baseurl="http://127.0.0.1:7788"), home=tmp_path, request=daemon)

    assert report.exit_code != 0
    round_trip = next(check for check in report.failed if check.name == "round-trip")
    assert "did not surface" in round_trip.detail
    assert round_trip.fix


def test_doctor_round_trip_accepts_reinforced_probe(tmp_path, monkeypatch) -> None:
    """A probe text near-identical to a stored one reinforces instead of
    creating a chunk; the surviving chunk_id still surfaces in recall, and
    the round-trip must accept that (issue #11)."""
    daemon = FakeDaemon(reinforced=True)
    _mark_ports(monkeypatch, open_port=True)
    report = run_doctor(Config(baseurl="http://127.0.0.1:7788"), home=tmp_path, request=daemon)

    round_trip = next(check for check in report.checks if check.name == "round-trip")
    assert round_trip.ok, round_trip.detail


def test_doctor_reports_host_registration_presence(tmp_path, monkeypatch) -> None:
    daemon = FakeDaemon()
    _mark_ports(monkeypatch, open_port=True)
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / ".claude.json").write_text(
        '{"mcpServers": {"mnemoseed": {"command": "mnemoseed", "args": ["mcp"]}}}\n', encoding="utf-8"
    )
    (home / ".codex").mkdir(parents=True, exist_ok=True)

    report = run_doctor(Config(baseurl="http://127.0.0.1:7788"), home=home, request=daemon)

    by_host = {check.name: check for check in report.checks if check.name.startswith("hosts.")}
    assert by_host["hosts.claude-code"].ok
    assert by_host["hosts.codex"].ok is False
    assert by_host["hosts.codex"].fix == "register the host: mnemoseed install"


def test_doctor_unreadable_host_config_reports_not_crash(tmp_path, monkeypatch) -> None:
    daemon = FakeDaemon()
    _mark_ports(monkeypatch, open_port=True)
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / ".claude.json").write_text("{broken", encoding="utf-8")

    report = run_doctor(Config(baseurl="http://127.0.0.1:7788"), home=home, request=daemon)

    unreadable = next(check for check in report.failed if check.name == "hosts.claude-code")
    assert "unreadable" in unreadable.detail
    assert unreadable.fix  # one actionable fix line


class NonJsonDaemon:
    """A daemon that answers 200 but whose body is not a JSON object."""

    def __init__(self, *, raises: bool = False) -> None:
        self.raises = raises

    def __call__(self, method: str, url: str, json: object = None) -> FakeResponse:
        if self.raises:

            class _Broken(FakeResponse):
                def json(self):
                    raise ValueError("no json here")

            return _Broken(status_code=200, payload=None)
        return FakeResponse(200, "<html><body>gateway</body></html>")


def test_doctor_daemon_non_json_200_reports_fail_without_crash(tmp_path, monkeypatch) -> None:
    """F2 regression: a 200 whose body is not a JSON object must degrade the
    daemon check to fail + fix, never escape a traceback, and keep running the
    other checks."""
    daemon = NonJsonDaemon()
    _mark_ports(monkeypatch, open_port=True)
    report = run_doctor(Config(baseurl="http://127.0.0.1:7788"), home=tmp_path, request=daemon)

    failed = {check.name: check for check in report.failed}
    assert "daemon" in failed
    assert "JSON object" in failed["daemon"].detail
    assert failed["daemon"].fix
    assert "round-trip" in {check.name for check in report.checks}
    assert not any("Traceback" in check.detail for check in report.checks)


def test_doctor_daemon_json_raise_reports_fail_without_crash(tmp_path, monkeypatch) -> None:
    """F2 regression: ``.json()`` itself raising (ValueError from a non-JSON
    body) must not crash the whole doctor run."""
    daemon = NonJsonDaemon(raises=True)
    _mark_ports(monkeypatch, open_port=True)
    report = run_doctor(Config(baseurl="http://127.0.0.1:7788"), home=tmp_path, request=daemon)

    failed = {check.name: check for check in report.failed}
    assert "daemon" in failed
    assert failed["daemon"].fix
    assert not any("Traceback" in check.detail for check in report.checks)


# ------------------------------------------------------------ live daemon smoke


def _console_script() -> list[str]:
    bin_dir = Path(sys.executable).parent
    for name in ("mnemoseed.exe", "mnemoseed", "mnemoseed.bat"):
        candidate = bin_dir / name
        if candidate.exists():
            return [str(candidate)]
    return [sys.executable, "-m", "mnemoseed.cli"]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _embedded_config(tmp_path: Path, port: int) -> Path:
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        'preset = "embedded"\n'
        f'baseurl = "http://127.0.0.1:{port}"\n'
        f'[storage.vector]\nuri = "{(tmp_path / "chunks.lance").as_posix()}"\ndimensions = 64\n'
        f'[storage.graph]\npath = "{(tmp_path / "cortex.db").as_posix()}"\n'
        f'[storage.meta]\npath = "{(tmp_path / "meta.db").as_posix()}"\n'
        f'[storage.embed]\ndriver = "synthetic"\nmodel_dir = "{(tmp_path / "models").as_posix()}"\n',
        encoding="utf-8",
    )
    return cfg


def test_doctor_live_daemon_all_green(tmp_path) -> None:
    """End-to-end FR-6.6: a booted daemon + doctor comes out all green and the
    round-trip probe really writes and reads through /memory."""
    home = tmp_path / "home"
    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    port = _free_port()
    cfg = _embedded_config(data, port)

    env = dict(os.environ)
    env["MNEMOSEED_USER_HOME"] = str(home)
    env["MNEMOSEED_HOME"] = str(data)
    env.pop("STORAGE_MODE", None)

    url = f"http://127.0.0.1:{port}/healthz"
    proc = subprocess.Popen(
        [*_console_script(), "up", "--host", "127.0.0.1", "--port", str(port)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline:
            try:
                response = httpx.get(url, timeout=1.0)
                if response.status_code == 200 and response.json().get("status") == "ok":
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        else:
            raise AssertionError("daemon did not come up in time")

        doctor = subprocess.run(
            [*_console_script(), "doctor"],
            env=env,
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    assert cfg.exists()
    assert doctor.returncode == 0, doctor.stdout + doctor.stderr
    assert "all checks passed" in doctor.stdout
    assert "round-trip" in doctor.stdout
