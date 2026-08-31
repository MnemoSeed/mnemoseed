"""``mnemoseed onboard`` (FR-6.10 / design/06 3.1): the guided shell.

A step-by-step aggregate over the existing primitives — owner setup → storage
preset → dream LLM wizard → host link → autostart → doctor all-green. Every
step is skippable + resumable (state persists under the config dir); the LLM
step keeps connectivity-test-before-persist and skipping it yields a bootable
capture-only daemon; the host-link step reuses backup + diff + per-item
confirmation; config operations are loopback-only.

The REST contract (server side owned by W1; mocked here): GET /api/v1/setup/
status, POST /api/v1/setup (exact-once 410), POST /api/v1/auth/login,
POST /api/v1/llm/test, POST /api/v1/llm/routes/{role},
GET /api/v1/config, POST /api/v1/config/set.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import pytest

from mnemoseed.cli import main
from mnemoseed.installer.doctor import Check, DoctorReport
from mnemoseed.onboard import OnboardService

BASE_URL = "http://localhost:7788"


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
            method: str, url: str, payload: object, headers: dict[str, str] | None
        ) -> _FakeResponse:
            self.calls.append({"method": method, "url": url, "body": payload, "headers": headers or {}})
            response = self._routes.get((method, url.split("?", 1)[0]))
            if response is None:
                raise AssertionError(f"no canned route for {method} {url}")
            return response

        def get(
            url: str, params: object = None, headers: dict[str, str] | None = None, timeout: object = None
        ) -> _FakeResponse:
            del params, timeout
            return _dispatch("GET", url, None, headers)

        def post(
            url: str, json: object = None, headers: dict[str, str] | None = None, timeout: object = None
        ) -> _FakeResponse:
            del timeout
            return _dispatch("POST", url, json, headers)

        monkeypatch.setattr(httpx, "get", get)
        monkeypatch.setattr(httpx, "post", post)


def _env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("mnemoseed.config.CONFIG_DIR", tmp_path)
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'preset = "embedded"\nbaseurl = "{BASE_URL}"\n', encoding="utf-8")
    monkeypatch.setattr("mnemoseed.config.CONFIG_PATH", cfg)
    monkeypatch.setattr("mnemoseed.identity.session.TOKEN_PATH", tmp_path / "token.json")
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    monkeypatch.delenv("MNEMOSEED_TOKEN", raising=False)
    monkeypatch.delenv("MNEMOSEED_PROFILE_ID", raising=False)


def _setup_routes(daemon: FakeDaemon) -> None:
    daemon.on("GET", f"{BASE_URL}/api/v1/setup/status", body={"setup_required": True, "owner_exists": False})
    daemon.on(
        "POST",
        f"{BASE_URL}/api/v1/setup",
        status=201,
        body={"username": "owner", "profile_id": "default", "role": "owner", "setup_required": False},
    )
    daemon.on(
        "POST",
        f"{BASE_URL}/api/v1/auth/login",
        body={
            "token": "onboard-token",
            "token_type": "bearer",
            "username": "owner",
            "profile_id": "default",
            "expires_at": time.time() + 86400,
        },
    )


def _config_routes(daemon: FakeDaemon) -> None:
    daemon.on(
        "GET",
        f"{BASE_URL}/api/v1/config",
        body={"config": {"preset": "embedded", "baseurl": BASE_URL}, "restart_required": {}},
    )
    daemon.on(
        "POST",
        f"{BASE_URL}/api/v1/config/set",
        body={"ok": True, "version_id": 1, "restart_required": False},
    )


def _llm_routes(daemon: FakeDaemon) -> None:
    daemon.on(
        "POST",
        f"{BASE_URL}/api/v1/llm/test",
        body={
            "role": "deep_reflection",
            "driver": "stub",
            "model": "m",
            "ok": True,
            "detail": {"status": "ok"},
        },
    )
    daemon.on(
        "POST",
        f"{BASE_URL}/api/v1/llm/routes/deep_reflection",
        body={"role": "deep_reflection", "driver": "stub", "model": "m", "persisted_to": "config.toml"},
    )


def _claude_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / ".claude.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("MNEMOSEED_USER_HOME", str(home))
    return home


def _ok_doctor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "mnemoseed.installer.doctor.run_doctor",
        lambda config=None, **_: DoctorReport(baseurl=BASE_URL, checks=[Check("daemon", True, "reachable")]),
    )


def _ok_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("mnemoseed.installer.startup.enable", lambda *_, **__: ("registered run key",))


# ---------------------------------------------------------------- happy path


def test_onboard_full_flow_runs_every_step(tmp_path, monkeypatch, capsys) -> None:
    _env(tmp_path, monkeypatch)
    daemon = FakeDaemon()
    _setup_routes(daemon)
    _config_routes(daemon)
    _llm_routes(daemon)
    daemon.install(monkeypatch)
    _claude_home(tmp_path, monkeypatch)
    _ok_doctor(monkeypatch)
    _ok_startup(monkeypatch)

    code = main(
        [
            "onboard",
            "--yes",
            "--username",
            "owner",
            "--password",
            "hunter2",
            "--llm-driver",
            "stub",
            "--llm-model",
            "m",
        ]
    )
    captured = capsys.readouterr()
    assert code == 0
    for step in ("setup", "storage", "llm", "link", "autostart", "doctor"):
        assert step in captured.out
    assert "Done" in captured.out
    assert captured.out.count("✓") >= 6

    # owner created through the exact-once endpoint
    setup_call = next(call for call in daemon.calls if call["url"].endswith("/api/v1/setup"))
    assert setup_call["body"] == {"username": "owner", "password": "hunter2"}
    # llm persisted only after the connectivity test passed
    urls = [call["url"] for call in daemon.calls]
    test_at = urls.index(f"{BASE_URL}/api/v1/llm/test")
    persist_at = urls.index(f"{BASE_URL}/api/v1/llm/routes/deep_reflection")
    assert test_at < persist_at
    # host config got the MCP registration
    assert "mnemoseed" in (tmp_path / "home" / ".claude.json").read_text(encoding="utf-8")
    # session persisted for later CLI use
    session = json.loads((tmp_path / "token.json").read_text(encoding="utf-8"))
    assert session["profile_id"] == "default"
    assert session["token"] == "onboard-token"


# ---------------------------------------------------------------- skippable + resumable


def test_onboard_skip_llm_prints_capture_only_daemon_note(tmp_path, monkeypatch, capsys) -> None:
    _env(tmp_path, monkeypatch)
    daemon = FakeDaemon()
    _setup_routes(daemon)
    _config_routes(daemon)
    daemon.install(monkeypatch)
    _claude_home(tmp_path, monkeypatch)
    _ok_doctor(monkeypatch)
    _ok_startup(monkeypatch)

    code = main(["onboard", "--yes", "--skip", "llm", "--username", "owner", "--password", "hunter2"])
    captured = capsys.readouterr()
    assert code == 0
    assert "capture-only" in captured.out  # the consequence is stated in the wizard
    assert not any("llm/routes" in call["url"] or "llm/test" in call["url"] for call in daemon.calls)


def test_onboard_resumes_from_persisted_state(tmp_path, monkeypatch, capsys) -> None:
    _env(tmp_path, monkeypatch)
    daemon = FakeDaemon()
    _setup_routes(daemon)
    _config_routes(daemon)
    _llm_routes(daemon)
    daemon.install(monkeypatch)
    _claude_home(tmp_path, monkeypatch)
    _ok_doctor(monkeypatch)
    _ok_startup(monkeypatch)

    code = main(["onboard", "--yes", "--username", "owner", "--password", "hunter2"])
    assert code == 0
    setup_calls = [call for call in daemon.calls if call["url"].endswith("/api/v1/setup")]
    assert len(setup_calls) == 1

    # a second run resumes: completed steps are not repeated
    capsys.readouterr()
    code = main(["onboard", "--yes", "--username", "owner", "--password", "hunter2"])
    captured = capsys.readouterr()
    assert code == 0
    assert captured.out.count("already") >= 6
    assert len([call for call in daemon.calls if call["url"].endswith("/api/v1/setup")]) == 1
    # a stale password never re-runs the owner step, so no reset is possible here


def test_onboard_llm_test_failure_does_not_persist(tmp_path, monkeypatch, capsys) -> None:
    _env(tmp_path, monkeypatch)
    daemon = FakeDaemon()
    _setup_routes(daemon)
    _config_routes(daemon)
    daemon.on(
        "POST",
        f"{BASE_URL}/api/v1/llm/test",
        body={
            "role": "deep_reflection",
            "driver": "stub",
            "model": "m",
            "ok": False,
            "detail": {"error": "boom"},
        },
    )
    daemon.install(monkeypatch)
    _claude_home(tmp_path, monkeypatch)
    _ok_doctor(monkeypatch)
    _ok_startup(monkeypatch)

    code = main(
        [
            "onboard",
            "--yes",
            "--username",
            "owner",
            "--password",
            "hunter2",
            "--llm-driver",
            "stub",
            "--llm-model",
            "m",
        ]
    )
    captured = capsys.readouterr()
    assert code == 0
    assert "boom" in captured.out  # the typed failure is shown
    assert not any("llm/routes" in call["url"] for call in daemon.calls)


# ---------------------------------------------------------------- loopback rule


def test_onboard_refuses_non_loopback_baseurl(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr("mnemoseed.config.CONFIG_DIR", tmp_path)
    cfg = tmp_path / "config.toml"
    cfg.write_text('preset = "embedded"\nbaseurl = "http://10.0.0.5:7788"\n', encoding="utf-8")
    monkeypatch.setattr("mnemoseed.config.CONFIG_PATH", cfg)
    monkeypatch.setattr("mnemoseed.identity.session.TOKEN_PATH", tmp_path / "token.json")
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    monkeypatch.delenv("MNEMOSEED_TOKEN", raising=False)
    monkeypatch.delenv("MNEMOSEED_PROFILE_ID", raising=False)
    daemon = FakeDaemon()
    daemon.install(monkeypatch)

    code = main(["onboard", "--yes", "--username", "owner", "--password", "hunter2"])
    captured = capsys.readouterr()
    assert code == 1
    assert "loopback" in captured.err.lower()
    assert daemon.calls == []  # nothing was sent anywhere


# ---------------------------------------------------------------- llm key paste


class _RecordingClient:
    """A minimal daemon stand-in for the onboard LLM step (key paste path)."""

    base_url = BASE_URL

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def post(self, path: str, body: dict[str, object] | None = None) -> dict[str, object]:
        self.calls.append((path, body or {}))
        if path.endswith("/api/v1/llm/test"):
            return {"ok": True, "detail": {"status": "ok"}}
        return {"ok": True}


def _onboard_service(**answers) -> OnboardService:
    return OnboardService(answers=answers, out=print)


def test_onboard_llm_step_pastes_key_posts_it_before_probe_and_persists_ref(
    tmp_path, monkeypatch, capsys
) -> None:
    """T2-4: the wizard collects the pasted API key, POSTs it to the key
    endpoint BEFORE the connectivity probe (the probe resolves the stored
    key), and persists the ``secrets:`` reference with the route."""
    service = _onboard_service(
        llm_provider="1", llm_model="kimi-k3", llm_api_key="sk-pasted-key-4321", llm_share=False
    )
    client = _RecordingClient()
    ok, message = service._step_llm(client)
    captured = capsys.readouterr()
    assert ok is True
    assert "stored locally" in captured.out  # the teaching print
    assert "never shown again" in captured.out
    assert "Advanced" in captured.out  # the env-var alternative is noted

    posts = client.calls
    key_at = next(i for i, (path, _) in enumerate(posts) if path.endswith("/api/v1/llm/key"))
    probe_at = next(i for i, (path, _) in enumerate(posts) if path.endswith("/api/v1/llm/test"))
    persist_at = next(
        i for i, (path, _) in enumerate(posts) if path.endswith("/api/v1/llm/routes/deep_reflection")
    )
    assert key_at < probe_at < persist_at  # key first, then probe, then persist

    key_call = next((path, body) for path, body in posts if path.endswith("/api/v1/llm/key"))
    assert key_call[1] == {"role": "deep_reflection", "key": "sk-pasted-key-4321"}
    probe = next((path, body) for path, body in posts if path.endswith("/api/v1/llm/test"))
    assert probe[1]["api_key_env"] == "secrets:mnemoseed/dream/deep_reflection"
    persist = next(
        (path, body) for path, body in posts if path.endswith("/api/v1/llm/routes/deep_reflection")
    )
    assert persist[1]["api_key_env"] == "secrets:mnemoseed/dream/deep_reflection"
    # the pasted value itself never appears in the probe/persist bodies
    assert "sk-pasted-key-4321" not in repr(probe[1])
    assert "sk-pasted-key-4321" not in repr(persist[1])


def test_onboard_llm_step_empty_paste_notes_the_env_var_fallback(tmp_path, monkeypatch, capsys) -> None:
    """Leaving the paste empty keeps the env-var alternative: no key endpoint
    call, and the route pins the provider's default env-var NAME instead."""
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    service = _onboard_service(llm_provider="1", llm_model="kimi-k3", llm_share=False)
    client = _RecordingClient()
    ok, message = service._step_llm(client)
    captured = capsys.readouterr()
    assert ok is True
    assert not any(path.endswith("/api/v1/llm/key") for path, _ in client.calls)
    persist = next(
        (path, body) for path, body in client.calls if path.endswith("/api/v1/llm/routes/deep_reflection")
    )
    assert persist[1]["api_key_env"] == "FIREWORKS_API_KEY"
    assert "env var" in captured.out


