"""Console Models & Routing wire flows (模型路由配置-UX §10.1/§10.2, D4).

The js-dom-free structure guards in ``test_console_ux_structure.py`` pin the
copy and the form skeleton; this file drives the REAL daemon app through
TestClient (embedded preset, synthetic embedder, stub driver so every probe
stays network-free) on a spare port and asserts the wire flows the console
wizard and ⑧ editor drive:

- D4 share: one passing probe of deep_reflection authorizes persisting the same
  provider+key to BOTH roles (the wizard's "also apply to short_increment").
- Save gate: a persist without a passing probe is rejected with 409, which the
  console maps to its save-gate error copy.
- Payload shape app.js reads defensively: roles[] with explicit-only
  base_url/api_key_env/provider + routes[]<role>.effective resolved defaults.
- OAuth availability with a throwaway user home reports absent logins (the
  wizard renders muted rows + disabled buttons; never a dead free-text field).
- The editor's save path (/api/v1/config/set) versioned round-trip.

Harness matches test_llm_admin_api.py: a temp config dir stands in for a fresh
MNEMOSEED_HOME, and the dogfood daemon (port 18888) is never touched — the app
runs in-process only.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from _identity_helpers import attach_token
from fastapi.testclient import TestClient

from mnemoseed.config import DEFAULT_LLM_ROUTES
from mnemoseed.daemon.app import create_app
from mnemoseed.llm.registry import LLM_DRIVERS, register
from mnemoseed.storage.drivers.lancedb_embedded import LanceDbEmbeddedStore
from mnemoseed.storage.drivers.sqlite_graph import SqliteGraphDriver
from mnemoseed.storage.drivers.sqlite_meta import SqliteMetaDriver
from mnemoseed.storage.drivers.synthetic_embedder import SyntheticEmbedder
from mnemoseed.storage.registry import (
    EMBED_DRIVERS,
    GRAPH_DRIVERS,
    META_DRIVERS,
    VECTOR_DRIVERS,
)
from mnemoseed.storage.registry import (
    register as register_storage,
)


@pytest.fixture(autouse=True)
def _ensure_real_drivers():
    """test_daemon clears the shared registries; re-register the real drivers."""
    for registry, cls in (
        (VECTOR_DRIVERS, LanceDbEmbeddedStore),
        (GRAPH_DRIVERS, SqliteGraphDriver),
        (META_DRIVERS, SqliteMetaDriver),
        (EMBED_DRIVERS, SyntheticEmbedder),
    ):
        if not registry.contains(cls.info.name):
            register_storage(registry)(cls)
    from mnemoseed.llm.drivers.anthropic import AnthropicLLM
    from mnemoseed.llm.drivers.oauth import OAuthLLM
    from mnemoseed.llm.drivers.ollama import OllamaLLM
    from mnemoseed.llm.drivers.openai_compatible import OpenAICompatibleLLM
    from mnemoseed.llm.drivers.stub import StubLLM

    for cls in (OpenAICompatibleLLM, AnthropicLLM, OllamaLLM, OAuthLLM, StubLLM):
        if not LLM_DRIVERS.contains(cls.info.name):
            register(LLM_DRIVERS)(cls)
    yield


def _config_toml(tmp_path: Path) -> Path:
    """Embedded config with both dream roles network-free (stub driver)."""
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        'preset = "embedded"\n'
        f'[storage.vector]\nuri = "{(tmp_path / "chunks.lance").as_posix()}"\ndimensions = 64\n'
        f'[storage.graph]\npath = "{(tmp_path / "cortex.db").as_posix()}"\n'
        f'[storage.meta]\npath = "{(tmp_path / "meta.db").as_posix()}"\n'
        f'[storage.embed]\ndriver = "synthetic"\ndimension = 64\n'
        "[dream.llm.deep_reflection]\n"
        'driver = "stub"\n'
        'model = "stub"\n'
        "[dream.llm.short_increment]\n"
        'driver = "stub"\n'
        'model = "stub"\n',
        encoding="utf-8",
    )
    return cfg


@contextmanager
def _client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    home: Path | None = None,
    cfg: Path | None = None,
) -> Iterator[TestClient]:
    """Boot the real daemon on a spare port with a throwaway config dir (the
    test twin of a fresh ``MNEMOSEED_HOME``); owner set up + token attached."""
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    home_dir = tmp_path / "home"
    monkeypatch.setenv("MNEMOSEED_HOME", str(home_dir))
    monkeypatch.setattr("mnemoseed.config.CONFIG_PATH", cfg if cfg is not None else _config_toml(tmp_path))
    monkeypatch.setattr("mnemoseed.dream.snapshot.CONFIG_DIR", tmp_path)
    if home is not None:
        monkeypatch.setenv("MNEMOSEED_USER_HOME", str(home))
    else:
        monkeypatch.delenv("MNEMOSEED_USER_HOME", raising=False)
    with TestClient(create_app(), client=("127.0.0.1", 50058)) as client:
        attach_token(client)
        yield client


def _roles(client: TestClient) -> dict[str, dict[str, object]]:
    body = client.get("/api/v1/llm/routes").json()
    return {entry["role"]: entry for entry in body["roles"]}


# ------------------------------------------------- custom-provider echo endpoint (JH dogfood regression)

#: The pasted key value JH walked the dogfood flow with (fictional here).
_CUSTOM_KEY = "sk-test-moonshot-kimi-9012"

_CUSTOM_MODEL = "moonshotai/Kimi-K3"


class _AuthCheckingHandler(BaseHTTPRequestHandler):
    """A throwaway OpenAI-compatible provider: GET /models answers 200 with the
    catalog ONLY when a Bearer key rides along — 401 otherwise, exactly the
    "wrong/missing key" rejection JH's modal.direct endpoint returned."""

    def log_message(self, *args):  # silence test output
        pass

    def _authorized(self) -> bool:
        return str(self.headers.get("Authorization", "")).startswith("Bearer ")

    def do_GET(self):
        if self.path.endswith("/models") and self._authorized():
            body = json.dumps(
                {"data": [{"id": _CUSTOM_MODEL}, {"id": "deepseek/deepseek-v4-flash"}]}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(401)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"error": "invalid api key"}).encode())

    def do_POST(self):
        if not self._authorized():
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "invalid api key"}).encode())
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode())


