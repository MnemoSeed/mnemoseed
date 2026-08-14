"""LLM admin service (issue #23; FR-6.9 wizard + design/07 section 8).

Unit-level contract for the shared validation + persistence surface the
console Models & Routing page, the setup wizard, and the ``mnemoseed llm`` CLI
all funnel through:

- routes() reports the current per-role config with ENV-VAR NAMES only —
  a literal key value anywhere in the payload is a hard failure.
- oauth_availability() detects Codex / Grok host login state (presence +
  expiry) without ever reading token values out.
- set_role() validates (unknown role / driver are typed errors), persists a
  surgical TOML patch (comments and unrelated keys survive), and audits the
  env-var name — never the value.
- test_config() runs a proposed route's check() against a live stub server
  (connectivity pass) and a closed port (typed fail), and never raises.
"""

from __future__ import annotations

import json
import socket
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from mnemoseed.config import LLM_ROLES, load_config
from mnemoseed.llm.admin import LLMAdminError, LLMAdminService
from mnemoseed.llm.registry import LLM_DRIVERS, register

_TOKEN_CODE = "sk-ultra-secret-codex-value"
_TOKEN_GROK = "gk-ultra-secret-grok-value"


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=UTC).isoformat()


def _config_toml(tmp_path: Path) -> Path:
    """A routable config whose three roles are network-free (stub + closed port)."""
    p = tmp_path / "config.toml"
    p.write_text(
        'preset = "embedded"\n'
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
    return p


@pytest.fixture(autouse=True)
def _ensure_drivers():
    """test_daemon clears the shared registries; re-register the real LLM drivers."""
    from mnemoseed.llm.drivers.anthropic import AnthropicLLM
    from mnemoseed.llm.drivers.oauth import OAuthLLM
    from mnemoseed.llm.drivers.ollama import OllamaLLM
    from mnemoseed.llm.drivers.openai_compatible import OpenAICompatibleLLM
    from mnemoseed.llm.drivers.stub import StubLLM

    for cls in (OpenAICompatibleLLM, AnthropicLLM, OllamaLLM, OAuthLLM, StubLLM):
        if not LLM_DRIVERS.contains(cls.info.name):
            register(LLM_DRIVERS)(cls)
    yield


def _service(tmp_path: Path, **kwargs) -> LLMAdminService:
    config = load_config(_config_toml(tmp_path))
    defaults: dict = {"clock": lambda: 1_700_000_000.0}
    defaults.update(kwargs)
    return LLMAdminService(config, meta=None, **defaults)


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


class _AuditSink:
    def __init__(self) -> None:
        self.entries: list = []

    def audit_append(self, entry) -> None:
        self.entries.append(entry)


# ---------------------------------------------------------------- routes()


def test_routes_reports_three_roles_with_driver_model_and_env_name(tmp_path) -> None:
    body = _service(tmp_path).routes()
    roles = {r["role"]: r for r in body["roles"]}
    assert set(roles) == set(LLM_ROLES)
    deep = roles["deep_reflection"]
    assert deep["driver"] == "stub"
    assert deep["model"] == "stub"
    assert deep["base_url"] is None  # stub carries no endpoint
    assert "connectivity" in deep
    assert isinstance(deep["connectivity"]["ok"], bool)
    local = roles["local_track"]
    assert local["driver"] == "ollama"
    assert local["base_url"] == "http://127.0.0.1:1"


def test_routes_payload_never_contains_key_values(tmp_path) -> None:
    env_name = "MNEMOSEED_TEST_SECRET_ENV"
    config = load_config(_config_toml(tmp_path))
    # give short_increment an env-var NAME (only a name is ever stored)
    service = LLMAdminService(config, meta=None, clock=lambda: 1_700_000_000.0)
    _arm(service, "short_increment", model="stub", api_key_env=env_name)
    service.set_role("short_increment", api_key_env=env_name)
    blob = json.dumps(service.routes())
    # attested names yes, literal values never
    assert env_name in blob
    assert "sk-" not in blob
    assert "gk-" not in blob
    assert "ultra-secret" not in blob


def test_routes_connectivity_is_cached_and_typed(tmp_path) -> None:
    service = _service(tmp_path)
    first = service.routes()["roles"][0]["connectivity"]
    second = service.routes()["roles"][0]["connectivity"]
    assert first["ok"] is True  # stub probe is healthy
    assert first["checked_at"] == second["checked_at"]  # cached within TTL


def test_routes_driver_catalog_lists_builtin_drivers(tmp_path) -> None:
    names = {d["name"] for d in _service(tmp_path).routes()["drivers"]}
    assert {"oauth", "anthropic", "ollama", "openai_compatible", "stub"} <= names


# ---------------------------------------------------------------- oauth_availability()


def test_oauth_availability_absent_home_reports_not_present(tmp_path) -> None:
    providers = _service(tmp_path, home=tmp_path).oauth_availability()["providers"]
    assert {p["provider"] for p in providers} == {"codex", "grok"}
    assert all(p["present"] is False for p in providers)


def test_oauth_availability_codex_present_and_unexpired(tmp_path) -> None:
    now = 1_700_000_000.0
    _write_codex(tmp_path, expires_at=_iso(now + 3000))
    service = _service(tmp_path, home=tmp_path, clock=lambda: now)
    codex = next(p for p in service.oauth_availability()["providers"] if p["provider"] == "codex")
    assert codex["present"] is True
    assert codex["expired"] is False
    assert codex["expires_at"] == pytest.approx(now + 3000, abs=1.0)


def test_oauth_availability_codex_expired(tmp_path) -> None:
    now = 1_700_000_000.0
    _write_codex(tmp_path, expires_at=_iso(now - 60))
    service = _service(tmp_path, home=tmp_path, clock=lambda: now)
    codex = next(p for p in service.oauth_availability()["providers"] if p["provider"] == "codex")
    assert codex["present"] is True
    assert codex["expired"] is True


def test_oauth_availability_grok_present_and_expired(tmp_path) -> None:
    now = 1_700_000_000.0
    _write_grok(tmp_path, expires_at=_iso(now - 60))
    service = _service(tmp_path, home=tmp_path, clock=lambda: now)
    grok = next(p for p in service.oauth_availability()["providers"] if p["provider"] == "grok")
    assert grok["present"] is True
    assert grok["expired"] is True
    assert grok["expires_at"] == pytest.approx(now - 60, abs=1.0)


def test_oauth_availability_never_emits_token_values(tmp_path) -> None:
    now = 1_700_000_000.0
    _write_codex(tmp_path, expires_at=_iso(now + 3000))
    _write_grok(tmp_path, expires_at=_iso(now + 3000))
    blob = json.dumps(_service(tmp_path, home=tmp_path, clock=lambda: now).oauth_availability())
    assert _TOKEN_CODE not in blob
    assert _TOKEN_GROK not in blob
    assert "ultra-secret" not in blob


# ---------------------------------------------------------------- set_role()


def _service_on(tmp_path: Path, **kwargs) -> tuple[LLMAdminService, Path]:
    path = _config_toml(tmp_path)
    defaults: dict = {"clock": lambda: 1_700_000_000.0}
    defaults.update(kwargs)
    return LLMAdminService(load_config(path), meta=None, **defaults), path


def _arm(service: LLMAdminService, role: str, *, model: str = "m", **route) -> None:
    """MUST-FIX 2: pass a connectivity test for the exact route to be persisted.

    Uses the stub driver, which passes offline, so the arming probe never needs
    the network.
    """
    report = service.test_config(role=role, driver="stub", model=model, **route)
    assert report.ok is True


def test_set_role_persists_toml_and_reads_back(tmp_path) -> None:
    service, path = _service_on(tmp_path)
    _arm(service, "short_increment", model="claude-sonnet-5", api_key_env="ANTHROPIC_API_KEY")
    service.set_role(
        "short_increment",
        driver="stub",
        model="claude-sonnet-5",
        api_key_env="ANTHROPIC_API_KEY",
    )
    text = path.read_text(encoding="utf-8")
    assert "[dream.llm.short_increment]" in text
    assert 'driver = "stub"' in text
    assert 'model = "claude-sonnet-5"' in text
    assert 'api_key_env = "ANTHROPIC_API_KEY"' in text
    # the persisted file round-trips through the loader as the source of truth
    reread = load_config(path).llm["short_increment"]
    assert reread.driver == "stub"
    assert reread.model == "claude-sonnet-5"
    assert reread.params["api_key_env"] == "ANTHROPIC_API_KEY"
    # unrelated roles were left untouched
    assert load_config(path).llm["local_track"].driver == "ollama"
    assert load_config(path).llm["deep_reflection"].driver == "stub"


def test_set_role_unknown_driver_is_typed_error(tmp_path) -> None:
    with pytest.raises(LLMAdminError, match="unknown llm driver"):
        _service(tmp_path).set_role("deep_reflection", driver="no_such_driver", model="m")


def test_set_role_unknown_role_is_typed_error(tmp_path) -> None:
    with pytest.raises(LLMAdminError, match="unknown llm role"):
        _service(tmp_path).set_role("no_such_role", driver="stub", model="m")


def test_set_role_empty_model_rejected(tmp_path) -> None:
    with pytest.raises(LLMAdminError, match="model"):
        _service(tmp_path).set_role("deep_reflection", driver="stub", model="  ")


def test_set_role_clears_optional_param_when_empty(tmp_path) -> None:
    service, path = _service_on(tmp_path)
    _arm(service, "short_increment", base_url="http://example.test")
    service.set_role("short_increment", driver="stub", model="m", base_url="http://example.test")
    assert load_config(path).llm["short_increment"].params["base_url"] == "http://example.test"
    _arm(service, "short_increment", base_url="")
    service.set_role("short_increment", base_url="")
    # the endpoint is no longer pinned in the file, and the typed routes()
    # surface reports explicit-only values so the cleared field reads back None
    table = path.read_text(encoding="utf-8").split("[dream.llm.short_increment]", 1)[1].split("[", 1)[0]
    assert "base_url" not in table
    short = next(r for r in service.routes()["roles"] if r["role"] == "short_increment")
    assert short["base_url"] is None


def test_set_role_oauth_requires_provider(tmp_path) -> None:
    with pytest.raises(LLMAdminError, match="oauth"):
        _service(tmp_path).set_role("deep_reflection", driver="oauth", model="gpt-5.6-codex")


def test_set_role_oauth_with_provider_persists(tmp_path) -> None:
    service, path = _service_on(tmp_path)
    # a stub probe (offline-passing) arms the exact provider-carrying signature
    _arm(service, "deep_reflection", provider="codex")
    service.set_role("deep_reflection", driver="stub", model="m", provider="codex", base_url="")
    cfg = load_config(path).llm["deep_reflection"]
    assert cfg.driver == "stub"
    assert cfg.params["provider"] == "codex"
    # the provider route pins no endpoint in the file (the loader's default
    # base_url remains a fallback param ignored at chat time)
    table = path.read_text(encoding="utf-8").split("[dream.llm.deep_reflection]", 1)[1].split("[", 1)[0]
    assert "base_url" not in table
    assert 'driver = "stub"' in table
    assert 'provider = "codex"' in table


def test_set_role_audits_env_name_never_value(tmp_path) -> None:
    sink = _AuditSink()
    service = LLMAdminService(load_config(_config_toml(tmp_path)), meta=sink, clock=lambda: 1_700_000_000.0)
    _arm(service, "deep_reflection", model="claude-opus-5", api_key_env="ANTHROPIC_API_KEY")
    service.set_role("deep_reflection", driver="stub", model="claude-opus-5", api_key_env="ANTHROPIC_API_KEY")
    assert len(sink.entries) == 1
    entry = sink.entries[0]
    assert entry.action == "llm_role_set"
    assert entry.detail["role"] == "deep_reflection"
    assert entry.detail["api_key_env"] == "ANTHROPIC_API_KEY"
    assert "sk-" not in json.dumps(entry.detail)


# ---------------------------------------------------------------- test_config()


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


def test_test_config_against_stub_server_passes(tmp_path, stub_server) -> None:
    report = _service(tmp_path).test_config(
        role="short_increment",
        driver="openai_compatible",
        model="stub-model",
        base_url=stub_server,
    )
    assert report.ok is True
    assert report.detail["models"] == ["stub-model"]


def test_test_config_closed_port_fails_typed(tmp_path) -> None:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    report = _service(tmp_path).test_config(
        role="short_increment",
        driver="openai_compatible",
        model="stub-model",
        base_url=f"http://127.0.0.1:{port}",
    )
    assert report.ok is False
    assert report.detail  # a typed failure payload, never an uncaught exception


def test_test_config_unknown_driver_returns_failed_health(tmp_path) -> None:
    report = _service(tmp_path).test_config(role="short_increment", driver="no_such_driver", model="m")
    assert report.ok is False
    assert "no_such_driver" in str(report.detail)


def test_test_config_never_raises_for_unknown_role(tmp_path) -> None:
    report = _service(tmp_path).test_config(role="no_such_role", driver="stub", model="m")
    assert report.ok is False
