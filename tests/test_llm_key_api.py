"""REST key custody endpoints (T2-3): ``POST /api/v1/llm/key`` and
``DELETE /api/v1/llm/key``.

Drives the REAL daemon app through TestClient (embedded preset, synthetic
embedder) on a spare port — the dogfood daemon is never touched:

- POST writes the secret into ``<CONFIG_DIR>/secrets/`` and persists the
  ``secrets:mnemoseed/dream/<role>`` reference through ConfigWriteService
  (versioned + audited), and answers ``{ok, masked_tail, restart_required}``.
- the secret VALUE never appears in the response, the audit log, the config
  file, or any read surface — only the reference does.
- role validation: unknown role / local_track are 422 with the deprecation
  wording; an empty key is 422.
- the endpoints are loopback-only, same gate as config writes (403 remote).
- DELETE removes the secret and the reference; the role falls back to the
  env-var chain (default or explicit).
- hot-apply (F2): the configwrite generation bump rebuilds the role on the
  NEXT resolve — a key set via REST is effective on the next dream run with no
  restart (proven through the daemon's own RoleRouter).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
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
from mnemoseed.storage.ports import AuditFilter, Page
from mnemoseed.storage.registry import (
    EMBED_DRIVERS,
    GRAPH_DRIVERS,
    META_DRIVERS,
    VECTOR_DRIVERS,
)
from mnemoseed.storage.registry import (
    register as register_storage,
)

_SECRET = "sk-ultra-secret-wire-value-9012"
_REF_DEEP = "secrets:mnemoseed/dream/deep_reflection"


@pytest.fixture(autouse=True)
def _force_file_secret_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """File-path assertions here require the file backend in head — force it
    (keychain-capable machines would otherwise chain to the OS keyring)."""
    monkeypatch.setenv("MNEMOSEED_SECRET_BACKEND", "file")


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


def _config_toml(tmp_path: Path, *, baseurl: str = "http://localhost:7788") -> Path:
    """Embedded config; deep_reflection uses the openai_compatible driver so
    the router exposes ``.api_key`` for the hot-apply assertions."""
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        'preset = "embedded"\n'
        f'baseurl = "{baseurl}"\n'
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


@contextmanager
def _client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    cfg: Path | None = None,
    authenticate: bool = True,
) -> Iterator[TestClient]:
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    monkeypatch.delenv("MNEMOSEED_TOKEN", raising=False)
    monkeypatch.delenv("MNEMOSEED_PROFILE_ID", raising=False)
    monkeypatch.setattr("mnemoseed.config.CONFIG_PATH", cfg if cfg is not None else _config_toml(tmp_path))
    monkeypatch.setattr("mnemoseed.dream.snapshot.CONFIG_DIR", tmp_path)
    with TestClient(create_app(), client=("127.0.0.1", 50059)) as client:
        if authenticate:
            attach_token(client)
        yield client


def _audit(client: TestClient, action: str) -> list[object]:
    return client.app.state.stores.meta.audit_query(AuditFilter(action=action), Page(limit=100)).items


# ---------------------------------------------------------------- POST /api/v1/llm/key


def test_post_key_writes_secret_persists_ref_and_answers_masked(tmp_path, monkeypatch) -> None:
    cfg = _config_toml(tmp_path)
    with _client(tmp_path, monkeypatch, cfg=cfg) as client:
        response = client.post("/api/v1/llm/key", json={"role": "deep_reflection", "key": _SECRET})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["ok"] is True
        assert body["masked_tail"] == "9012"
        assert body["restart_required"] is False
        assert _SECRET not in response.text  # only the masked tail ever travels

        # the secret landed under <CONFIG_DIR>/secrets/ as its own file
        secret_file = tmp_path / "secrets" / "mnemoseed.dream.deep_reflection.key"
        assert secret_file.read_text(encoding="utf-8") == _SECRET

        # the config file carries the REFERENCE only, never the value
        on_disk = cfg.read_text(encoding="utf-8")
        assert _REF_DEEP in on_disk
        assert _SECRET not in on_disk

        # read surfaces report the reference, never the value
        routes = client.get("/api/v1/llm/routes").json()
        deep = next(r for r in routes["roles"] if r["role"] == "deep_reflection")
        assert deep["api_key_env"] == _REF_DEEP
        config_body = client.get("/api/v1/config").text
        assert _REF_DEEP in config_body
        assert _SECRET not in config_body


def test_post_key_is_audited_with_the_reference_never_the_value(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        response = client.post("/api/v1/llm/key", json={"role": "deep_reflection", "key": _SECRET})
        assert response.status_code == 200
        entries = _audit(client, "config.set")
        key_write = [e for e in entries if "api_key_env" in str(e.detail)]
        assert key_write, "the reference write must be audited"
        assert key_write[-1].detail["value"] == _REF_DEEP
        assert _SECRET not in repr(key_write[-1].detail)
        # no audit action ever carries the value
        assert _SECRET not in repr(_audit(client, "config.set"))


def test_post_key_unknown_role_is_422(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        response = client.post("/api/v1/llm/key", json={"role": "no_such_role", "key": _SECRET})
        assert response.status_code == 422
        assert "unknown llm role" in response.json()["detail"]
        assert _SECRET not in response.text


def test_post_key_local_track_is_422_with_deprecation_wording(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        response = client.post("/api/v1/llm/key", json={"role": "local_track", "key": _SECRET})
        assert response.status_code == 422
        assert "deprecated" in response.json()["detail"].lower()
        assert _SECRET not in response.text


def test_post_key_empty_key_is_422(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        response = client.post("/api/v1/llm/key", json={"role": "deep_reflection", "key": "   "})
        assert response.status_code == 422
        assert _SECRET not in response.text
        assert (tmp_path / "secrets").exists() is False  # nothing was written


def test_post_key_is_loopback_only(tmp_path, monkeypatch) -> None:
    cfg = _config_toml(tmp_path, baseurl="http://10.0.0.5:7788")
    with _client(tmp_path, monkeypatch, cfg=cfg) as client:
        response = client.post("/api/v1/llm/key", json={"role": "deep_reflection", "key": _SECRET})
        assert response.status_code == 403
        assert "loopback" in response.json()["detail"]
        assert (tmp_path / "secrets").exists() is False


# ---------------------------------------------------------------- DELETE /api/v1/llm/key


def test_delete_key_removes_secret_and_reference(tmp_path, monkeypatch) -> None:
    cfg = _config_toml(tmp_path)
    with _client(tmp_path, monkeypatch, cfg=cfg) as client:
        assert (
            client.post("/api/v1/llm/key", json={"role": "deep_reflection", "key": _SECRET}).status_code
            == 200
        )
        response = client.request("DELETE", "/api/v1/llm/key", json={"role": "deep_reflection"})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["ok"] is True
        assert body["restart_required"] is False

        assert (tmp_path / "secrets" / "mnemoseed.dream.deep_reflection.key").exists() is False
        assert _REF_DEEP not in cfg.read_text(encoding="utf-8")
        routes = client.get("/api/v1/llm/routes").json()
        deep = next(r for r in routes["roles"] if r["role"] == "deep_reflection")
        assert deep["api_key_env"] is None  # the explicit reference is cleared
        effective_env = routes["routes"]["deep_reflection"]["effective"]["api_key_env"]
        assert effective_env == DEFAULT_LLM_ROUTES["deep_reflection"].params["api_key_env"]


def test_delete_key_unknown_role_is_422(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        response = client.request("DELETE", "/api/v1/llm/key", json={"role": "no_such_role"})
        assert response.status_code == 422
        assert "unknown llm role" in response.json()["detail"]


def test_delete_key_is_loopback_only(tmp_path, monkeypatch) -> None:
    cfg = _config_toml(tmp_path, baseurl="http://10.0.0.5:7788")
    with _client(tmp_path, monkeypatch, cfg=cfg) as client:
        response = client.request("DELETE", "/api/v1/llm/key", json={"role": "deep_reflection"})
        assert response.status_code == 403
        assert "loopback" in response.json()["detail"]


# ---------------------------------------------------------------- hot-apply (F2)


def test_post_key_hot_applies_to_the_next_role_resolve(tmp_path, monkeypatch) -> None:
    """F2: the reference write bumps the role generation, so the daemon's own
    RoleRouter rebuilds the role on the next resolve — the new key is effective
    on the next dream run with NO restart."""
    with _client(tmp_path, monkeypatch) as client:
        router = client.app.state.role_router
        # before any key: no env, no secret -> empty key
        assert router.resolve("deep_reflection").api_key == ""

        first = client.post("/api/v1/llm/key", json={"role": "deep_reflection", "key": "sk-new-key-1111"})
        assert first.status_code == 200
        assert router.resolve("deep_reflection").api_key == "sk-new-key-1111"

        second = client.post("/api/v1/llm/key", json={"role": "deep_reflection", "key": "sk-newer-key-2222"})
        assert second.status_code == 200
        assert second.json()["masked_tail"] == "2222"
        assert router.resolve("deep_reflection").api_key == "sk-newer-key-2222"


def test_delete_key_returns_the_role_to_the_env_fallback(tmp_path, monkeypatch) -> None:
    """After DELETE the role resolves through the env-var chain (the reference
    is gone): the env value wins at resolve time."""
    monkeypatch.setenv("MNEMOSEED_DEEP_REFLECTION_API_KEY", "sk-env-fallback-value")
    with _client(tmp_path, monkeypatch) as client:
        router = client.app.state.role_router
        client.post("/api/v1/llm/key", json={"role": "deep_reflection", "key": "sk-stored-value"})
        # the reference takes precedence while present: the STORED key wins
        assert router.resolve("deep_reflection").api_key == "sk-stored-value"
        client.request("DELETE", "/api/v1/llm/key", json={"role": "deep_reflection"})
        # the reference is gone: the env chain is the fallback again
        assert router.resolve("deep_reflection").api_key == "sk-env-fallback-value"


# ---------------------------------------------------------------- auth gate parity


def test_key_endpoints_sit_behind_the_identity_gate(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch, authenticate=False) as client:
        assert (
            client.post("/api/v1/llm/key", json={"role": "deep_reflection", "key": _SECRET}).status_code
            == 503
        )
        assert (
            client.request("DELETE", "/api/v1/llm/key", json={"role": "deep_reflection"}).status_code == 503
        )
    with _client(tmp_path, monkeypatch) as client:
        client.headers.pop("authorization", None)
        assert (
            client.post("/api/v1/llm/key", json={"role": "deep_reflection", "key": _SECRET}).status_code
            == 401
        )
        assert (
            client.request("DELETE", "/api/v1/llm/key", json={"role": "deep_reflection"}).status_code == 401
        )
