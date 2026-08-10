"""Config loading: ~/.mnemoseed/config.toml is the single source of truth.

A preset (embedded/docker/custom) maps each storage layer to a default driver;
layers can be overridden individually and a layer may declare named instances
(e.g. graph.main / graph.isolated, D6). STORAGE_MODE is kept as a preset
shortcut environment variable. Parse and resolution errors always name the
offending config key.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CONFIG_DIR = Path(os.environ.get("MNEMOSEED_HOME", Path.home() / ".mnemoseed"))
CONFIG_PATH = CONFIG_DIR / "config.toml"

LAYER_TYPES: tuple[str, ...] = ("vector", "graph", "meta", "embed")

PRESETS: dict[str, dict[str, str]] = {
    # layer -> default driver (M0 names from prd-08 FR-8.3 / FR-8.4)
    "embedded": {
        "vector": "lancedb_embedded",
        "graph": "sqlite_graph",
        "meta": "sqlite_meta",
        "embed": "bge_m3_onnx",
    },
    "docker": {
        "vector": "pgvector",
        "graph": "pg_graph",
        "meta": "pg_meta",
        "embed": "openai_compatible",
    },
    "custom": {},  # everything explicit; a missing layer is an error
}

VALID_PRESETS: tuple[str, ...] = tuple(PRESETS)
DEFAULT_PRESET = "embedded"
DEFAULT_INSTANCE = "main"


class ConfigError(ValueError):
    """Config parse or resolution error with the offending key named."""

    def __init__(self, key: str, message: str) -> None:
        self.key = key
        super().__init__(f"config[{key}]: {message}")


@dataclass(frozen=True)
class InstanceConfig:
    """A resolved driver instance for a layer."""

    name: str
    driver: str
    params: dict[str, Any]


@dataclass(frozen=True)
class _InstanceOverride:
    """An explicit named instance from the config file (driver optional)."""

    driver: str | None
    params: dict[str, Any]


@dataclass
class LayerSpec:
    """Per-layer explicit configuration from the config file."""

    layer: str
    driver: str | None = None  # optional per-layer override; falls back to the preset
    params: dict[str, Any] = field(default_factory=dict)
    instances: dict[str, _InstanceOverride] = field(default_factory=dict)


@dataclass(frozen=True)
class DreamConfig:
    """Dream-engine runtime flags (PRD-02 FR-2.8 manual-first discipline).

    ``auto_trigger`` decides whether ScorePool events drive dreams directly
    (True) or are held as pending manual runs for ``mnemoseed dream --once``
    (False, the M1 default until reflection quality passes review).
    """

    auto_trigger: bool = False


# T6 (FR-2.14): the three dream LLM roles. All are cloud/network-backed except
# local_track, which is the offline role (FR-2.7). Deep_reflection runs the slow
# full digests; short_increment runs the cheap per-increment passes; local_track
# runs the privacy/cost-hard-line track offline.
LLM_ROLES: tuple[str, ...] = ("deep_reflection", "short_increment", "local_track")


@dataclass(frozen=True)
class RoleLLMConfig:
    """One role's resolved LLM route: driver + model + params.

    Keys are config attestations, never values: an API key is referenced by
    env-var NAME in ``params["api_key_env"]`` and resolved at materialization
    time by the RoleRouter (mnemoseed.llm.routing) — a literal key in config is
    an error.
    """

    role: str
    driver: str
    model: str
    params: dict[str, Any] = field(default_factory=dict)


# Route defaults per design/02 section 6 (PRD FR-2.14): deep_reflection ->
# Kimi K3 via Fireworks (OpenAI-compatible); short_increment -> DeepSeek V4
# Flash via Fireworks; local_track -> a local Ollama model. NOTE: design/02 §6
# + FR-2.7 pin the offline default to a <=14B quantized model ("70B is not a
# default assumption"), which conflicts with PRD FR-2.14's Llama-3.3-70B line;
# the <=14B default wins here (flagged in the T6 report). Provider model ids
# verified against the Fireworks catalog: kimi-k3 ($3.00/$0.30/$15.00 per M
# input/cached/output) and deepseek-v4-flash ($0.14/$0.028/$0.28).
DEFAULT_LLM_ROUTES: dict[str, RoleLLMConfig] = {
    "deep_reflection": RoleLLMConfig(
        role="deep_reflection",
        driver="openai_compatible",
        model="accounts/fireworks/models/kimi-k3",
        params={
            "base_url": "https://api.fireworks.ai/inference/v1",
            "api_key_env": "FIREWORKS_API_KEY",
            "max_tokens": 2048,
        },
    ),
    "short_increment": RoleLLMConfig(
        role="short_increment",
        driver="openai_compatible",
        model="accounts/fireworks/models/deepseek-v4-flash",
        params={
            "base_url": "https://api.fireworks.ai/inference/v1",
            "api_key_env": "FIREWORKS_API_KEY",
        },
    ),
    "local_track": RoleLLMConfig(
        role="local_track",
        driver="ollama",
        model="llama3.1:8b",
        params={"base_url": "http://localhost:11434"},
    ),
}


@dataclass
class Config:
    """Resolved configuration. layer_instances() materializes per-layer drivers."""

    preset: str = DEFAULT_PRESET
    baseurl: str = "http://localhost:7788"
    storage: dict[str, LayerSpec] = field(default_factory=dict)
    dream: DreamConfig = field(default_factory=DreamConfig)
    llm: dict[str, RoleLLMConfig] = field(default_factory=lambda: dict(DEFAULT_LLM_ROUTES))
    source: Path | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def layer_instances(self, kind: str) -> dict[str, InstanceConfig]:
        """Resolve the instance set for one layer (always non-empty).

        order of precedence (weakest to strongest):
        preset default driver < explicit layer driver < named-instance driver.
        The default instance name is D6's "main".
        """
        if kind not in LAYER_TYPES:
            raise ConfigError(
                f"storage.{kind}", f"unknown storage layer (expected one of {', '.join(LAYER_TYPES)})"
            )
        if self.preset not in PRESETS:
            raise ConfigError("preset", f"unknown preset {self.preset!r}")

        spec = self.storage.get(kind)
        if spec is not None and spec.driver is not None:
            base_driver = spec.driver
            base_params = spec.params
        else:
            preset_driver = PRESETS[self.preset].get(kind)
            if preset_driver is None:
                raise ConfigError(
                    f"storage.{kind}.driver",
                    f"preset {self.preset!r} defines no default for layer {kind!r}; "
                    "an explicit driver is required under the custom preset",
                )
            base_driver = preset_driver
            base_params = spec.params if spec is not None else {}

        resolved: dict[str, InstanceConfig] = {}
        if spec is not None:
            for name, override in spec.instances.items():
                driver = override.driver if override.driver is not None else base_driver
                resolved[name] = InstanceConfig(name=name, driver=driver, params=override.params)
        if DEFAULT_INSTANCE not in resolved:
            resolved[DEFAULT_INSTANCE] = InstanceConfig(
                name=DEFAULT_INSTANCE, driver=base_driver, params=base_params
            )
        return resolved


def _require_table(value: Any, key: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(key, "must be a table")
    return value


def _optional_driver(value: Any, key: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ConfigError(key, "must be a non-empty string")
    return value


def load_config(path: Path | None = None) -> Config:
    """Load and validate config from the TOML file (STORAGE_MODE overrides preset)."""
    path = path or CONFIG_PATH
    raw: dict[str, Any] = {}
    if path.exists():
        raw = _require_table(tomllib.loads(path.read_text(encoding="utf-8")), "<config>")

    env_preset = os.environ.get("STORAGE_MODE")
    preset_raw: Any = env_preset if env_preset is not None else raw.get("preset", DEFAULT_PRESET)
    if not isinstance(preset_raw, str) or preset_raw not in PRESETS:
        key = "STORAGE_MODE" if env_preset is not None else "preset"
        raise ConfigError(key, f"unknown preset {preset_raw!r} (choose from: {', '.join(VALID_PRESETS)})")

    baseurl_raw: Any = raw.get("baseurl", "http://localhost:7788")
    if not isinstance(baseurl_raw, str):
        raise ConfigError("baseurl", "must be a string")

    storage: dict[str, LayerSpec] = {}
    storage_raw = raw.get("storage")
    if storage_raw is not None:
        storage_table = _require_table(storage_raw, "storage")
        for layer_key, layer_value in storage_table.items():
            layer_key = str(layer_key)
            layer_path = f"storage.{layer_key}"
            if layer_key not in LAYER_TYPES:
                raise ConfigError(
                    layer_path, f"unknown storage layer (expected one of {', '.join(LAYER_TYPES)})"
                )
            layer_table = _require_table(layer_value, layer_path)

            driver = _optional_driver(layer_table.get("driver"), f"{layer_path}.driver")
            params = {k: v for k, v in layer_table.items() if k not in ("driver", "instances")}

            overrides: dict[str, _InstanceOverride] = {}
            instances_raw = layer_table.get("instances")
            if instances_raw is not None:
                instances_table = _require_table(instances_raw, f"{layer_path}.instances")
                for name, entry in instances_table.items():
                    name = str(name)
                    entry_table = _require_table(entry, f"{layer_path}.instances.{name}")
                    entry_driver = _optional_driver(
                        entry_table.get("driver"), f"{layer_path}.instances.{name}.driver"
                    )
                    overrides[name] = _InstanceOverride(
                        driver=entry_driver,
                        params={k: v for k, v in entry_table.items() if k != "driver"},
                    )

            storage[layer_key] = LayerSpec(
                layer=layer_key,
                driver=driver,
                params=params,
                instances=overrides,
            )

    dream = DreamConfig()
    llm_routes = {role: cfg for role, cfg in DEFAULT_LLM_ROUTES.items()}
    dream_raw = raw.get("dream")
    if dream_raw is not None:
        dream_table = _require_table(dream_raw, "dream")
        auto_raw = dream_table.get("auto_trigger", False)
        if not isinstance(auto_raw, bool):
            raise ConfigError("dream.auto_trigger", "must be a boolean")
        dream = DreamConfig(auto_trigger=auto_raw)

        # T6 (FR-2.14): [dream.llm.<role>] overrides per role. Only structural
        # validation happens here (table-ity, known role, non-empty driver/model
        # strings); semantic failures (unknown driver, bad params) defer to the
        # RoleRouter when that role is actually resolved — a misconfigured unused
        # role never breaks boot.
        llm_raw = dream_table.get("llm")
        if llm_raw is not None:
            llm_table = _require_table(llm_raw, "dream.llm")
            for role in LLM_ROLES:
                entry = llm_table.get(role)
                if entry is None:
                    continue  # unconfigured role keeps its default route
                role_path = f"dream.llm.{role}"
                entry_table = _require_table(entry, role_path)
                driver = _optional_driver(entry_table.get("driver"), f"{role_path}.driver")
                model = _optional_driver(entry_table.get("model"), f"{role_path}.model")
                base = DEFAULT_LLM_ROUTES[role]
                params = {k: v for k, v in entry_table.items() if k not in ("driver", "model")}
                llm_routes[role] = RoleLLMConfig(
                    role=role,
                    driver=driver if driver is not None else base.driver,
                    model=model if model is not None else base.model,
                    params={**base.params, **params},
                )
            unknown = [str(role) for role in llm_table if str(role) not in LLM_ROLES]
            if unknown:
                raise ConfigError(
                    f"dream.llm.{unknown[0]}",
                    f"unknown llm role (expected one of {', '.join(LLM_ROLES)})",
                )

    return Config(
        preset=preset_raw,
        baseurl=baseurl_raw,
        storage=storage,
        dream=dream,
        llm=llm_routes,
        source=path,
        raw=raw,
    )


def default_config_toml() -> str:
    """Default config written by init."""
    return """\
# MnemoSeed configuration — single source of truth
preset = "embedded"          # embedded | docker | custom
baseurl = "http://localhost:7788"

# Dream-engine manual-first discipline (PRD-02 FR-2.8): keep dreams manual
# until reflection quality passes review, then flip to automatic.
# [dream]
# auto_trigger = false

# Per-layer overrides (required under the custom preset):
# [storage.vector]
# driver = "lancedb_embedded"
#
# [storage.graph]
# driver = "sqlite_graph"
#
# Named multi-instance (D6): a second GraphStore under its own name.
# [storage.graph.instances.isolated]
# driver = "sqlite_graph"
# path = "~/.mnemoseed/isolated.db"

# Dream LLM role routing (T6 / FR-2.14): pick the driver + model per role.
# API keys are referenced by ENV-VAR NAME only — never a literal key here;
# the router reads them from the process environment at materialization time.
# Other params (base_url, max_tokens, ...) override the per-role defaults.
# [dream.llm.deep_reflection]
# driver = "anthropic"
# model = "claude-sonnet-5"
# api_key_env = "ANTHROPIC_API_KEY"
#
# [dream.llm.local_track]
# driver = "ollama"
# model = "llama3.1:8b"
# base_url = "http://localhost:11434"
"""
