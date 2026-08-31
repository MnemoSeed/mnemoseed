"""LLM probe-gate (connectivity-test-before-persist, MUST-FIX 2 / QA W1+W2).

The gate lives in ``LLMAdminService`` (``_passed_tests`` / ``_TEST_GRACE``) and
surfaces as ``LLMTestRequiredError`` (HTTP 409). These tests pin it down so a
mutation that bypasses the check cannot survive:

- a persist without any prior passing probe is rejected;
- a probe of signature A never authorizes a persist of signature B;
- a matching probe-then-set succeeds;
- the grace window (600s) is enforced against the injected clock;
- a daemon restart (a fresh service instance) forgets every cached probe;
- the passed-test cache is actively bounded (expiry eviction + 128 cap).

``test_config`` is armed with the stub driver, which passes offline, so no
probe ever touches the network.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from _identity_helpers import attach_token
from fastapi.testclient import TestClient

from mnemoseed.config import load_config
from mnemoseed.daemon.app import create_app
from mnemoseed.llm.admin import LLMAdminService, LLMTestRequiredError
from mnemoseed.llm.registry import LLM_DRIVERS, register
from mnemoseed.storage.drivers import lancedb_embedded, sqlite_graph, sqlite_meta
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

_TEST_GRACE_SECONDS = 600.0


def _config_toml(tmp_path: Path) -> Path:
    """A routable config whose roles are network-free (stub driver)."""
    p = tmp_path / "config.toml"
    p.write_text(
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
        'model = "stub"\n'
        'base_url = "http://127.0.0.1:1"\n'
        "[dream.llm.local_track]\n"
        'driver = "ollama"\n'
        'model = "llama3.1:8b"\n'
        'base_url = "http://127.0.0.1:1"\n',
        encoding="utf-8",
    )
    return p


@pytest.fixture(autouse=True)
def _ensure_drivers():
    """test_daemon clears the shared registries; re-register the real drivers."""
    for registry, cls in (
        (VECTOR_DRIVERS, lancedb_embedded.LanceDbEmbeddedStore),
        (GRAPH_DRIVERS, sqlite_graph.SqliteGraphDriver),
        (META_DRIVERS, sqlite_meta.SqliteMetaDriver),
        (EMBED_DRIVERS, SyntheticEmbedder),
    ):
        if not registry.contains(cls.info.name):
            register_storage(registry)(cls)
    from mnemoseed.llm.drivers.stub import StubLLM

    if not LLM_DRIVERS.contains(StubLLM.info.name):
        register(LLM_DRIVERS)(StubLLM)
    yield


class _MutableClock:
    """A clock the tests can wind forward to prove grace-window expiry."""

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _service_on(tmp_path: Path, *, clock: Callable[[], float] | None = None) -> tuple[LLMAdminService, Path]:
    path = _config_toml(tmp_path)
    return LLMAdminService(load_config(path), meta=None, clock=clock), path


def _arm(service: LLMAdminService, role: str, *, model: str = "m", **route) -> None:
    """Pass a connectivity test for the exact route to be persisted (stub,
    which passes offline, so the arming probe never needs the network)."""
    report = service.test_config(role=role, driver="stub", model=model, **route)
    assert report.ok is True


# ---------------------------------------------------------------- service-level gate


def test_set_role_without_prior_test_raises_llm_test_required(tmp_path) -> None:
    """(a) A persist is impossible before ANY probe passes — deleting the gate
    check makes this test fail (mutation guard)."""
    service, _ = _service_on(tmp_path)
    with pytest.raises(LLMTestRequiredError, match="connectivity test"):
        service.set_role("deep_reflection", driver="stub", model="m")


def test_set_role_rejects_test_of_a_different_signature(tmp_path) -> None:
    """(b) A passing probe of signature A never authorizes a persist of B."""
    service, _ = _service_on(tmp_path)
    _arm(service, "deep_reflection", model="claude-sonnet-5")
    with pytest.raises(LLMTestRequiredError):
        service.set_role("deep_reflection", driver="stub", model="claude-opus-5")


def test_set_role_rejects_test_missing_an_optional_field(tmp_path) -> None:
    """(b) An optional-field difference is also a signature mismatch."""
    service, _ = _service_on(tmp_path)
    _arm(service, "short_increment", model="m", base_url="http://example.test")
    with pytest.raises(LLMTestRequiredError):
        service.set_role("short_increment", driver="stub", model="m", base_url="http://other.test")


def test_set_role_after_matching_test_persists(tmp_path) -> None:
    """(c) Matching probe-then-set persists the route."""
    service, path = _service_on(tmp_path)
    _arm(service, "deep_reflection", model="claude-sonnet-5")
    result = service.set_role("deep_reflection", driver="stub", model="claude-sonnet-5")
    assert result["model"] == "claude-sonnet-5"
    assert 'model = "claude-sonnet-5"' in path.read_text(encoding="utf-8")


def test_set_role_grace_window_expiry_requires_fresh_test(tmp_path) -> None:
    """(d) A probe older than the 600s grace window no longer authorizes."""
    clock = _MutableClock()
    service, _ = _service_on(tmp_path, clock=clock)
    _arm(service, "deep_reflection", model="m")
    service.set_role("deep_reflection", driver="stub", model="m")

    clock.advance(_TEST_GRACE_SECONDS + 1)
    with pytest.raises(LLMTestRequiredError):
        service.set_role("deep_reflection", driver="stub", model="m")

    # a fresh probe inside the new window authorizes again
    _arm(service, "deep_reflection", model="m")
    service.set_role("deep_reflection", driver="stub", model="m")


def test_daemon_restart_forgets_every_cached_probe(tmp_path) -> None:
    """(e) A fresh service instance (daemon restart) shares no probe cache."""
    service1, path = _service_on(tmp_path)
    _arm(service1, "deep_reflection", model="m")
    service1.set_role("deep_reflection", driver="stub", model="m")

    service2 = LLMAdminService(load_config(path), meta=None)
    with pytest.raises(LLMTestRequiredError):
        service2.set_role("deep_reflection", driver="stub", model="m")


# ---------------------------------------------------------------- bounded cache (item 6)


def test_passed_tests_evicts_expired_entries_on_record(tmp_path) -> None:
    """Active eviction: recording a new pass drops signatures past the grace
    window immediately, not only when they are next looked up."""
    clock = _MutableClock()
    service, _ = _service_on(tmp_path, clock=clock)
    _arm(service, "deep_reflection", model="old-model")
    _arm(service, "deep_reflection", model="fresh-model")
    assert len(service._passed_tests) == 2

    clock.advance(_TEST_GRACE_SECONDS + 1)
    _arm(service, "deep_reflection", model="newest-model")
    # "old-model" expired and "fresh-model" expired; only the newest survives
    newest = service._signature(
        driver="stub", model="newest-model", base_url=None, api_key_env=None, provider=None
    )
    old = service._signature(driver="stub", model="old-model", base_url=None, api_key_env=None, provider=None)
    assert set(service._passed_tests) == {newest}
    assert old not in service._passed_tests
    with pytest.raises(LLMTestRequiredError):
        service.set_role("deep_reflection", driver="stub", model="old-model")
    service.set_role("deep_reflection", driver="stub", model="newest-model")


def test_passed_tests_keeps_at_most_128_most_recent_signatures(tmp_path) -> None:
    """Max-size guard: the cache keeps the 128 most recent signatures and drops
    the oldest once full."""
    clock = _MutableClock()
    service, _ = _service_on(tmp_path, clock=clock)
    for index in range(200):
        clock.advance(1.0)
        _arm(service, "deep_reflection", model=f"model-{index:03d}")
    assert len(service._passed_tests) <= 128
    newest = "\x1f".join(("stub", "model-199", "", "", ""))
    assert newest in service._passed_tests
    oldest = "\x1f".join(("stub", "model-000", "", "", ""))
    assert oldest not in service._passed_tests
    # the kept-most-recent signature still authorizes; the evicted one does not
    service.set_role("deep_reflection", driver="stub", model="model-199")
    with pytest.raises(LLMTestRequiredError):
        service.set_role("deep_reflection", driver="stub", model="model-000")


# ---------------------------------------------------------------- wire-level gate (409)


@contextmanager
def _client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, cfg: Path | None = None
) -> Iterator[TestClient]:
    """Boot the real daemon with a throwaway config; owner pre-set up."""
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    monkeypatch.setattr("mnemoseed.config.CONFIG_PATH", cfg if cfg is not None else _config_toml(tmp_path))
    monkeypatch.setattr("mnemoseed.dream.snapshot.CONFIG_DIR", tmp_path)
    with TestClient(create_app(), client=("127.0.0.1", 50057)) as client:
        attach_token(client)
        yield client


def test_llm_set_role_without_probe_is_409(tmp_path, monkeypatch) -> None:
    """(a, wire) A persist without a prior probe answers HTTP 409, not 200."""
    with _client(tmp_path, monkeypatch) as client:
        response = client.post("/api/v1/llm/routes/deep_reflection", json={"driver": "stub", "model": "m"})
        assert response.status_code == 409
        assert "connectivity test" in response.json()["detail"]


def test_llm_set_role_after_wrong_signature_probe_is_409(tmp_path, monkeypatch) -> None:
    """(b, wire) Probing model m1 never authorizes persisting model m2."""
    with _client(tmp_path, monkeypatch) as client:
        probe = client.post(
            "/api/v1/llm/test",
            json={"role": "deep_reflection", "driver": "stub", "model": "claude-sonnet-5"},
        )
        assert probe.status_code == 200
        response = client.post(
            "/api/v1/llm/routes/deep_reflection",
            json={"driver": "stub", "model": "claude-opus-5"},
        )
        assert response.status_code == 409


def test_llm_set_role_after_matching_probe_is_200(tmp_path, monkeypatch) -> None:
    """(c, wire) Matching probe-then-set round-trips over HTTP."""
    with _client(tmp_path, monkeypatch) as client:
        probe = client.post(
            "/api/v1/llm/test",
            json={"role": "deep_reflection", "driver": "stub", "model": "claude-sonnet-5"},
        )
        assert probe.status_code == 200
        response = client.post(
            "/api/v1/llm/routes/deep_reflection",
            json={"driver": "stub", "model": "claude-sonnet-5"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["model"] == "claude-sonnet-5"


def test_llm_set_role_daemon_restart_requires_fresh_probe(tmp_path, monkeypatch) -> None:
    """(e, wire) A rebooted daemon forgets the previous process's probes."""
    cfg = _config_toml(tmp_path)
    with _client(tmp_path, monkeypatch, cfg=cfg) as client:
        probe = client.post(
            "/api/v1/llm/test",
            json={"role": "deep_reflection", "driver": "stub", "model": "m"},
        )
        assert probe.status_code == 200
        first = client.post("/api/v1/llm/routes/deep_reflection", json={"driver": "stub", "model": "m"})
        assert first.status_code == 200

    with _client(tmp_path, monkeypatch, cfg=cfg) as client:
        second = client.post("/api/v1/llm/routes/deep_reflection", json={"driver": "stub", "model": "m"})
        assert second.status_code == 409
