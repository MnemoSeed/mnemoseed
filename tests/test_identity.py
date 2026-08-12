"""Identity chain (issue #14): first-run setup wizard + owner account + tokens.

Drives the REAL daemon app (embedded preset, synthetic embedder) through
TestClient so the wire contract is asserted, never internal helpers:

- FR-6.1a setup exactly once: POST /api/v1/setup creates the owner (argon2 hash
  at rest, default profile) and a second call is permanently 410.
- Pre-setup: every /memory/* and /api/v1/* read/write responds 503 with a setup
  pointer; /console statics stay reachable so the wizard page can load.
- Post-setup: the same surfaces require a profile token even from loopback
  (loopback implicit trust ends once an owner exists); wrong credentials 401.
- Login issues a one-shot bearer secret (only its sha256 digest is persisted in
  tokens.token_hash); /auth/logout revokes it; /auth/me resolves it.
- The owner hard limit is enforced at the identity-service layer (typed 409) and
  surfaces as a permanent 410 on the open setup route.
"""

from __future__ import annotations

import os
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mnemoseed.daemon.app import create_app
from mnemoseed.identity import IdentityService
from mnemoseed.identity.service import InvalidCredentialsError, OwnerExistsError
from mnemoseed.storage.drivers.lancedb_embedded import LanceDbEmbeddedStore
from mnemoseed.storage.drivers.sqlite_graph import SqliteGraphDriver
from mnemoseed.storage.drivers.sqlite_meta import SqliteMetaDriver
from mnemoseed.storage.drivers.synthetic_embedder import SyntheticEmbedder
from mnemoseed.storage.registry import (
    EMBED_DRIVERS,
    GRAPH_DRIVERS,
    META_DRIVERS,
    VECTOR_DRIVERS,
    register,
)

USERNAME = "owner"
PASSWORD = "a-strong-test-password"


@pytest.fixture(autouse=True)
def _ensure_real_drivers():
    """test_daemon clears the shared registries; re-register the real drivers."""
    for registry, cls in (
        (VECTOR_DRIVERS, LanceDbEmbeddedStore),
        (GRAPH_DRIVERS, SqliteGraphDriver),
        (META_DRIVERS, SqliteMetaDriver),
        (EMBED_DRIVERS, SyntheticEmbedder),
    ):
        if not registry.contains(cls.info.name):
            register(registry)(cls)
    yield


def _config_toml(tmp_path) -> object:
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        'preset = "embedded"\n'
        f'[storage.vector]\nuri = "{(tmp_path / "chunks.lance").as_posix()}"\ndimensions = 64\n'
        f'[storage.graph]\npath = "{(tmp_path / "cortex.db").as_posix()}"\n'
        f'[storage.meta]\npath = "{(tmp_path / "meta.db").as_posix()}"\n'
        f'[storage.embed]\ndriver = "synthetic"\ndimension = 64\n',
        encoding="utf-8",
    )
    return cfg


def _client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    monkeypatch.setattr("mnemoseed.config.CONFIG_PATH", _config_toml(tmp_path))
    monkeypatch.setattr("mnemoseed.dream.snapshot.CONFIG_DIR", tmp_path)
    return TestClient(create_app())


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _setup(client: TestClient) -> dict[str, object]:
    response = client.post("/api/v1/setup", json={"username": USERNAME, "password": PASSWORD})
    assert response.status_code == 201, response.text
    return response.json()


def _login(client: TestClient) -> str:
    response = client.post("/api/v1/auth/login", json={"username": USERNAME, "password": PASSWORD})
    assert response.status_code == 200, response.text
    token = response.json().get("token")
    assert token and isinstance(token, str)
    return token


def _meta_password_hash(client: TestClient) -> str | None:
    def _row():
        rows = client.app.state.stores.meta.list_users()
        return rows[0].password_hash if rows else None

    return client.portal.call(_row)


# ---------------------------------------------------------------- setup exact-once


