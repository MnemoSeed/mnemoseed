"""LLM drivers: request/response mappings over httpx MockTransport (PRD-02 T6).

All network access lives behind the driver boundary and is exercised through
``httpx.MockTransport`` (or a loopback stub server for the oauth concurrency
case) — no live third-party network anywhere. Each driver test pins the wire
shape it sends (path, headers, body) and the typed degradation it surfaces
``LLMUnavailable`` on transport/auth failure (FR-2.6).
"""

from __future__ import annotations

import http.server
import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from mnemoseed.llm import LLMUnavailable, OAuthNotImplemented
from mnemoseed.llm.drivers.anthropic import AnthropicLLM
from mnemoseed.llm.drivers.oauth import CODEX_CLIENT_ID, OAuthLLM
from mnemoseed.llm.drivers.ollama import OllamaLLM
from mnemoseed.llm.drivers.openai_compatible import OpenAICompatibleLLM
from mnemoseed.llm.registry import LLM_DRIVERS


def _client(base_url: str, handler: Any) -> httpx.Client:
    return httpx.Client(base_url=base_url, transport=httpx.MockTransport(handler), timeout=5.0)


def _body(request: httpx.Request) -> dict[str, Any]:
    return json.loads(request.content)


# ---------------------------------------------------------------- openai_compatible


def test_openai_no_network_at_construction() -> None:
    llm = OpenAICompatibleLLM(base_url="http://127.0.0.1:1/v1", api_key="k", model="m")
    assert llm.model == "m"


def test_openai_requires_base_url_and_model() -> None:
    with pytest.raises(ValueError, match="base_url"):
        OpenAICompatibleLLM(base_url="", api_key="k", model="m")
    with pytest.raises(ValueError, match="model"):
        OpenAICompatibleLLM(base_url="http://x/v1", api_key="k", model="")


def test_openai_chat_sends_chat_completions_request() -> None:
    sent: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        # base_url "…/v1" + "/chat/completions" joins with the prefix on the wire
        assert request.url.path == "/v1/chat/completions"
        assert request.headers.get("authorization") == "Bearer sk-test"
        body = _body(request)
        sent["body"] = body
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"role": "assistant", "content": "[]"}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 4},
            },
        )

    llm = OpenAICompatibleLLM(base_url="https://llm.test/v1", api_key="sk-test", model="gpt-5.6-terra")
    llm._client = _client("https://llm.test/v1", handler)
    result = llm.chat(system="sys", user="usr")
    assert result.text == "[]"
    assert result.model == "gpt-5.6-terra"
    assert result.driver == "openai_compatible"
    assert sent["body"]["model"] == "gpt-5.6-terra"
    assert sent["body"]["messages"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "usr"},
    ]
    assert sent["body"]["max_tokens"] == 2048
    assert result.usage is not None
    assert result.usage.prompt_tokens == 12
    assert result.usage.completion_tokens == 4
    assert result.usage.cache_read_input_tokens is None


def test_openai_no_api_key_sends_no_auth_header() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "authorization" not in request.headers
        return httpx.Response(200, json={"choices": [{"message": {"content": "x"}}]})

    llm = OpenAICompatibleLLM(base_url="https://llm.test/v1", api_key="", model="m")
    llm._client = _client("https://llm.test/v1", handler)
    llm.chat(system="s", user="u")


def test_openai_auth_error_is_typed_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(401, json={"error": {"message": "bad key"}})

    llm = OpenAICompatibleLLM(base_url="https://llm.test/v1", api_key="bad", model="m")
    llm._client = _client("https://llm.test/v1", handler)
    with pytest.raises(LLMUnavailable):
        llm.chat(system="s", user="u")


def test_openai_server_error_is_typed_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(500, json={})

    llm = OpenAICompatibleLLM(base_url="https://llm.test/v1", api_key="k", model="m")
    llm._client = _client("https://llm.test/v1", handler)
    with pytest.raises(LLMUnavailable):
        llm.chat(system="s", user="u")


def test_openai_transport_error_is_typed_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        raise httpx.ConnectError("refused")

    llm = OpenAICompatibleLLM(base_url="https://llm.test/v1", api_key="k", model="m")
    llm._client = _client("https://llm.test/v1", handler)
    with pytest.raises(LLMUnavailable, match="refused"):
        llm.chat(system="s", user="u")


def test_openai_check_reports_reachable_and_models() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/models"
        return httpx.Response(200, json={"data": [{"id": "m1"}, {"id": "m2"}]})

    llm = OpenAICompatibleLLM(base_url="https://llm.test/v1", api_key="k", model="m")
    llm._client = _client("https://llm.test/v1", handler)
    report = llm.check()
    assert report.ok is True
    assert report.detail["models"] == ["m1", "m2"]


