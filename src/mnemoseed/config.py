"""Config loading: ~/.mnemoseed/config.toml is the single source of truth.

A preset (embedded/docker/custom) maps each storage layer to a default driver;
layers can be overridden individually and a layer may declare named instances
(e.g. graph.main / graph.isolated, D6). STORAGE_MODE is kept as a preset
shortcut environment variable. Parse and resolution errors always name the
offending config key.
"""

from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mnemoseed.schema.graph import NodeType
from mnemoseed.secrets.refs import SECRETS_REF_RE, is_secrets_ref

logger = logging.getLogger("mnemoseed.config")

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


# FR-2.5b: monthly per-profile dream token budget in USD (default $5/month, NFR-2.2).
# Defined here — AND mirrored as DEFAULT_MONTHLY_BUDGET_USD in dream/ledger.py —
# because config cannot import the ledger (ledger -> delta -> snapshot -> config
# would be a cycle); test_ledger_default_budget_matches_config_default pins them
# equal.
DEFAULT_DREAM_TOKEN_BUDGET_USD: float = 5.0


@dataclass(frozen=True)
class DreamConfig:
    """Dream-engine runtime flags (PRD-02 FR-2.8 manual-first discipline).

    ``auto_trigger`` decides whether ScorePool events drive dreams directly
    (True) or are held as pending manual runs for ``mnemoseed dream --once``
    (False, the M1 default until reflection quality passes review).
    ``token_budget_usd`` is the monthly ledger cap (FR-2.5b): once the projected
    UTC-month spend exceeds it, dreams degrade to capture-only until the next
    month rolls over.
    """

    auto_trigger: bool = False
    token_budget_usd: float = DEFAULT_DREAM_TOKEN_BUDGET_USD


#: Decay sweep cadence (NFR-4.1: the batch runs once daily).
DEFAULT_SWEEP_INTERVAL_S: float = 86400.0

#: Weight-change floor under which a sweep write is skipped ("dumb write").
DEFAULT_MIN_APPLY_DELTA: float = 0.01

#: Per-type exponential decay rates (PRD-04 FR-4.1, design/01 §5):
#: fact 0.01 (half-life ≈ 69 days), preference 0.005 (≈ 139 days),
#: episode 0.03 (≈ 23 days). The ``"chunk"`` pseudo-type covers the verbatim
#: vector channel, which carries no node_type.
DEFAULT_LAMBDA_PER_TYPE: dict[str, float] = {
    # fact-class (λ_fact = 0.01, half-life ≈ 69 days)
    "USER": 0.01,
    "HABIT": 0.01,
    "DECISION": 0.01,
    "PROJECT": 0.01,
    "TOOL": 0.01,
    "SKILL_SEQUENCE": 0.01,
    "CONSTRAINT": 0.01,
    # preference-class (λ_preference = 0.005, half-life ≈ 139 days)
    "PREFERENCE": 0.005,
    "ANIMA": 0.005,
    # episode-class (λ_episode = 0.03, half-life ≈ 23 days)
    "EPISODE": 0.03,
    "INTENTION": 0.03,
    # the verbatim channel has no node_type; chunks decay like episodes
    "chunk": 0.03,
}

#: The writable λ-map keys: every frozen node type plus the chunk pseudo-type.
LAMBDA_TARGETS: frozenset[str] = frozenset(NodeType.frozen_set()) | {"chunk"}


@dataclass(frozen=True)
class DecayConfig:
    """Decay-engine runtime flags (PRD-04 FR-4.1 / FR-4.4, design/01 stage ⑤).

    ``enabled`` gates the daemon's sweep task at boot. ``sweep_interval_s`` is
    the sweep cadence (NFR-4.1: once daily by default); ``min_apply_delta`` is
    the write floor that skips sub-threshold drops. ``lambda_per_type`` maps a
    node type (or the ``"chunk"`` pseudo-type for the verbatim channel) to its
    exponential rate; the map is carried verbatim from the file — entries the
    user omitted resolve to the per-type design default at sweep time
    (``decay.model.lambda_for``), keeping the file and the settings DB always
    in agreement.
    """

    enabled: bool = True
    sweep_interval_s: float = DEFAULT_SWEEP_INTERVAL_S
    min_apply_delta: float = DEFAULT_MIN_APPLY_DELTA
    lambda_per_type: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_LAMBDA_PER_TYPE))


# T6 (FR-2.14): the dream LLM roles, both cloud/network-backed. Deep_reflection
# runs the slow full digests; short_increment runs the cheap per-increment
# passes. The offline "local_track" role was deprecated and removed (E1-1): any
# legacy [dream.llm.local_track] table is tolerated on load and ignored with a
# warning, never applied.
LLM_ROLES: tuple[str, ...] = ("deep_reflection", "short_increment")