def test_onboard_llm_step_share_posts_the_key_to_both_roles(tmp_path, monkeypatch, capsys) -> None:
    """Sharing the configured model also shares the stored key: both roles get
    their own key-endpoint write and their own reference."""
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
    service = _onboard_service(
        llm_provider="1", llm_model="kimi-k3", llm_api_key="sk-pasted-key-4321", llm_share=True
    )
    client = _RecordingClient()
    ok, message = service._step_llm(client)
    assert ok is True
    key_calls = [(path, body) for path, body in client.calls if path.endswith("/api/v1/llm/key")]
    roles = sorted(call[1]["role"] for call in key_calls)
    assert roles == ["deep_reflection", "short_increment"]
    short_persist = next(
        (path, body) for path, body in client.calls if path.endswith("/api/v1/llm/routes/short_increment")
    )
    assert short_persist[1]["api_key_env"] == "secrets:mnemoseed/dream/short_increment"


# ---------------------------------------------------------------- setup exact-once + doctor failure


def test_onboard_skips_setup_when_owner_exists(tmp_path, monkeypatch, capsys) -> None:
    _env(tmp_path, monkeypatch)
    daemon = FakeDaemon()
    daemon.on("GET", f"{BASE_URL}/api/v1/setup/status", body={"setup_required": False, "owner_exists": True})
    daemon.on(
        "POST",
        f"{BASE_URL}/api/v1/auth/login",
        body={
            "token": "onboard-token",
            "token_type": "bearer",
            "username": "owner",
            "profile_id": "default",
            "expires_at": time.time() + 86400,
        },
    )
    _config_routes(daemon)
    daemon.install(monkeypatch)
    _claude_home(tmp_path, monkeypatch)
    _ok_doctor(monkeypatch)
    _ok_startup(monkeypatch)

    code = main(["onboard", "--yes", "--skip", "llm", "--username", "owner", "--password", "hunter2"])
    captured = capsys.readouterr()
    assert code == 0
    assert not any(call["url"].endswith("/api/v1/setup") for call in daemon.calls)
    assert "owner already exists" in captured.out.lower()


