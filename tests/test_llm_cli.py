"""LLM admin CLI (issue #23; FR-6.9 wizard + design/07 section 8).

``mnemoseed llm`` drives the SAME validation + persistence service the console
API uses, so a route changed through the CLI reads back identically and stays
consistent with the versioned config + audit log:

- ``mnemoseed llm status``  shows the per-role routes with a live connectivity
                            probe and the driver catalog, and never echoes a
                            token value.
- ``mnemoseed llm set <role> --driver/--model/--base-url/--api-key-env``
                            persists a route through the daemon REST surface
                            (``POST /api/v1/llm/routes/{role}`` — the same
                            endpoint the console wizard drives), so validation
                            happens server-side (422 with a typed message)
                            and the change is audited with actor=cli.

``llm status`` stays offline (read-only probe of the local config file);
``llm set`` is a write and must NOT touch config.toml directly — that is the
``config set --force`` escape only (design/07 5).
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from mnemoseed.cli import main
from mnemoseed.config import LLM_ROLES

_SECRET = "sk-ultra-secret-cli-value"

BASE_URL = "http://localhost:7788"
TOKEN = "llm-test-token"


class _FakeResponse:
    def __init__(self, status_code: int, body: object) -> None:
        self.status_code = status_code
        self._body = body
        self.text = json.dumps(body)

    def json(self) -> object:
        return self._body


class FakeDaemon:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self._routes: dict[tuple[str, str], _FakeResponse] = {}

    def on(self, method: str, path: str, *, status: int = 200, body: object = None) -> None:
        self._routes[(method, path)] = _FakeResponse(status, body if body is not None else {})

    def on_test_ok(self) -> None:
        """A passing connectivity probe for any proposed route (MUST-FIX 2)."""
        self.on(
            "POST",
            f"{BASE_URL}/api/v1/llm/test",
            body={"role": "deep_reflection", "ok": True, "detail": {}},
        )

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _dispatch(
            method: str, url: str, payload: object, headers: dict[str, str] | None
        ) -> _FakeResponse:
            self.calls.append({"method": method, "url": url, "body": payload, "headers": headers or {}})
            response = self._routes.get((method, url))
            if response is None:
                raise AssertionError(f"no canned route for {method} {url}")
            return response

        def post(
            url: str, json: object = None, headers: dict[str, str] | None = None, timeout: object = None
        ) -> _FakeResponse:
            del timeout
            return _dispatch("POST", url, json, headers)

        monkeypatch.setattr(httpx, "post", post)


def _config_toml(tmp_path: Path) -> Path:
    """Network-free routes so the status probe never leaves the host."""
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


def _env(tmp_path: Path, monkeypatch) -> Path:
    cfg = _config_toml(tmp_path)
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    monkeypatch.delenv("MNEMOSEED_USER_HOME", raising=False)
    monkeypatch.delenv("MNEMOSEED_TOKEN", raising=False)
    monkeypatch.delenv("MNEMOSEED_PROFILE_ID", raising=False)
    monkeypatch.setattr("mnemoseed.config.CONFIG_PATH", cfg)
    monkeypatch.setattr("mnemoseed.config.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("mnemoseed.dream.snapshot.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("mnemoseed.identity.session.TOKEN_PATH", tmp_path / "session.toml")
    return cfg


# ---------------------------------------------------------------- llm status


def test_llm_status_lists_all_roles_with_routes_and_connectivity(tmp_path, monkeypatch, capsys) -> None:
    cfg = _env(tmp_path, monkeypatch)
    code = main(["llm", "status"])
    captured = capsys.readouterr()
    assert code == 0
    for role in LLM_ROLES:
        assert role in captured.out
    assert "deep_reflection" in captured.out
    assert "short_increment" in captured.out
    assert "local_track" not in captured.out  # the legacy role is gone
    assert "connectivity: ok" in captured.out  # stub probe is healthy
    assert str(cfg) in captured.out  # the source file is named


def test_llm_status_reports_failed_connectivity_for_closed_port(tmp_path, monkeypatch, capsys) -> None:
    cfg = _config_toml(tmp_path)
    cfg.write_text(
        cfg.read_text(encoding="utf-8").replace(
            '[dream.llm.short_increment]\ndriver = "stub"\nmodel = "stub"\n',
            '[dream.llm.short_increment]\ndriver = "ollama"\nmodel = "llama3.1:8b"\n'
            'base_url = "http://127.0.0.1:1"\n',
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    monkeypatch.delenv("MNEMOSEED_USER_HOME", raising=False)
    monkeypatch.setattr("mnemoseed.config.CONFIG_PATH", cfg)
    monkeypatch.setattr("mnemoseed.dream.snapshot.CONFIG_DIR", tmp_path)
    code = main(["llm", "status"])
    captured = capsys.readouterr()
    assert code == 0
    assert "short_increment" in captured.out
    assert "connectivity: FAIL" in captured.out


def test_llm_status_never_emits_secret_values(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("MNEMOSEED_TEST_SECRET", _SECRET)
    # an attested env name is fine; its value must never appear -- add the name
    # only (never the value) into the short_increment row, then patch the env
    # directly (do not re-run _config_toml, which would clobber the edit)
    cfg = _config_toml(tmp_path)
    cfg.write_text(
        cfg.read_text(encoding="utf-8").replace(
            '[dream.llm.short_increment]\ndriver = "stub"\nmodel = "stub"\n',
            '[dream.llm.short_increment]\ndriver = "stub"\nmodel = "stub"\n'
            'api_key_env = "MNEMOSEED_TEST_SECRET"\n',
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    monkeypatch.delenv("MNEMOSEED_USER_HOME", raising=False)
    monkeypatch.setattr("mnemoseed.config.CONFIG_PATH", cfg)
    monkeypatch.setattr("mnemoseed.dream.snapshot.CONFIG_DIR", tmp_path)
    code = main(["llm", "status"])
    captured = capsys.readouterr()
    assert code == 0
    assert "MNEMOSEED_TEST_SECRET" in captured.out  # the NAME is reported
    assert _SECRET not in captured.out  # the value never is


# ---------------------------------------------------------------- llm set


def test_llm_set_persists_route_via_daemon_rest(tmp_path, monkeypatch, capsys) -> None:
    cfg = _env(tmp_path, monkeypatch)
    daemon = FakeDaemon()
    daemon.on_test_ok()
    daemon.on(
        "POST",
        f"{BASE_URL}/api/v1/llm/routes/short_increment",
        body={
            "role": "short_increment",
            "driver": "anthropic",
            "model": "claude-sonnet-5",
            "persisted_to": str(cfg),
        },
    )
    daemon.install(monkeypatch)
    code = main(
        [
            "llm",
            "set",
            "short_increment",
            "--driver",
            "anthropic",
            "--model",
            "claude-sonnet-5",
            "--api-key-env",
            "ANTHROPIC_API_KEY",
        ]
    )
    captured = capsys.readouterr()
    assert code == 0
    assert "short_increment" in captured.out
    # MUST-FIX 2: a connectivity probe precedes the persist.
    assert daemon.calls[0]["url"] == f"{BASE_URL}/api/v1/llm/test"
    call = daemon.calls[1]
    assert call["url"] == f"{BASE_URL}/api/v1/llm/routes/short_increment"
    assert call["body"] == {
        "driver": "anthropic",
        "model": "claude-sonnet-5",
        "api_key_env": "ANTHROPIC_API_KEY",
    }
    assert call["headers"]["X-MnemoSeed-Actor"] == "cli"
    # the local config file is untouched: writes happen server-side
    text = cfg.read_text(encoding="utf-8")
    assert "anthropic" not in text
    assert "claude-sonnet-5" not in text


def test_llm_set_clears_optional_param(tmp_path, monkeypatch, capsys) -> None:
    _env(tmp_path, monkeypatch)
    daemon = FakeDaemon()
    daemon.on_test_ok()
    daemon.on(
        "POST",
        f"{BASE_URL}/api/v1/llm/routes/short_increment",
        body={
            "role": "short_increment",
            "driver": "stub",
            "model": "stub",
            "persisted_to": "config.toml",
        },
    )
    daemon.install(monkeypatch)
    code = main(["llm", "set", "short_increment", "--base-url", "http://example.test"])
    assert code == 0
    # each persist is preceded by a probe (MUST-FIX 2): calls 0,2 = test, 1,3 = set
    assert daemon.calls[1]["body"] == {"base_url": "http://example.test"}
    code = main(["llm", "set", "short_increment", "--base-url", ""])
    captured = capsys.readouterr()
    assert code == 0
    assert daemon.calls[3]["body"] == {"base_url": ""}
    assert "http://example.test" not in captured.out


def test_llm_set_unknown_role_exits_1(tmp_path, monkeypatch, capsys) -> None:
    _env(tmp_path, monkeypatch)
    daemon = FakeDaemon()
    daemon.on_test_ok()
    daemon.on(
        "POST",
        f"{BASE_URL}/api/v1/llm/routes/no_such_role",
        status=422,
        body={"detail": "unknown llm role 'no_such_role'"},
    )
    daemon.install(monkeypatch)
    code = main(["llm", "set", "no_such_role", "--driver", "stub", "--model", "m"])
    captured = capsys.readouterr()
    assert code == 1
    assert "unknown llm role" in captured.err


def test_llm_set_unknown_driver_exits_1(tmp_path, monkeypatch, capsys) -> None:
    _env(tmp_path, monkeypatch)
    daemon = FakeDaemon()
    daemon.on_test_ok()
    daemon.on(
        "POST",
        f"{BASE_URL}/api/v1/llm/routes/deep_reflection",
        status=422,
        body={"detail": "unknown llm driver 'no_such_driver'"},
    )
    daemon.install(monkeypatch)
    code = main(["llm", "set", "deep_reflection", "--driver", "no_such_driver", "--model", "m"])
    captured = capsys.readouterr()
    assert code == 1
    assert "unknown llm driver" in captured.err


def test_llm_set_empty_model_exits_1(tmp_path, monkeypatch, capsys) -> None:
    _env(tmp_path, monkeypatch)
    daemon = FakeDaemon()
    daemon.on_test_ok()
    daemon.on(
        "POST",
        f"{BASE_URL}/api/v1/llm/routes/deep_reflection",
        status=422,
        body={"detail": "model cannot be empty"},
    )
    daemon.install(monkeypatch)
    code = main(["llm", "set", "deep_reflection", "--driver", "stub", "--model", ""])
    captured = capsys.readouterr()
    assert code == 1
    assert "model" in captured.err


def test_llm_set_oauth_without_provider_exits_1(tmp_path, monkeypatch, capsys) -> None:
    _env(tmp_path, monkeypatch)
    daemon = FakeDaemon()
    daemon.on_test_ok()
    daemon.on(
        "POST",
        f"{BASE_URL}/api/v1/llm/routes/deep_reflection",
        status=422,
        body={"detail": "driver=oauth requires a provider (codex|grok)"},
    )
    daemon.install(monkeypatch)
    code = main(["llm", "set", "deep_reflection", "--driver", "oauth", "--model", "gpt-5.6-codex"])
    captured = capsys.readouterr()
    assert code == 1
    assert "oauth" in captured.err


def test_llm_set_refuses_non_loopback_baseurl(tmp_path, monkeypatch, capsys) -> None:
    _env(tmp_path, monkeypatch)
    daemon = FakeDaemon()
    daemon.install(monkeypatch)
    code = main(
        [
            "llm",
            "set",
            "short_increment",
            "--driver",
            "stub",
            "--model",
            "m",
            "--baseurl",
            "http://10.0.0.5:7788",
        ]
    )
    captured = capsys.readouterr()
    assert code == 1
    assert "loopback" in captured.err.lower()
    assert daemon.calls == []  # nothing was sent anywhere


# ---------------------------------------------------------------- llm set --api-key


def test_llm_set_api_key_posts_the_key_endpoint(tmp_path, monkeypatch, capsys) -> None:
    _env(tmp_path, monkeypatch)
    daemon = FakeDaemon()
    daemon.on(
        "POST",
        f"{BASE_URL}/api/v1/llm/key",
        body={"ok": True, "role": "deep_reflection", "masked_tail": "9012", "restart_required": False},
    )
    daemon.install(monkeypatch)
    code = main(["llm", "set", "deep_reflection", "--api-key", _SECRET])
    captured = capsys.readouterr()
    assert code == 0
    # the key write is the ONLY call: no connectivity probe, no route persist
    assert len(daemon.calls) == 1
    call = daemon.calls[0]
    assert call["url"] == f"{BASE_URL}/api/v1/llm/key"
    assert call["body"] == {"role": "deep_reflection", "key": _SECRET}
    assert call["headers"]["X-MnemoSeed-Actor"] == "cli"
    # the response reports the masked tail and the no-restart teaching, never
    # the value itself
    assert "9012" in captured.out
    assert "restart" in captured.out.lower()
    assert _SECRET not in captured.out


def test_llm_set_api_key_daemon_down_is_a_clear_error(tmp_path, monkeypatch, capsys) -> None:
    """No --force bypass exists for secrets: a down daemon is a hard error."""
    _env(tmp_path, monkeypatch)

    def _refuse(url: str, json: object = None, headers: dict[str, str] | None = None, timeout: object = None):
        del url, json, headers, timeout
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "post", _refuse)
    code = main(["llm", "set", "deep_reflection", "--api-key", _SECRET])
    captured = capsys.readouterr()
    assert code == 1
    assert "cannot reach" in captured.err
    assert _SECRET not in captured.err


def test_llm_set_api_key_empty_value_exits_1(tmp_path, monkeypatch, capsys) -> None:
    _env(tmp_path, monkeypatch)
    daemon = FakeDaemon()
    daemon.install(monkeypatch)
    code = main(["llm", "set", "deep_reflection", "--api-key", "   "])
    captured = capsys.readouterr()
    assert code == 1
    assert "empty" in captured.err.lower()
    assert daemon.calls == []


def test_llm_set_api_key_refuses_non_loopback_baseurl(tmp_path, monkeypatch, capsys) -> None:
    _env(tmp_path, monkeypatch)
    daemon = FakeDaemon()
    daemon.install(monkeypatch)
    code = main(["llm", "set", "deep_reflection", "--api-key", _SECRET, "--baseurl", "http://10.0.0.5:7788"])
    captured = capsys.readouterr()
    assert code == 1
    assert "loopback" in captured.err.lower()
    assert daemon.calls == []
