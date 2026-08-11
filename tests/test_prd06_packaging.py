"""PRD-06 T1 packaging guarantees (FR-6.2 / FR-6.8 / NFR-8.1):

- the default embedded boot path never invokes docker or docker-compose
  (zero-Docker guarantee, FR-6.2);
- the daemon reads the storage/embedding driver choices from
  ~/.mnemoseed/config.toml at boot — the single config source (FR-6.8);
- an embedded cold boot without a model download stays within a generous
  budget so packaging changes cannot regress boot order-of-magnitude
  (NFR-8.1 pin).
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from mnemoseed.daemon.app import create_app
from mnemoseed.storage.drivers import (
    bge_m3_onnx,
    lancedb_embedded,
    sqlite_graph,
    sqlite_meta,
    synthetic_embedder,
)
from mnemoseed.storage.registry import (
    EMBED_DRIVERS,
    GRAPH_DRIVERS,
    META_DRIVERS,
    VECTOR_DRIVERS,
    register,
)

_REAL_DRIVERS = (
    (VECTOR_DRIVERS, lancedb_embedded.LanceDbEmbeddedStore),
    (GRAPH_DRIVERS, sqlite_graph.SqliteGraphDriver),
    (META_DRIVERS, sqlite_meta.SqliteMetaDriver),
    (EMBED_DRIVERS, bge_m3_onnx.BgeM3OnnxEmbedder),
    (EMBED_DRIVERS, synthetic_embedder.SyntheticEmbedder),
)


@pytest.fixture(autouse=True)
def _ensure_real_drivers():
    """Other modules clear the driver registries; re-register the real M0
    drivers (plus the synthetic embedder) so the daemon boots below always
    resolves them."""
    for registry, cls in _REAL_DRIVERS:
        if not registry.contains(cls.info.name):
            register(registry)(cls)
    yield


def _embedded_config_toml(tmp_path: Path, embed_driver: str = "synthetic") -> Path:
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        'preset = "embedded"\n'
        f'[storage.vector]\nuri = "{(tmp_path / "chunks.lance").as_posix()}"\ndimensions = 64\n'
        f'[storage.graph]\npath = "{(tmp_path / "cortex.db").as_posix()}"\n'
        f'[storage.meta]\npath = "{(tmp_path / "meta.db").as_posix()}"\n'
        f"[storage.embed]\ndriver = {embed_driver!r}\n"
        f'model_dir = "{(tmp_path / "models").as_posix()}"\n',
        encoding="utf-8",
    )
    return cfg


def _console_script() -> list[str]:
    """Prefer the installed [project.scripts] entry, fall back to -m."""
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


def _wait_healthz(url: str, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            response = httpx.get(url, timeout=1.0)
            if response.status_code == 200:
                return response.json()
        except httpx.HTTPError as exc:
            last_error = exc
        time.sleep(0.2)
    raise AssertionError(f"/healthz not green within {timeout}s at {url} (last error: {last_error})")


# ------------------------------------------------------------ FR-6.2 zero-Docker


_SENTINEL_TRIGGER = """\
import os, sys


def _main() -> None:
    marker = os.environ.get("MNEMOSEED_DOCKER_SENTINEL")
    if not marker:
        return
    with open(marker, "a", encoding="utf-8") as fh:
        fh.write("docker-invoked\\n")
    os._exit(1)


_main()
"""

_SENTINEL_GUARD = """\
import os, sys
import subprocess


def _main() -> None:
    marker = os.environ.get("MNEMOSEED_DOCKER_SENTINEL")
    if not marker:
        return
    orig = subprocess.Popen.__init__
    if getattr(orig, "_mnemoseed_sentinel", False):
        return

    def patched(self, args, *a, **kw):
        first = args if isinstance(args, str) else (args[0] if args else "")
        base = os.path.basename(first).lower()
        if base in ("docker", "docker-compose", "docker.exe", "docker-compose.exe"):
            with open(marker, "a", encoding="utf-8") as fh:
                fh.write("docker-invoked\\n")
        orig(self, args, *a, **kw)

    patched._mnemoseed_sentinel = True
    subprocess.Popen.__init__ = patched


