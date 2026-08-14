"""LLM admin wire contract (issue #23; FR-6.9 wizard + design/07 section 8).

Drives the REAL daemon app through TestClient (embedded preset, synthetic
embedder) so the HTTP surface — not internal helpers — is asserted:

- GET    /api/v1/llm/routes            per-role config (env-var NAMES only) +
                                        connectivity + driver catalog, behind the
                                        profile-token gate (503 pre-setup, 401
                                        post-setup without a token).
- GET    /api/v1/llm/oauth-availability Codex / Grok host-login state (presence +
                                        expiry; never token values).
- POST   /api/v1/llm/routes/{role}      validate + persist a route change to the
                                        config TOML (surgical patch: comments and
                                        unrelated keys survive) + audit; typed 422
                                        for unknown role/driver.
- POST   /api/v1/llm/test               run a proposed route's check() against a
                                        live stub server (pass) and a closed port
                                        (typed fail).

Credentials are referenced by env-var NAME end to end: no response body of any
of these routes may ever carry a literal token value.
"""

from __future__ import annotations

import json
import socket
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from time import time

import pytest
from _identity_helpers import attach_token
from fastapi.testclient import TestClient

from mnemoseed.config import DEFAULT_LLM_ROUTES
from mnemoseed.daemon.app import create_app
from mnemoseed.llm.registry import LLM_DRIVERS, register
from mnemoseed.storage.drivers.lancedb_embedded import LanceDbEmbeddedStore
from mnemoseed.storage.drivers.sqlite_graph import SqliteGraphDriver
from mnemoseed.storage.drivers.sqlite_meta import SqliteMetaDriver
from mnemoseed.storage.drivers.synthetic_embedder import SyntheticEmbedder
from mnemoseed.storage.ports import AuditFilter, Page
from mnemoseed.storage.registry import (
    EMBED_DRIVERS,
    GRAPH_DRIVERS,
    META_DRIVERS,
    VECTOR_DRIVERS,
)
from mnemoseed.storage.registry import (
    register as register_storage,
)

_TOKEN_CODE = "sk-ultra-secret-codex-value"
_TOKEN_GROK = "gk-ultra-secret-grok-value"


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
            register_storage(registry)(cls)
    from mnemoseed.llm.drivers.anthropic import AnthropicLLM
    from mnemoseed.llm.drivers.oauth import OAuthLLM
    from mnemoseed.llm.drivers.ollama import OllamaLLM
    from mnemoseed.llm.drivers.openai_compatible import OpenAICompatibleLLM
    from mnemoseed.llm.drivers.stub import StubLLM

    for cls in (OpenAICompatibleLLM, AnthropicLLM, OllamaLLM, OAuthLLM, StubLLM):
        if not LLM_DRIVERS.contains(cls.info.name):
            register(LLM_DRIVERS)(cls)
    yield


def _config_toml(tmp_path: Path) -> Path:
    """Embedded config with all three dream roles network-free for probing."""
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        'preset = "embedded"\n'
        f'[storage.vector]\nuri = "{(tmp_path / "chunks.lance").as_posix()}"\ndimensions = 64\n'
        f'[storage.graph]\npath = "{(tmp_path / "cortex.db").as_posix()}"\n'
        f'[storage.meta]\npath = "{(tmp_path / "meta.db").as_posix()}"\n'
        f'[storage.embed]\ndriver = "synthetic"\ndimension = 64\n'
        "[dream.llm.deep_reflection]\n"
        'driver = "stub"\n'
        'model = "stub"\n'
        "[dream.llm.short_increment]\n"
        'driver = "stub"\n'
        'model = "stub"\n'
        "[dream.llm.local_track]\n"
        'driver = "ollama"\n'
        'model = "llama3.1:8b"\n'
        'base_url = "http://127.0.0.1:1"\n',
        encoding="utf-8",
    )
    return cfg


