"""Server-side audit actor attribution across every write surface (W1+W2 QA).

Drives the REAL daemon app through TestClient and asserts the append-only audit
trail records the surface actor resolved from the ``X-MnemoSeed-Actor`` header:

- config set, llm set, console forget/pin/weights, dream --once, memory
  remember / forget_this, and conflict resolve.
- A CLI-tagged request (``X-MnemoSeed-Actor: cli``) records ``actor=cli``; an
  untagged or console-tagged request records ``actor=console`` (the reference
  default, matching configwrite's route).

This is the real-server seam the header-mocking tests cannot see: the audit
row's actor is asserted end to end, not just that the header was forwarded.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from _identity_helpers import attach_token
from fastapi.testclient import TestClient

from mnemoseed.daemon.app import create_app
from mnemoseed.schema.graph import GraphNode, NodeType
from mnemoseed.schema.stamp import ChunkStamp, CognitiveTier, Cues, Provenance, ProvenanceEvent
from mnemoseed.storage.drivers import lancedb_embedded, sqlite_graph, sqlite_meta
from mnemoseed.storage.drivers.synthetic_embedder import SyntheticEmbedder
from mnemoseed.storage.ports import AuditFilter, Page, StoredProfile
from mnemoseed.storage.registry import (
    EMBED_DRIVERS,
    GRAPH_DRIVERS,
    META_DRIVERS,
    VECTOR_DRIVERS,
    register,
)

_PROFILE = "prof-actor"


@pytest.fixture(autouse=True)
def _ensure_real_drivers():
    for registry, cls in (
        (VECTOR_DRIVERS, lancedb_embedded.LanceDbEmbeddedStore),
        (GRAPH_DRIVERS, sqlite_graph.SqliteGraphDriver),
        (META_DRIVERS, sqlite_meta.SqliteMetaDriver),
        (EMBED_DRIVERS, SyntheticEmbedder),
    ):
        if not registry.contains(cls.info.name):
            register(registry)(cls)
    from mnemoseed.llm.drivers.stub import StubLLM
    from mnemoseed.llm.registry import LLM_DRIVERS
    from mnemoseed.llm.registry import register as register_llm

    if not LLM_DRIVERS.contains(StubLLM.info.name):
        register_llm(LLM_DRIVERS)(StubLLM)
    yield


def _config_toml(tmp_path: Path) -> Path:
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
        'model = "stub"\n'
        "[dream.llm.local_track]\n"
        'driver = "stub"\n'
        'model = "stub"\n',
        encoding="utf-8",
    )
    return cfg


@contextmanager
def _client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    monkeypatch.setattr("mnemoseed.config.CONFIG_PATH", _config_toml(tmp_path))
    monkeypatch.setattr("mnemoseed.dream.snapshot.CONFIG_DIR", tmp_path)
    monkeypatch.setenv("MNEMOSEED_CONSOLE_ADMIN_TOKEN", "actor-test-token")
    with TestClient(create_app(), client=("127.0.0.1", 50057)) as client:
        attach_token(client)
        yield client


def _seed_profile(client: TestClient, profile_id: str = _PROFILE) -> None:
    client.portal.call(client.app.state.stores.meta.upsert_profile, StoredProfile(profile_id=profile_id))


def _write_chunk(client: TestClient, chunk_id: str, text: str) -> None:
    now = time.time()
    stamp = ChunkStamp(
        chunk_id=chunk_id,
        profile_id=_PROFILE,
        text=text,
        cognitive_tier=CognitiveTier.TIER_1,
        model_id="test-model",
        cues=Cues(),
        provenance=Provenance(
            asserted_by="test-agent",
            session_id="s",
            source="seed",
            asserted_at=now,
            history=[ProvenanceEvent(at=now - 1.0, action="created", actor="test", detail={"round": 0})],
        ),
        decay_weight=1.0,
        score=0.0,
        ingested_at=now,
    )
    embedded = client.app.state.stores.embed.embed(text)
    client.portal.call(client.app.state.stores.vector.upsert_chunk, stamp, embedded.dense, embedded.sparse)


def _write_node(client: TestClient, node: GraphNode) -> None:
    client.portal.call(client.app.state.stores.graph.upsert_node, node)


def _pref(node_id: str, statement: str) -> GraphNode:
    now = time.time()
    return GraphNode(
        node_id=node_id,
        profile_id=_PROFILE,
        node_type=NodeType.PREFERENCE,
        entities=["Fmt"],
        props={
            "domain": "coding",
            "statement": statement,
            "valence": 0.8,
            "prior_width": 0.3,
            "trait_anchor": "anima-1",
            "evidence_chain": [{"event": "created", "at": 1.0}],
        },
        confidence=0.9,
        provenance=Provenance(
            asserted_by="dream-engine",
            session_id="s",
            source="seed",
            asserted_at=now - 1.0,
        ),
        version=1,
        valid_from=now - 100.0,
        updated_at=now,
        decay_weight=0.9,
    )


def _conflict_pair(client: TestClient, group_id: str) -> None:
    now = time.time()

    def node(node_id: str, statement: str) -> GraphNode:
        return GraphNode(
            node_id=node_id,
            profile_id=_PROFILE,
            node_type=NodeType.PREFERENCE,
            entities=["Fmt"],
            props={
                "domain": "coding",
                "statement": statement,
                "valence": 0.8,
                "prior_width": 0.3,
                "trait_anchor": "anima-1",
                "evidence_chain": [{"event": "created", "at": 1.0}],
            },
            confidence=0.9,
            conflict_flag=True,
            conflict_group=group_id,
            provenance=Provenance(
                asserted_by="dream-engine",
                session_id="s",
                source="seed",
                asserted_at=now - 1.0,
            ),
            version=1,
            valid_from=now - 100.0,
            updated_at=now,
            decay_weight=0.9,
        )

    _write_node(client, node("a-tabs", "prefers tabs"))
    _write_node(client, node("b-spaces", "prefers spaces"))


def _audit(client: TestClient, action: str) -> list[object]:
    return client.app.state.stores.meta.audit_query(AuditFilter(action=action), Page(limit=50)).items


def _cli(client: TestClient, method: str, path: str, **kwargs):
    headers = dict(kwargs.pop("headers", {}) or {})
    headers["X-MnemoSeed-Actor"] = "cli"
    return client.request(method, path, headers=headers, **kwargs)


# ---------------------------------------------------------------- config set


def test_config_set_cli_tagged_records_cli_and_untagged_console(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        _cli(client, "POST", "/api/v1/config/set", json={"key_path": "dream.auto_trigger", "value": True})
        assert _audit(client, "config.set")[-1].actor == "cli"

        client.post("/api/v1/config/set", json={"key_path": "dream.auto_trigger", "value": False})
        assert _audit(client, "config.set")[-1].actor == "console"

        client.post(
            "/api/v1/config/set",
            json={"key_path": "dream.auto_trigger", "value": True},
            headers={"X-MnemoSeed-Actor": "console"},
        )
        assert _audit(client, "config.set")[-1].actor == "console"


# ---------------------------------------------------------------- llm set


def test_llm_set_cli_tagged_records_cli(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        _cli(
            client,
            "POST",
            "/api/v1/llm/test",
            json={"role": "short_increment", "driver": "stub", "model": "m2"},
        )
        _cli(
            client,
            "POST",
            "/api/v1/llm/routes/short_increment",
            json={"driver": "stub", "model": "m2"},
        )
        entries = _audit(client, "llm_role_set")
        assert entries[-1].actor == "cli"
        assert entries[-1].detail["model"] == "m2"


# ---------------------------------------------------------------- console forget / pin / weights


def test_console_forget_cli_tagged_records_cli(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        _seed_profile(client)
        _write_chunk(client, "c-forget", "forget me")
        _cli(client, "POST", "/api/v1/forget", json={"profile_id": _PROFILE, "chunk_id": "c-forget"})
        assert _audit(client, "forget_this")[-1].actor == "cli"


def test_console_pin_cli_tagged_records_cli(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        _seed_profile(client)
        _write_node(client, _pref("n-pin", "prefers x"))
        _cli(client, "POST", "/api/v1/pin", json={"profile_id": _PROFILE, "node_id": "n-pin", "pinned": True})
        assert _audit(client, "pin")[-1].actor == "cli"


def test_console_weight_cli_tagged_records_cli(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        _seed_profile(client)
        _write_node(client, _pref("n-w", "prefers y"))
        _cli(
            client,
            "POST",
            "/api/v1/weights",
            json={"profile_id": _PROFILE, "kind": "node", "target_id": "n-w", "decay_weight": 0.3},
        )
        assert _audit(client, "weight_adjust")[-1].actor == "cli"


# ---------------------------------------------------------------- dream --once


def test_dream_once_cli_tagged_records_cli(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        _seed_profile(client)
        _cli(client, "POST", "/api/v1/dream/once", json={"profile_id": _PROFILE})
        assert _audit(client, "dream_once")[-1].actor == "cli"


# ---------------------------------------------------------------- memory remember / forget_this


def test_memory_remember_cli_tagged_records_cli(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        _seed_profile(client)
        _cli(client, "POST", "/memory/remember", json={"profile_id": _PROFILE, "text": "cli fact"})
        assert _audit(client, "remember")[-1].actor == "cli"


def test_memory_remember_untagged_records_console(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        _seed_profile(client)
        client.post("/memory/remember", json={"profile_id": _PROFILE, "text": "untagged fact"})
        assert _audit(client, "remember")[-1].actor == "console"


def test_memory_forget_this_cli_tagged_records_cli(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        _seed_profile(client)
        _write_chunk(client, "c-forget2", "forget me too")
        _cli(
            client,
            "POST",
            "/memory/forget_this",
            json={"profile_id": _PROFILE, "chunk_id": "c-forget2"},
        )
        assert _audit(client, "forget_this")[-1].actor == "cli"


# ---------------------------------------------------------------- conflict resolve (CLI parity)


def test_conflict_resolve_cli_tagged_records_cli(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        _seed_profile(client)
        _conflict_pair(client, "cg-actor")
        _cli(
            client,
            "POST",
            "/api/v1/conflicts/cg-actor/resolve",
            json={"profile_id": _PROFILE, "branch": "coexist", "scope": "s"},
        )
        assert _audit(client, "conflict.resolve")[-1].actor == "cli"
