"""ConfigWriteService REST surface (PRD-07 FR-7.11, W1.1): the /api/v1/config
contract the CLI codes against.

Drives the REAL daemon app (embedded preset, synthetic embedder) through
TestClient and asserts the wire contract only:

- GET    /api/v1/config             resolved config, secrets redacted (env-var
                                    NAMES only); body is {"config": {...},
                                    "restart_required": {...}}.
- POST   /api/v1/config/set         {key_path, value} -> {ok, version_id,
                                    restart_required}; 4xx names the offending key.
- GET    /api/v1/config/versions    versioned history (baseline + writes).
- POST   /api/v1/config/rollback    {version_id} -> {ok, version_id, restored},
                                    append-only.
- actor  from the X-MnemoSeed-Actor header (cli|console|mcp), default console;
- writes are rejected (403) when the daemon baseurl is non-loopback.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import pytest
from _identity_helpers import attach_token
from fastapi.testclient import TestClient

from mnemoseed.daemon.app import create_app
from mnemoseed.storage.drivers import lancedb_embedded, sqlite_graph, sqlite_meta
from mnemoseed.storage.drivers.synthetic_embedder import SyntheticEmbedder
from mnemoseed.storage.ports import AuditFilter, Page
from mnemoseed.storage.registry import (
    EMBED_DRIVERS,
    GRAPH_DRIVERS,
    META_DRIVERS,
    VECTOR_DRIVERS,
    register,
)

_ADMIN_TOKEN = "configwrite-test-token"


@pytest.fixture(autouse=True)
def _ensure_real_drivers():
    """test_daemon clears the shared registries; re-register the real drivers."""
    for registry, cls in (
        (VECTOR_DRIVERS, lancedb_embedded.LanceDbEmbeddedStore),
        (GRAPH_DRIVERS, sqlite_graph.SqliteGraphDriver),
        (META_DRIVERS, sqlite_meta.SqliteMetaDriver),
        (EMBED_DRIVERS, SyntheticEmbedder),
    ):
        if not registry.contains(cls.info.name):
            register(registry)(cls)
    yield


def _config_toml(tmp_path: Path, *, baseurl: str = "http://localhost:7788") -> Path:
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        'preset = "embedded"\n'
        f'baseurl = "{baseurl}"\n'
        f'[storage.vector]\nuri = "{(tmp_path / "chunks.lance").as_posix()}"\ndimensions = 64\n'
        f'[storage.graph]\npath = "{(tmp_path / "cortex.db").as_posix()}"\n'
        f'[storage.meta]\npath = "{(tmp_path / "meta.db").as_posix()}"\n'
        f'[storage.embed]\ndriver = "synthetic"\ndimension = 64\n'
        "[dream]\ntoken_budget_usd = 5.0\n"
        '[dream.llm.deep_reflection]\ndriver = "stub"\nmodel = "stub"\n',
        encoding="utf-8",
    )
    return cfg


@contextmanager
def _client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, baseurl: str = "http://localhost:7788"):
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    monkeypatch.setattr("mnemoseed.config.CONFIG_PATH", _config_toml(tmp_path, baseurl=baseurl))
    monkeypatch.setenv("MNEMOSEED_CONSOLE_ADMIN_TOKEN", _ADMIN_TOKEN)
    with TestClient(create_app(), client=("127.0.0.1", 50057)) as client:
        attach_token(client)
        yield client


def _audit(client: TestClient, action: str) -> list[object]:
    return client.app.state.stores.meta.audit_query(AuditFilter(action=action), Page(limit=100)).items


# ---------------------------------------------------------------- GET /config


def test_config_get_resolved_and_secrets_redacted(tmp_path, monkeypatch) -> None:
    """GET /api/v1/config: the resolved config with env-var NAMES only -- a
    literal key value anywhere in the payload is a test failure."""
    with _client(tmp_path, monkeypatch) as client:
        response = client.get("/api/v1/config")
        assert response.status_code == 200
        body = response.json()
        config = body["config"]
        assert config["preset"] == "embedded"
        assert config["baseurl"] == "http://localhost:7788"
        assert config["dream"]["token_budget_usd"] == 5.0
        deep = config["dream"]["llm"]["deep_reflection"]
        assert deep["driver"] == "stub"
        assert deep["model"] == "stub"
        assert body["restart_required"] == {}
        # the stub route never had a key set: the NAMES field surfaces, values never
        assert "api_key_env" in deep
        assert "sk-" not in response.text


def test_config_get_accepts_actor_header_and_rejects_unknown(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        assert client.get("/api/v1/config", headers={"X-MnemoSeed-Actor": "cli"}).status_code == 200
        unknown = client.get("/api/v1/config", headers={"X-MnemoSeed-Actor": "nobody"})
        assert unknown.status_code == 422
        assert "nobody" in unknown.json()["detail"]


# ---------------------------------------------------------------- POST /config/set


def test_config_set_writes_file_versions_and_live_config(tmp_path, monkeypatch) -> None:
    cfg = _config_toml(tmp_path)
    with _client(tmp_path, monkeypatch) as client:
        response = client.post("/api/v1/config/set", json={"key_path": "dream.auto_trigger", "value": True})
        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is True
        assert isinstance(body["version_id"], int)
        assert body["restart_required"] is False

        assert "auto_trigger = true" in cfg.read_text(encoding="utf-8")
        assert client.app.state.config.dream.auto_trigger is True  # live-apply
        # the write is audited with the default actor
        entries = _audit(client, "config.set")
        assert entries[-1].actor == "console"
        assert entries[-1].detail["key_path"] == "dream.auto_trigger"


def test_config_set_actor_header_attributed(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        response = client.post(
            "/api/v1/config/set",
            json={"key_path": "dream.auto_trigger", "value": True},
            headers={"X-MnemoSeed-Actor": "cli"},
        )
        assert response.status_code == 200
        assert _audit(client, "config.set")[-1].actor == "cli"
        bad = client.post(
            "/api/v1/config/set",
            json={"key_path": "dream.auto_trigger", "value": True},
            headers={"X-MnemoSeed-Actor": "mcp"},
        )
        assert bad.status_code == 200
        assert _audit(client, "config.set")[-1].actor == "mcp"


def test_config_set_unknown_key_422_names_key(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        response = client.post("/api/v1/config/set", json={"key_path": "scoring.w1", "value": 0.5})
        assert response.status_code == 422
        assert "scoring.w1" in response.json()["detail"]


def test_config_set_key_like_api_key_env_422(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        response = client.post(
            "/api/v1/config/set",
            json={"key_path": "dream.llm.short_increment.api_key_env", "value": "sk-proj-literal"},
        )
        assert response.status_code == 422
        assert "dream.llm.short_increment.api_key_env" in response.json()["detail"]
        assert "sk-proj-literal" not in response.text


def test_config_set_bad_value_422(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        response = client.post("/api/v1/config/set", json={"key_path": "dream.auto_trigger", "value": "yes"})
        assert response.status_code == 422
        assert "dream.auto_trigger" in response.json()["detail"]


def test_config_set_requires_key_path_body(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        assert client.post("/api/v1/config/set", json={"value": 1}).status_code == 422
        assert client.post("/api/v1/config/set", json={"key_path": "", "value": 1}).status_code == 422


# ---------------------------------------------------------------- GET /config/versions


def test_config_versions_lists_history(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        client.post("/api/v1/config/set", json={"key_path": "dream.auto_trigger", "value": True})
        body = client.get("/api/v1/config/versions").json()
        versions = body["versions"]
        assert versions
        for version in versions:
            assert "version_id" in version
            assert "key" in version
            assert "updated_at" in version
        auto = [v for v in versions if v["key"] == "dream.auto_trigger"]
        assert any(v["value"] is True for v in auto)
        # internal bookkeeping records never leak into the public history
        assert all("__" not in v["key"] for v in versions)


# ---------------------------------------------------------------- POST /config/rollback


def test_config_rollback_round_trip(tmp_path, monkeypatch) -> None:
    cfg = _config_toml(tmp_path)
    with _client(tmp_path, monkeypatch) as client:
        enabled = client.post(
            "/api/v1/config/set", json={"key_path": "dream.auto_trigger", "value": True}
        ).json()["version_id"]
        client.post("/api/v1/config/set", json={"key_path": "dream.auto_trigger", "value": False})

        rolled = client.post("/api/v1/config/rollback", json={"version_id": enabled}).json()
        assert rolled["ok"] is True
        assert rolled["restored"] == rolled["version_id"]
        assert rolled["version_id"] != enabled  # append-only: a new version

        assert "auto_trigger = true" in cfg.read_text(encoding="utf-8")
        assert client.get("/api/v1/config").json()["config"]["dream"]["auto_trigger"] is True
        entries = _audit(client, "config.rollback")
        assert entries[-1].actor == "console"
        assert entries[-1].detail["key_path"] == "dream.auto_trigger"


def test_config_rollback_unknown_version_422(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        response = client.post("/api/v1/config/rollback", json={"version_id": 9_999_999_999})
        assert response.status_code == 422
        assert "9" in response.json()["detail"]


# ---------------------------------------------------------------- loopback write gate


def test_config_writes_rejected_for_remote_baseurl_reads_allowed(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch, baseurl="http://10.0.0.5:7788") as client:
        assert client.get("/api/v1/config").status_code == 200
        for path, payload in (
            ("/api/v1/config/set", {"key_path": "dream.auto_trigger", "value": True}),
            ("/api/v1/config/rollback", {"version_id": 1}),
        ):
            response = client.post(path, json=payload)
            assert response.status_code == 403
            assert "loopback" in response.json()["detail"]