def test_openai_check_reports_unreachable_on_http_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(503, json={})

    llm = OpenAICompatibleLLM(base_url="https://llm.test/v1", api_key="k", model="m")
    llm._client = _client("https://llm.test/v1", handler)
    report = llm.check()
    assert report.ok is False
    assert "503" in report.detail["error"]


# ---------------------------------------------------------------- anthropic


def test_anthropic_chat_sends_messages_request() -> None:
    sent: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/messages"
        assert request.headers.get("x-api-key") == "sk-ant-test"
        assert request.headers.get("anthropic-version") == "2023-06-01"
        sent["body"] = _body(request)
        return httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": "[]"}],
                "usage": {
                    "input_tokens": 20,
                    "output_tokens": 5,
                    "cache_read_input_tokens": 100,
                    "cache_creation_input_tokens": 50,
                },
                "model": "claude-sonnet-5",
            },
        )

    llm = AnthropicLLM(base_url="https://anthropic.test", api_key="sk-ant-test", model="claude-sonnet-5")
    llm._client = _client("https://anthropic.test", handler)
    result = llm.chat(system="sys", user="usr")
    assert result.text == "[]"
    assert result.driver == "anthropic"
    assert sent["body"]["model"] == "claude-sonnet-5"
    assert "max_tokens" in sent["body"]  # required by the Messages API
    assert sent["body"]["system"] == "sys"
    assert sent["body"]["messages"] == [{"role": "user", "content": "usr"}]
    assert result.usage is not None
    assert result.usage.prompt_tokens == 20
    assert result.usage.completion_tokens == 5
    assert result.usage.cache_read_input_tokens == 100
    assert result.usage.cache_creation_input_tokens == 50


def test_anthropic_multiple_text_blocks_joined() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            json={"content": [{"type": "text", "text": "[{"}, {"type": "text", "text": "}]"}]},
        )

    llm = AnthropicLLM(base_url="https://anthropic.test", api_key="k", model="m")
    llm._client = _client("https://anthropic.test", handler)
    assert llm.chat(system="s", user="u").text == "[{}]"


def test_anthropic_requires_non_empty_model_and_base_url() -> None:
    with pytest.raises(ValueError, match="model"):
        AnthropicLLM(base_url="https://anthropic.test", api_key="k")
    with pytest.raises(ValueError, match="base_url"):
        AnthropicLLM(base_url="", api_key="k", model="m")


def test_anthropic_auth_error_is_typed_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(401, json={"type": "error", "error": {"type": "authentication_error"}})

    llm = AnthropicLLM(base_url="https://anthropic.test", api_key="bad", model="m")
    llm._client = _client("https://anthropic.test", handler)
    with pytest.raises(LLMUnavailable):
        llm.chat(system="s", user="u")


def test_anthropic_check_reports_models() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/models"
        return httpx.Response(200, json={"data": [{"id": "claude-sonnet-5"}, {"id": "claude-haiku-4-5"}]})

    llm = AnthropicLLM(base_url="https://anthropic.test", api_key="k", model="m")
    llm._client = _client("https://anthropic.test", handler)
    report = llm.check()
    assert report.ok is True
    assert report.detail["models"] == ["claude-sonnet-5", "claude-haiku-4-5"]


# ---------------------------------------------------------------- ollama (offline track)


def test_ollama_chat_sends_native_api_chat() -> None:
    sent: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/chat"
        assert "authorization" not in request.headers  # offline: no key, no auth header
        sent["body"] = _body(request)
        return httpx.Response(
            200,
            json={
                "model": "llama3.1:8b",
                "message": {"role": "assistant", "content": "[]"},
                "usage": {"prompt_eval_count": 9, "eval_count": 3},
            },
        )

    llm = OllamaLLM(base_url="http://127.0.0.1:11434")
    llm._client = _client("http://127.0.0.1:11434", handler)
    result = llm.chat(system="sys", user="usr")
    assert result.text == "[]"
    assert result.driver == "ollama"
    assert sent["body"]["model"] == "llama3.1:8b"
    assert sent["body"]["stream"] is False
    assert sent["body"]["messages"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "usr"},
    ]
    assert result.usage is not None
    assert result.usage.prompt_tokens == 9
    assert result.usage.completion_tokens == 3


def test_ollama_check_reports_tags() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/tags"
        return httpx.Response(200, json={"models": [{"name": "llama3.1:8b"}, {"name": "qwen2.5:7b"}]})

    llm = OllamaLLM(base_url="http://127.0.0.1:11434")
    llm._client = _client("http://127.0.0.1:11434", handler)
    report = llm.check()
    assert report.ok is True
    assert report.detail["models"] == ["llama3.1:8b", "qwen2.5:7b"]


