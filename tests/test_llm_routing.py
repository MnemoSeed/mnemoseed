"""Role routing: [dream.llm] config, lazy resolution, env-var key substitution,
audit logging, and boot-safe isolation (PRD-02 T6; FR-2.14 / design/02 §6-§7).

Behavior pinned here: route defaults follow design/02 §6 (deep_reflection ->
Claude Sonnet, short_increment -> OpenAI-compatible class, local_track -> local
Ollama); API keys are referenced by env-var NAME and never stored as values;
drivers materialize lazily per-role so a misconfigured unused role never breaks
boot; and RoleRouter.check() (the console 实测 button) never raises.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from mnemoseed.config import (
    DEFAULT_LLM_ROUTES,
    LLM_ROLES,
    Config,
    ConfigError,
    RoleLLMConfig,
    load_config,
)
from mnemoseed.llm import (
    ChatResult,
    HealthReport,
    LLMDriverInfo,
    LLMRouteError,
    LLMUnavailable,
    RoleRouter,
    UnknownLLMDriverError,
)
from mnemoseed.llm.drivers.oauth import OAuthLLM
from mnemoseed.llm.drivers.ollama import OllamaLLM
from mnemoseed.llm.registry import LLMRegistry, register


def _write(path: Any, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------- config defaults


def test_default_roles_are_exactly_the_prd_triple() -> None:
    assert LLM_ROLES == ("deep_reflection", "short_increment", "local_track")
    assert set(DEFAULT_LLM_ROUTES) == set(LLM_ROLES)


def test_defaults_follow_design_02_section_6(monkeypatch) -> None:
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    cfg = load_config(Path("/nonexistent/config.toml"))
    deep = cfg.llm["deep_reflection"]
    assert deep.driver == "anthropic"
    assert deep.model == "claude-sonnet-5"  # design/02 "Claude 5 Sonnet" maps here
    assert deep.params["api_key_env"] == "ANTHROPIC_API_KEY"
    assert deep.params["base_url"] == "https://api.anthropic.com"
    short = cfg.llm["short_increment"]
    assert short.driver == "openai_compatible"
    assert short.params["api_key_env"] == "OPENAI_API_KEY"
    local = cfg.llm["local_track"]
    assert local.driver == "ollama"
    # FR-2.7: the offline default is a <=14B quantized model, never the PRD 70B line
    assert "70b" not in local.model.lower()
    assert local.params["base_url"] == "http://localhost:11434"


def test_default_key_references_are_env_var_names_never_literals() -> None:
    for role in LLM_ROLES:
        env_name = DEFAULT_LLM_ROUTES[role].params.get("api_key_env")
        if env_name is not None:
            assert env_name == env_name.upper()
            assert env_name and not any(ch.isspace() for ch in env_name)
    # no literal-looking secret anywhere in the defaults
    blob = str(DEFAULT_LLM_ROUTES).lower()
    assert "sk-" not in blob


# ---------------------------------------------------------------- config parsing


def test_dream_llm_table_parses(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    p = tmp_path / "config.toml"
    _write(
        p,
        'preset = "embedded"\n'
        "[dream.llm.local_track]\n"
        'driver = "ollama"\n'
        'model = "qwen2.5:7b"\n'
        'base_url = "http://127.0.0.1:11434"\n',
    )
    local = load_config(p).llm["local_track"]
    assert local.driver == "ollama"
    assert local.model == "qwen2.5:7b"
    assert local.params["base_url"] == "http://127.0.0.1:11434"


def test_dream_llm_unconfigured_roles_inherit_defaults(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    p = tmp_path / "config.toml"
    _write(
        p,
        'preset = "embedded"\n[dream.llm.deep_reflection]\ndriver = "anthropic"\nmodel = "claude-opus-5"\n',
    )
    cfg = load_config(p)
    assert cfg.llm["deep_reflection"].model == "claude-opus-5"
    assert cfg.llm["short_increment"].driver == "openai_compatible"  # untouched: default
    assert cfg.llm["local_track"].driver == "ollama"


def test_dream_llm_partial_override_inherits_driver_and_model(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    p = tmp_path / "config.toml"
    _write(p, 'preset = "embedded"\n[dream.llm.deep_reflection]\napi_key_env = "MY_ALT_KEY"\n')
    cfg = load_config(p)
    deep = cfg.llm["deep_reflection"]
    assert deep.driver == "anthropic"  # inherited
    assert deep.model == "claude-sonnet-5"  # inherited
    assert deep.params["api_key_env"] == "MY_ALT_KEY"


def test_dream_llm_unknown_role_names_key(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    p = tmp_path / "config.toml"
    _write(
        p,
        'preset = "embedded"\n[dream.llm.extra_role]\ndriver = "anthropic"\nmodel = "m"\n',
    )
    with pytest.raises(ConfigError, match=r"config\[dream.llm.extra_role\]"):
        load_config(p)


def test_dream_llm_must_be_a_table(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    p = tmp_path / "config.toml"
    _write(p, 'preset = "embedded"\n[dream]\nllm = "nope"\n')
    with pytest.raises(ConfigError, match=r"config\[dream.llm\]"):
        load_config(p)


def test_dream_llm_role_must_be_a_table(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    p = tmp_path / "config.toml"
    _write(p, 'preset = "embedded"\n[dream.llm]\ndeep_reflection = "nope"\n')
    with pytest.raises(ConfigError, match=r"config\[dream.llm.deep_reflection\]"):
        load_config(p)


def test_dream_llm_bad_driver_type_names_key(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    p = tmp_path / "config.toml"
    _write(p, 'preset = "embedded"\n[dream.llm.local_track]\ndriver = 42\n')
    with pytest.raises(ConfigError, match=r"config\[dream.llm.local_track.driver\]"):
        load_config(p)


def test_programmatic_config_carries_dream_llm_defaults() -> None:
    cfg = Config()
    assert set(cfg.llm) == set(LLM_ROLES)
    assert cfg.llm["local_track"].driver == "ollama"


# ---------------------------------------------------------------- router fakes


class _FakeLLM:
    info = LLMDriverInfo(name="fake_chat", description="test double")

    def __init__(self, **params):
        self.params = params

    def chat(self, *, system: str, user: str) -> ChatResult:
        del system, user
        return ChatResult(
            text="[]",
            model=str(self.params.get("model", "")),
            driver="fake_chat",
        )

    def check(self) -> HealthReport:
        return HealthReport(ok=True, detail={"model": str(self.params.get("model", ""))})


class _BrokenLLM:
    info = LLMDriverInfo(name="broken_chat", description="test double")

    def __init__(self, **params):
        self.params = params

    def chat(self, *, system: str, user: str) -> ChatResult:
        del system, user
        raise LLMUnavailable("provider down")

    def check(self) -> HealthReport:
        return HealthReport(ok=False, detail={"error": "provider down"})


class _AuditSink:
    def __init__(self) -> None:
        self.entries: list[Any] = []

    def audit_append(self, entry: Any) -> None:
        self.entries.append(entry)


def _router(routes, *, registry: LLMRegistry | None = None, audit: Any = None, env: Any = None) -> RoleRouter:
    return RoleRouter(
        routes=routes,
        registry=registry,
        audit=audit,
        env=env if env is not None else (lambda name: None),
        clock=lambda: 42.0,
    )


# ---------------------------------------------------------------- router behavior


def test_router_resolves_local_role_to_ollama_instance() -> None:
    router = _router(Config().llm)
    llm = router.resolve("local_track")
    assert isinstance(llm, OllamaLLM)
    assert llm.model == "llama3.1:8b"


def test_router_resolves_deep_reflection_with_env_key() -> None:
    env: dict[str, str] = {"ANTHROPIC_API_KEY": "sk-ant-test"}
    router = _router(Config().llm, env=env.get)
    llm = router.resolve("deep_reflection")
    assert llm.model == "claude-sonnet-5"
    assert llm.api_key == "sk-ant-test"


def test_router_missing_env_yet_constructs_no_auth() -> None:
    # an unset key env var never blocks resolve: the driver constructs with an
    # empty key and the provider 401 surfaces as LLMUnavailable at chat time
    router = _router(Config().llm)
    llm = router.resolve("deep_reflection")
    assert llm.api_key == ""


def test_router_resolve_caches_same_instance() -> None:
    router = _router(Config().llm)
    a = router.resolve("local_track")
    b = router.resolve("local_track")
    assert a is b
    other = router.resolve("short_increment")
    assert other is not a


def test_router_unknown_role_raises_typed_route_error() -> None:
    router = _router(Config().llm)
    with pytest.raises(LLMRouteError, match="no llm route configured for role 'no_such_role'"):
        router.resolve("no_such_role")


def test_router_unknown_driver_only_fails_when_that_role_resolved() -> None:
    routes = dict(Config().llm)
    routes["short_increment"] = RoleLLMConfig(role="short_increment", driver="no_such_driver", model="m")
    router = _router(routes)
    # boot path: an unrelated role resolves fine, the broken role is untouched
    assert isinstance(router.resolve("local_track"), OllamaLLM)
    with pytest.raises(UnknownLLMDriverError, match="no_such_driver"):
        router.resolve("short_increment")


def test_router_unused_broken_role_never_breaks_boot() -> None:
    routes = dict(Config().llm)
    routes["deep_reflection"] = RoleLLMConfig(
        role="deep_reflection", driver="not_registered_anywhere", model="x"
    )
    router = _router(routes)
    assert isinstance(router.resolve("local_track"), OllamaLLM)  # boot only uses this role
    report = router.check("deep_reflection")  # the console button still degrades, never raises
    assert report.ok is False
    assert "not_registered_anywhere" in report.detail["error"]


def test_router_audit_logs_role_configured_once_env_name_never_value() -> None:
    sink = _AuditSink()
    env: dict[str, str] = {"ANTHROPIC_API_KEY": "sk-super-secret"}
    router = _router(Config().llm, audit=sink, env=env.get)
    router.resolve("deep_reflection")
    router.resolve("deep_reflection")  # cached: no second audit entry
    assert len(sink.entries) == 1
    entry = sink.entries[0]
    assert entry.actor == "dream-router"
    assert entry.action == "llm_role_configured"
    assert entry.at == 42.0
    assert entry.detail["role"] == "deep_reflection"
    assert entry.detail["driver"] == "anthropic"
    assert entry.detail["model"] == "claude-sonnet-5"
    assert entry.detail["api_key_env"] == "ANTHROPIC_API_KEY"
    assert "sk-super-secret" not in str(entry.detail)  # never the key value


def test_router_audit_batches_refresh_entry_same_role_reuses_instance() -> None:
    sink = _AuditSink()
    router = _router(Config().llm, audit=sink)
    router.resolve("local_track")
    router.resolve("local_track")
    assert len(sink.entries) == 1
    assert sink.entries[0].detail["driver"] == "ollama"


def test_router_check_returns_driver_health() -> None:
    reg = LLMRegistry("test-router")
    register(reg)(_FakeLLM)
    routes = {"short_increment": RoleLLMConfig(role="short_increment", driver="fake_chat", model="m")}
    router = _router(routes, registry=reg)
    report = router.check("short_increment")
    assert isinstance(report, HealthReport)
    assert report.ok is True
    assert report.detail["model"] == "m"


def test_router_check_surfaces_driver_failure_typed() -> None:
    reg = LLMRegistry("test-router-2")
    register(reg)(_BrokenLLM)
    routes = {"short_increment": RoleLLMConfig(role="short_increment", driver="broken_chat", model="m")}
    router = _router(routes, registry=reg)
    report = router.check("short_increment")
    assert report.ok is False
    assert "provider down" in report.detail["error"]


def test_router_check_unconfigured_role_is_failed_health() -> None:
    routes = {"deep_reflection": RoleLLMConfig(role="deep_reflection", driver="no_such_driver", model="m")}
    router = _router(routes)
    report = router.check("deep_reflection")
    assert report.ok is False
    assert "no_such_driver" in report.detail["error"]


def test_router_roles_in_config_order() -> None:
    router = _router(Config().llm)
    assert router.roles() == ("deep_reflection", "short_increment", "local_track")


def test_router_can_resolve_oauth_stub() -> None:
    routes = {"local_track": RoleLLMConfig(role="local_track", driver="oauth", model="")}
    router = _router(routes)
    llm = router.resolve("local_track")
    assert isinstance(llm, OAuthLLM)
    with pytest.raises(LLMUnavailable):
        llm.chat(system="s", user="u")
