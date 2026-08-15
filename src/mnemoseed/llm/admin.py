"""LLM admin surface (issue #23; FR-6.9 wizard + design/07 section 8).

The shared validation + persistence layer the console Models & Routing page,
the first-run dream-model step, and ``mnemoseed llm`` all funnel through:

- ``routes()`` reports each dream role's route (driver / model / endpoint /
  env-var NAME) plus a cached live connectivity probe and the built-in driver
  catalog. A literal key value anywhere in the payload is a hard failure.
- ``oauth_availability()`` detects host Codex / Grok OAuth login state
  (presence + expiry) without ever reading a token value out of the files.
- ``set_role()`` validates (unknown role / driver are typed errors), funnels
  the persistence through the single config writer (PRD-07 FR-7.11 — surgical
  TOML patch, versioned record, audit) and audits the env-var NAME -- never the
  value.
- ``test_config()`` runs a proposed route's ``check()`` against a live endpoint
  so the console test buttons probe BEFORE anything is written (FR-6.9).

Credential hygiene follows the role router's rule (FR-2.14): config stores
env-var NAMES, this surface attests them, and the runtime resolves values from
the process environment only.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol, cast

from mnemoseed.config import (
    DEFAULT_LLM_ROUTES,
    LEGACY_LOCAL_TRACK_ROLE,
    LLM_ROLES,
    LOCAL_TRACK_DEPRECATION,
    Config,
    RoleLLMConfig,
)
from mnemoseed.configwrite.service import ConfigWriteError, ConfigWriteService
from mnemoseed.llm.drivers.oauth import SUPPORTED_PROVIDERS, OAuthLLM
from mnemoseed.llm.registry import LLM_DRIVERS, LLMRegistry
from mnemoseed.llm.routing import RoleRouter
from mnemoseed.llm.types import HealthReport, LLMError, LLMUnavailable
from mnemoseed.secrets.refs import is_secrets_ref, secret_name_from_ref
from mnemoseed.secrets.store import SecretStore
from mnemoseed.storage.ports import AuditEntry

#: How long a per-role connectivity probe stays cached before re-running the
#: driver's checks (the console page polls; the probes must not hammer a host).
_CONNECTIVITY_TTL = 30.0

#: A successful connectivity test authorizes a matching route persist for this
#: long (in-process only; a daemon restart requires a fresh test).
_TEST_GRACE = 600.0

#: How many successfully-probed signatures stay remembered in-process before
#: the oldest are actively dropped (a bounded cap on top of lazy expiry).
_MAX_PASSED_TESTS = 128


class LLMAdminError(ValueError):
    """Typed validation failure on the LLM admin surface (mapped to 422)."""


class LLMTestRequiredError(LLMAdminError):
    """A matching connectivity test must pass before this route can persist.

    The persist carries the exact signature the caller must have probed first
    (driver + model + base_url + api_key_env + provider) within the grace
    window; any mismatch or expiry rejects the write (mapped to 409).
    """


class _AuditSink(Protocol):
    """The minimal MetaStore surface the admin service audits through."""

    def audit_append(self, entry: AuditEntry) -> None: ...


class LLMAdminService:
    """Daemon/config-facing hub for reading, probing, and editing dream roles."""

    def __init__(
        self,
        config: Config,
        meta: _AuditSink | None = None,
        *,
        registry: LLMRegistry | None = None,
        clock: Callable[[], float] | None = None,
        home: str | Path | None = None,
        env: Callable[[str], str | None] | None = None,
        configwrite: ConfigWriteService | None = None,
        secrets: SecretStore | None = None,
    ) -> None:
        self._config = config
        self._meta = meta
        self._registry = registry if registry is not None else LLM_DRIVERS
        self._clock = clock if clock is not None else time.time
        self._home = Path(home).expanduser() if home is not None else None
        self._env = env if env is not None else os.environ.get
        self._secrets = secrets
        self._connectivity_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        # Signature -> last successful probe time (in-process only; MUST-FIX 2).
        self._passed_tests: dict[str, float] = {}
        # The single config writer (PRD-07 FR-7.11): every route change lands
        # through its registry -> validate -> patch -> record -> audit flow. The
        # default runs offline (file patch only) so a bare audit-only meta
        # never drags the versioned store surface into unit construction.
        self._configwrite = (
            configwrite if configwrite is not None else ConfigWriteService(config, None, clock=self._clock)
        )

    # ---------------------------------------------------------------- read

    def routes(self) -> dict[str, Any]:
        """Per-role route + connectivity + the driver catalog, for the wire.

        ``roles`` keeps the backward-compatible descriptor list; the new
        ``routes`` map adds the RESOLVED defaults chain per role: top-level
        fields are explicit-only (None when the file pins nothing) and
        ``effective`` carries the merged values a fresh load would apply
        (E1-1 hot-apply readiness).
        """
        roles: list[dict[str, Any]] = []
        routes: dict[str, Any] = {}
        for role in LLM_ROLES:
            cfg = self._config.llm.get(role)
            if cfg is None:
                continue  # unconfigured role: never in the payload
            table = self._explicit_table(role)
            roles.append(
                {
                    "role": role,
                    "driver": table.get("driver") or cfg.driver,
                    "model": table.get("model") or cfg.model,
                    "base_url": table.get("base_url"),  # explicit only
                    "api_key_env": table.get("api_key_env"),  # explicit only
                    "provider": table.get("provider"),
                    "explicit": bool(table),
                    "connectivity": self._connectivity(role),
                }
            )
            routes[role] = {
                "driver": table.get("driver"),
                "model": table.get("model"),
                "base_url": table.get("base_url"),
                "api_key_env": table.get("api_key_env"),
                "provider": table.get("provider"),
                "effective": {
                    "driver": cfg.driver,
                    "model": cfg.model,
                    "base_url": cfg.params.get("base_url"),
                    "api_key_env": cfg.params.get("api_key_env"),
                    "provider": cfg.params.get("provider"),
                },
            }
        return {
            "roles": roles,
            "routes": routes,
            "drivers": [
                {"name": info.name, "description": info.description} for info in self._registry.catalog()
            ],
            "checked_at": self._clock(),
        }

    def oauth_availability(self) -> dict[str, Any]:
        """Host Codex / Grok login state: presence + expiry, never token values."""
        now = self._clock()
        providers: list[dict[str, Any]] = []
        for provider in SUPPORTED_PROVIDERS:
            try:
                oauth = OAuthLLM(provider=provider, home=self._home, clock=self._clock)
                auth = oauth._read_auth()
            except LLMUnavailable:
                providers.append(
                    {"provider": provider, "present": False, "expires_at": None, "expired": None}
                )
                continue
            expiry = oauth._parsed_expiry(auth)
            providers.append(
                {
                    "provider": provider,
                    "present": True,
                    "expires_at": expiry,
                    "expired": True if expiry is None else now >= expiry,
                }
            )
        return {"providers": providers, "checked_at": now}

    # ---------------------------------------------------------------- write

    def set_role(
        self,
        role: str,
        *,
        driver: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        api_key_env: str | None = None,
        provider: str | None = None,
        actor: str = "console",
    ) -> dict[str, Any]:
        """Validate and persist one role-route change; network-free.

        ``None`` keeps the current value; ``""`` clears an optional param
        (driver/model cannot be cleared). The exact written keys land back in
        the config TOML (surgical patch) and the ENV-VAR NAME -- never a
        value -- is audit-logged.

        A persist is rejected unless a connectivity test with the exact same
        signature (driver + model + base_url + api_key_env + provider) passed
        within the grace window (see :class:`LLMTestRequiredError`).
        """
        self._validate_role(role)
        current = self._config.llm[role]

        # Resolve the merged route: None keeps the current value, "" clears an
        # optional param, and the returned table is exactly what gets written.
        new_driver, new_model, table = self._merged_route(
            role,
            driver=driver,
            model=model,
            base_url=base_url,
            api_key_env=api_key_env,
            provider=provider,
        )
        if not new_driver:
            raise LLMAdminError("driver must be a non-empty string")
        if not new_model:
            raise LLMAdminError("model must be a non-empty string")
        if not self._registry.contains(new_driver):
            raise LLMAdminError(
                f"unknown llm driver {new_driver!r} (available: {', '.join(self._registry.names())})"
            )
        provider_value = cast(
            str | None, provider.strip() if provider is not None else current.params.get("provider")
        )
        if new_driver == "oauth" and provider_value not in SUPPORTED_PROVIDERS:
            raise LLMAdminError(
                f"the oauth driver requires a provider (built-in: {', '.join(SUPPORTED_PROVIDERS)})"
            )

        # MUST-FIX 2: reject the write unless a test with the exact same
        # signature passed within the grace window.
        signature = self._signature(
            driver=new_driver,
            model=new_model,
            base_url=table.get("base_url"),
            api_key_env=table.get("api_key_env"),
            provider=table.get("provider"),
        )
        tested_at = self._passed_tests.get(signature)
        if tested_at is None or self._clock() - tested_at > _TEST_GRACE:
            raise LLMTestRequiredError(
                f"a connectivity test for this exact route (driver={new_driver!r}, "
                f"model={new_model!r}) must pass before it can be persisted"
            )

        path = self._persist_role_table(role, table, actor=actor)

        # Keep the in-memory config in lockstep with the file (the explicit
        # table now lives in config.raw; the merged role reflects the change).
        merged_params = dict(current.params)
        if api_key_env is not None:
            merged_params.pop("api_key_env", None)
            if api_key_env.strip():
                merged_params["api_key_env"] = api_key_env.strip()
        if base_url is not None:
            merged_params.pop("base_url", None)
            if base_url.strip():
                merged_params["base_url"] = base_url.strip()
        if provider is not None:
            merged_params.pop("provider", None)
            if provider.strip():
                merged_params["provider"] = provider.strip()

        self._config.llm[role] = RoleLLMConfig(
            role=role, driver=new_driver, model=new_model, params=merged_params
        )
        self._connectivity_cache.pop(role, None)

        audited = self._audit_role_set(role, table, path, actor=actor)
        return {
            "role": role,
            "driver": new_driver,
            "model": new_model,
            "base_url": table.get("base_url"),
            "api_key_env": table.get("api_key_env"),
            "provider": table.get("provider"),
            "persisted_to": str(path),
            "audited": audited,
        }

    # ---------------------------------------------------------------- keys (T2-3)

    def set_key(self, role: str, key: str, *, actor: str = "console") -> dict[str, Any]:
        """Store one role's API key and pin the ``secrets:`` reference.

        The key VALUE lands in the SecretStore (a restricted file under the
        config dir) and the config carries only the reference — persisted
        through the single config writer, so the write is versioned + audited
        and the role-generation bump hot-applies it to the next dream run
        (no restart). The value never reaches the response, the audit entry,
        or the config file.
        """
        self._validate_role(role)
        key = key.strip()
        if not key:
            raise LLMAdminError("key must be a non-empty string")
        if self._secrets is None:
            raise LLMAdminError("the secret store is not available")
        ref = f"secrets:mnemoseed/dream/{role}"
        self._secrets.set(f"mnemoseed/dream/{role}", key)
        try:
            self._configwrite.set(f"dream.llm.{role}.api_key_env", ref, actor=actor)
        except ConfigWriteError as exc:
            raise LLMAdminError(str(exc)) from exc
        return {
            "ok": True,
            "role": role,
            "masked_tail": self._secrets.masked_tail(f"mnemoseed/dream/{role}"),
            "restart_required": False,
        }

    def delete_key(self, role: str, *, actor: str = "console") -> dict[str, Any]:
        """Remove a stored key and clear its reference.

        The role falls back to the env-var chain (the loader default, or an
        explicitly configured env chain) once the reference is gone.
        """
        self._validate_role(role)
        if self._secrets is None:
            raise LLMAdminError("the secret store is not available")
        self._secrets.delete(f"mnemoseed/dream/{role}")
        current = self._config.llm[role]
        if is_secrets_ref(str(current.params.get("api_key_env"))):
            try:
                self._configwrite.set(f"dream.llm.{role}.api_key_env", "", actor=actor)
            except ConfigWriteError as exc:
                raise LLMAdminError(str(exc)) from exc
            # The explicit reference is gone; restore the loader-default env
            # chain into the live role so the router falls back to env vars
            # without waiting for a reload.
            params = dict(self._config.llm[role].params)
            params.pop("api_key_env", None)
            default_chain = DEFAULT_LLM_ROUTES[role].params.get("api_key_env")
            if default_chain:
                params["api_key_env"] = default_chain
            self._config.llm[role] = RoleLLMConfig(
                role=role,
                driver=current.driver,
                model=current.model,
                params=params,
            )
        return {"ok": True, "role": role, "restart_required": False}

    def _validate_role(self, role: str) -> None:
        if role == LEGACY_LOCAL_TRACK_ROLE:
            raise LLMAdminError(LOCAL_TRACK_DEPRECATION)
        if role not in LLM_ROLES:
            raise LLMAdminError(f"unknown llm role {role!r} (choose from: {', '.join(LLM_ROLES)})")

    # ---------------------------------------------------------------- probe

    def test_config(
        self,
        *,
        role: str,
        driver: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        api_key_env: str | None = None,
        provider: str | None = None,
    ) -> HealthReport:
        """Run a proposed route's connectivity check against a live endpoint.

        Omitted fields are merged server-side against the current route (the
        same merge ``set_role`` persists), so a partial probe arms exactly the
        signature a partial set will be checked against. An omitted
        ``api_key_env`` resolves the route's EFFECTIVE key source (secrets
        reference, env chain, or the loader-default chain) so the probe never
        authenticates worse than a live resolve. Never raises (returns a
        failed HealthReport instead) so the console test buttons always render
        a typed inline result.
        """
        if role == LEGACY_LOCAL_TRACK_ROLE:
            return HealthReport(ok=False, detail={"error": LOCAL_TRACK_DEPRECATION})
        if role not in LLM_ROLES:
            return HealthReport(ok=False, detail={"error": f"unknown llm role {role!r}"})
        try:
            merged_driver, merged_model, table = self._merged_route(
                role,
                driver=driver,
                model=model,
                base_url=base_url,
                api_key_env=api_key_env,
                provider=provider,
            )
        except KeyError:
            return HealthReport(ok=False, detail={"error": f"unknown llm role {role!r}"})
        if not self._registry.contains(merged_driver):
            return HealthReport(ok=False, detail={"error": f"unknown llm driver {merged_driver!r}"})
        params: dict[str, Any] = {}
        for name, value in (
            ("base_url", table.get("base_url") or ""),
            ("provider", table.get("provider") or ""),
        ):
            if value:
                params[name] = value
        # A proposed KEY is referenced by env-var NAME or a secrets:
        # reference; resolve it exactly like the role router does (never a
        # value over the wire). When the probe payload OMITS api_key_env (and
        # the explicit table pins nothing either), fall back to the role's
        # EFFECTIVE key source — a secrets: reference, an env chain, or the
        # loader-default env chain — so a partial probe authenticates exactly
        # like a live resolve instead of probing with no auth. An explicit ""
        # is a clear, not an omit: the probe stays unauthenticated.
        key_source: Any = table.get("api_key_env")
        if not key_source and api_key_env is None:
            key_source = self._config.llm[role].params.get("api_key_env")
        api_key = ""
        if key_source:
            key_source = str(key_source)
            if is_secrets_ref(key_source):
                secret_key = secret_name_from_ref(key_source)
                if secret_key and self._secrets is not None:
                    api_key = self._secrets.get(secret_key) or ""
            else:
                for name in (entry.strip() for entry in key_source.split(",")):
                    env_value = self._env(name) if name else None
                    if env_value:
                        api_key = env_value
                        break
        params["model"] = merged_model
        params["api_key"] = api_key
        try:
            instance = self._registry.build(merged_driver, params)
            report = cast(HealthReport, instance.check())
        except LLMError as exc:
            return HealthReport(ok=False, detail={"error": str(exc)})
        if report.ok:
            # MUST-FIX 2: a passing probe authorizes a matching persist within
            # the grace window (exact signature, in-process cache only).
            self._record_passed_test(
                self._signature(
                    driver=merged_driver,
                    model=merged_model,
                    base_url=table.get("base_url"),
                    api_key_env=table.get("api_key_env"),
                    provider=table.get("provider"),
                ),
                self._clock(),
            )
        return report

    # ---------------------------------------------------------------- plumbing

    def _merged_route(
        self,
        role: str,
        *,
        driver: str | None,
        model: str | None,
        base_url: str | None,
        api_key_env: str | None,
        provider: str | None,
    ) -> tuple[str, str, dict[str, Any]]:
        """Merge a partial route update against the current explicit table.

        ``None`` keeps the current value; ``""`` clears an optional param. The
        returned table is exactly what ``set_role`` would persist, so probing
        and persisting the same merge always share one signature.
        """
        current = self._config.llm[role]
        table = dict(self._explicit_table(role))
        new_driver = current.driver if driver is None else driver.strip()
        new_model = current.model if model is None else model.strip()
        if driver is not None:
            table["driver"] = new_driver
        if model is not None:
            table["model"] = new_model
        for name, field_value in (
            ("base_url", base_url),
            ("api_key_env", api_key_env),
            ("provider", provider),
        ):
            if field_value is None:
                continue
            # The console's "Another OpenAI-compatible API" card id is UI
            # metadata, never a route field: dropping it here keeps the probe
            # and the persist on one signature and the config mirror free of a
            # dead ``provider = "other"`` line.
            if name == "provider" and field_value.strip() == "other":
                continue
            if not field_value.strip():
                table.pop(name, None)
            else:
                table[name] = field_value.strip()
        return new_driver, new_model, table

    def _record_passed_test(self, signature: str, now: float) -> None:
        """Record a passing probe, actively evicting expired signatures and
        capping the cache at the most recent ``_MAX_PASSED_TESTS``."""
        for stale in [sig for sig, at in self._passed_tests.items() if now - at > _TEST_GRACE]:
            del self._passed_tests[stale]
        self._passed_tests[signature] = now
        if len(self._passed_tests) > _MAX_PASSED_TESTS:
            overflow = len(self._passed_tests) - _MAX_PASSED_TESTS
            for stale in sorted(self._passed_tests, key=lambda sig: self._passed_tests[sig])[:overflow]:
                del self._passed_tests[stale]

    @staticmethod
    def _signature(
        *,
        driver: str,
        model: str,
        base_url: str | None,
        api_key_env: str | None,
        provider: str | None,
    ) -> str:
        """The exact route signature that must be connectivity-tested first.

        ``None`` and ``""`` are normalized to the same empty value so a cleared
        optional field still keys identically.
        """
        return "\x1f".join(
            (
                driver.strip(),
                model.strip(),
                (base_url or "").strip(),
                (api_key_env or "").strip(),
                (provider or "").strip(),
            )
        )

    def _explicit_table(self, role: str) -> dict[str, Any]:
        """The role's own [dream.llm.<role>] table from the source TOML."""
        dream = self._config.raw.get("dream")
        if not isinstance(dream, dict):
            return {}
        llm = dream.get("llm")
        if not isinstance(llm, dict):
            return {}
        table = llm.get(role)
        if not isinstance(table, dict):
            return {}
        return {str(key): value for key, value in table.items()}

    def _persist_role_table(self, role: str, table: dict[str, Any], *, actor: str = "console") -> Path:
        """Persist one role-route change through the single config writer.

        Every field this call actually changed is a ``config.set`` on
        ``dream.llm.<role>.<field>`` (registry validation + surgical TOML
        patch + versioned record + audit, live-applied); untouched fields are
        never re-written. The written file is returned so the audit entry still
        reports where the route landed.
        """
        old_table = self._explicit_table(role)
        persisted: Path | None = None
        for field in sorted(set(old_table) | set(table)):
            if old_table.get(field) == table.get(field):
                continue
            try:
                result = self._configwrite.set(f"dream.llm.{role}.{field}", table.get(field), actor=actor)
            except ConfigWriteError as exc:
                raise LLMAdminError(str(exc)) from exc
            persisted = Path(result["persisted_to"])
        if persisted is None:
            source = self._config.source
            persisted = source if source is not None else Path.home() / ".mnemoseed" / "config.toml"
        return persisted

    def _connectivity(self, role: str) -> dict[str, Any]:
        now = self._clock()
        cached = self._connectivity_cache.get(role)
        if cached is not None and now - cached[0] < _CONNECTIVITY_TTL:
            return cached[1]
        report = RoleRouter(
            routes=dict(self._config.llm), audit=None, clock=self._clock, secrets=self._secrets
        ).check(role)
        payload: dict[str, Any] = {
            "ok": report.ok,
            "detail": report.detail,
            "checked_at": now,
        }
        self._connectivity_cache[role] = (now, payload)
        return payload

    def _audit_role_set(
        self, role: str, table: dict[str, Any], path: Path, *, actor: str = "console"
    ) -> bool:
        if self._meta is None:
            return False
        self._meta.audit_append(
            AuditEntry(
                actor=actor,
                action="llm_role_set",
                detail={
                    "role": role,
                    "driver": table.get("driver"),
                    "model": table.get("model"),
                    "base_url": table.get("base_url"),
                    # the env var NAME, never the secret value (FR-2.14 hygiene)
                    "api_key_env": table.get("api_key_env"),
                    "provider": table.get("provider"),
                    "persisted_to": str(path),
                },
                at=self._clock(),
            )
        )
        return True