def test_ollama_server_error_is_typed_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(500, json={"error": "model not found"})

    llm = OllamaLLM(base_url="http://127.0.0.1:11434")
    llm._client = _client("http://127.0.0.1:11434", handler)
    with pytest.raises(LLMUnavailable):
        llm.chat(system="s", user="u")


# ---------------------------------------------------------------- oauth (Codex / Grok via host login state)


def _iso(timestamp: float) -> str:
    """An ISO-8601 UTC stamp the auth-file fixtures use (clock-injected, so no wall clock)."""
    return datetime.fromtimestamp(timestamp, tz=UTC).isoformat()


def _write_codex_auth(
    home: Path,
    *,
    access: str = "ak-one",
    refresh: str = "rk-one",
    account: str = "user-1",
    last_refresh: str,
    expires_at: str | None = None,
) -> Path:
    """Write a fake ``~/.codex/auth.json`` (Codex CLI shape)."""
    tokens: dict[str, Any] = {
        "id_token": "id-one",
        "access_token": access,
        "refresh_token": refresh,
        "account_id": account,
    }
    if expires_at is not None:
        tokens["expires_at"] = expires_at
    data = {"auth_mode": "login", "tokens": tokens, "last_refresh": last_refresh}
    path = home / ".codex" / "auth.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _write_grok_data(home: Path, data: dict[str, Any]) -> Path:
    """Write a fake ``~/.grok/auth.json`` from raw contents."""
    path = home / ".grok" / "auth.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _write_grok_auth(
    home: Path,
    *,
    issuer: str = "https://auth.x.ai",
    key: str = "gk-one",
    refresh: str = "grk-one",
    expires_at: str,
) -> Path:
    """Write a single-account fake ``~/.grok/auth.json`` (keyed by issuer URL)."""
    account_key = f"{issuer}::11111111-2222-3333-4444-555555555555"
    data: dict[str, Any] = {
        account_key: {
            "key": key,
            "refresh_token": refresh,
            "expires_at": expires_at,
            "oidc_issuer": issuer,
            "oidc_client_id": "grok-client-test",
        }
    }
    return _write_grok_data(home, data)


def _refusal(path: object = None) -> httpx.Response:
    del path
    raise AssertionError("this endpoint must not be called")


def test_oauth_registered_name() -> None:
    assert LLM_DRIVERS.contains("oauth")


def test_oauth_chat_raises_typed_not_implemented_for_unsupported_provider() -> None:
    llm = OAuthLLM(provider="anthropic")  # Anthropic subscription OAuth is deliberately out of scope
    with pytest.raises(OAuthNotImplemented, match="not implemented"):
        llm.chat(system="s", user="u")
    # typed degradation: the unsupported branch is catchable through LLMUnavailable too
    with pytest.raises(LLMUnavailable):
        llm.chat(system="s", user="u")


def test_oauth_check_reports_not_configured() -> None:
    llm = OAuthLLM()  # no provider selected: not configured, never a crash
    report = llm.check()
    assert report.ok is False
    assert report.detail["status"] == "not_configured"


def test_oauth_codex_chat_wire_shape(tmp_path: Path) -> None:
    now = 1_700_000_000.0
    _write_codex_auth(tmp_path, last_refresh=_iso(now - 60), expires_at=_iso(now + 3000))
    sent: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/backend-api/codex/responses"
        assert request.headers.get("authorization") == "Bearer ak-one"
        assert request.headers.get("chatgpt-account-id") == "user-1"
        sent["body"] = _body(request)
        return httpx.Response(
            200,
            json={
                "output": [{"content": [{"type": "output_text", "text": "[]"}]}],
                "usage": {"input_tokens": 5, "output_tokens": 2},
            },
        )

    llm = OAuthLLM(provider="codex", model="gpt-5.6-codex", home=tmp_path, clock=lambda: now)
    llm._client = _client("https://chatgpt.test", handler)
    llm._token_client = _client("https://token.test", handler)
    result = llm.chat(system="sys", user="usr")
    assert result.text == "[]"
    assert result.model == "gpt-5.6-codex"
    assert result.driver == "oauth"
    assert sent["body"]["model"] == "gpt-5.6-codex"
    assert sent["body"]["instructions"] == "sys"
    assert sent["body"]["input"] == [{"role": "user", "content": "usr"}]
    assert result.usage is not None
    assert result.usage.prompt_tokens == 5
    assert result.usage.completion_tokens == 2


