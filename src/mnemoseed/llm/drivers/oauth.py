"""OAuth driver: reuse host CLI login state (Codex / Grok) (PRD-02 T6; FR-2.14).

This driver is the OAuth leg of the model-routing story: it reads the host
CLI's local login state instead of a MnemoSeed-stored API key, and refreshes
the access token itself via the provider's OIDC refresh-token grant. ``chat()``
reads the auth file on each call and refreshes at most once per call under a
per-instance lock; any missing/malformed auth file, failed refresh, or transport
failure degrades to the typed ``LLMUnavailable`` (FR-2.6) — never a traceback.
``check()`` (the console 实测 button, design/07 section 8) never raises.

Providers:
  - codex — ``~/.codex/auth.json`` ``tokens.{access_token, refresh_token,
    account_id}``; refresh is the OIDC refresh_token grant against the public
    Codex CLI client id.
  - grok — ``~/.grok/auth.json`` keyed by issuer URL; each account entry carries
    ``key`` / ``refresh_token`` / ``expires_at`` / ``oidc_issuer`` /
    ``oidc_client_id`` and refresh is the same grant against the issuer.

Anthropic subscription OAuth is intentionally unsupported (their terms; the
``anthropic`` driver stays API-key-only) — any provider other than codex/grok
raises ``OAuthNotImplemented``, the typed "no implementation" branch that was
once this driver's whole body.

The user home defaults to the OS home and is overridable through the
``MNEMOSEED_USER_HOME`` env seam (same seam the installer uses) or an explicit
``home`` parameter. Cache write-back is atomic (tmp file + ``os.replace``) and
leaves any sibling ``*.lock`` file untouched. No token value is ever logged or
emitted in an exception message.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from mnemoseed.llm.registry import LLM_DRIVERS, register
from mnemoseed.llm.types import (
    ChatResult,
    HealthReport,
    LLMDriverInfo,
    LLMUnavailable,
    OAuthNotImplemented,
    Usage,
)

USER_HOME_ENV = "MNEMOSEED_USER_HOME"

# Well-known public Codex CLI OIDC client (refresh_token grant, no secret).
CODEX_CLIENT_ID = "app_EMoamEEZ73f0cCkXaXp7hrann"
CODEX_TOKEN_URL = "https://auth.openai.com/oauth/token"
CODEX_API_BASE = "https://chatgpt.com"
CODEX_CHAT_PATH = "/backend-api/codex/responses"

GROK_API_BASE = "https://api.x.ai/v1"
GROK_CHAT_PATH = "/responses"
GROK_DEFAULT_ISSUER = "https://auth.x.ai"
# Literal token-endpoint fallback when the issuer's OIDC discovery document is
# unreachable (the x.ai issuer advertises ``/oauth2/token``, not ``/oauth/token``).
GROK_DEFAULT_TOKEN_PATH = "/oauth2/token"
GROK_DISCOVERY_PATH = "/.well-known/openid-configuration"

SUPPORTED_PROVIDERS = ("codex", "grok")

# Fallback access-token lifetime for a codex record that only carries
# ``last_refresh`` (no explicit ``expires_at``).
DEFAULT_TOKEN_TTL = 1800.0


def _resolve_home() -> Path:
    raw = os.environ.get(USER_HOME_ENV)
    return Path(raw).expanduser() if raw else Path.home()


def _replace(tmp: Path, target: Path) -> None:
    os.replace(tmp, target)


@register(LLM_DRIVERS)
class OAuthLLM:
    """Chat through a provider whose OAuth login state lives on this host."""

    info = LLMDriverInfo(
        name="oauth",
        description="reuse host OAuth login state (Codex / Grok) with OIDC auto-refresh",
    )

    def __init__(
        self,
        *,
        provider: str = "",
        model: str = "",
        base_url: str = "",
        token_url: str = "",
        timeout: float = 30.0,
        token_ttl: float = DEFAULT_TOKEN_TTL,
        home: str | Path | None = None,
        clock: Callable[[], float] | None = None,
        **kwargs: Any,
    ) -> None:
        self.provider = provider
        self.model = model
        self.timeout = float(timeout)
        self.token_ttl = float(token_ttl)
        self.home = Path(home) if home is not None else _resolve_home()
        self.base_url = base_url.rstrip("/")
        self.token_url = token_url.rstrip("/")
        self.clock = clock if clock is not None else time.time
        self.params: dict[str, Any] = kwargs
        self._lock = threading.Lock()
        # issuer URL -> advertised token_endpoint, discovered lazily and cached
        self._discovered_token_endpoints: dict[str, str] = {}
        self._client = httpx.Client(base_url=self._api_base(), timeout=self.timeout)
        self._token_client = httpx.Client(timeout=self.timeout)

    def _api_base(self) -> str:
        if self.provider == "grok":
            return self.base_url or GROK_API_BASE
        return self.base_url or CODEX_API_BASE

    def _api_path(self) -> str:
        return GROK_CHAT_PATH if self.provider == "grok" else CODEX_CHAT_PATH

    def _ensure_provider(self) -> None:
        if self.provider not in SUPPORTED_PROVIDERS:
            raise OAuthNotImplemented(
                f"oauth provider {self.provider!r} is not implemented (built-in: codex, grok)"
            )

    def _auth_path(self) -> Path:
        if self.provider == "grok":
            return self.home / ".grok" / "auth.json"
        return self.home / ".codex" / "auth.json"

    @staticmethod
    def _iso(value: float) -> str:
        return datetime.fromtimestamp(value, tz=UTC).isoformat()

    @staticmethod
    def _parse_iso(value: Any) -> float | None:
        """Parse an ISO stamp to a UTC epoch, or ``None`` if it is unusable.

        Naive (timezone-less) stamps are read as UTC rather than host-local time,
        and any stamp the platform clock cannot represent (e.g. out-of-range dates
        raising ``OSError`` in ``mktime`` on Windows) degrades to ``None`` — the
        driver must never leak a traceback through ``chat()`` / ``check()``
        (FR-2.6).
        """
        if not isinstance(value, str) or not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed.timestamp()
        except (ValueError, OSError):
            return None

    def chat(self, *, system: str, user: str) -> ChatResult:
        self._ensure_provider()
        token, account_id = self._access_token()
        payload = {
            "model": self.model,
            "instructions": system,
            "input": [{"role": "user", "content": user}],
        }
        try:
            response = self._client.post(
                self._api_path(), json=payload, headers=self._headers(token, account_id)
            )
            response.raise_for_status()
            body = response.json()
        except httpx.HTTPStatusError as exc:
            raise LLMUnavailable(
                f"oauth {self.provider} chat failed: HTTP {exc.response.status_code}"
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise LLMUnavailable(f"oauth {self.provider} chat failed: {exc}") from exc
        return ChatResult(
            text=_responses_text(body), usage=_usage_from(body), model=self.model, driver="oauth"
        )

    def check(self) -> HealthReport:
        try:
            self._ensure_provider()
            token, account_id = self._access_token()
        except LLMUnavailable as exc:
            return HealthReport(ok=False, detail={"status": "not_configured", "error": str(exc)})
        try:
            response = self._client.post(
                self._api_path(),
                json={"model": self.model, "instructions": "ping", "input": "ping"},
                headers=self._headers(token, account_id),
            )
        except (httpx.HTTPError, ValueError) as exc:
            return HealthReport(ok=False, detail={"status": "unreachable", "error": str(exc)})
        if response.status_code != 200:
            return HealthReport(
                ok=False, detail={"status": "provider", "error": f"HTTP {response.status_code}"}
            )
        return HealthReport(ok=True, detail={"provider": self.provider, "model": self.model})

    def _headers(self, token: str, account_id: str) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {token}"}
        if self.provider == "codex" and account_id:
            headers["chatgpt-account-id"] = account_id
        return headers

    def _access_token(self) -> tuple[str, str]:
        """Resolve a usable access token (and codex account id), refreshing once if expired.

        Thread-safe: concurrent callers serialize on ``self._lock`` and
        double-check expiry after acquiring it, so at most one refresh fires.
        """
        auth = self._read_auth()
        if not self._expired(auth):
            return self._token_and_account(auth)
        with self._lock:
            auth = self._read_auth()
            if not self._expired(auth):
                return self._token_and_account(auth)
            self._refresh(auth)
            return self._token_and_account(self._read_auth())

    def _read_auth(self) -> dict[str, Any]:
        path = self._auth_path()
        if not path.exists():
            raise LLMUnavailable(f"oauth {self.provider} auth file not found: {path}")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise LLMUnavailable(f"oauth {self.provider} auth file unreadable: {path}: {exc}") from exc
        if not isinstance(data, dict):
            raise LLMUnavailable(f"oauth {self.provider} auth file is not a JSON object: {path}")
        return data

    def _expired(self, auth: dict[str, Any]) -> bool:
        expiry = self._parsed_expiry(auth)
        if expiry is None:
            return True  # no usable expiry: attempt one refresh (or degrade)
        return self.clock() >= expiry

    def _parsed_expiry(self, auth: dict[str, Any]) -> float | None:
        if self.provider == "grok":
            _, entry = self._grok_entry(auth)
            return self._parse_iso(entry.get("expires_at"))
        tokens = auth.get("tokens")
        if isinstance(tokens, dict):
            explicit = self._parse_iso(tokens.get("expires_at"))
            if explicit is not None:
                return explicit
        explicit = self._parse_iso(auth.get("expires_at"))
        if explicit is not None:
            return explicit
        last = self._parse_iso(auth.get("last_refresh"))
        if last is not None:
            return last + self.token_ttl
        return None

    def _token_and_account(self, auth: dict[str, Any]) -> tuple[str, str]:
        if self.provider == "grok":
            _, entry = self._grok_entry(auth)
            key = entry.get("key")
            if not isinstance(key, str) or not key:
                raise LLMUnavailable(f"oauth grok auth file has no access key: {self._auth_path()}")
            return key, ""
        tokens = auth.get("tokens")
        if not isinstance(tokens, dict):
            raise LLMUnavailable(f"oauth codex auth file has no tokens record: {self._auth_path()}")
        token = tokens.get("access_token")
        account_id = tokens.get("account_id")
        if not isinstance(token, str) or not token:
            raise LLMUnavailable(f"oauth codex auth file has no access_token: {self._auth_path()}")
        if not isinstance(account_id, str) or not account_id:
            raise LLMUnavailable(f"oauth codex auth file has no account_id: {self._auth_path()}")
        return token, account_id

    def _grok_entry(self, auth: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        candidates: dict[str, dict[str, Any]] = {}
        for key, value in auth.items():
            if isinstance(value, dict) and value.get("key") and value.get("refresh_token"):
                candidates[key] = value
        if not candidates:
            raise LLMUnavailable(
                f"oauth grok auth file has no account entry with key and refresh_token: {self._auth_path()}"
            )
        preferred = [
            key
            for key, value in candidates.items()
            if (value.get("oidc_issuer") or "").rstrip("/") == GROK_DEFAULT_ISSUER.rstrip("/")
        ]
        if preferred:
            key = preferred[0]
        else:
            key = next(iter(candidates))
        return key, candidates[key]

    def _grok_token_url(self, entry: dict[str, Any]) -> str:
        """Resolve the Grok token endpoint for this account entry.

        Prefers the issuer's OIDC discovery document (advertised
        ``token_endpoint``, cached per issuer) so a provider endpoint change does
        not require a release; falls back to the literal ``/oauth2/token`` suffix
        (the x.ai issuer advertises ``/oauth2/token``, not ``/oauth/token``) when
        discovery is unreachable or inconsistent.
        """
        issuer = (entry.get("oidc_issuer") or GROK_DEFAULT_ISSUER).rstrip("/")
        cached = self._discovered_token_endpoints.get(issuer)
        if cached:
            return cached
        endpoint = self._discover_token_endpoint(issuer)
        if endpoint:
            self._discovered_token_endpoints[issuer] = endpoint
            return endpoint
        return issuer + GROK_DEFAULT_TOKEN_PATH

    def _discover_token_endpoint(self, issuer: str) -> str | None:
        """Read ``token_endpoint`` from the issuer's OIDC discovery document."""
        try:
            response = self._token_client.get(issuer + GROK_DISCOVERY_PATH)
            response.raise_for_status()
            meta = response.json()
        except (httpx.HTTPError, ValueError):
            return None
        endpoint = meta.get("token_endpoint") if isinstance(meta, dict) else None
        if not isinstance(endpoint, str) or not endpoint:
            return None
        return endpoint.rstrip("/")

    def _refresh(self, auth: dict[str, Any]) -> None:
        if self.provider == "grok":
            key, entry = self._grok_entry(auth)
            refresh_token = entry.get("refresh_token")
            if not isinstance(refresh_token, str):
                raise LLMUnavailable(f"oauth grok auth file has no refresh_token: {self._auth_path()}")
            token_url = self.token_url or self._grok_token_url(entry)
            client_id = entry.get("oidc_client_id") or ""
        else:
            key = ""
            tokens = auth.get("tokens")
            if not isinstance(tokens, dict) or not tokens.get("refresh_token"):
                raise LLMUnavailable(f"oauth codex auth file has no refresh_token: {self._auth_path()}")
            refresh_token = tokens["refresh_token"]
            if not isinstance(refresh_token, str):
                raise LLMUnavailable(f"oauth codex auth file has no refresh_token: {self._auth_path()}")
            token_url = self.token_url or CODEX_TOKEN_URL
            client_id = CODEX_CLIENT_ID
        body = self._token_grant(token_url, refresh_token, client_id)
        self._write_back(auth, body, key=key)

    def _token_grant(self, token_url: str, refresh_token: str, client_id: str) -> dict[str, Any]:
        form = {"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": client_id}
        try:
            response = self._token_client.post(token_url, data=form)
            response.raise_for_status()
            body = response.json()
        except httpx.HTTPStatusError as exc:
            raise LLMUnavailable(
                f"oauth {self.provider} token refresh failed: HTTP {exc.response.status_code}"
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise LLMUnavailable(f"oauth {self.provider} token refresh failed: {exc}") from exc
        if not isinstance(body, dict):
            raise LLMUnavailable(f"oauth {self.provider} token refresh returned no access_token")
        access = body.get("access_token")
        if not isinstance(access, str) or not access:
            raise LLMUnavailable(f"oauth {self.provider} token refresh returned no access_token")
        return body

    def _write_back(self, auth: dict[str, Any], body: dict[str, Any], *, key: str) -> None:
        expires_in = body.get("expires_in")
        expires_in = (
            float(expires_in) if isinstance(expires_in, (int, float)) and expires_in else self.token_ttl
        )
        now = self.clock()
        expires_at = self._iso(now + expires_in)
        if self.provider == "grok":
            entry = auth[key]
            entry = {
                **entry,
                "key": body["access_token"],
                "refresh_token": body.get("refresh_token") or entry.get("refresh_token", ""),
                "expires_at": expires_at,
            }
            data = {**auth, key: entry}
        else:
            tokens = auth.get("tokens")
            if not isinstance(tokens, dict):
                raise LLMUnavailable(f"oauth codex auth file has no tokens record: {self._auth_path()}")
            tokens = {
                **tokens,
                "access_token": body["access_token"],
                "refresh_token": body.get("refresh_token") or tokens.get("refresh_token", ""),
                "expires_at": expires_at,
            }
            data = {**auth, "tokens": tokens, "last_refresh": self._iso(now)}
        self._write_atomic(data)

    def _write_atomic(self, data: dict[str, Any]) -> None:
        """Atomic auth-file write: tmp + rename in the same directory.

        The rename only touches the target path, so any sibling ``*.lock`` file
        is left exactly as the provider CLI left it.
        """
        path = self._auth_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.parent / f"{path.name}.tmp"
            tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            _replace(tmp, path)
        except OSError as exc:
            # Deferring persistence is a refresh failure (FR-2.6): typed, never a traceback.
            raise LLMUnavailable(f"oauth {self.provider} failed to persist refreshed tokens: {exc}") from exc


def _responses_text(body: Any) -> str:
    """First response text from a Responses-API-shaped body (Codex + Grok)."""
    parts: list[str] = []
    for item in body.get("output") or []:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    text = block.get("text")
                    if isinstance(text, str):
                        parts.append(text)
        elif isinstance(content, str):
            parts.append(content)
    text = body.get("output_text")
    if isinstance(text, str):
        parts.append(text)
    return "".join(parts)


def _usage_from(body: Any) -> Usage | None:
    usage = body.get("usage")
    if not isinstance(usage, dict):
        return None
    return Usage(
        prompt_tokens=_to_int(usage.get("input_tokens")),
        completion_tokens=_to_int(usage.get("output_tokens")),
    )


def _to_int(value: Any) -> int | None:
    return int(value) if isinstance(value, (int, float)) else None