@contextmanager
def _echo_provider() -> Iterator[str]:
    """A live auth-checking endpoint (base_url) on an ephemeral port."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _AuthCheckingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def _custom_config_toml(tmp_path: Path) -> Path:
    """Embedded config whose deep_reflection role starts on openai_compatible
    (the editor's custom-provider card), network-free short_increment."""
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        'preset = "embedded"\n'
        f'[storage.vector]\nuri = "{(tmp_path / "chunks.lance").as_posix()}"\ndimensions = 64\n'
        f'[storage.graph]\npath = "{(tmp_path / "cortex.db").as_posix()}"\n'
        f'[storage.meta]\npath = "{(tmp_path / "meta.db").as_posix()}"\n'
        f'[storage.embed]\ndriver = "synthetic"\ndimension = 64\n'
        "[dream.llm.deep_reflection]\n"
        'driver = "openai_compatible"\n'
        'model = "kimi-k3"\n'
        'base_url = "http://127.0.0.1:9"\n'
        "[dream.llm.short_increment]\n"
        'driver = "stub"\n'
        'model = "stub"\n',
        encoding="utf-8",
    )
    return cfg


def test_custom_provider_paste_probe_save_end_to_end(tmp_path, monkeypatch) -> None:
    """JH dogfood regression, green: the ⑧ editor's custom-provider
    chain (paste key -> probe -> save) persists end to end — the key lands in
    the secret store, the probe authenticates through the pinned reference, the
    save writes every route field into the config mirror, and the routes
    payload reports the custom endpoint. No dead ``provider = "other"`` field
    is ever written."""
    monkeypatch.setenv("MNEMOSEED_SECRET_BACKEND", "file")  # pin the file backend
    cfg = _custom_config_toml(tmp_path)
    role = "deep_reflection"
    ref = f"secrets:mnemoseed/dream/{role}"
    with _echo_provider() as base_url, _client(tmp_path, monkeypatch, cfg=cfg) as client:
        # 1) llmKeyPaste -> POST /api/v1/llm/key {role, key} (no provider id)
        response = client.post("/api/v1/llm/key", json={"role": role, "key": _CUSTOM_KEY})
        assert response.status_code == 200, response.text
        assert response.json()["ok"] is True
        assert response.json()["masked_tail"] == "9012"
        # the secret FILE materializes under the config home's secrets/ dir
        secret_file = tmp_path / "secrets" / "mnemoseed.dream.deep_reflection.key"
        assert secret_file.read_text(encoding="utf-8") == _CUSTOM_KEY

        # 2) the probe carries the reference the SPA pins into the key field ->
        #    the pasted key resolves through the SecretStore and authenticates
        probe = client.post(
            "/api/v1/llm/test",
            json={
                "role": role,
                "driver": "openai_compatible",
                "model": _CUSTOM_MODEL,
                "base_url": base_url,
                "api_key_env": ref,
            },
        )
        assert probe.status_code == 200, probe.text
        body = probe.json()
        assert body["ok"] is True, body["detail"]
        assert _CUSTOM_MODEL in body["detail"]["models"]

        # 3) saveRoute -> one /api/v1/config/set per route field
        sets = [
            {"key_path": f"dream.llm.{role}.driver", "value": "openai_compatible"},
            {"key_path": f"dream.llm.{role}.model", "value": _CUSTOM_MODEL},
            {"key_path": f"dream.llm.{role}.base_url", "value": base_url},
            {"key_path": f"dream.llm.{role}.max_tokens", "value": 2048},
        ]
        for set_body in sets:
            response = client.post("/api/v1/config/set", json=set_body)
            assert response.status_code == 200, response.text

        # 4) the config mirror holds every route field — and the pinned ref is
        #    never cleared by the save (api_key_env stays the secrets reference)
        table = cfg.read_text(encoding="utf-8").split(f"[dream.llm.{role}]", 1)[1].split("[", 1)[0]
        for line in (
            'driver = "openai_compatible"',
            f'model = "{_CUSTOM_MODEL}"',
            f'base_url = "{base_url}"',
            "max_tokens = 2048",
            f'api_key_env = "{ref}"',
        ):
            assert line in table, f"missing {line!r} in the config mirror"
        assert "provider" not in table  # no dead field

        # 5) the routes payload reports the custom endpoint + the reference
        role_row = _roles(client)[role]
        assert role_row["base_url"] == base_url
        assert role_row["api_key_env"] == ref
        assert role_row["explicit"] is True
        effective = client.get("/api/v1/llm/routes").json()["routes"][role]["effective"]
        assert effective["base_url"] == base_url
        assert effective["api_key_env"] == ref