def test_oauth_grok_chat_wire_shape(tmp_path: Path) -> None:
    now = 1_700_000_000.0
    _write_grok_auth(tmp_path, key="gk-one", expires_at=_iso(now + 3000))
    sent: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/responses"
        assert request.headers.get("authorization") == "Bearer gk-one"
        assert "chatgpt-account-id" not in request.headers  # codex-only header
        sent["body"] = _body(request)
        return httpx.Response(200, json={"output": [{"content": [{"type": "output_text", "text": "ok"}]}]})

    llm = OAuthLLM(provider="grok", model="grok-4-test", home=tmp_path, clock=lambda: now)
    llm._client = _client("https://api.x.test/v1", handler)
    llm._token_client = _client("https://auth.x.test", handler)
    result = llm.chat(system="s", user="u")
    assert result.text == "ok"
    assert result.driver == "oauth"
    assert sent["body"]["model"] == "grok-4-test"


def test_oauth_missing_codex_auth_file_is_typed_unavailable(tmp_path: Path) -> None:
    llm = OAuthLLM(provider="codex", model="m", home=tmp_path, clock=lambda: 0.0)
    with pytest.raises(LLMUnavailable, match="not found"):
        llm.chat(system="s", user="u")


def test_oauth_missing_grok_auth_file_is_typed_unavailable(tmp_path: Path) -> None:
    llm = OAuthLLM(provider="grok", model="m", home=tmp_path, clock=lambda: 0.0)
    with pytest.raises(LLMUnavailable, match="not found"):
        llm.chat(system="s", user="u")


def test_oauth_malformed_auth_json_is_typed_unavailable(tmp_path: Path) -> None:
    path = tmp_path / ".codex" / "auth.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ nope", encoding="utf-8")
    llm = OAuthLLM(provider="codex", model="m", home=tmp_path, clock=lambda: 0.0)
    with pytest.raises(LLMUnavailable, match="unreadable"):
        llm.chat(system="s", user="u")


def test_oauth_codex_missing_access_token_is_typed_unavailable(tmp_path: Path) -> None:
    now = 0.0
    data = {
        "auth_mode": "login",
        "tokens": {"refresh_token": "rk", "account_id": "user-1"},
        "last_refresh": _iso(now),
    }
    path = tmp_path / ".codex" / "auth.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    llm = OAuthLLM(provider="codex", model="m", home=tmp_path, clock=lambda: now)
    with pytest.raises(LLMUnavailable, match="access_token"):
        llm.chat(system="s", user="u")


def test_oauth_codex_missing_account_id_is_typed_unavailable(tmp_path: Path) -> None:
    now = 0.0
    data = {
        "auth_mode": "login",
        "tokens": {"access_token": "ak", "refresh_token": "rk"},
        "last_refresh": _iso(now),
    }
    path = tmp_path / ".codex" / "auth.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    llm = OAuthLLM(provider="codex", model="m", home=tmp_path, clock=lambda: now)
    with pytest.raises(LLMUnavailable, match="account_id"):
        llm.chat(system="s", user="u")


def test_oauth_grok_no_account_entry_is_typed_unavailable(tmp_path: Path) -> None:
    _write_grok_data(tmp_path, {"not_an_account": "just a string"})
    llm = OAuthLLM(provider="grok", model="m", home=tmp_path, clock=lambda: 0.0)
    with pytest.raises(LLMUnavailable, match="no account entry"):
        llm.chat(system="s", user="u")


def test_oauth_not_expired_uses_cached_token_without_refresh(tmp_path: Path) -> None:
    now = 1_700_000_000.0
    _write_codex_auth(tmp_path, last_refresh=_iso(now - 60), expires_at=_iso(now + 3000))

    def chat_handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("authorization") == "Bearer ak-one"
        return httpx.Response(200, json={"output": [{"content": [{"type": "output_text", "text": "ok"}]}]})

    llm = OAuthLLM(
        provider="codex",
        model="m",
        home=tmp_path,
        clock=lambda: now,
        token_url="https://token.test/oauth/token",
    )
    llm._client = _client("https://chatgpt.test", chat_handler)
    llm._token_client = _client("https://token.test", _refusal)
    assert llm.chat(system="s", user="u").text == "ok"