_main()
"""


def _write_docker_sentinels(shim: Path, marker: Path) -> None:
    """Install ``docker`` / ``docker-compose`` sentinels into ``shim``.

    Every sentinel records an invocation to ``marker`` and exits non-zero while
    otherwise doing nothing, so a clean embedded boot never notices it and a
    regressing boot path that shells out to docker trips the marker.

    POSIX gets executable shebang scripts. Windows additionally gets real
    ``docker.exe`` / ``docker-compose.exe`` -- copies of the base interpreter
    driven by a ``pythonXY._pth`` file -- because ``CreateProcess`` (how
    Python launches ``subprocess.run([\"docker\", ...])`` with shell=False)
    only resolves a name with no extension to ``.exe``: it skips the
    extensionless/.bat shims and a real docker.exe deeper in PATH would answer
    instead, making the sentinel a silent no-op there. The ``_pth`` loads the
    original stdlib by absolute path and a "trigger" sitecustomize so a hit
    records the marker and dies non-zero. A "guard" sitecustomize is handed to
    the boot subprocess via PYTHONPATH and patches subprocess.Popen there, so a
    docker launch from inside the boot path also records the marker -- even a
    ``docker --version`` call, which the interpreter answers before site init
    (so the trigger cannot see it) but which the guard sees before resolving.
    """
    for name in ("docker", "docker-compose"):
        posix = shim / name
        posix.write_text(
            f'#!/bin/sh\necho "docker-invoked" >> "{marker}"\nexit 1\n',
            encoding="utf-8",
        )
        posix.chmod(0o755)
        bat = shim / (name + ".bat")
        bat.write_text(
            f'@echo off\r\necho docker-invoked>> "{marker}"\r\nexit /b 1\r\n',
            encoding="utf-8",
        )
    if os.name == "nt":
        trigger = shim / "trigger_site"
        trigger.mkdir()
        (trigger / "sitecustomize.py").write_text(_SENTINEL_TRIGGER, encoding="utf-8")
        base = Path(sys._base_executable)
        for exe in ("docker.exe", "docker-compose.exe"):
            shutil.copy2(base, shim / exe)
        for dll in base.parent.glob("python*.dll"):
            shutil.copy2(dll, shim / dll.name)
        for dll in ("vcruntime140.dll", "vcruntime140_1.dll"):
            src = base.parent / dll
            if src.exists():
                shutil.copy2(src, shim / dll)
        tag = f"python{sys.version_info.major}{sys.version_info.minor}._pth"
        (shim / tag).write_text(
            "\n".join(
                [
                    str(base.parent / "Lib"),
                    str(base.parent / "DLLs"),
                    str(trigger),
                    "import site",
                    "",
                ]
            ),
            encoding="utf-8",
        )


def _sentinel_env(shim: Path, marker: Path, tmp_home: Path) -> dict[str, str]:
    """Environment for a boot under docker / docker-compose sentinels."""
    _write_docker_sentinels(shim, marker)
    env = dict(os.environ)
    env["PATH"] = str(shim) + os.pathsep + env.get("PATH", "")
    env["MNEMOSEED_HOME"] = str(tmp_home)
    env["MNEMOSEED_DOCKER_SENTINEL"] = str(marker)
    env.pop("STORAGE_MODE", None)
    if os.name == "nt":
        guard = shim / "guard_site"
        guard.mkdir()
        (guard / "sitecustomize.py").write_text(_SENTINEL_GUARD, encoding="utf-8")
        inherited = env.pop("PYTHONPATH", "")
        env["PYTHONPATH"] = str(guard) + (os.pathsep + inherited if inherited else "")
    return env


def _terminate(proc: subprocess.Popen[str]) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=10.0)
    except subprocess.TimeoutExpired:
        proc.kill()


def _probe_sentinel_authority(env: dict[str, str], marker: Path) -> None:
    """Assert the sentinel, not any real docker on the host, answers docker.

    The argv style deliberately avoids ``--version``: the interpreter answers
    that flag before site init, so the trigger cannot observe it (the guard
    inside the boot path covers that case). Any leading non-flag word is run
    through site first, so the trigger records the hit and exits non-zero.
    """
    probe = subprocess.run(["docker", "compose", "version"], env=env, capture_output=True, timeout=30.0)
    assert probe.returncode != 0, (
        "docker sentinel is not authoritative on this host (real docker answered the probe)"
    )
    assert marker.exists(), "docker sentinel did not record the probe invocation"


def test_up_embedded_never_invokes_docker(tmp_path, monkeypatch) -> None:
    """Default embedded boot with PATH shadowed by docker / docker-compose
    sentinels: the daemon still comes up green and the sentinels are never hit.

    The sentinels record any invocation to a marker file and exit non-zero, so
    even a best-effort docker call that the caller ignores would trip the test.
    """
    shim = tmp_path / "shim"
    shim.mkdir()
    marker = tmp_path / "docker-invoked"
    # On Windows CreateProcess resolves the program from the *calling* process
    # PATH (the Popen env dict does not steer it), so prepend the shim there;
    # the boot child still receives the same shim-first PATH via its env.
    monkeypatch.setenv("PATH", str(shim) + os.pathsep + os.environ.get("PATH", ""))
    env = _sentinel_env(shim, marker, tmp_path / "home")

    # The sentinel must actually shadow any docker installed on the machine:
    # the probe exits non-zero and writes the marker only when it hit our
    # interpreter, proving the guard is authoritative rather than vacuous.
    _probe_sentinel_authority(env, marker)
    marker.unlink()

    port = _free_port()
    url = f"http://127.0.0.1:{port}/healthz"
    proc = subprocess.Popen(
        [*_console_script(), "up", "--host", "127.0.0.1", "--port", str(port)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        body = _wait_healthz(url, timeout=60.0)
        assert body["status"] == "ok"
        assert body["preset"] == "embedded"
    finally:
        _terminate(proc)
    assert not marker.exists(), "embedded boot invoked docker or docker-compose"


def test_sentinel_files_a_docker_call_inside_the_boot_path(tmp_path, monkeypatch) -> None:
    """Regression: with the complete sentinels, a docker invocation made from
    inside the boot path is recorded to the marker.

    The sibling zero-Docker test asserts the marker is absent after a clean
    boot, so this proves that guard turns red exactly when the boot path
    regresses to shelling out to docker -- including a best-effort call the
    caller ignores (the daemon still comes up green here on purpose).
    """
    shim = tmp_path / "shim"
    shim.mkdir()
    marker = tmp_path / "docker-invoked"
    monkeypatch.setenv("PATH", str(shim) + os.pathsep + os.environ.get("PATH", ""))
    env = _sentinel_env(shim, marker, tmp_path / "home")

    wrapper = tmp_path / "_mutated_boot.py"
    wrapper.write_text(
        "import subprocess, sys\n"
        'subprocess.run(["docker", "--version"], timeout=30)\n'
        'sys.argv = ["mnemoseed", *sys.argv[1:]]\n'
        "from mnemoseed.cli import main\n"
        "main()\n",
        encoding="utf-8",
    )

    port = _free_port()
    url = f"http://127.0.0.1:{port}/healthz"
    proc = subprocess.Popen(
        [sys.executable, str(wrapper), "up", "--host", "127.0.0.1", "--port", str(port)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_healthz(url, timeout=60.0)
    finally:
        _terminate(proc)
    assert marker.exists(), "sentinel missed the docker call injected into the boot path"


# ------------------------------------------------------------ FR-6.8 config single source


def test_boot_reads_storage_choices_from_config_toml(tmp_path, monkeypatch) -> None:
    """The daemon's storage/embedding choices at boot come from config.toml
    alone: an explicit embed driver there wins over the embedded preset default
    even with the STORAGE_MODE shortcut set, and no env knob is consulted."""
    monkeypatch.setenv("STORAGE_MODE", "embedded")
    monkeypatch.setattr(
        "mnemoseed.config.CONFIG_PATH", _embedded_config_toml(tmp_path, embed_driver="synthetic")
    )

    with TestClient(create_app()) as client:
        body = client.get("/healthz").json()

    assert body["status"] == "ok"
    assert body["preset"] == "embedded"
    assert body["stores"]["embed"]["main"] == "synthetic"


# ------------------------------------------------------------ NFR-8.1 cold-start budget


def test_embedded_cold_boot_within_budget(tmp_path, monkeypatch) -> None:
    """Boot -> /healthz green with a synthetic embedder stays well under the
    30s packaging budget (NFR-8.1 measures <=10s on an ordinary dev machine
    with the model cached; this excludes model download entirely)."""
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    monkeypatch.setattr(
        "mnemoseed.config.CONFIG_PATH", _embedded_config_toml(tmp_path, embed_driver="synthetic")
    )

    started = time.perf_counter()
    with TestClient(create_app()) as client:
        body = client.get("/healthz").json()
        boot_ms = (time.perf_counter() - started) * 1000.0

    assert body["status"] == "ok"
    assert body["migrations"]["main"] >= 1
    assert boot_ms < 30_000.0, f"embedded cold boot took {boot_ms:.0f}ms (budget 30s)"