@contextmanager
def _client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    cfg: Path | None = None,
    home: Path | None = None,
    authenticate: bool = True,
) -> Iterator[TestClient]:
    """Boot the real daemon with a throwaway config + optional fake user home.

    On entry the owner is set up and the profile token stamped onto the default
    headers (the gate requires it post-setup, issue #14); pass
    ``authenticate=False`` to stay in zero-owner setup mode.
    """
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    monkeypatch.setattr("mnemoseed.config.CONFIG_PATH", cfg if cfg is not None else _config_toml(tmp_path))
    monkeypatch.setattr("mnemoseed.dream.snapshot.CONFIG_DIR", tmp_path)
    if home is not None:
        monkeypatch.setenv("MNEMOSEED_USER_HOME", str(home))
    else:
        monkeypatch.delenv("MNEMOSEED_USER_HOME", raising=False)
    with TestClient(create_app(), client=("127.0.0.1", 50057)) as client:
        if authenticate:
            attach_token(client)
        yield client


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=UTC).isoformat()


def _write_codex(home: Path, *, expires_at: str | None) -> None:
    tokens: dict = {"access_token": _TOKEN_CODE, "refresh_token": "rk-test", "account_id": "user-1"}
    if expires_at is not None:
        tokens["expires_at"] = expires_at
    data = {"auth_mode": "login", "tokens": tokens, "last_refresh": _iso(1_700_000_000.0)}
    path = home / ".codex" / "auth.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _write_grok(home: Path, *, expires_at: str) -> None:
    data = {
        "https://auth.x.ai::00000000-0000-0000-0000-000000000001": {
            "key": _TOKEN_GROK,
            "refresh_token": "grk-test",
            "expires_at": expires_at,
            "oidc_issuer": "https://auth.x.ai",
        }
    }
    path = home / ".grok" / "auth.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _audit(client: TestClient, action: str) -> list[object]:
    return client.app.state.stores.meta.audit_query(
        AuditFilter(actor="console", action=action), Page(limit=10)
    ).items


# ---------------------------------------------------------------- routes (read)


