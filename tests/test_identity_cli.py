"""Identity CLI + client seams (issue #14): login/logout/whoami/auth reset.

Drives the REAL daemon app (embedded preset, synthetic embedder) through
TestClient, routing the CLI's httpx calls back into that in-process client:

- ``mnemoseed login`` persists a profile-token session file (owner-only on
  POSIX) and never echoes the token value.
- ``mnemoseed logout`` revokes the token server-side and deletes the file.
- ``mnemoseed whoami`` reports the stored identity against the daemon and fails
  cleanly on an invalid/revoked token.
- ``mnemoseed auth reset`` rotates the owner password locally (direct
  meta-store access, no HTTP), enforcing the argon2 hash at rest.
- The MCP memory client attaches ``Authorization: Bearer`` when
  ``MNEMOSEED_TOKEN`` is set and stays fail-open (no header) otherwise.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from mnemoseed.cli import main
from mnemoseed.daemon.app import create_app
from mnemoseed.identity import IdentityService
from mnemoseed.identity.passwords import verify_password
from mnemoseed.identity.session import AuthSession, file_perms, save_session
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
BASE_URL = "http://testserver"


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


def _config_toml(tmp_path) -> Path:
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
    monkeypatch.delenv("MNEMOSEED_TOKEN", raising=False)
    monkeypatch.setattr("mnemoseed.config.CONFIG_PATH", _config_toml(tmp_path))
    monkeypatch.setattr("mnemoseed.dream.snapshot.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("mnemoseed.identity.session.TOKEN_PATH", tmp_path / "token.json")
    return TestClient(create_app())


def _setup(client: TestClient) -> None:
    response = client.post("/api/v1/setup", json={"username": USERNAME, "password": PASSWORD})
    assert response.status_code == 201, response.text


def _login_token(client: TestClient) -> str:
    response = client.post("/api/v1/auth/login", json={"username": USERNAME, "password": PASSWORD})
    assert response.status_code == 200, response.text
    token = response.json().get("token")
    assert token and isinstance(token, str)
    return token


@pytest.fixture
def _http_to_daemon(monkeypatch):
    """Route the CLI's httpx.post/get into the in-process TestClient."""

    def install(client: TestClient) -> None:
        def post(url: str, json: Any = None, headers: dict[str, str] | None = None, timeout: Any = None):
            del timeout
            return client.post(url, json=json, headers=headers)

        def get(url: str, headers: dict[str, str] | None = None, timeout: Any = None):
            del timeout
            return client.get(url, headers=headers)

        monkeypatch.setattr(httpx, "post", post)
        monkeypatch.setattr(httpx, "get", get)

    return install


def _session_path(tmp_path) -> Path:
    return tmp_path / "token.json"


# ---------------------------------------------------------------- login


def test_login_persists_0600_session_and_never_echoes_token(
    tmp_path, monkeypatch, _http_to_daemon, capsys
) -> None:
    with _client(tmp_path, monkeypatch) as client:
        _setup(client)
        _http_to_daemon(client)
        code = main(
            [
                "login",
                "--baseurl",
                BASE_URL,
                "--username",
                USERNAME,
                "--password",
                PASSWORD,
            ]
        )
        path = _session_path(tmp_path)
        assert path.exists()
        from mnemoseed.identity.session import load_session

        # load_session round-trips exactly what login recorded; the recorded
        # token is live on the daemon (/auth/me answers with the owner identity).
        loaded = load_session(path)
        assert loaded is not None
        assert loaded.username == USERNAME
        assert loaded.profile_id == "default"
        assert loaded.base_url == BASE_URL
        me = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {loaded.token}"})
        assert me.status_code == 200
        assert me.json()["username"] == USERNAME
    captured = capsys.readouterr()
    assert code == 0
    assert "logged in as owner" in captured.out
    assert loaded.token not in captured.out, "the bearer value must never be echoed"
    # POSIX enforces owner-only; Windows reports 0666 because it cannot carry
    # POSIX bits (os.open still requested 0600).
    actual = file_perms(path)
    if os.name == "posix":
        assert actual == 0o600
    else:
        assert actual in (0o600, 0o666)


def test_login_wrong_password_exits_1(tmp_path, monkeypatch, _http_to_daemon, capsys) -> None:
    with _client(tmp_path, monkeypatch) as client:
        _setup(client)
        _http_to_daemon(client)
        code = main(
            [
                "login",
                "--baseurl",
                BASE_URL,
                "--username",
                USERNAME,
                "--password",
                "wrong-password",
            ]
        )
    captured = capsys.readouterr()
    assert code == 1
    assert "invalid username or password" in captured.err
    assert not _session_path(tmp_path).exists()


def test_login_pre_setup_fails_with_setup_pointer(tmp_path, monkeypatch, _http_to_daemon, capsys) -> None:
    with _client(tmp_path, monkeypatch) as client:
        _http_to_daemon(client)
        code = main(
            [
                "login",
                "--baseurl",
                BASE_URL,
                "--username",
                USERNAME,
                "--password",
                PASSWORD,
            ]
        )
    captured = capsys.readouterr()
    assert code == 1
    assert "setup required" in captured.err


# ---------------------------------------------------------------- whoami


def test_whoami_reports_stored_identity(tmp_path, monkeypatch, _http_to_daemon, capsys) -> None:
    with _client(tmp_path, monkeypatch) as client:
        _setup(client)
        token = _login_token(client)
        _http_to_daemon(client)
        save_session(
            AuthSession(base_url=BASE_URL, username=USERNAME, profile_id="default", token=token),
            path=_session_path(tmp_path),
        )
        code = main(["whoami", "--baseurl", BASE_URL])
    captured = capsys.readouterr()
    assert code == 0
    assert f"daemon:   {BASE_URL}" in captured.out
    assert f"username: {USERNAME}" in captured.out
    assert "profile:  default" in captured.out
    assert "role:     owner" in captured.out


