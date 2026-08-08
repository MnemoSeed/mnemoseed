"""`mnemoseed up` at the process level (prd-08 FR-8.8 + NFR-8.1, AC-2):

- the console script boots the embedded single-process daemon (zero external
  services) and serves a healthy /healthz on a chosen port;
- a degraded/bad config fails fast with a clear message (non-zero exit);
- the daemon shuts the lifespan down cleanly (stores close) when the server is
  asked to stop, without a signal;
- `mnemoseed embed-sidecar` serves the dev OpenAI-compatible embeddings stub.
"""

from __future__ import annotations

import os
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from mnemoseed import __version__

_EMBED_MODEL = "text-embedding-synthetic"


@pytest.fixture(autouse=True)
def _no_storage_mode(monkeypatch):
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    yield


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


def _spawn_up(tmp_home: Path, port: int, extra_env: dict[str, str] | None = None) -> subprocess.Popen:
    env = dict(os.environ)
    env["MNEMOSEED_HOME"] = str(tmp_home)
    env.pop("STORAGE_MODE", None)
    if extra_env:
        env.update(extra_env)
    return subprocess.Popen(
        [*_console_script(), "up", "--host", "127.0.0.1", "--port", str(port)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def test_up_boots_embedded_and_healthz_green(tmp_path) -> None:
    port = _free_port()
    url = f"http://127.0.0.1:{port}/healthz"
    proc = _spawn_up(tmp_path, port)
    try:
        body = _wait_healthz(url, timeout=60.0)
        assert body["status"] == "ok"
        assert body["preset"] == "embedded"
        assert body["stores"]["vector"]["main"] == "lancedb_embedded"
        assert body["stores"]["graph"]["main"] == "sqlite_graph"
        assert body["stores"]["meta"]["main"] == "sqlite_meta"
        assert body["stores"]["embed"]["main"] == "bge_m3_onnx"
        assert body["gate"]["ok"] is True
        assert body["migrations"]["main"] >= 1

        # NFR-8.1: /healthz on the hot path stays under 100ms. Measure warm
        # latency over a reused connection (per-call httpx.get pays fresh TCP
        # setup that masks the endpoint latency), and take the min of a few
        # probes to shed scheduler jitter on a busy CI host.
        warm_ms: list[float] = []
        with httpx.Client() as client:
            for _ in range(5):
                started_req = time.perf_counter()
                response = client.get(url, timeout=2.0)
                warm_ms.append((time.perf_counter() - started_req) * 1000.0)
        assert response.status_code == 200
        assert min(warm_ms) < 100.0, f"/healthz min warm latency {min(warm_ms):.1f}ms"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10.0)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_up_server_shuts_down_cleanly(tmp_path, monkeypatch) -> None:
    """request_shutdown() runs uvicorn's shutdown path: lifespan teardown runs
    and the stores are closed before the server thread exits."""
    import uvicorn

    from mnemoseed.daemon import app as daemon_app
    from mnemoseed.daemon.runner import MnemoseedServer

    cfg_path = tmp_path / "config.toml"
    # as_posix(): Windows backslashes are invalid escapes in TOML strings
    cfg_path.write_text(
        'preset = "embedded"\n'
        f'[storage.vector]\nuri = "{(tmp_path / "chunks.lance").as_posix()}"\ndimensions = 64\n'
        f'[storage.graph]\npath = "{(tmp_path / "cortex.db").as_posix()}"\n'
        f'[storage.meta]\npath = "{(tmp_path / "meta.db").as_posix()}"\n'
        f'[storage.embed]\nmodel_dir = "{(tmp_path / "models").as_posix()}"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr("mnemoseed.config.CONFIG_PATH", cfg_path)

    port = _free_port()
    url = f"http://127.0.0.1:{port}/healthz"
    config = uvicorn.Config("mnemoseed.daemon.app:app", host="127.0.0.1", port=port, log_level="warning")
    server = MnemoseedServer(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        assert _wait_healthz(url, timeout=40.0)["status"] == "ok"
    finally:
        server.request_shutdown()
        thread.join(timeout=30.0)
    assert not thread.is_alive(), "server thread did not exit after request_shutdown"

    # lifefspan teardown closed the sqlite meta store: queries now fail
    # uvicorn serves the module-level `app` created by create_app()
    with pytest.raises(sqlite3.ProgrammingError):
        daemon_app.app.state.stores.meta.pool_state()


def test_up_bad_preset_fails_clearly(tmp_path) -> None:
    (tmp_path / "config.toml").write_text('preset = "nope"\n', encoding="utf-8")
    proc = _spawn_up(tmp_path, _free_port())
    out, err = proc.communicate(timeout=30.0)
    assert proc.returncode != 0
    combined = out + err
    assert "unknown preset" in combined
    assert "config[preset]" in combined


def _assert_clean_assembly_failure(proc: subprocess.Popen[str], *needles: str) -> None:
    """A bad storage config must exit 1 with a one-line error, never a traceback."""
    try:
        out, err = proc.communicate(timeout=60.0)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10.0)
    combined = out + err
    assert proc.returncode == 1, f"expected exit 1, got {proc.returncode}\n{combined}"
    # the daemon must never have announced itself as running
    assert "daemon on http" not in combined
    for needle in needles:
        assert needle in combined, f"missing {needle!r} in:\n{combined}"
    assert "Traceback" not in combined


def test_up_unknown_driver_fails_clearly(tmp_path) -> None:
    (tmp_path / "config.toml").write_text(
        'preset = "embedded"\n[storage.vector]\ndriver = "nosuchdriver"\n', encoding="utf-8"
    )
    proc = _spawn_up(tmp_path, _free_port())
    _assert_clean_assembly_failure(
        proc, "error:", "storage stack failed to build", "unknown vector driver", "nosuchdriver"
    )


def test_up_bad_driver_param_fails_clearly(tmp_path) -> None:
    # invalid driver params (negative dimensions) surface the same clean error
    (tmp_path / "config.toml").write_text(
        'preset = "embedded"\n[storage.vector]\n'
        f'uri = "{(tmp_path / "chunks.lance").as_posix()}"\ndimensions = -4\n',
        encoding="utf-8",
    )
    proc = _spawn_up(tmp_path, _free_port())
    _assert_clean_assembly_failure(
        proc, "error:", "storage stack failed to build", "dimensions must be positive"
    )


def test_embed_sidecar_stub_speaks_openai_protocol() -> None:
    from mnemoseed.daemon.embed_sidecar import _synthetic_vector
    from mnemoseed.daemon.embed_sidecar import create_app as create_sidecar_app

    with TestClient(create_sidecar_app()) as client:
        health = client.get("/healthz").json()
        assert health["status"] == "ok"
        assert health["service"] == "embed"

        payload = {"model": _EMBED_MODEL, "input": ["first", "second"]}
        data = client.post("/v1/embeddings", json=payload).json()
        entries = sorted(data["data"], key=lambda e: e["index"])
        assert [e["index"] for e in entries] == [0, 1]
        assert len(entries[0]["embedding"]) == 1024
        assert all(isinstance(v, float) for v in entries[0]["embedding"])
        # deterministic stub: same text in, same vector out
        assert entries[0]["embedding"] == _synthetic_vector("first")
        assert data["model"] == _EMBED_MODEL

        models = client.get("/v1/models").json()
        assert models["data"][0]["id"] == _EMBED_MODEL


@pytest.mark.parametrize("bad_input", [None, 42, True, 3.5, [], ["ok", 7], {"a": 1}])
def test_embed_sidecar_rejects_malformed_input(bad_input) -> None:
    from mnemoseed.daemon.embed_sidecar import create_app as create_sidecar_app

    with TestClient(create_sidecar_app()) as client:
        response = client.post("/v1/embeddings", json={"model": _EMBED_MODEL, "input": bad_input})
        assert response.status_code == 400
        body = response.json()
        assert body["error"]["message"]
        assert body["error"]["type"] == "invalid_request_error"
        assert body["error"]["param"] == "input"


def test_cli_reports_version() -> None:
    env = dict(os.environ)
    env.pop("STORAGE_MODE", None)
    proc = subprocess.run(
        [*_console_script(), "--version"],
        capture_output=True,
        text=True,
        env=env,
        timeout=30.0,
        check=False,
    )
    assert proc.returncode == 0
    assert __version__ in proc.stdout
