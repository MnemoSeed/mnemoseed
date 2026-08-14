"""CLI config verbs (PRD-07 FR-7.12, design/06 §6): REST-client config ops.

``mnemoseed config set|get|rollback`` talk to the daemon's /api/v1/config REST
surface (the same backend the console Settings page uses) and are **loopback
only** — against a non-loopback baseurl they fail with a clear error instead of
mutating a remote instance's config. ``--force`` on set is the one offline
escape: it patches config.toml directly and prints "not audited (daemon down)".

The /api/v1/config contract (server side owned by the W1 config task; mocked
here): GET returns the resolved config, POST /config/set accepts
{key_path, value} and returns {ok, version_id, restart_required},
POST /config/rollback accepts {version_id}, and GET /config/versions lists the
versioned history.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import httpx
import pytest

from mnemoseed.cli import main

BASE_URL = "http://localhost:7788"
TOKEN = "config-test-token"

CONFIG_PAYLOAD = {
    "config": {
        "preset": "embedded",
        "scoring": {"w1": 0.4, "w2": 0.4, "w3": 0.2},
        "baseurl": BASE_URL,
    },
    "restart_required": {},
}


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
        route = (method, path.split("?", 1)[0])
        self._routes[route] = _FakeResponse(status, body if body is not None else {})

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _dispatch(
            method: str, url: str, payload: object, headers: dict[str, str] | None, params: object
        ) -> _FakeResponse:
            self.calls.append(
                {"method": method, "url": url, "body": payload, "headers": headers or {}, "params": params}
            )
            response = self._routes.get((method, url.split("?", 1)[0]))
            if response is None:
                raise AssertionError(f"no canned route for {method} {url}")
            return response

        def get(
            url: str, params: object = None, headers: dict[str, str] | None = None, timeout: object = None
        ) -> _FakeResponse:
            del timeout
            return _dispatch("GET", url, None, headers, params)

        def post(
            url: str, json: object = None, headers: dict[str, str] | None = None, timeout: object = None
        ) -> _FakeResponse:
            del timeout
            return _dispatch("POST", url, json, headers, None)

        monkeypatch.setattr(httpx, "get", get)
        monkeypatch.setattr(httpx, "post", post)


def _env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, baseurl: str = BASE_URL) -> Path:
    monkeypatch.setattr("mnemoseed.config.CONFIG_DIR", tmp_path)
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'preset = "embedded"\nbaseurl = "{baseurl}"\n', encoding="utf-8")
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    monkeypatch.delenv("MNEMOSEED_TOKEN", raising=False)
    monkeypatch.delenv("MNEMOSEED_PROFILE_ID", raising=False)
    monkeypatch.setattr("mnemoseed.config.CONFIG_PATH", cfg)
    return cfg


# ---------------------------------------------------------------- get


def test_config_get_prints_resolved_config(tmp_path, monkeypatch, capsys) -> None:
    _env(tmp_path, monkeypatch)
    daemon = FakeDaemon()
    daemon.on("GET", f"{BASE_URL}/api/v1/config", body=CONFIG_PAYLOAD)
    daemon.install(monkeypatch)
    code = main(["config", "get"])
    captured = capsys.readouterr()
    assert code == 0
    assert "preset" in captured.out
    assert "embedded" in captured.out
    assert "w1" in captured.out
    assert daemon.calls[0]["headers"].get("X-MnemoSeed-Actor") == "cli"


def test_config_get_json_mode(tmp_path, monkeypatch, capsys) -> None:
    _env(tmp_path, monkeypatch)
    daemon = FakeDaemon()
    daemon.on("GET", f"{BASE_URL}/api/v1/config", body=CONFIG_PAYLOAD)
    daemon.install(monkeypatch)
    code = main(["config", "get", "--json"])
    captured = capsys.readouterr()
    assert code == 0
    assert json.loads(captured.out)["config"]["preset"] == "embedded"


# ---------------------------------------------------------------- set


def test_config_set_posts_key_path_and_value(tmp_path, monkeypatch, capsys) -> None:
    _env(tmp_path, monkeypatch)
    daemon = FakeDaemon()
    daemon.on(
        "POST",
        f"{BASE_URL}/api/v1/config/set",
        body={"ok": True, "version_id": 3, "restart_required": False},
    )
    daemon.install(monkeypatch)
    code = main(["config", "set", "scoring.w1", "0.5"])
    captured = capsys.readouterr()
    assert code == 0
    call = daemon.calls[0]
    assert call["url"].endswith("/api/v1/config/set")
    assert call["body"] == {"key_path": "scoring.w1", "value": 0.5}
    assert call["headers"]["X-MnemoSeed-Actor"] == "cli"
    assert "version 3" in captured.out


def test_config_set_keeps_non_numeric_value_as_string(tmp_path, monkeypatch, capsys) -> None:
    _env(tmp_path, monkeypatch)
    daemon = FakeDaemon()
    daemon.on(
        "POST",
        f"{BASE_URL}/api/v1/config/set",
        body={"ok": True, "version_id": 1, "restart_required": False},
    )
    daemon.install(monkeypatch)
    code = main(["config", "set", "baseurl", "http://10.0.0.5:7788"])
    assert code == 0
    assert daemon.calls[0]["body"] == {"key_path": "baseurl", "value": "http://10.0.0.5:7788"}


def test_config_set_refuses_non_loopback_baseurl(tmp_path, monkeypatch, capsys) -> None:
    _env(tmp_path, monkeypatch, baseurl="http://10.0.0.5:7788")
    daemon = FakeDaemon()
    daemon.install(monkeypatch)
    code = main(["config", "set", "scoring.w1", "0.5"])
    captured = capsys.readouterr()
    assert code == 1
    assert "loopback" in captured.err.lower()
    assert daemon.calls == []  # nothing was sent anywhere


def test_config_get_refuses_non_loopback_baseurl(tmp_path, monkeypatch, capsys) -> None:
    _env(tmp_path, monkeypatch, baseurl="http://10.0.0.5:7788")
    daemon = FakeDaemon()
    daemon.install(monkeypatch)
    code = main(["config", "get"])
    captured = capsys.readouterr()
    assert code == 1
    assert "loopback" in captured.err.lower()


# ---------------------------------------------------------------- rollback + versions


def test_config_versions_lists_history(tmp_path, monkeypatch, capsys) -> None:
    _env(tmp_path, monkeypatch)
    daemon = FakeDaemon()
    daemon.on(
        "GET",
        f"{BASE_URL}/api/v1/config/versions",
        body={
            "versions": [
                {"version_id": 3, "key": "scoring.w1", "updated_at": "2026-08-14T00:00:00Z"},
                {"version_id": 2, "key": "scoring.w1", "updated_at": "2026-08-13T00:00:00Z"},
            ]
        },
    )
    daemon.install(monkeypatch)
    code = main(["config", "versions"])
    captured = capsys.readouterr()
    assert code == 0
    assert "3" in captured.out
    assert "scoring.w1" in captured.out


def test_config_rollback_posts_version_id(tmp_path, monkeypatch, capsys) -> None:
    _env(tmp_path, monkeypatch)
    daemon = FakeDaemon()
    daemon.on(
        "POST",
        f"{BASE_URL}/api/v1/config/rollback",
        body={"ok": True, "version_id": 2},
    )
    daemon.install(monkeypatch)
    code = main(["config", "rollback", "2"])
    captured = capsys.readouterr()
    assert code == 0
    call = daemon.calls[0]
    assert call["url"].endswith("/api/v1/config/rollback")
    assert call["body"] == {"version_id": 2}
    assert "version 2" in captured.out


# ---------------------------------------------------------------- --force offline escape


def test_config_set_force_offline_patches_toml_when_daemon_down(tmp_path, monkeypatch, capsys) -> None:
    cfg = _env(tmp_path, monkeypatch)
    daemon = FakeDaemon()
    daemon.install(monkeypatch)

    def fail(url, json=None, headers=None, timeout=None):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "post", fail)
    code = main(["config", "set", "scoring.w2", "0.35", "--force"])
    captured = capsys.readouterr()
    assert code == 0
    assert "not audited (daemon down)" in captured.out
    text = cfg.read_text(encoding="utf-8")
    assert re.search(r"scoring\]", text)
    assert re.search(r"w2\s*=\s*0\.35", text)


def test_config_set_force_offline_works_when_daemon_up(tmp_path, monkeypatch, capsys) -> None:
    """--force prefers the offline path (it is the explicit escape hatch)."""
    cfg = _env(tmp_path, monkeypatch)
    code = main(["config", "set", "baseurl", "http://localhost:9999", "--force"])
    captured = capsys.readouterr()
    assert code == 0
    assert "not audited (daemon down)" in captured.out
    assert 'baseurl = "http://localhost:9999"' in cfg.read_text(encoding="utf-8")


def test_config_set_force_refuses_composite_value(tmp_path, monkeypatch, capsys) -> None:
    _env(tmp_path, monkeypatch)
    daemon = FakeDaemon()
    daemon.install(monkeypatch)

    def fail(url, json=None, headers=None, timeout=None):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "post", fail)
    code = main(["config", "set", "storage", '{"driver": "docker"}', "--force"])
    captured = capsys.readouterr()
    assert code == 1
    assert "composite" in captured.err.lower()