def test_llm_routes_reports_roles_env_names_and_connectivity(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        response = client.get("/api/v1/llm/routes")
        assert response.status_code == 200
        body = response.json()
        assert body.get("checked_at")
        roles = {r["role"]: r for r in body["roles"]}
        assert set(roles) == {"deep_reflection", "short_increment"}
        assert "local_track" not in roles  # the legacy role never reaches the wire
        deep = roles["deep_reflection"]
        assert deep["driver"] == "stub"
        assert deep["model"] == "stub"
        assert deep["base_url"] is None  # explicit-only: this role's file row has none
        assert isinstance(deep["connectivity"]["ok"], bool)
        assert deep["connectivity"]["checked_at"]
        short = roles["short_increment"]
        assert short["base_url"] is None  # explicit-only: this role's file row has none
        names = {d["name"] for d in body["drivers"]}
        assert {"oauth", "anthropic", "ollama", "openai_compatible", "stub"} <= names
        # E1-1: the routes map resolves the defaults chain into effective
        routes = body["routes"]
        assert set(routes) == {"deep_reflection", "short_increment"}
        eff_deep = routes["deep_reflection"]["effective"]
        assert eff_deep["model"] == "stub"
        assert eff_deep["base_url"] == DEFAULT_LLM_ROUTES["deep_reflection"].params["base_url"]
        assert eff_deep["api_key_env"] == DEFAULT_LLM_ROUTES["deep_reflection"].params["api_key_env"]
        assert routes["short_increment"]["effective"]["model"] == "stub"


def test_llm_routes_auth_gate_503_pre_setup_and_401_post(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch, authenticate=False) as client:
        pre = client.get("/api/v1/llm/routes")
        assert pre.status_code == 503
        assert pre.json()["detail"]["setup_url"] == "/console/#/setup"
        assert client.get("/api/v1/llm/oauth-availability").status_code == 503
        assert client.post("/api/v1/llm/routes/short_increment", json={}).status_code == 503

    with _client(tmp_path, monkeypatch, authenticate=True) as client:
        client.headers.pop("authorization", None)
        assert client.get("/api/v1/llm/routes").status_code == 401
        assert client.get("/api/v1/llm/oauth-availability").status_code == 401
        assert client.post("/api/v1/llm/routes/short_increment", json={}).status_code == 401
        assert client.post("/api/v1/llm/test", json={}).status_code == 401


# ---------------------------------------------------------------- oauth detection


def test_llm_oauth_availability_reports_absent_when_no_home(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch, home=tmp_path / "empty-home") as client:
        body = client.get("/api/v1/llm/oauth-availability").json()
        providers = {p["provider"]: p for p in body["providers"]}
        assert set(providers) == {"codex", "grok"}
        assert providers["codex"]["present"] is False
        assert providers["grok"]["present"] is False


def test_llm_oauth_availability_codex_present_grok_absent(tmp_path, monkeypatch) -> None:
    now = time()  # wired against the daemon's real clock
    home = tmp_path / "home"
    _write_codex(home, expires_at=_iso(now + 3000))
    with _client(tmp_path, monkeypatch, home=home) as client:
        body = client.get("/api/v1/llm/oauth-availability").json()
        providers = {p["provider"]: p for p in body["providers"]}
        assert providers["codex"]["present"] is True
        assert providers["codex"]["expired"] is False
        assert providers["codex"]["expires_at"] == pytest.approx(now + 3000, abs=1.0)
        assert providers["grok"]["present"] is False


def test_llm_oauth_availability_both_providers_and_expiry_flags(tmp_path, monkeypatch) -> None:
    now = time()  # wired against the daemon's real clock
    home = tmp_path / "home"
    _write_codex(home, expires_at=_iso(now - 60))  # expired
    _write_grok(home, expires_at=_iso(now + 3000))  # live
    with _client(tmp_path, monkeypatch, home=home) as client:
        body = client.get("/api/v1/llm/oauth-availability").json()
        providers = {p["provider"]: p for p in body["providers"]}
        assert providers["codex"]["present"] is True
        assert providers["codex"]["expired"] is True
        assert providers["grok"]["present"] is True
        assert providers["grok"]["expired"] is False


# ---------------------------------------------------------------- set role (write)


def test_llm_set_role_roundtrips_persists_and_audits(tmp_path, monkeypatch) -> None:
    cfg = _config_toml(tmp_path)
    with _client(tmp_path, monkeypatch, cfg=cfg) as client:
        # MUST-FIX 2: a matching connectivity probe must pass before a persist.
        probe = client.post(
            "/api/v1/llm/test",
            json={
                "role": "short_increment",
                "driver": "stub",
                "model": "claude-sonnet-5",
                "api_key_env": "ANTHROPIC_API_KEY",
            },
        )
        assert probe.status_code == 200
        assert probe.json()["ok"] is True
        response = client.post(
            "/api/v1/llm/routes/short_increment",
            json={"driver": "stub", "model": "claude-sonnet-5", "api_key_env": "ANTHROPIC_API_KEY"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["role"] == "short_increment"
        assert body["driver"] == "stub"
        assert body["model"] == "claude-sonnet-5"
        assert body["api_key_env"] == "ANTHROPIC_API_KEY"
        assert body["persisted_to"] == str(cfg)
        assert body["audited"] is True

        # read-back through the wire
        roles = {r["role"]: r for r in client.get("/api/v1/llm/routes").json()["roles"]}
        assert roles["short_increment"]["driver"] == "stub"
        assert roles["short_increment"]["model"] == "claude-sonnet-5"
        assert roles["short_increment"]["api_key_env"] == "ANTHROPIC_API_KEY"

        # the config file on disk matches (surgical patch; the other roles intact)
        on_disk = cfg.read_text(encoding="utf-8")
        assert "[dream.llm.short_increment]" in on_disk
        assert 'driver = "stub"' in on_disk
        assert 'model = "claude-sonnet-5"' in on_disk
        assert 'api_key_env = "ANTHROPIC_API_KEY"' in on_disk
        assert 'driver = "stub"' in on_disk  # deep_reflection row untouched
        assert on_disk.count("[dream.llm.") == 3  # no duplicated tables

        # audited with the env-var NAME, never a value
        entries = _audit(client, "llm_role_set")
        assert len(entries) == 1
        assert entries[0].detail["role"] == "short_increment"
        assert entries[0].detail["api_key_env"] == "ANTHROPIC_API_KEY"
        assert "sk-" not in json.dumps(entries[0].detail)


def test_llm_set_role_clears_optional_param(tmp_path, monkeypatch) -> None:
    cfg = _config_toml(tmp_path)
    with _client(tmp_path, monkeypatch, cfg=cfg) as client:
        set_url = "/api/v1/llm/routes/short_increment"
        probe = {"role": "short_increment", "driver": "stub", "model": "m", "base_url": "http://example.test"}
        client.post("/api/v1/llm/test", json=probe)
        client.post(set_url, json={"driver": "stub", "model": "m", "base_url": "http://example.test"})
        assert 'base_url = "http://example.test"' in cfg.read_text(encoding="utf-8")
        client.post(
            "/api/v1/llm/test",
            json={"role": "short_increment", "driver": "stub", "model": "m", "base_url": ""},
        )
        response = client.post(set_url, json={"base_url": ""})
        assert response.status_code == 200
        short_table = (
            cfg.read_text(encoding="utf-8").split("[dream.llm.short_increment]", 1)[1].split("[", 1)[0]
        )
        assert "base_url" not in short_table  # the cleared endpoint is un-pinned


def test_llm_set_role_partial_model_only_merges_current_route(tmp_path, monkeypatch) -> None:
    """A partial set keeps the current resolved values server-side: a model-only
    change must not clobber driver or an explicitly pinned base_url."""
    cfg = _config_toml(tmp_path)
    with _client(tmp_path, monkeypatch, cfg=cfg) as client:
        set_url = "/api/v1/llm/routes/short_increment"
        # pin an explicit endpoint first (full probe + full set)
        client.post(
            "/api/v1/llm/test",
            json={
                "role": "short_increment",
                "driver": "stub",
                "model": "m",
                "base_url": "http://example.test",
            },
        )
        pinned = client.post(
            set_url, json={"driver": "stub", "model": "m", "base_url": "http://example.test"}
        )
        assert pinned.status_code == 200

        # partial probe: omitted driver/base_url are merged from the current route
        probe = client.post("/api/v1/llm/test", json={"role": "short_increment", "model": "claude-x"})
        assert probe.status_code == 200
        assert probe.json()["ok"] is True
        # partial set: driver + pinned base_url survive the model-only write
        response = client.post(set_url, json={"model": "claude-x"})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["driver"] == "stub"
        assert body["model"] == "claude-x"
        assert body["base_url"] == "http://example.test"
        roles = {r["role"]: r for r in client.get("/api/v1/llm/routes").json()["roles"]}
        assert roles["short_increment"]["model"] == "claude-x"
        assert roles["short_increment"]["base_url"] == "http://example.test"
        assert 'model = "claude-x"' in cfg.read_text(encoding="utf-8")
        assert 'base_url = "http://example.test"' in cfg.read_text(encoding="utf-8")


def test_llm_set_role_partial_gate_enforced_on_merged_signature(tmp_path, monkeypatch) -> None:
    """The probe-gate runs against the MERGED signature: probing model X never
    authorizes persisting model Y, even when both persist partially."""
    with _client(tmp_path, monkeypatch) as client:
        probe = client.post("/api/v1/llm/test", json={"role": "short_increment", "model": "claude-x"})
        assert probe.status_code == 200
        assert probe.json()["ok"] is True
        response = client.post("/api/v1/llm/routes/short_increment", json={"model": "claude-y"})
        assert response.status_code == 409
        # the matching partial persist still lands
        matched = client.post("/api/v1/llm/routes/short_increment", json={"model": "claude-x"})
        assert matched.status_code == 200


def test_llm_set_role_unknown_role_is_422(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        response = client.post("/api/v1/llm/routes/no_such_role", json={"driver": "stub", "model": "m"})
        assert response.status_code == 422
        assert "unknown llm role" in response.json()["detail"]


def test_llm_set_role_local_track_is_422_with_deprecation_wording(tmp_path, monkeypatch) -> None:
    """E1-1: the removed offline role is rejected with deprecation wording."""
    with _client(tmp_path, monkeypatch) as client:
        response = client.post("/api/v1/llm/routes/local_track", json={"driver": "stub", "model": "m"})
        assert response.status_code == 422
        assert "deprecated" in response.json()["detail"].lower()


def test_llm_set_role_unknown_driver_is_422(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        response = client.post(
            "/api/v1/llm/routes/deep_reflection",
            json={"driver": "no_such_driver", "model": "m"},
        )
        assert response.status_code == 422
        assert "unknown llm driver" in response.json()["detail"]


def test_llm_set_role_oauth_without_provider_is_422(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        response = client.post(
            "/api/v1/llm/routes/deep_reflection",
            json={"driver": "oauth", "model": "gpt-5.6-codex"},
        )
        assert response.status_code == 422
        assert "oauth" in response.json()["detail"]


# ---------------------------------------------------------------- test config (post)


class _StubHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/models":
            body = b'{"data": [{"id": "stub-model"}]}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args) -> None:  # noqa: A002 - BaseHTTPRequestHandler signature
        del format, args


@pytest.fixture
def stub_server() -> str:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    try:
        yield url
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_llm_test_against_stub_server_passes(tmp_path, monkeypatch, stub_server) -> None:
    with _client(tmp_path, monkeypatch) as client:
        response = client.post(
            "/api/v1/llm/test",
            json={
                "role": "short_increment",
                "driver": "openai_compatible",
                "model": "stub-model",
                "base_url": stub_server,
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["role"] == "short_increment"
        assert body["ok"] is True
        assert body["detail"]["models"] == ["stub-model"]


def test_llm_test_closed_port_fails_typed(tmp_path, monkeypatch) -> None:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    with _client(tmp_path, monkeypatch) as client:
        response = client.post(
            "/api/v1/llm/test",
            json={
                "role": "short_increment",
                "driver": "openai_compatible",
                "model": "stub-model",
                "base_url": f"http://127.0.0.1:{port}",
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is False
        assert body["detail"]  # a typed failure, not an exception


def test_llm_test_unknown_driver_is_health_failure_not_422(tmp_path, monkeypatch) -> None:
    """A connectivity probe for a proposed config answers a typed failed health
    report (the console shows it inline), it never raises or 422s."""
    with _client(tmp_path, monkeypatch) as client:
        response = client.post(
            "/api/v1/llm/test", json={"role": "short_increment", "driver": "no_such_driver", "model": "m"}
        )
        assert response.status_code == 200
        assert response.json()["ok"] is False
        assert "no_such_driver" in str(response.json()["detail"])


# ---------------------------------------------------------------- credential hygiene


def test_llm_responses_never_contain_token_values(tmp_path, monkeypatch) -> None:
    """Every LLM-admin surface attests env-var NAMES -- a literal key value in
    any response body is a hard failure (red line: credential hygiene)."""
    now = 1_700_000_000.0
    home = tmp_path / "home"
    _write_codex(home, expires_at=_iso(now + 3000))
    _write_grok(home, expires_at=_iso(now + 3000))
    cfg = _config_toml(tmp_path)
    with _client(tmp_path, monkeypatch, cfg=cfg, home=home) as client:
        for path in ("/api/v1/llm/routes", "/api/v1/llm/oauth-availability"):
            blob = client.get(path).text
            assert _TOKEN_CODE not in blob
            assert _TOKEN_GROK not in blob
            assert "ultra-secret" not in blob
        probe = client.post(
            "/api/v1/llm/test",
            json={
                "role": "short_increment",
                "driver": "stub",
                "model": "claude-opus-5",
                "api_key_env": "CLAUDE_API_KEY",
            },
        )
        assert probe.json()["ok"] is True
        written = client.post(
            "/api/v1/llm/routes/short_increment",
            json={"driver": "stub", "model": "claude-opus-5", "api_key_env": "CLAUDE_API_KEY"},
        )
        assert _TOKEN_CODE not in written.text
        assert _TOKEN_GROK not in written.text
        tested = client.post(
            "/api/v1/llm/test",
            json={"role": "deep_reflection", "driver": "stub", "model": "stub"},
        )
        assert _TOKEN_CODE not in tested.text
    # the persisted config row attests the NAME only -- never a token value
    on_disk = cfg.read_text(encoding="utf-8")
    assert "CLAUDE_API_KEY" in on_disk
    assert _TOKEN_CODE not in on_disk
    assert _TOKEN_GROK not in on_disk