def test_oauth_codex_expiry_falls_back_to_last_refresh_plus_ttl(tmp_path: Path) -> None:
    now = 5_000.0
    _write_codex_auth(tmp_path, last_refresh=_iso(now - 600), expires_at=None)
    refreshed: list[str] = []

    def cached_handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("authorization") == "Bearer ak-one"
        return httpx.Response(200, json={"output": [{"content": [{"type": "output_text", "text": "ok"}]}]})

    def refreshed_handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("authorization") == "Bearer ak-new"
        return httpx.Response(200, json={"output": [{"content": [{"type": "output_text", "text": "ok"}]}]})

    # Inside the default token TTL: the cached access token is used untouched.
    fresh = OAuthLLM(
        provider="codex",
        model="m",
        home=tmp_path,
        clock=lambda: now,
        token_url="https://token.test/oauth/token",
    )
    fresh._client = _client("https://chatgpt.test", cached_handler)
    fresh._token_client = _client("https://token.test", _refusal)
    assert fresh.chat(system="s", user="u").text == "ok"

    # Past last_refresh + TTL: a refresh runs and the new token is used.
    def token_handler(request: httpx.Request) -> httpx.Response:
        refreshed.append("refresh")
        return httpx.Response(200, json={"access_token": "ak-new", "expires_in": 3600})

    stale = OAuthLLM(
        provider="codex",
        model="m",
        home=tmp_path,
        clock=lambda: now + 1801,
        token_url="https://token.test/oauth/token",
    )
    stale._client = _client("https://chatgpt.test", refreshed_handler)
    stale._token_client = _client("https://token.test", token_handler)
    assert stale.chat(system="s", user="u").text == "ok"
    assert refreshed == ["refresh"]


def test_oauth_expired_triggers_refresh_and_writes_back(tmp_path: Path) -> None:
    now = 1_700_000_000.0
    path = _write_codex_auth(tmp_path, last_refresh=_iso(now - 7200), expires_at=_iso(now - 60))

    def token_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/oauth/token"
        form = parse_qs(request.content.decode())
        assert form["grant_type"] == ["refresh_token"]
        assert form["refresh_token"] == ["rk-one"]
        assert form["client_id"] == [CODEX_CLIENT_ID]
        return httpx.Response(
            200, json={"access_token": "ak-new", "refresh_token": "rk-new", "expires_in": 3600}
        )

    def chat_handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("authorization") == "Bearer ak-new"  # the refreshed token rides the chat
        return httpx.Response(200, json={"output": [{"content": [{"type": "output_text", "text": "ok"}]}]})

    llm = OAuthLLM(
        provider="codex",
        model="m",
        home=tmp_path,
        clock=lambda: now,
        token_url="https://token.test/oauth/token",
    )
    llm._client = _client("https://chatgpt.test", chat_handler)
    llm._token_client = _client("https://token.test", token_handler)
    assert llm.chat(system="s", user="u").text == "ok"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["tokens"]["access_token"] == "ak-new"
    assert data["tokens"]["refresh_token"] == "rk-new"
    assert data["tokens"]["expires_at"] == _iso(now + 3600)
    assert data["tokens"]["account_id"] == "user-1"  # unrelated field preserved
    assert data["auth_mode"] == "login"  # unrelated field preserved
    assert data["last_refresh"] == _iso(now)


def test_oauth_expired_refresh_failure_is_typed_unavailable(tmp_path: Path) -> None:
    now = 1_700_000_000.0
    path = _write_codex_auth(tmp_path, last_refresh=_iso(now - 7200), expires_at=_iso(now - 60))

    def token_handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(400, json={"error": "invalid_grant"})

    llm = OAuthLLM(
        provider="codex",
        model="m",
        home=tmp_path,
        clock=lambda: now,
        token_url="https://token.test/oauth/token",
    )
    llm._client = _client("https://chatgpt.test", lambda r: _refusal(r).status_code and httpx.Response(500))
    llm._token_client = _client("https://token.test", token_handler)
    with pytest.raises(LLMUnavailable, match="refresh"):
        llm.chat(system="s", user="u")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["tokens"]["access_token"] == "ak-one"  # failed refresh: disk untouched


def test_oauth_refresh_network_down_is_typed_unavailable(tmp_path: Path) -> None:
    now = 1_700_000_000.0
    _write_codex_auth(tmp_path, last_refresh=_iso(now - 7200), expires_at=_iso(now - 60))

    def token_handler(request: httpx.Request) -> httpx.Response:
        del request
        raise httpx.ConnectError("refused")

    llm = OAuthLLM(
        provider="codex",
        model="m",
        home=tmp_path,
        clock=lambda: now,
        token_url="https://token.test/oauth/token",
    )
    llm._client = _client("https://chatgpt.test", lambda r: _refusal(r).status_code and httpx.Response(500))
    llm._token_client = _client("https://token.test", token_handler)
    with pytest.raises(LLMUnavailable, match="refused"):
        llm.chat(system="s", user="u")


