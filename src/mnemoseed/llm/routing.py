"""Role routing: [dream.llm] config -> configured DreamLLM instances (FR-2.14).

Each dream role (deep_reflection / short_increment / local_track) maps to a
driver + model + params. The RoleRouter materializes drivers lazily, only when a
role is first resolved, so a missing or misconfigured route for a role nothing
uses never breaks boot (FR-2.14 boot safety). API keys are referenced by
env-var NAME in config and resolved from the environment at materialization
time — the secret value is never stored anywhere. A materialized route is
audit-logged through the MetaStore ``audit_append`` seam (the same mechanism
T4's salvage writer used); the entry records the env var name, never the value.
``check()`` is the console's live-check probe (design/07 section 8): it never raises.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping
from typing import Protocol, cast

from mnemoseed.config import RoleLLMConfig
from mnemoseed.llm.registry import LLM_DRIVERS, LLMRegistry
from mnemoseed.llm.types import DreamLLM, HealthReport, LLMError, LLMRouteError
from mnemoseed.storage.ports import AuditEntry


class _AuditSink(Protocol):
    """The minimal MetaStore surface the router audits through."""

    def audit_append(self, entry: AuditEntry) -> None: ...


class RoleRouter:
    """Resolve dream roles to lazily-built, cached DreamLLM instances."""

    def __init__(
        self,
        *,
        routes: Mapping[str, RoleLLMConfig],
        registry: LLMRegistry | None = None,
        audit: _AuditSink | None = None,
        env: Callable[[str], str | None] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._routes = dict(routes)
        self._registry = registry if registry is not None else LLM_DRIVERS
        self._audit = audit
        self._env = env if env is not None else os.environ.get
        self._clock = clock if clock is not None else time.time
        self._cache: dict[str, DreamLLM] = {}

    def roles(self) -> tuple[str, ...]:
        """Configured role names, in config order."""
        return tuple(self._routes)

    def resolve(self, role: str) -> DreamLLM:
        """Materialize (and cache) the DreamLLM for one role.

        Lazy by design: an unused role is never constructed and its config is
        never validated, so a broken route for a role no one uses cannot break
        boot. Unknown driver names, missing env vars, and bad params all fail
        here only when the role is actually resolved.
        """
        cached = self._cache.get(role)
        if cached is not None:
            return cached
        cfg = self._routes.get(role)
        if cfg is None:
            raise LLMRouteError(f"no llm route configured for role {role!r}")
        params = dict(cfg.params)
        env_name = params.pop("api_key_env", None)
        # api_key_env may name a comma-separated fallback chain ("role-specific
        # var, shared provider var"): the first variable actually set wins, so
        # a single provider key covers every role by default while any role can
        # be pointed at a different provider/key on its own.
        api_key = ""
        if env_name:
            for name in (n.strip() for n in env_name.split(",")):
                value = self._env(name) if name else None
                if value:
                    api_key = value
                    break
        params["model"] = cfg.model
        params["api_key"] = api_key
        instance = cast(DreamLLM, self._registry.build(cfg.driver, params))
        self._cache[role] = instance
        self._audit_configured(role, cfg, env_name)
        return instance

    def check(self, role: str) -> HealthReport:
        """Probe one role for the console's live-check button; never raises.

        Returns the driver's health report, or a failed report naming the
        routing problem when the role cannot be materialized.
        """
        try:
            driver = self.resolve(role)
        except LLMError as exc:
            return HealthReport(ok=False, detail={"error": str(exc), "status": "unconfigured"})
        return driver.check()

    def _audit_configured(self, role: str, cfg: RoleLLMConfig, env_name: str | None) -> None:
        if self._audit is None:
            return
        self._audit.audit_append(
            AuditEntry(
                actor="dream-router",
                action="llm_role_configured",
                detail={
                    "role": role,
                    "driver": cfg.driver,
                    "model": cfg.model,
                    "api_key_env": env_name,  # the env var NAME, never the secret value
                },
                at=self._clock(),
            )
        )