#: The removed offline role name (E1-1): recognized for deprecation tolerance.
LEGACY_LOCAL_TRACK_ROLE = "local_track"

#: Wording shared by the loader warning, the admin surface, and the wire: a
#: legacy table or a write targeting the removed role answers the same message.
LOCAL_TRACK_DEPRECATION = (
    "[dream.llm.local_track] was deprecated and removed; the offline track was "
    "merged into deep_reflection and short_increment"
)


@dataclass(frozen=True)
class RoleLLMConfig:
    """One role's resolved LLM route: driver + model + params.

    Keys are config attestations, never values: an API key is referenced by
    env-var NAME or a ``secrets:mnemoseed/dream/<role>`` reference in
    ``params["api_key_env"]`` and resolved at materialization time by the
    RoleRouter (mnemoseed.llm.routing) — a literal key in config is an error.
    """

    role: str
    driver: str
    model: str
    params: dict[str, Any] = field(default_factory=dict)


# Route defaults per design/02 section 6 (PRD FR-2.14): deep_reflection ->
# Kimi K3 via Fireworks (OpenAI-compatible); short_increment -> DeepSeek V4
# Flash (0731) via Fireworks. NOTE:
# design/02 §6 + FR-2.7 pin the offline default to a <=14B quantized model
# ("70B is not a default assumption"), which conflicts with PRD FR-2.14's
# Llama-3.3-70B line; the <=14B default wins here (flagged in the T6 report).
# Provider model ids verified against the Fireworks catalog: kimi-k3
# ($3.00/$0.30/$15.00 per M input/cached/output) and deepseek-v4-flash-0731
# ($0.14/$0.028/$0.28). Each role defaults to its own key env var with the
# shared FIREWORKS_API_KEY as fallback, so the two cloud roles can be pointed
# at different providers/keys independently.
DEFAULT_LLM_ROUTES: dict[str, RoleLLMConfig] = {
    "deep_reflection": RoleLLMConfig(
        role="deep_reflection",
        driver="openai_compatible",
        model="accounts/fireworks/models/kimi-k3",
        params={
            "base_url": "https://api.fireworks.ai/inference/v1",
            "api_key_env": "MNEMOSEED_DEEP_REFLECTION_API_KEY,FIREWORKS_API_KEY",
            "max_tokens": 2048,
        },
    ),
    "short_increment": RoleLLMConfig(
        role="short_increment",
        driver="openai_compatible",
        model="accounts/fireworks/models/deepseek-v4-flash-0731",
        params={
            "base_url": "https://api.fireworks.ai/inference/v1",
            "api_key_env": "MNEMOSEED_SHORT_INCREMENT_API_KEY,FIREWORKS_API_KEY",
        },
    ),
}


@dataclass
class Config:
    """Resolved configuration. layer_instances() materializes per-layer drivers."""

    preset: str = DEFAULT_PRESET
    baseurl: str = "http://localhost:7788"
    storage: dict[str, LayerSpec] = field(default_factory=dict)
    dream: DreamConfig = field(default_factory=DreamConfig)
    decay: DecayConfig = field(default_factory=DecayConfig)
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


def _is_positive_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


def _is_non_negative_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0