def test_oauth_chat_transport_error_is_typed_unavailable(tmp_path: Path) -> None:
    now = 1_700_000_000.0
    _write_codex_auth(tmp_path, last_refresh=_iso(now - 60), expires_at=_iso(now + 3000))

    def chat_handler(request: httpx.Request) -> httpx.Response:
        del request
        raise httpx.ConnectError("refused")

    llm = OAuthLLM(provider="codex", model="m", home=tmp_path, clock=lambda: now)
    llm._client = _client("https://chatgpt.test", chat_handler)
    llm._token_client = _client("https://token.test", _refusal)
    with pytest.raises(LLMUnavailable, match="refused"):
        llm.chat(system="s", user="u")


def test_oauth_refresh_write_back_is_atomic(tmp_path: Path, monkeypatch) -> None:
    now = 1_700_000_000.0
    path = _write_codex_auth(tmp_path, last_refresh=_iso(now - 7200), expires_at=_iso(now - 60))

    def token_handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json={"access_token": "ak-new", "expires_in": 3600})

    def boom(tmp: object, target: object) -> None:
        del tmp, target
        raise OSError("simulated crash before atomic rename")

    llm = OAuthLLM(
        provider="codex",
        model="m",
        home=tmp_path,
        clock=lambda: now,
        token_url="https://token.test/oauth/token",
    )
    llm._client = _client("https://chatgpt.test", lambda r: _refusal(r).status_code and httpx.Response(500))
    llm._token_client = _client("https://token.test", token_handler)
    monkeypatch.setattr("mnemoseed.llm.drivers.oauth._replace", boom)
    with pytest.raises(LLMUnavailable):
        llm.chat(system="s", user="u")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["tokens"]["access_token"] == "ak-one"  # crash mid-write left the old file intact


def test_oauth_refresh_preserves_unrelated_fields_and_lock_file(tmp_path: Path) -> None:
    now = 1_700_000_000.0
    path = _write_codex_auth(tmp_path, last_refresh=_iso(now - 7200), expires_at=_iso(now - 60))
    lock = Path(str(path) + ".lock")
    lock.write_text("held by the codex cli", encoding="utf-8")

    def token_handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json={"access_token": "ak-new", "expires_in": 3600})

    def chat_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"output": [{"content": [{"type": "output_text", "text": "ok"}]}]})

    llm = OAuthLLM(
        provider="codex",
        model="m",
        home=tmp_path,
        clock=lambda: now,
        token_url="https://token.test/oauth/token",
    )
    llm._client = _client("https://chatgpt.test", chat_handler)
    llm._token_client = _client("https://token.test", token_handler)
    assert llm.chat(system="s", user="u").text == "ok"
    assert lock.exists()
    assert lock.read_text(encoding="utf-8") == "held by the codex cli"  # sibling lock untouched


def test_oauth_grok_refresh_writes_back_own_entry_only(tmp_path: Path) -> None:
    now = 1_700_000_000.0
    issuer = "https://auth.x.test"
    data: dict[str, Any] = {
        f"{issuer}::aaaa": {
            "key": "ak-a-old",
            "refresh_token": "rk-a",
            "expires_at": _iso(now - 60),  # expired: this account is the refresh target
            "oidc_issuer": issuer,
            "oidc_client_id": "c-a",
        },
        f"{issuer}::bbbb": {
            "key": "ak-b",
            "refresh_token": "rk-b",
            "expires_at": _iso(now + 3600),
            "oidc_issuer": issuer,
            "oidc_client_id": "c-b",
        },
    }
    _write_grok_data(tmp_path, data)

    def token_handler(request: httpx.Request) -> httpx.Response:
        # the issuer advertises its token endpoint through OIDC discovery
        if request.method == "GET" and request.url.path == "/.well-known/openid-configuration":
            return httpx.Response(200, json={"token_endpoint": "https://auth.x.test/oauth2/token"})
        assert request.method == "POST"
        # what discovery advertises, never a hard-coded /oauth/token guess
        assert request.url.path == "/oauth2/token"
        form = parse_qs(request.content.decode())
        assert form["refresh_token"] == ["rk-a"]
        assert form["client_id"] == ["c-a"]
        return httpx.Response(200, json={"access_token": "ak-a-new", "expires_in": 3600})

    def chat_handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("authorization") == "Bearer ak-a-new"
        return httpx.Response(200, json={"output": [{"content": [{"type": "output_text", "text": "ok"}]}]})

    llm = OAuthLLM(provider="grok", model="m", home=tmp_path, clock=lambda: now)
    llm._client = _client("https://api.x.test/v1", chat_handler)
    llm._token_client = _client("https://auth.x.test", token_handler)
    assert llm.chat(system="s", user="u").text == "ok"
    after = json.loads((tmp_path / ".grok" / "auth.json").read_text(encoding="utf-8"))
    assert after[f"{issuer}::aaaa"]["key"] == "ak-a-new"
    assert after[f"{issuer}::aaaa"]["expires_at"] == _iso(now + 3600)
    assert after[f"{issuer}::bbbb"]["key"] == "ak-b"  # the other account is untouched
    assert after[f"{issuer}::bbbb"]["expires_at"] == _iso(now + 3600)