def test_setup_creates_owner_and_default_profile(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        body = _setup(client)
        assert body["profile_id"] == "default"
        assert body["username"] == USERNAME
        assert body["setup_required"] is False
        # the owner is a single row, holding an argon2 hash -- never plaintext
        assert client.app.state.stores.meta.count_users() == 1
        assert client.app.state.stores.meta.get_profile("default").display_name == USERNAME


def test_setup_exactly_once_second_post_is_410(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        first = client.post("/api/v1/setup", json={"username": USERNAME, "password": PASSWORD})
        assert first.status_code == 201
        repeat = client.post("/api/v1/setup", json={"username": "another", "password": "x"})
        assert repeat.status_code == 410
        assert client.get("/api/v1/setup/status").json()["owner_exists"] is True
        # the single-user hard limit held: no second row was written
        assert client.app.state.stores.meta.count_users() == 1


def test_setup_requires_username_and_password(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        assert client.post("/api/v1/setup", json={"username": "", "password": "x"}).status_code == 422
        assert client.post("/api/v1/setup", json={"username": "nope", "password": ""}).status_code == 422
        assert client.app.state.stores.meta.count_users() == 0


def test_identity_service_owner_exactly_once_is_typed_409(tmp_path, monkeypatch) -> None:
    """The service layer enforces the hard limit with a typed error; the route
    maps it to a permanent 410."""
    with _client(tmp_path, monkeypatch) as client:
        store = client.app.state.stores.meta
        identity = IdentityService(store)
        identity.setup_owner("owner", "pw")
        with pytest.raises(OwnerExistsError):
            identity.setup_owner("second", "pw")
        assert store.count_users() == 1


def test_setup_status_pointer(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        assert client.get("/api/v1/setup/status").json() == {
            "setup_required": True,
            "owner_exists": False,
        }
        _setup(client)
        assert client.get("/api/v1/setup/status").json() == {
            "setup_required": False,
            "owner_exists": True,
        }


# ---------------------------------------------------------------- setup-mode gate (503)


def test_memory_and_console_are_503_with_setup_pointer_pre_setup(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        status = client.get("/api/v1/status")
        assert status.status_code == 503
        body = status.json()
        assert body["detail"]["setup_url"] == "/console/#/setup"
        assert "setup" in body["detail"].get("detail", "").lower() or "setup_required" in str(body["detail"])

        recall = client.post("/memory/recall", json={"profile_id": "default", "query": "anything"})
        assert recall.status_code == 503
        assert recall.json()["detail"]["setup_url"] == "/console/#/setup"


def test_login_before_setup_is_503(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        response = client.post("/api/v1/auth/login", json={"username": USERNAME, "password": PASSWORD})
        assert response.status_code == 503


def test_password_hash_uses_argon2_at_rest(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        _setup(client)
        stored = _meta_password_hash(client)
        assert stored is not None
        assert stored.startswith("$argon2id$")
        assert PASSWORD not in stored
        # the auth path verifies against the stored hash (round-trip, no plaintext)
        assert client.app.state.stores.meta.get_user_by_username(USERNAME).password_hash == stored


# ---------------------------------------------------------------- login / token lifecycle


def test_login_wrong_password_401(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        _setup(client)
        bad = client.post("/api/v1/auth/login", json={"username": USERNAME, "password": "not-the-password"})
        assert bad.status_code == 401
        ok = client.post("/api/v1/auth/login", json={"username": USERNAME, "password": PASSWORD})
        assert ok.status_code == 200
        assert ok.json()["token_type"] == "bearer"


def test_token_issue_and_me_roundtrip(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        _setup(client)
        token = _login(client)
        me = client.get("/api/v1/auth/me", headers=_bearer(token))
        assert me.status_code == 200
        body = me.json()
        assert body["username"] == USERNAME
        assert body["profile_id"] == "default"
        assert body["role"] == "owner"

        # an unknown secret resolves to nothing, and missing header is 401
        assert client.get("/api/v1/auth/me", headers=_bearer("bogus")).status_code == 401
        assert client.get("/api/v1/auth/me").status_code == 401


def test_logout_revokes_the_token(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        _setup(client)
        token = _login(client)
        assert client.get("/api/v1/auth/me", headers=_bearer(token)).status_code == 200
        revoked = client.post("/api/v1/auth/logout", headers=_bearer(token))
        assert revoked.status_code == 200
        assert client.get("/api/v1/auth/me", headers=_bearer(token)).status_code == 401
        # a fresh login yields a working token again
        token2 = _login(client)
        assert client.get("/api/v1/auth/me", headers=_bearer(token2)).status_code == 200


def test_token_secret_never_persisted_only_its_hash(tmp_path, monkeypatch) -> None:
    """Tokens at rest carry the sha256 digest of the bearer secret only."""
    with _client(tmp_path, monkeypatch) as client:
        _setup(client)
        token = _login(client)
        digest = __import__("hashlib").sha256(token.encode("utf-8")).hexdigest()
        row = client.app.state.stores.meta.authenticate_token(token)
        assert row is not None
        raw = client.app.state.stores.meta
        # authenticate_token resolves the digest back to the same row
        assert raw.authenticate_token(token).token_id == row.token_id
        # the token value itself is not the stored digest (hash at rest, not the secret)
        assert digest != token
        assert client.app.state.stores.meta.list_users()[0].password_hash != token


# ---------------------------------------------------------------- post-setup gate (401+token)


def test_loopback_implicit_trust_ends_once_owner_exists(tmp_path, monkeypatch) -> None:
    """After setup a loopback /api/v1 call still needs a token (issue #14);
    the loopback bypass only exists in the pre-setup setup mode."""
    with _client(tmp_path, monkeypatch) as client:
        # loopback, no token, pre-setup -> 503 (setup mode)
        assert client.get("/api/v1/status").status_code == 503
        _setup(client)
        # loopback, no token, post-setup -> 401
        assert client.get("/api/v1/status").status_code == 401
        token = _login(client)
        # loopback, valid token -> served
        assert client.get("/api/v1/status", headers=_bearer(token)).status_code == 200


def test_memory_surface_requires_token_after_setup(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        _setup(client)
        assert client.post("/memory/recall", json={"profile_id": "default", "query": "x"}).status_code == 401
        token = _login(client)
        assert (
            client.post(
                "/memory/recall",
                json={"profile_id": "default", "query": "x"},
                headers=_bearer(token),
            ).status_code
            != 401
        )


# ---------------------------------------------------------------- password rotation (auth reset)


def test_set_owner_password_rotates_login(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        _setup(client)
        identity = IdentityService(client.app.state.stores.meta)
        identity.set_owner_password("rotated-password")
        assert (
            client.post("/api/v1/auth/login", json={"username": USERNAME, "password": PASSWORD}).status_code
            == 401
        )
        ok = client.post("/api/v1/auth/login", json={"username": USERNAME, "password": "rotated-password"})
        assert ok.status_code == 200
        # the stored hash is again argon2 (never plaintext on rotation)
        assert _meta_password_hash(client).startswith("$argon2id$")


def test_set_owner_password_requires_existing_owner(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        with pytest.raises(InvalidCredentialsError):
            IdentityService(client.app.state.stores.meta).set_owner_password("x")


# ---------------------------------------------------------------- setup concurrency (issue #14)


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


def _boot_config(data: Path, port: int) -> None:
    """Write the embedded config the daemon subprocess boots with."""
    (data / "config.toml").write_text(
        'preset = "embedded"\n'
        f'[storage.vector]\nuri = "{(data / "chunks.lance").as_posix()}"\ndimensions = 64\n'
        f'[storage.graph]\npath = "{(data / "cortex.db").as_posix()}"\n'
        f'[storage.meta]\npath = "{(data / "meta.db").as_posix()}"\n'
        f'[storage.embed]\ndriver = "synthetic"\nmodel_dir = "{(data / "models").as_posix()}"\n',
        encoding="utf-8",
    )


def _boot_daemon(tmp_path: Path) -> tuple[subprocess.Popen[str], int, Path]:
    """Boot a real embedded daemon subprocess; returns (proc, port, data_dir)."""
    home = tmp_path / "home"
    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    port = _free_port()
    _boot_config(data, port)

    env = dict(os.environ)
    env["MNEMOSEED_USER_HOME"] = str(home)
    env["MNEMOSEED_HOME"] = str(data)
    env.pop("STORAGE_MODE", None)

    proc = subprocess.Popen(
        [*_console_script(), "up", "--host", "127.0.0.1", "--port", str(port)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return proc, port, data


def test_setup_concurrent_requests_create_exactly_one_owner(tmp_path) -> None:
    """FR-6.1a concurrency (issue #14): two threads POST /api/v1/setup at the
    same instant against a booted daemon. Setup used to be check-then-insert, so
    a race could create two owners -- or the losing insert hit the username
    UNIQUE and blew up as a naked IntegrityError 500. The atomic create_owner
    must leave exactly one 201, the loser the same typed 410 a late sequential
    setup gets, and exactly one owner row on disk."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    import httpx

    proc, port, data = _boot_daemon(tmp_path)
    results: list[tuple[int, str]] = []
    try:
        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline:
            try:
                response = httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=1.0)
                if response.status_code == 200 and response.json().get("status") == "ok":
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        else:
            raise AssertionError("daemon did not come up in time")

        setup_url = f"http://127.0.0.1:{port}/api/v1/setup"
        barrier = threading.Barrier(2)

        def _post() -> tuple[int, str]:
            barrier.wait()
            # 20s: pre-fix the losing write blocks on SQLite's busy_timeout
            # (~5s) before erroring out -- longer than httpx's 5s default so the
            # red state surfaces as [201, 500] instead of a ReadTimeout.
            with httpx.Client(timeout=20.0) as client:
                response = client.post(
                    setup_url, json={"username": "owner", "password": "a-strong-test-password"}
                )
                return response.status_code, response.text

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(_post) for _ in range(2)]
            results = [future.result() for future in futures]

        codes = sorted(code for code, _ in results)
        assert codes == [201, 410], results
        # never a naked 500 / IntegrityError traceback
        assert all("Traceback" not in body for _, body in results)
        loser_body = next(body for code, body in results if code != 201)
        assert "already completed" in loser_body
        # read the file while the daemon still runs: WAL lets a reader see the
        # committed winner, and a killed-process teardown cannot disturb it
        with sqlite3.connect(data / "meta.db") as conn:
            count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        assert count == 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