def _validate_api_key_ref(role: str, value: Any) -> None:
    """Shape-check an ``api_key_env`` param (T2-2).

    A ``secrets:`` reference must be well-formed and name a live dream role;
    a malformed reference can never resolve, so it is a load error naming the
    key. Anything else (env-var NAME lists, and hand-edited literal keys)
    passes through unchanged — literal keys stay the pre-existing
    tolerated-then-redacted contract.
    """
    if not isinstance(value, str) or not is_secrets_ref(value):
        return
    key = f"dream.llm.{role}.api_key_env"
    match = SECRETS_REF_RE.fullmatch(value)
    if match is None:
        raise ConfigError(
            key,
            "a secrets: reference must look like 'secrets:mnemoseed/dream/<role>'",
        )
    if match.group(1) not in LLM_ROLES:
        raise ConfigError(
            key,
            f"a secrets: reference must name a live dream role (one of {', '.join(LLM_ROLES)})",
        )


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
        budget_raw = dream_table.get("token_budget_usd", DEFAULT_DREAM_TOKEN_BUDGET_USD)
        if not isinstance(budget_raw, (int, float)) or isinstance(budget_raw, bool) or budget_raw <= 0:
            raise ConfigError("dream.token_budget_usd", "must be a positive number")
        dream = DreamConfig(auto_trigger=auto_raw, token_budget_usd=float(budget_raw))

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
                if "api_key_env" in params:
                    _validate_api_key_ref(role, params["api_key_env"])
                llm_routes[role] = RoleLLMConfig(
                    role=role,
                    driver=driver if driver is not None else base.driver,
                    model=model if model is not None else base.model,
                    params={**base.params, **params},
                )
            unknown = [
                str(role)
                for role in llm_table
                if str(role) not in LLM_ROLES and str(role) != LEGACY_LOCAL_TRACK_ROLE
            ]
            if unknown:
                raise ConfigError(
                    f"dream.llm.{unknown[0]}",
                    f"unknown llm role (expected one of {', '.join(LLM_ROLES)})",
                )
            if LEGACY_LOCAL_TRACK_ROLE in llm_table:
                logger.warning(LOCAL_TRACK_DEPRECATION)

    # [decay] table (PRD-04): sweep cadence, write floor, enabled flag and the
    # per-type λ map. The λ map is carried verbatim (replace semantics: the
    # map IS what the file says) — omitted types resolve to their design
    # default at sweep time via decay.model.lambda_for, so the live config
    # always equals the file (and the DB-primary settings mirror never sees a
    # phantom drift).
    decay = DecayConfig()
    decay_raw = raw.get("decay")
    if decay_raw is not None:
        decay_table = _require_table(decay_raw, "decay")
        enabled_raw = decay_table.get("enabled", True)
        if not isinstance(enabled_raw, bool):
            raise ConfigError("decay.enabled", "must be a boolean")
        interval_raw = decay_table.get("sweep_interval_s", DEFAULT_SWEEP_INTERVAL_S)
        if not _is_positive_number(interval_raw):
            raise ConfigError("decay.sweep_interval_s", "must be a positive number")
        delta_raw = decay_table.get("min_apply_delta", DEFAULT_MIN_APPLY_DELTA)
        if not _is_non_negative_number(delta_raw):
            raise ConfigError("decay.min_apply_delta", "must be a non-negative number")
        lambda_map = dict(DEFAULT_LAMBDA_PER_TYPE)
        lambda_raw = decay_table.get("lambda_per_type")
        if lambda_raw is not None:
            lambda_table = _require_table(lambda_raw, "decay.lambda_per_type")
            parsed: dict[str, float] = {}
            for key, rate in lambda_table.items():
                key = str(key)
                if key not in LAMBDA_TARGETS:
                    raise ConfigError(f"decay.lambda_per_type.{key}", "unknown memory type")
                if not _is_positive_number(rate):
                    raise ConfigError(f"decay.lambda_per_type.{key}", "must be a positive number")
                parsed[key] = float(rate)
            lambda_map = parsed
        decay = DecayConfig(
            enabled=enabled_raw,
            sweep_interval_s=float(interval_raw),
            min_apply_delta=float(delta_raw),
            lambda_per_type=lambda_map,
        )

    return Config(
        preset=preset_raw,
        baseurl=baseurl_raw,
        storage=storage,
        dream=dream,
        decay=decay,
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
# token_budget_usd = 5.0   # monthly ledger cap in USD; capture-only once spent (FR-2.5b)

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
# API keys are referenced by ENV-VAR NAME or a secrets:mnemoseed/dream/<role>
# reference — never a literal key here; the router resolves the value from the
# process environment / the local secret store at materialization time.
# Other params (base_url, max_tokens, ...) override the per-role defaults.
# [dream.llm.deep_reflection]
# driver = "anthropic"
# model = "claude-sonnet-5"
# api_key_env = "ANTHROPIC_API_KEY"

# The offline local_track role was deprecated and removed: any legacy
# [dream.llm.local_track] table is tolerated on load and ignored with a warning.
# [dream.llm.short_increment]
# driver = "openai_compatible"
# model = "accounts/fireworks/models/deepseek-v4-flash-0731"

# Decay engine (PRD-04 FR-4.1 / design/01 stage ⑤): unreinforced memories fade
# through w = confidence × exp(-λ × days). λ is layered per node type
# (fact 0.01 / preference 0.005 / episode 0.03) plus the "chunk" pseudo-type
# for the verbatim channel; the sweep runs once daily (NFR-4.1) and skips
# writes whose weight change is below min_apply_delta. The map is replace
# semantics: keys you omit fall back to their per-type default.
# [decay]
# enabled = true
# sweep_interval_s = 86400.0
# min_apply_delta = 0.01
# lambda_per_type = {"PREFERENCE": 0.005, "EPISODE": 0.03, "chunk": 0.03}
"""