def test_oauth_grok_refresh_falls_back_to_literal_default_when_discovery_down(tmp_path: Path) -> None:
    now = 1_700_000_000.0
    issuer = "https://auth.x.test"
    _write_grok_auth(tmp_path, issuer=issuer, expires_at=_iso(now - 60))  # expired: refresh needed
    grants: list[str] = []

    def token_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/openid-configuration":
            return httpx.Response(503, json={"error": "card"})  # discovery unreachable
        grants.append(request.url.path)
        return httpx.Response(200, json={"access_token": "ak-new", "expires_in": 3600})

    def chat_handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("authorization") == "Bearer ak-new"
        return httpx.Response(200, json={"output": [{"content": [{"type": "output_text", "text": "ok"}]}]})

    llm = OAuthLLM(provider="grok", model="m", home=tmp_path, clock=lambda: now)
    llm._client = _client("https://api.x.test/v1", chat_handler)
    llm._token_client = _client("https://auth.x.test", token_handler)
    assert llm.chat(system="s", user="u").text == "ok"
    assert grants == ["/oauth2/token"]  # literal default, never the old /oauth/token guess


def test_oauth_grok_discovery_endpoint_is_cached(tmp_path: Path) -> None:
    now = 1_700_000_000.0
    issuer = "https://auth.x.test"
    _write_grok_auth(tmp_path, issuer=issuer, expires_at=_iso(now - 60))
    discoveries: list[str] = []

    def token_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/openid-configuration":
            discoveries.append(request.url.path)
            return httpx.Response(200, json={"token_endpoint": "https://auth.x.test/oauth2/token"})
        return httpx.Response(200, json={"access_token": "ak-new", "expires_in": 3600})

    def chat_handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("authorization") == "Bearer ak-new"
        return httpx.Response(200, json={"output": [{"content": [{"type": "output_text", "text": "ok"}]}]})

    llm = OAuthLLM(provider="grok", model="m", home=tmp_path, clock=lambda: now)
    llm._client = _client("https://api.x.test/v1", chat_handler)
    llm._token_client = _client("https://auth.x.test", token_handler)
    assert llm.chat(system="s", user="u").text == "ok"
    assert discoveries == ["/.well-known/openid-configuration"]

    # force a second refresh: the discovered endpoint must be reused, not re-fetched
    path = tmp_path / ".grok" / "auth.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    entry_key = next(iter(data))
    data[entry_key]["expires_at"] = _iso(now - 60)
    path.write_text(json.dumps(data), encoding="utf-8")
    assert llm.chat(system="s", user="u").text == "ok"
    assert discoveries == ["/.well-known/openid-configuration"]  # cached: still exactly one discovery


def test_oauth_naive_expires_at_is_read_as_utc(tmp_path: Path) -> None:
    now = 1_700_000_000.0
    naive_expiry = _iso(now + 3600).replace("+00:00", "")  # 1h out in UTC, no timezone marker
    _write_codex_auth(tmp_path, last_refresh=_iso(now - 60), expires_at=naive_expiry)

    def chat_handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("authorization") == "Bearer ak-one"
        return httpx.Response(200, json={"output": [{"content": [{"type": "output_text", "text": "ok"}]}]})

    llm = OAuthLLM(provider="codex", model="m", home=tmp_path, clock=lambda: now)
    llm._client = _client("https://chatgpt.test", chat_handler)
    llm._token_client = _client("https://token.test", _refusal)  # no refresh: naive stamp is UTC, still valid
    assert llm.chat(system="s", user="u").text == "ok"


def test_oauth_out_of_range_expires_at_is_typed_unavailable(tmp_path: Path) -> None:
    now = 1_700_000_000.0
    data: dict[str, Any] = {
        "https://auth.x.ai::111": {
            "key": "ak",
            "refresh_token": "rk",
            "expires_at": "9999-12-31T23:59:59",  # naive date outside the host clock range
            "oidc_issuer": "https://auth.x.ai",
            "oidc_client_id": "c",
        }
    }
    _write_grok_data(tmp_path, data)

    def token_handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(400, json={"error": "invalid_grant"})

    llm = OAuthLLM(provider="grok", model="m", home=tmp_path, clock=lambda: now)
    llm._token_client = _client("https://auth.x.test", token_handler)
    report = llm.check()  # FR-2.6: never raises, even on an out-of-range expiry stamp
    assert report.ok is False
    with pytest.raises(LLMUnavailable):  # chat degrades typed, never a raw OSError traceback
        llm.chat(system="s", user="u")