def test_whoami_not_logged_in_exits_1(tmp_path, monkeypatch, capsys) -> None:
    with _client(tmp_path, monkeypatch):
        code = main(["whoami"])
    captured = capsys.readouterr()
    assert code == 1
    assert "not logged in" in captured.err


def test_whoami_with_revoked_token_exits_1(tmp_path, monkeypatch, _http_to_daemon, capsys) -> None:
    with _client(tmp_path, monkeypatch) as client:
        _setup(client)
        token = _login_token(client)
        # Revoke the token server-side behind whoami's back.
        client.post("/api/v1/auth/logout", headers={"Authorization": f"Bearer {token}"})
        _http_to_daemon(client)
        save_session(
            AuthSession(base_url=BASE_URL, username=USERNAME, profile_id="default", token=token),
            path=_session_path(tmp_path),
        )
        code = main(["whoami", "--baseurl", BASE_URL])
    captured = capsys.readouterr()
    assert code == 1
    assert "invalid or expired" in captured.err


# ---------------------------------------------------------------- logout


def test_logout_revokes_token_and_deletes_session(tmp_path, monkeypatch, _http_to_daemon) -> None:
    with _client(tmp_path, monkeypatch) as client:
        _setup(client)
        token = _login_token(client)
        _http_to_daemon(client)
        save_session(
            AuthSession(base_url=BASE_URL, username=USERNAME, profile_id="default", token=token),
            path=_session_path(tmp_path),
        )
        code = main(["logout", "--baseurl", BASE_URL])
        assert code == 0
        assert not _session_path(tmp_path).exists()
        # The presented token is revoked server-side: /auth/me now 401s.
        response = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 401


def test_logout_not_logged_in_exits_0(tmp_path, monkeypatch, capsys) -> None:
    with _client(tmp_path, monkeypatch):
        code = main(["logout"])
    captured = capsys.readouterr()
    assert code == 0
    assert "no stored session" in captured.out


# ---------------------------------------------------------------- auth reset


def test_auth_reset_rotates_owner_password(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    monkeypatch.setattr("mnemoseed.config.CONFIG_PATH", _config_toml(tmp_path))
    monkeypatch.setattr("mnemoseed.dream.snapshot.CONFIG_DIR", tmp_path)
    meta_path = tmp_path / "meta.db"
    meta = SqliteMetaDriver(path=meta_path)
    IdentityService(meta).setup_owner(USERNAME, PASSWORD)
    asyncio.run(meta.close())

    code = main(["auth", "reset", "--password", "a-reset-password"])
    captured = capsys.readouterr()
    assert code == 0
    assert "owner password updated" in captured.out

    reopened = SqliteMetaDriver(path=meta_path)
    owner = reopened.get_user_by_username(USERNAME)
    assert owner is not None
    assert verify_password("a-reset-password", owner.password_hash)
    assert not verify_password(PASSWORD, owner.password_hash)
    asyncio.run(reopened.close())


def test_auth_reset_prompt_mismatch_fails(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    monkeypatch.setattr("mnemoseed.config.CONFIG_PATH", _config_toml(tmp_path))
    monkeypatch.setattr("mnemoseed.dream.snapshot.CONFIG_DIR", tmp_path)

    import getpass

    monkeypatch.setattr(getpass, "getpass", lambda prompt="": "not-the-same")
    code = main(["auth", "reset"])
    assert code == 1


def test_auth_reset_without_owner_fails(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    monkeypatch.setattr("mnemoseed.config.CONFIG_PATH", _config_toml(tmp_path))
    monkeypatch.setattr("mnemoseed.dream.snapshot.CONFIG_DIR", tmp_path)
    code = main(["auth", "reset", "--password", "a-reset-password"])
    captured = capsys.readouterr()
    assert code == 1
    assert "no owner account" in captured.err


# ---------------------------------------------------------------- MCP client bearer


def _fake_http(monkeypatch, captured: dict[str, Any]) -> None:
    def post(url: str, json: Any = None, headers: dict[str, str] | None = None, timeout: Any = None):
        del timeout
        captured["url"] = url
        captured["headers"] = headers or {}
        captured["json"] = json or {}
        return _FakeResponse()

    monkeypatch.setattr(httpx, "post", post)


class _FakeResponse:
    status_code = 200
    text = "{}"

    def json(self):
        return {}


def test_mcp_client_sends_bearer_when_env_token_set(monkeypatch) -> None:
    from mnemoseed.mcp import client as mcp_client

    monkeypatch.setenv(mcp_client.ENV_TOKEN, "env-token-value")
    captured: dict[str, Any] = {}
    _fake_http(monkeypatch, captured)
    mcp_client.MemoryDaemonClient("http://daemon:7788").recall("default", "what did we do?")
    assert captured["headers"]["Authorization"] == "Bearer env-token-value"


def test_mcp_client_fails_open_without_env_token(monkeypatch) -> None:
    from mnemoseed.mcp import client as mcp_client

    monkeypatch.delenv(mcp_client.ENV_TOKEN, raising=False)
    captured: dict[str, Any] = {}
    _fake_http(monkeypatch, captured)
    mcp_client.MemoryDaemonClient("http://daemon:7788").recall("default", "what did we do?")
    assert "Authorization" not in captured["headers"]