def test_custom_provider_probe_without_key_is_a_typed_authenticated_failure(tmp_path, monkeypatch) -> None:
    """JH: before any key is wired, the custom-endpoint probe fails against the
    provider (401) and that failure is a typed result — never a silent success
    and never a silent no-op — so the console renders the failure text."""
    with _echo_provider() as base_url, _client(tmp_path, monkeypatch) as client:
        probe = client.post(
            "/api/v1/llm/test",
            json={
                "role": "deep_reflection",
                "driver": "openai_compatible",
                "model": _CUSTOM_MODEL,
                "base_url": base_url,
                "api_key_env": "",  # the unauthenticated SPA payload before a paste
            },
        )
        assert probe.status_code == 200
        body = probe.json()
        assert body["ok"] is False
        assert "401" in str(body["detail"])


# ---------------------------------------------------------------- wizard D4 share


def test_wizard_share_probe_then_save_writes_both_roles(tmp_path, monkeypatch) -> None:
    """D4: the wizard probes deep_reflection once, then (share checked) persists
    the same provider + key to both roles; the route payload then reports both."""
    with _client(tmp_path, monkeypatch) as client:
        payload = {
            "driver": "stub",
            "model": "accounts/fireworks/models/kimi-k3",
            "provider": "fireworks",
            "base_url": "https://api.fireworks.ai/inference/v1",
            "api_key_env": "FIREWORKS_API_KEY",
        }
        probe = client.post("/api/v1/llm/test", json={"role": "deep_reflection", **payload})
        assert probe.status_code == 200
        assert probe.json()["ok"] is True

        for role in ("deep_reflection", "short_increment"):
            response = client.post(f"/api/v1/llm/routes/{role}", json=payload)
            assert response.status_code == 200, response.text
            assert response.json()["api_key_env"] == "FIREWORKS_API_KEY"

        roles = _roles(client)
        for role in ("deep_reflection", "short_increment"):
            assert roles[role]["driver"] == "stub"
            assert roles[role]["model"] == "accounts/fireworks/models/kimi-k3"
            assert roles[role]["provider"] == "fireworks"
            assert roles[role]["base_url"] == "https://api.fireworks.ai/inference/v1"
            assert roles[role]["api_key_env"] == "FIREWORKS_API_KEY"
            assert roles[role]["explicit"] is True


def test_wizard_save_without_a_passing_probe_is_rejected(tmp_path, monkeypatch) -> None:
    """§3.3: the save stays armed only after a passing probe; a raw persist is
    a 409 the console maps to its save-gate copy, never a silent write."""
    with _client(tmp_path, monkeypatch) as client:
        response = client.post(
            "/api/v1/llm/routes/deep_reflection",
            json={"driver": "stub", "model": "m"},
        )
        assert response.status_code == 409
        assert "connectivity test" in response.json()["detail"]