def test_onboard_doctor_failure_prints_fix_and_fails(tmp_path, monkeypatch, capsys) -> None:
    _env(tmp_path, monkeypatch)
    daemon = FakeDaemon()
    _setup_routes(daemon)
    _config_routes(daemon)
    daemon.install(monkeypatch)
    _claude_home(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "mnemoseed.installer.doctor.run_doctor",
        lambda config=None, **_: DoctorReport(
            baseurl=BASE_URL,
            checks=[Check("daemon", False, "unreachable at " + BASE_URL, "start the daemon: mnemoseed up")],
        ),
    )
    _ok_startup(monkeypatch)

    code = main(["onboard", "--yes", "--skip", "llm", "--username", "owner", "--password", "hunter2"])
    captured = capsys.readouterr()
    assert code == 1
    assert "unreachable" in captured.out
    assert "mnemoseed up" in captured.out


def test_provider_catalog_has_no_dead_key_prompt_field() -> None:
    """The LLM wizard's provider catalog carries no dead ``key_prompt`` field:
    the paste-key flow superseded it, so no entry may read it and the catalog
    shape (driver/provider/name/base_url/key_env/key_url/model_prompt) stays."""
    from mnemoseed.onboard.service import _LLM_PROVIDERS

    assert _LLM_PROVIDERS
    for meta in _LLM_PROVIDERS.values():
        assert "key_prompt" not in meta
        assert set(meta) == {
            "driver",
            "provider",
            "name",
            "base_url",
            "key_env",
            "key_url",
            "model_prompt",
        }
