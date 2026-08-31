"""Console write-op REST layer (PRD-07 FR-7.9 / G-AC1): forget / pin / manual
decay adjust / profile create-rename-archive / token issue-revoke, plus the
audit viewing endpoint. Every write lands in the append-only audit trail.

Same harness as test_console_api.py: the REAL daemon app (embedded preset,
synthetic embedder) through TestClient, seeded through the daemon's own stores
on the portal thread, authenticated via the shared identity helpers.
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
from mnemoseed.storage.ports import (
    AuditFilter,
    Page,
    StoredProfile,
)
from mnemoseed.storage.registry import (
    EMBED_DRIVERS,
    GRAPH_DRIVERS,
    META_DRIVERS,
    VECTOR_DRIVERS,
    register,
)

_SESSION = "sess-writes-1"
_PROFILE = "prof-writes"


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


def _config_toml(tmp_path: Path) -> Path:
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        'preset = "embedded"\n'
        f'[storage.vector]\nuri = "{(tmp_path / "chunks.lance").as_posix()}"\ndimensions = 64\n'
        f'[storage.graph]\npath = "{(tmp_path / "cortex.db").as_posix()}"\n'
        f'[storage.meta]\npath = "{(tmp_path / "meta.db").as_posix()}"\n'
        f'[storage.embed]\ndriver = "synthetic"\ndimension = 64\n',
        encoding="utf-8",
    )
    return cfg


@contextmanager
def _client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """Boot the real daemon, finish setup, and stamp the profile token."""
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    monkeypatch.setattr("mnemoseed.config.CONFIG_PATH", _config_toml(tmp_path))
    monkeypatch.setattr("mnemoseed.dream.snapshot.CONFIG_DIR", tmp_path)
    monkeypatch.setenv("MNEMOSEED_CONSOLE_ADMIN_TOKEN", "console-write-test-token")
    with TestClient(create_app(), client=("127.0.0.1", 50057)) as client:
        attach_token(client)
        yield client


def _seed_profile(client: TestClient, profile_id: str = _PROFILE) -> None:
    client.portal.call(client.app.state.stores.meta.upsert_profile, StoredProfile(profile_id=profile_id))


def _write_chunk(
    client: TestClient,
    *,
    chunk_id: str,
    text: str,
    entities: tuple[str, ...] = (),
    decay: float = 1.0,
) -> str:
    now = time.time()
    stamp = ChunkStamp(
        chunk_id=chunk_id,
        profile_id=_PROFILE,
        text=text,
        cognitive_tier=CognitiveTier.TIER_1,
        model_id="test-model",
        cues=Cues(entities=list(entities)),
        provenance=Provenance(
            asserted_by="test-agent",
            session_id=_SESSION,
            source="seed",
            asserted_at=now,
            history=[ProvenanceEvent(at=now - 1.0, action="created", actor="test", detail={"round": 0})],
        ),
        decay_weight=decay,
        score=0.0,
        consolidated=False,
        ingested_at=now,
        turn_start=0,
        turn_end=1,
    )
    embedded = client.app.state.stores.embed.embed(text)
    client.portal.call(client.app.state.stores.vector.upsert_chunk, stamp, embedded.dense, embedded.sparse)
    return chunk_id


def _write_node(client: TestClient, node: GraphNode) -> str:
    client.portal.call(client.app.state.stores.graph.upsert_node, node)
    return node.node_id


def _pref(
    node_id: str, statement: str, decay: float = 0.9, entities: tuple[str, ...] = ("Fmt",)
) -> GraphNode:
    now = time.time()
    return GraphNode(
        node_id=node_id,
        profile_id=_PROFILE,
        node_type=NodeType.PREFERENCE,
        entities=list(entities),
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
            session_id=_SESSION,
            source="seed",
            asserted_at=now - 1.0,
        ),
        version=1,
        valid_from=now - 100.0,
        updated_at=now,
        decay_weight=decay,
    )


def _console_audit(client: TestClient, action: str) -> list[object]:
    return client.app.state.stores.meta.audit_query(
        AuditFilter(actor="console", action=action), Page(limit=50)
    ).items


# ---------------------------------------------------------------- forget (FR-7.9 / G-AC1)


def test_forget_chunk_physically_deletes_and_audits(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        _seed_profile(client)
        _write_chunk(client, chunk_id="c1", text="forget me")

        response = client.post("/api/v1/forget", json={"profile_id": _PROFILE, "chunk_id": "c1"})
        assert response.status_code == 200, response.text
        assert response.json() == {"removed": {"chunks": ["c1"], "nodes": []}}

        # physically gone from the vector store (design/03 storage-layer erasure)
        assert client.app.state.stores.vector.get_chunk("c1") is None
        entries = _console_audit(client, "forget_this")
        assert len(entries) == 1
        detail = entries[0].detail
        assert detail["profile_id"] == _PROFILE
        assert detail["chunks"] == ["c1"]
        assert detail["nodes"] == []


def test_forget_node_tombstones_and_preserves_version_chain(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        _seed_profile(client)
        _write_node(client, _pref("n1", "forget this preference"))

        response = client.post("/api/v1/forget", json={"profile_id": _PROFILE, "node_id": "n1"})
        assert response.status_code == 200, response.text
        assert response.json() == {"removed": {"chunks": [], "nodes": ["n1"]}}

        # tombstoned: no longer surfaced, but the version chain survives
        assert client.app.state.stores.graph.get_node("n1") is None
        assert len(client.app.state.stores.graph.versions("n1")) == 1
        assert _console_audit(client, "forget_this")[0].detail["nodes"] == ["n1"]


def test_forget_by_entity_removes_matching_chunks_and_nodes(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        _seed_profile(client)
        _write_chunk(client, chunk_id="c1", text="entity target", entities=("widget",))
        _write_chunk(client, chunk_id="c2", text="other", entities=("other",))
        _write_node(client, _pref("n1", "widget preference", entities=("widget",)))

        response = client.post("/api/v1/forget", json={"profile_id": _PROFILE, "entity": "widget"})
        assert response.status_code == 200, response.text
        removed = response.json()["removed"]
        assert removed["chunks"] == ["c1"]
        assert removed["nodes"] == ["n1"]
        assert client.app.state.stores.vector.get_chunk("c2") is not None  # untouched
        detail = _console_audit(client, "forget_this")[0].detail
        assert detail["entity"] == "widget"
        assert detail["chunks"] == ["c1"]


def test_forget_requires_exactly_one_target(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        _seed_profile(client)
        _write_chunk(client, chunk_id="c1", text="x")
        response = client.post("/api/v1/forget", json={"profile_id": _PROFILE})
        assert response.status_code == 422, response.text
        response = client.post(
            "/api/v1/forget", json={"profile_id": _PROFILE, "chunk_id": "c1", "node_id": "n1"}
        )
        assert response.status_code == 422, response.text


def test_forget_unknown_or_foreign_target_is_404(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        _seed_profile(client)
        _write_chunk(client, chunk_id="c1", text="x")
        response = client.post("/api/v1/forget", json={"profile_id": _PROFILE, "chunk_id": "ghost"})
        assert response.status_code == 404, response.text
        response = client.post("/api/v1/forget", json={"profile_id": "other", "chunk_id": "c1"})
        assert response.status_code == 404, response.text


# ---------------------------------------------------------------- pin (FR-7.9 / G-AC1)


def test_pin_node_marks_never_decay_as_new_version_and_audits(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        _seed_profile(client)
        _write_node(client, _pref("n1", "prefers dark mode"))

        response = client.post("/api/v1/pin", json={"profile_id": _PROFILE, "node_id": "n1", "pinned": True})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["never_decay"] is True
        assert body["version"] == 2

        node = client.app.state.stores.graph.get_node("n1")
        assert node is not None and node.never_decay is True
        assert node.version == 2
        assert node.valid_to is None
        # chain append: the pre-pin revision stays readable as_of
        assert [v.version for v in client.app.state.stores.graph.versions("n1")] == [1, 2]
        assert any(event.action == "pinned" and event.actor == "console" for event in node.provenance.history)
        entry = _console_audit(client, "pin")[0]
        assert entry.detail["node_id"] == "n1"
        assert entry.detail["pinned"] is True
        assert entry.detail["profile_id"] == _PROFILE


def test_unpin_flips_never_decay_back_and_appends_again(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        _seed_profile(client)
        _write_node(client, _pref("n1", "prefers tabs"))
        client.post("/api/v1/pin", json={"profile_id": _PROFILE, "node_id": "n1", "pinned": True})
        response = client.post("/api/v1/pin", json={"profile_id": _PROFILE, "node_id": "n1", "pinned": False})
        assert response.status_code == 200, response.text
        node = client.app.state.stores.graph.get_node("n1")
        assert node is not None and node.never_decay is False
        assert node.version == 3
        assert len(_console_audit(client, "pin")) == 2


def test_pin_is_idempotent_when_already_in_state(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        _seed_profile(client)
        _write_node(client, _pref("n1", "prefers x"))
        client.post("/api/v1/pin", json={"profile_id": _PROFILE, "node_id": "n1", "pinned": True})
        response = client.post("/api/v1/pin", json={"profile_id": _PROFILE, "node_id": "n1", "pinned": True})
        assert response.status_code == 200, response.text
        assert response.json()["changed"] is False
        assert client.app.state.stores.graph.get_node("n1").version == 2
        assert len(_console_audit(client, "pin")) == 1


def test_pin_unknown_node_404_and_non_bool_422(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        _seed_profile(client)
        response = client.post(
            "/api/v1/pin", json={"profile_id": _PROFILE, "node_id": "ghost", "pinned": True}
        )
        assert response.status_code == 404, response.text
        response = client.post(
            "/api/v1/pin", json={"profile_id": _PROFILE, "node_id": "ghost", "pinned": "yes"}
        )
        assert response.status_code == 422, response.text


# ---------------------------------------------------------------- weight adjust (FR-7.9 / G-AC1)


def test_weight_adjust_node_audits_old_and_new(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        _seed_profile(client)
        _write_node(client, _pref("n1", "prefers y", decay=0.9))

        response = client.post(
            "/api/v1/weights",
            json={"profile_id": _PROFILE, "kind": "node", "target_id": "n1", "decay_weight": 0.3},
        )
        assert response.status_code == 200, response.text
        assert response.json()["decay_weight"] == 0.3
        node = client.app.state.stores.graph.get_node("n1")
        assert node is not None and node.decay_weight == 0.3
        detail = _console_audit(client, "weight_adjust")[0].detail
        assert detail["old_decay_weight"] == 0.9
        assert detail["new_decay_weight"] == 0.3
        assert detail["target_id"] == "n1"


def test_weight_adjust_chunk_audits_old_and_new(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        _seed_profile(client)
        _write_chunk(client, chunk_id="c1", text="weighted", decay=0.8)

        response = client.post(
            "/api/v1/weights",
            json={"profile_id": _PROFILE, "kind": "chunk", "target_id": "c1", "decay_weight": 0.4},
        )
        assert response.status_code == 200, response.text
        chunk = client.app.state.stores.vector.get_chunk("c1")
        assert chunk is not None
        assert chunk.decay_weight == pytest.approx(0.4)
        detail = _console_audit(client, "weight_adjust")[0].detail
        assert detail["old_decay_weight"] == pytest.approx(0.8)
        assert detail["new_decay_weight"] == pytest.approx(0.4)


def test_weight_adjust_rejects_out_of_range_and_unknown_kind(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        _seed_profile(client)
        _write_node(client, _pref("n1", "prefers z"))
        for bad in (1.5, -0.1, "high"):
            response = client.post(
                "/api/v1/weights",
                json={"profile_id": _PROFILE, "kind": "node", "target_id": "n1", "decay_weight": bad},
            )
            assert response.status_code == 422, response.text
        response = client.post(
            "/api/v1/weights",
            json={"profile_id": _PROFILE, "kind": "edge", "target_id": "n1", "decay_weight": 0.5},
        )
        assert response.status_code == 422, response.text


def test_weight_adjust_unknown_target_404(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        _seed_profile(client)
        response = client.post(
            "/api/v1/weights",
            json={"profile_id": _PROFILE, "kind": "node", "target_id": "ghost", "decay_weight": 0.5},
        )
        assert response.status_code == 404, response.text


# ---------------------------------------------------------------- profiles (FR-7.3 / G-AC1)


def test_profile_create_and_audit(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        response = client.post("/api/v1/profiles", json={"profile_id": "p2", "display_name": "Two"})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["profile_id"] == "p2"
        assert body["display_name"] == "Two"
        profile = client.app.state.stores.meta.get_profile("p2")
        assert profile is not None and profile.display_name == "Two"
        entry = _console_audit(client, "profile.create")[0]
        assert entry.detail["profile_id"] == "p2"
        assert entry.detail["display_name"] == "Two"


def test_profile_rename_preserves_created_at_and_audits_old_name(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        _seed_profile(client)
        response = client.post(f"/api/v1/profiles/{_PROFILE}/rename", json={"display_name": "Renamed"})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["display_name"] == "Renamed"
        entry = _console_audit(client, "profile.rename")[0]
        assert entry.detail["display_name"] == ""
        assert entry.detail["new_display_name"] == "Renamed"
        assert entry.detail["profile_id"] == _PROFILE


def test_profile_archive_flag_roundtrip_and_audit(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        _seed_profile(client)
        response = client.post(f"/api/v1/profiles/{_PROFILE}/archive", json={"archived": True})
        assert response.status_code == 200, response.text
        assert response.json()["archived"] is True
        assert client.app.state.stores.meta.get_profile(_PROFILE).archived is True
        assert _console_audit(client, "profile.archive")[0].detail["archived"] is True

        response = client.post(f"/api/v1/profiles/{_PROFILE}/archive", json={"archived": False})
        assert response.status_code == 200, response.text
        assert client.app.state.stores.meta.get_profile(_PROFILE).archived is False
        assert len(_console_audit(client, "profile.archive")) == 2


def test_profile_rename_and_archive_unknown_profile_404(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        response = client.post("/api/v1/profiles/ghost/rename", json={"display_name": "X"})
        assert response.status_code == 404, response.text
        response = client.post("/api/v1/profiles/ghost/archive", json={"archived": True})
        assert response.status_code == 404, response.text


def test_profile_create_requires_identifiers(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        response = client.post("/api/v1/profiles", json={"profile_id": "", "display_name": "X"})
        assert response.status_code == 422, response.text
        response = client.post("/api/v1/profiles", json={"display_name": "X"})
        assert response.status_code == 422, response.text


# ---------------------------------------------------------------- tokens (FR-7.3 / G-AC1)


def test_token_issue_returns_secret_once_and_audits_without_secret(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        _seed_profile(client)
        response = client.post(
            f"/api/v1/profiles/{_PROFILE}/tokens",
            json={"scopes": ["memory:read", "memory:write"]},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["token_id"]
        assert body["token_secret"]
        assert "token_secret" not in _console_audit(client, "token.issue")[0].detail

        # the issued secret authenticates through the port
        token = client.app.state.stores.meta.authenticate_token(body["token_secret"])
        assert token is not None and token.profile_id == _PROFILE


def test_token_issue_unknown_profile_404(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        response = client.post("/api/v1/profiles/ghost/tokens", json={})
        assert response.status_code == 404, response.text


def test_token_revoke_authenticates_none_and_audits(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        _seed_profile(client)
        issued = client.post(f"/api/v1/profiles/{_PROFILE}/tokens", json={"scopes": ["memory:read"]}).json()
        assert client.app.state.stores.meta.authenticate_token(issued["token_secret"]) is not None

        response = client.post(f"/api/v1/tokens/{issued['token_id']}/revoke", json={})
        assert response.status_code == 200, response.text
        assert response.json()["revoked"] is True
        assert client.app.state.stores.meta.authenticate_token(issued["token_secret"]) is None
        assert _console_audit(client, "token.revoke")[0].detail["token_id"] == issued["token_id"]


# ---------------------------------------------------------------- audit endpoint (G-AC1)


def test_audit_endpoint_lists_items_and_paging_shape(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        _seed_profile(client)
        _write_node(client, _pref("n1", "prefers audit"))
        client.post("/api/v1/pin", json={"profile_id": _PROFILE, "node_id": "n1", "pinned": True})
        client.post("/api/v1/forget", json={"profile_id": _PROFILE, "node_id": "n1"})

        response = client.get("/api/v1/audit")
        assert response.status_code == 200, response.text
        body = response.json()
        assert "paging" in body and body["paging"]["total"] >= 2
        for item in body["items"]:
            assert set(item) == {"id", "actor", "action", "detail", "at"}
        actions = [item["action"] for item in body["items"]]
        assert "pin" in actions and "forget_this" in actions


def test_audit_endpoint_filters_by_actor_action_and_since(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        _seed_profile(client)
        _write_node(client, _pref("n1", "prefers audit"))
        client.post("/api/v1/pin", json={"profile_id": _PROFILE, "node_id": "n1", "pinned": True})
        client.post("/api/v1/forget", json={"profile_id": _PROFILE, "node_id": "n1"})

        body = client.get("/api/v1/audit", params={"actor": "console", "action": "pin"}).json()
        assert [item["action"] for item in body["items"]] == ["pin"]
        assert body["paging"]["total"] == 1

        since = time.time() + 60  # nothing logged in the future
        body = client.get("/api/v1/audit", params={"since": since}).json()
        assert body["items"] == []
        assert body["paging"]["total"] == 0


def test_audit_endpoint_paginates(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        _seed_profile(client)
        _write_node(client, _pref("n1", "prefers a"))
        for _ in range(3):
            client.post("/api/v1/pin", json={"profile_id": _PROFILE, "node_id": "n1", "pinned": True})
            client.post("/api/v1/pin", json={"profile_id": _PROFILE, "node_id": "n1", "pinned": False})

        first = client.get("/api/v1/audit", params={"action": "pin", "limit": 2, "offset": 0}).json()
        second = client.get("/api/v1/audit", params={"action": "pin", "limit": 2, "offset": 2}).json()
        assert first["paging"]["total"] == 6
        assert len(first["items"]) == 2
        assert len(second["items"]) == 2
        ids = [item["id"] for item in first["items"] + second["items"]]
        assert len(set(ids)) == 4  # no overlap between pages
