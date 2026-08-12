"""LLM admin surface (issue #23; FR-6.9 wizard + design/07 section 8).

The shared validation + persistence layer the console Models & Routing page,
the first-run dream-model step, and ``mnemoseed llm`` all funnel through:

- ``routes()`` reports each dream role's route (driver / model / endpoint /
  env-var NAME) plus a cached live connectivity probe and the built-in driver
  catalog. A literal key value anywhere in the payload is a hard failure.
- ``oauth_availability()`` detects host Codex / Grok OAuth login state
  (presence + expiry) without ever reading a token value out of the files.
- ``set_role()`` validates (unknown role / driver are typed errors), persists a
  line-oriented TOML patch (comments and unrelated keys survive), and audits
  the env-var NAME -- never the value.
- ``test_config()`` runs a proposed route's ``check()`` against a live endpoint
  so the console test buttons probe BEFORE anything is written (FR-6.9).

Credential hygiene follows the role router's rule (FR-2.14): config stores
env-var NAMES, this surface attests them, and the runtime resolves values from
the process environment only.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol, cast

from mnemoseed.config import LLM_ROLES, Config
from mnemoseed.llm.drivers.oauth import SUPPORTED_PROVIDERS, OAuthLLM
from mnemoseed.llm.registry import LLM_DRIVERS, LLMRegistry
from mnemoseed.llm.routing import RoleRouter
from mnemoseed.llm.types import HealthReport, LLMError, LLMUnavailable
from mnemoseed.storage.ports import AuditEntry

#: How long a per-role connectivity probe stays cached before re-running the
#: driver's checks (the console page polls; the probes must not hammer a host).
_CONNECTIVITY_TTL = 30.0


class LLMAdminError(ValueError):
    """Typed validation failure on the LLM admin surface (mapped to 422)."""


class _AuditSink(Protocol):
    """The minimal MetaStore surface the admin service audits through."""

    def audit_append(self, entry: AuditEntry) -> None: ...


def _line_key(line: str) -> str | None:
    """The TOML key of a key=value line, or None for headers/comments/blanks."""
    stripped = line.strip()
    if not stripped or stripped.startswith("[") or stripped.startswith("#") or "=" not in stripped:
        return None
    return stripped.split("=", 1)[0].strip().strip('"').strip("'")


def _table_spans(lines: list[str]) -> dict[str, tuple[int, int]]:
    """Header name -> (start line index, end line index) for every TOML table.

    ``end`` is the index of the next header (exclusive), so the body of table
    ``name`` is ``lines[start + 1 : end]``; the last table runs to EOF.
    """
    spans: dict[str, tuple[int, int]] = {}
    current: str | None = None
    start: int = 0
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if current is not None:
                spans[current] = (start, index)
            current = stripped[1:-1].strip()
            start = index
    if current is not None:
        spans[current] = (start, len(lines))
    return spans


def _toml_str(value: Any) -> str:
    """Encode a scalar as a TOML literal (double-quoted strings, JSON booleans)."""
    return json.dumps(value)


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
    ) -> None:
        self._config = config
        self._meta = meta
        self._registry = registry if registry is not None else LLM_DRIVERS
        self._clock = clock if clock is not None else time.time
        self._home = Path(home).expanduser() if home is not None else None
        self._env = env if env is not None else os.environ.get
        self._connectivity_cache: dict[str, tuple[float, dict[str, Any]]] = {}

    # ---------------------------------------------------------------- read

    def routes(self) -> dict[str, Any]:
        """Per-role route + connectivity + the driver catalog, for the wire."""
        roles: list[dict[str, Any]] = []
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
        return {
            "roles": roles,
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
    ) -> dict[str, Any]:
        """Validate and persist one role-route change; network-free.

        ``None`` keeps the current value; ``""`` clears an optional param
        (driver/model cannot be cleared). The exact written keys land back in
        the config TOML (surgical patch) and the ENV-VAR NAME -- never a
        value -- is audit-logged.
        """
        if role not in LLM_ROLES:
            raise LLMAdminError(f"unknown llm role {role!r} (choose from: {', '.join(LLM_ROLES)})")
        current = self._config.llm[role]

        # Resolve the new effective values (defaults preserved unless replaced).
        new_driver = current.driver if driver is None else driver.strip()
        new_model = current.model if model is None else model.strip()
        if driver is not None and not new_driver:
            raise LLMAdminError("driver must be a non-empty string")
        if model is not None and not new_model:
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

        # The explicit file table: keep every previously written key, update the
        # ones this call owns, and drop the ones this call clears.
        table = dict(self._explicit_table(role))
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
            if not field_value.strip():
                table.pop(name, None)
            else:
                table[name] = field_value.strip()

        path = self._persist_llm_role(role, table)

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
        from mnemoseed.config import RoleLLMConfig

        self._config.llm[role] = RoleLLMConfig(
            role=role, driver=new_driver, model=new_model, params=merged_params
        )
        self._connectivity_cache.pop(role, None)

        audited = self._audit_role_set(role, table, path)
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

    # ---------------------------------------------------------------- probe

    def test_config(
        self,
        *,
        role: str,
        driver: str,
        model: str,
        base_url: str = "",
        api_key_env: str | None = None,
        provider: str | None = None,
    ) -> HealthReport:
        """Run a proposed route's connectivity check against a live endpoint.

        Never raises (returns a failed HealthReport instead) so the console test
        buttons always render a typed inline result.
        """
        if role not in LLM_ROLES:
            return HealthReport(ok=False, detail={"error": f"unknown llm role {role!r}"})
        if not self._registry.contains(driver):
            return HealthReport(ok=False, detail={"error": f"unknown llm driver {driver!r}"})
        params: dict[str, Any] = {}
        for name, value in (("base_url", base_url), ("provider", provider or "")):
            if value:
                params[name] = value
        # A proposed KEY is referenced by env-var NAME; resolve it against the
        # process environment exactly like the role router does (never a value
        # over the wire).
        api_key = ""
        if api_key_env:
            for name in (entry.strip() for entry in api_key_env.split(",")):
                env_value = self._env(name) if name else None
                if env_value:
                    api_key = env_value
                    break
        params["model"] = model
        params["api_key"] = api_key
        try:
            instance = self._registry.build(driver, params)
            return cast(HealthReport, instance.check())
        except LLMError as exc:
            return HealthReport(ok=False, detail={"error": str(exc)})

    # ---------------------------------------------------------------- plumbing

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

    def _persist_llm_role(self, role: str, table: dict[str, Any]) -> Path:
        """Write the role's table back into the config TOML (line-oriented patch).

        Comments and unrelated keys survive untouched; the role's keys are
        rewritten in place or a new ``[dream.llm.<role>]`` table is inserted
        after the last dream table (so a new header never redefines an implicit
        parent that ``[dream.*]`` siblings already created).
        """
        source = self._config.source
        path = source if source is not None else Path.home() / ".mnemoseed" / "config.toml"
        original = path.read_text(encoding="utf-8") if path.exists() else ""
        lines = original.split("\n")
        role_header = f"[dream.llm.{role}]"
        role_name = f"dream.llm.{role}"  # span keys are the bare dotted names
        written = set(table)
        new_lines = [f"{key} = {_toml_str(table[key])}" for key in _ordered_keys(table) if key in table]

        spans = _table_spans(lines)
        if role_name in spans:
            start, end = spans[role_name]
            # A key being cleared this write is also "written" (removed): its old
            # line must be dropped, not kept. Every old key we leave untouched is
            # carried over into the new table by set_role, so anything here that
            # is absent from the new table is a deliberate clear.
            for line in lines[start + 1 : end]:
                key = _line_key(line)
                if key is not None and key not in table:
                    written.add(key)
            kept = [line for line in lines[start + 1 : end] if _line_key(line) not in written]
            out = lines[: start + 1]
            out.extend(new_lines)
            out.extend(kept)
            out.extend(lines[end:])
        else:
            dream_ends = [
                finish for name, (_, finish) in spans.items() if name == "dream" or name.startswith("dream.")
            ]
            insert_at = max(dream_ends) if dream_ends else len(lines)
            block = [role_header, *new_lines]
            out = lines[:insert_at]
            out.extend(block)
            out.extend(lines[insert_at:])

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(out).strip("\n") + "\n", encoding="utf-8")
        # Keep the in-memory raw mirror consistent for subsequent reads.
        self._config.raw.setdefault("dream", {}).setdefault("llm", {})[role] = dict(table)
        return path

    def _connectivity(self, role: str) -> dict[str, Any]:
        now = self._clock()
        cached = self._connectivity_cache.get(role)
        if cached is not None and now - cached[0] < _CONNECTIVITY_TTL:
            return cached[1]
        report = RoleRouter(routes=dict(self._config.llm), audit=None, clock=self._clock).check(role)
        payload: dict[str, Any] = {
            "ok": report.ok,
            "detail": report.detail,
            "checked_at": now,
        }
        self._connectivity_cache[role] = (now, payload)
        return payload

    def _audit_role_set(self, role: str, table: dict[str, Any], path: Path) -> bool:
        if self._meta is None:
            return False
        self._meta.audit_append(
            AuditEntry(
                actor="console",
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


def _ordered_keys(table: dict[str, Any]) -> list[str]:
    """Deterministic key order: the canonical route fields first, then any
    extra keys in the table's own order (converter params etc. stay put)."""
    head = [key for key in ("driver", "model", "base_url", "api_key_env", "provider") if key in table]
    tail = [key for key in table if key not in head]
    return head + tail