# ------------------------------------------------- payload app.js reads (⑧ editor)


def test_routes_payload_supplies_explicit_and_effective_values(tmp_path, monkeypatch) -> None:
    """§2.2/§10.1: the ⑧ editor reads explicit-only fields plus the resolved
    ``effective`` defaults chain (base_url / api_key_env fallbacks) that the
    payload must carry for the effective-value cards to render."""
    with _client(tmp_path, monkeypatch) as client:
        body = client.get("/api/v1/llm/routes").json()
        assert set(body) == {"roles", "routes", "drivers", "checked_at"}
        routes = body["routes"]
        assert set(routes) == {"deep_reflection", "short_increment"}
        for role in ("deep_reflection", "short_increment"):
            effective = routes[role]["effective"]
            assert set(effective) >= {"driver", "model", "base_url", "api_key_env", "provider"}
            assert effective["driver"] == "stub"
            assert effective["model"] == "stub"
            # the defaults chain resolves the Fireworks defaults (config.py) —
            # the same values the console editor pre-fills for a "defaults" role
            assert effective["base_url"] == DEFAULT_LLM_ROUTES[role].params["base_url"]
            assert effective["api_key_env"] == DEFAULT_LLM_ROUTES[role].params["api_key_env"]
            # explicit-only: nothing is pinned in this throwaway config
            assert routes[role]["base_url"] is None
            assert routes[role]["api_key_env"] is None
            assert routes[role]["provider"] is None


def test_role_card_uses_driver_field_for_offline_derivation(tmp_path, monkeypatch) -> None:
    """§9: the derived "fully offline" badge app.js computes is driven by the
    roles' driver field (resolved, so a defaults-only role still reports it)."""
    with _client(tmp_path, monkeypatch) as client:
        roles = _roles(client)
        assert roles["deep_reflection"]["driver"] == "stub"
        # the drivers catalog app.js renders must never list the provider cards'
        # ids as selectable user options beyond the five provider cards
        body = client.get("/api/v1/llm/routes").json()
        names = {entry["name"] for entry in body["drivers"]}
        assert {"stub", "oauth", "ollama", "anthropic", "openai_compatible"} <= names


# ---------------------------------------------------------------- oauth availability


def test_oauth_availability_absent_home_reports_muted_rows(tmp_path, monkeypatch) -> None:
    """§6.2: with no host logins the availability payload reports present=false
    per provider — the wizard renders a muted row with a disabled button, never
    a free-text provider input."""
    with _client(tmp_path, monkeypatch, home=tmp_path / "empty-home") as client:
        body = client.get("/api/v1/llm/oauth-availability").json()
        providers = {entry["provider"]: entry for entry in body["providers"]}
        assert set(providers) == {"codex", "grok"}
        for entry in providers.values():
            assert entry["present"] is False
            assert "token" not in body  # never a value over the wire


# ------------------------------------------------- ⑧ editor save path (configwrite)


def test_editor_config_set_save_path_roundtrip(tmp_path, monkeypatch) -> None:
    """§10.1: the editor's save writes one key at a time through the versioned
    config service; the routes payload reflects the change on the next read."""
    with _client(tmp_path, monkeypatch) as client:
        sets = [
            {"key_path": "dream.llm.deep_reflection.driver", "value": "stub"},
            {"key_path": "dream.llm.deep_reflection.model", "value": "kimi-k3"},
            {
                "key_path": "dream.llm.deep_reflection.base_url",
                "value": "https://api.fireworks.ai/inference/v1",
            },
            {"key_path": "dream.llm.deep_reflection.api_key_env", "value": "FIREWORKS_API_KEY"},
            {"key_path": "dream.llm.deep_reflection.provider", "value": "fireworks"},
        ]
        versions: list[int] = []
        for set_body in sets:
            response = client.post("/api/v1/config/set", json=set_body)
            assert response.status_code == 200, response.text
            version_id = int(response.json()["version_id"])
            assert version_id > 0
            versions.append(version_id)
        # version ids are per-key-slot, not a global sequence; the last write's
        # id is what the editor's saved banner reports
        assert versions[-1] > 0

        role = _roles(client)["deep_reflection"]
        assert role["driver"] == "stub"
        assert role["model"] == "kimi-k3"
        assert role["base_url"] == "https://api.fireworks.ai/inference/v1"
        assert role["api_key_env"] == "FIREWORKS_API_KEY"
        assert role["provider"] == "fireworks"
        assert role["explicit"] is True