def test_oauth_grok_prefers_entry_matching_default_issuer(tmp_path: Path) -> None:
    now = 1_700_000_000.0
    data: dict[str, Any] = {
        "https://other.issuer::111": {
            "key": "ak-other",
            "refresh_token": "rk-o",
            "expires_at": _iso(now + 3000),
            "oidc_issuer": "https://other.issuer",
            "oidc_client_id": "c",
        },
        "https://auth.x.ai::222": {
            "key": "ak-good",
            "refresh_token": "rk-g",
            "expires_at": _iso(now + 3000),
            "oidc_issuer": "https://auth.x.ai",
            "oidc_client_id": "c2",
        },
    }
    _write_grok_data(tmp_path, data)

    def chat_handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("authorization") == "Bearer ak-good"
        return httpx.Response(200, json={"output": [{"content": [{"type": "output_text", "text": "ok"}]}]})

    llm = OAuthLLM(provider="grok", model="m", home=tmp_path, clock=lambda: now)
    llm._client = _client("https://api.x.test/v1", chat_handler)
    llm._token_client = _client("https://auth.x.test", _refusal)
    assert llm.chat(system="s", user="u").text == "ok"


def test_oauth_check_reports_ok_when_reachable(tmp_path: Path) -> None:
    now = 1_700_000_000.0
    _write_codex_auth(tmp_path, last_refresh=_iso(now - 60), expires_at=_iso(now + 3000))

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("authorization") == "Bearer ak-one"
        return httpx.Response(200, json={"output": []})

    llm = OAuthLLM(provider="codex", model="m", home=tmp_path, clock=lambda: now)
    llm._client = _client("https://chatgpt.test", handler)
    llm._token_client = _client("https://token.test", _refusal)
    report = llm.check()
    assert report.ok is True
    assert report.detail["provider"] == "codex"


def test_oauth_check_reports_unconfigured_on_auth_failure(tmp_path: Path) -> None:
    llm = OAuthLLM(provider="codex", model="m", home=tmp_path, clock=lambda: 0.0)
    report = llm.check()  # missing auth file: check never raises
    assert report.ok is False
    assert report.detail["status"] == "not_configured"


def test_oauth_concurrent_callers_refresh_once(tmp_path: Path) -> None:
    """Two threads refresh simultaneously -> exactly one token grant; both still chat."""
    now = 1_700_000_000.0
    _write_codex_auth(tmp_path, last_refresh=_iso(now - 7200), expires_at=_iso(now - 60))
    refresh_count = 0
    count_lock = threading.Lock()

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            if self.path.startswith("/oauth/token"):
                nonlocal refresh_count
                with count_lock:
                    refresh_count += 1
                body = json.dumps({"access_token": "ak-concurrent", "expires_in": 3600}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *args: Any) -> None:
            del args

    # Only the token endpoint is a real socket; the chat endpoint stays mocked
    # (the rest of this file's convention). The concurrency property under test
    # is the refresh lock — one token grant for all callers — not Windows socket
    # teardown races under a concurrent burst, so the 8 chat calls run over
    # MockTransport while the single refresh hits the loopback stub.
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        llm = OAuthLLM(
            provider="codex",
            model="m",
            home=tmp_path,
            clock=lambda: now,
            token_url=f"http://127.0.0.1:{port}/oauth/token",
        )

        def chat_handler(request: httpx.Request) -> httpx.Response:
            # every caller's chat rides the single refreshed token
            assert request.headers.get("authorization") == "Bearer ak-concurrent"
            return httpx.Response(
                200, json={"output": [{"content": [{"type": "output_text", "text": "ok"}]}]}
            )

        llm._client = _client("https://chatgpt.test", chat_handler)
        results: list[str] = []
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                results.append(llm.chat(system="s", user="u").text)
            except BaseException as exc:  # noqa: BLE001 - surfaced via errors below
                errors.append(exc)

        workers = [threading.Thread(target=worker) for _ in range(8)]
        for w in workers:
            w.start()
        for w in workers:
            w.join(timeout=15)
        assert not errors
        assert results == ["ok"] * 8  # every caller got a valid answer
        assert refresh_count == 1  # single refresh for all callers
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------- registration side effect


def test_importing_drivers_registers_all_four() -> None:
    for name in ("anthropic", "oauth", "ollama", "openai_compatible"):
        assert LLM_DRIVERS.contains(name)
