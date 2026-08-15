"""Daemon memory surface (PRD-03 T4): the six /memory HTTP endpoints over the
retrieval engine + storage ports, driven through a real embedded boot whose
``embed`` layer is the deterministic synthetic driver.

Covers the task AC-T4-1..AC-T4-8: recall ranking with the anti-dilution gate
(AC-2 shape), honest empty (FR-3.13), conflict pairing (AC-3), usage events
(FR-3.7), as_of point-in-time replay (AC-6), remember idempotent pinning with
user provenance (FR-3.1), audit / timeline / export read shapes, forget_this
GDPR deletion (chunk / node tombstone / entity), validation failures, and the
HybridRetriever close() lifecycle seam the daemon owns.

Cue extraction is deterministic and offline; the summary rule that matters here
is that backticked identifiers always extract as entities and plain lowercase
English never does, so the tests drive entity filters with backticked terms.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from _identity_helpers import attach_token
from fastapi.testclient import TestClient

from mnemoseed.daemon.app import create_app
from mnemoseed.schema.graph import GraphNode, NodeType
from mnemoseed.schema.stamp import ChunkStamp, CognitiveTier, Cues, Provenance
from mnemoseed.storage.drivers import lancedb_embedded, sqlite_graph, sqlite_meta
from mnemoseed.storage.drivers.synthetic_embedder import SyntheticEmbedder
from mnemoseed.storage.ports import ChunkFilter, Page
from mnemoseed.storage.registry import (
    EMBED_DRIVERS,
    GRAPH_DRIVERS,
    META_DRIVERS,
    VECTOR_DRIVERS,
    register,
)

_SESSION = "sess-mem-1"
_PROFILE = "prof-main"

# A constraint whose text is reused verbatim as the recall query: the synthetic
# embedder maps identical text to identical vectors (dense cosine ~1.0), so the
# exact match scores far above same-entity noise and must rank first.
_CONSTRAINT_TEXT = "`Mnx` 关键约束：提交前必须跑完整测试"


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


def _memory_config_toml(tmp_path: Path) -> Path:
    # as_posix(): Windows backslashes are invalid escapes in TOML strings
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
def _client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    """Boot the real daemon, finish setup, and attach the profile token to the
    default headers so the suite keeps asserting through the HTTP surface."""
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    monkeypatch.setattr("mnemoseed.config.CONFIG_PATH", _memory_config_toml(tmp_path))
    monkeypatch.setattr("mnemoseed.dream.snapshot.CONFIG_DIR", tmp_path)
    client = TestClient(create_app())
    with client:
        attach_token(client)
        yield client


def _write_chunk(
    client: TestClient,
    *,
    chunk_id: str,
    text: str,
    entities: tuple[str, ...] = (),
    profile_id: str = _PROFILE,
    decay: float = 1.0,
    ingested_at: float | None = None,
    turn_start: int = 0,
) -> str:
    """Seed one chunk through the daemon's own stores on the portal thread."""
    stores = client.app.state.stores
    now = time.time() if ingested_at is None else ingested_at
    stamp = ChunkStamp(
        chunk_id=chunk_id,
        profile_id=profile_id,
        text=text,
        cognitive_tier=CognitiveTier.TIER_1,
        model_id="test-model",
        cues=Cues(entities=list(entities)),
        provenance=Provenance(
            asserted_by="test-agent",
            session_id=_SESSION,
            source="seed",
            asserted_at=now,
        ),
        decay_weight=decay,
        score=0.0,
        ingested_at=now,
        turn_start=turn_start,
        turn_end=turn_start,
    )
    embedded = stores.embed.embed(text)
    client.portal.call(stores.vector.upsert_chunk, stamp, embedded.dense, embedded.sparse)
    return chunk_id


def _pref(
    node_id: str,
    statement: str,
    *,
    entities: tuple[str, ...] = ("Fmt",),
    version: int = 1,
    valid_from: float | None = None,
    conflict_flag: bool = False,
    conflict_group: str | None = None,
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
            asserted_at=(valid_from if valid_from is not None else now - 1.0),
        ),
        version=version,
        valid_from=valid_from if valid_from is not None else now - 100.0,
        updated_at=now,
        conflict_flag=conflict_flag,
        conflict_group=conflict_group,
    )


def _write_node(client: TestClient, node: GraphNode) -> str:
    client.portal.call(client.app.state.stores.graph.upsert_node, node)
    return node.node_id


def _raw_chunk(client: TestClient, chunk_id: str) -> dict[str, Any]:
    """Raw lancedb row (the public ports deliberately hide usage counters)."""
    from mnemoseed.storage.drivers.lancedb_embedded import _escape

    store = client.app.state.stores.vector
    rows = store._table.search().where(f"chunk_id = {_escape(chunk_id)}").limit(1).to_list()
    return rows[0] if rows else {}


def _chunks(client: TestClient) -> list[ChunkStamp]:
    return client.app.state.stores.vector.list_chunks(ChunkFilter(profile_id=_PROFILE), Page(limit=100)).items


def _graph_path(tmp_path: Path) -> Path:
    return tmp_path / "cortex.db"


def _graph_nodes(path: Path) -> int:
    """Read the graph through a connection bound to the CURRENT (test) thread."""
    driver = sqlite_graph.SqliteGraphDriver(path=path)
    try:
        from mnemoseed.storage.ports import NodeFilter

        return driver.list_nodes(NodeFilter(profile_id=_PROFILE), Page(limit=10)).total
    finally:
        asyncio.run(driver.close())


# ---------------------------------------------------------------- recall (AC-T4-1..4)


def _recall(client: TestClient, query: str, **over: Any) -> dict[str, Any]:
    body = {"profile_id": _PROFILE, "query": query}
    body.update(over)
    response = client.post("/memory/recall", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def test_recall_ranks_exact_constraint_first_within_dropped_count(tmp_path, monkeypatch) -> None:
    """AC-2 / AC-T4-1: 1 key constraint + same-entity noise -> <=5 entries,
    the constraint inside, and the dropped tail is reported, never silent."""
    with _client(tmp_path, monkeypatch) as client:
        _write_chunk(client, chunk_id="c-constraint", text=_CONSTRAINT_TEXT, entities=("Mnx",))
        for index in range(30):
            _write_chunk(
                client,
                chunk_id=f"noise-{index:02d}",
                text=f"noise subject {index:02d} with `Mnx` entity",
                entities=("Mnx",),
            )

        body = _recall(client, _CONSTRAINT_TEXT)

        entries = body["memory"]["entries"]
        assert 1 <= len(entries) <= 5
        assert entries[0]["text"] == _CONSTRAINT_TEXT
        # The vector track caps its candidate pool at HybridConfig.vector_top_k,
        # so the honest accounting is 20 searched, 5 admitted, 15 dropped.
        coverage = body["memory"]["coverage"]
        assert coverage["profile_chunks"] == 31
        assert coverage["pool_size"] == 20
        assert body["memory"]["dropped_count"] == 20 - len(entries)


def test_recall_honest_empty_when_nothing_qualifies(tmp_path, monkeypatch) -> None:
    """FR-3.13 / AC-T4-2: no qualifying candidates -> explicit empty package with
    a coverage self-report, never padded with junk."""
    with _client(tmp_path, monkeypatch) as client:
        body = _recall(client, "this has nothing in the profile")

        assert body["memory"]["entries"] == []
        assert body["memory"]["dropped_count"] == 0
        coverage = body["memory"]["coverage"]
        assert coverage["pool_size"] == 0
        assert coverage["profile_chunks"] == 0
        assert coverage["vector_hits"] == 0
        assert coverage["graph_hits"] == 0


def test_recall_conflict_pair_returns_atomically_with_marker(tmp_path, monkeypatch) -> None:
    """AC-3 / AC-T4-3: two conflicting preferences come back together carrying
    the conflict_pair marker, never silently resolved to one side."""
    with _client(tmp_path, monkeypatch) as client:
        _write_node(
            client,
            _pref("cg-a", "use spaces", entities=("Mnx",), conflict_flag=True, conflict_group="cg-1"),
        )
        _write_node(
            client,
            _pref("cg-b", "use tabs", entities=("Mnx",), conflict_flag=True, conflict_group="cg-1"),
        )

        body = _recall(client, "`Mnx` 偏好")

        entries = body["memory"]["entries"]
        graph_entries = [entry for entry in entries if entry["kind"] == "graph"]
        assert {entry["id"] for entry in graph_entries} == {"cg-a", "cg-b"}
        for entry in graph_entries:
            assert "conflict_pair" in entry["flags"]
            assert entry["conflict_group"] == "cg-1"


def test_recall_records_usage_event_on_chunk_hit(tmp_path, monkeypatch) -> None:
    """FR-3.7 / AC-T4-4: a recall hit bumps the chunk's usage counters (hit_count
    +1, last_hit_at refreshed) through the update_chunk_state seam."""
    with _client(tmp_path, monkeypatch) as client:
        _write_chunk(client, chunk_id="c-used", text=_CONSTRAINT_TEXT, entities=("Mnx",))

        assert _raw_chunk(client, "c-used").get("hit_count", 0) == 0
        _recall(client, _CONSTRAINT_TEXT)

        raw = _raw_chunk(client, "c-used")
        assert raw["hit_count"] == 1
        assert raw.get("last_hit_at") is not None


def test_recall_reinforces_hit_chunk_fr_4_2(tmp_path, monkeypatch) -> None:
    """FR-4.2 event side over the wire: a recall hit refreshes last_reinforced
    and rebounds the decay_weight (bounded at 1.0) through the Reinforcer, while
    the hit_count still increments (FR-3.7 preserved)."""
    with _client(tmp_path, monkeypatch) as client:
        _write_chunk(client, chunk_id="c-rf", text=_CONSTRAINT_TEXT, entities=("Mnx",), decay=0.7)

        _recall(client, _CONSTRAINT_TEXT)

        raw = _raw_chunk(client, "c-rf")
        assert raw["decay_weight"] == pytest.approx(0.8)
        assert raw.get("last_reinforced") is not None
        assert raw["hit_count"] == 1


def test_recall_reinforces_hit_graph_node_fr_4_2(tmp_path, monkeypatch) -> None:
    """FR-4.2 over the graph track: a recalled node gets last_reinforced
    refreshed and a bounded rebound; below-floor nodes only count the usage."""
    with _client(tmp_path, monkeypatch) as client:
        node = _pref("g-rf", "prefers dark mode", entities=("Mnx",))
        node.decay_weight = 0.7
        _write_node(client, node)

        _recall(client, "`Mnx` 偏好")

        stored = client.app.state.stores.graph.get_node("g-rf")
        assert stored is not None
        assert stored.decay_weight == pytest.approx(0.8)
        assert stored.last_reinforced is not None
        assert stored.hit_count == 1


def test_recall_as_of_replays_old_version_fact(tmp_path, monkeypatch) -> None:
    """AC-6 / FR-3.9: an as_of in the past returns the fact as it stood then,
    while the current recall returns the present value."""
    with _client(tmp_path, monkeypatch) as client:
        now = time.time()
        _write_node(client, _pref("fmt-v", "prefers tabs", entities=("Fmt",), valid_from=now - 200.0))
        take_over = now
        v2 = _pref("fmt-v", "prefers spaces", entities=("Fmt",), version=2, valid_from=take_over)
        graph_store = client.app.state.stores.graph
        # portal.call is positional-only; the keyword-only invalidate_at needs a closure
        client.portal.call(lambda node, at: graph_store.append_version(node, invalidate_at=at), v2, take_over)

        past = _recall(client, "`Fmt` 格式偏好", as_of=take_over - 1.0)
        assert past["memory"]["entries"][0]["text"] == "prefers tabs"

        future = _recall(client, "`Fmt` 格式偏好", as_of=take_over + 1.0)
        assert future["memory"]["entries"][0]["text"] == "prefers spaces"

        current = _recall(client, "`Fmt` 格式偏好")
        assert current["memory"]["entries"][0]["text"] == "prefers spaces"


def test_recall_reports_pending_consolidation_and_fresh_evidence(tmp_path, monkeypatch) -> None:
    """AC-T4-5 / FR-3.8: the Freshness Guard fires on the wire — a graph
    preference with unconsolidated fragments returns pending_consolidation with
    the fresh evidence attached, plus the honest pending_marked count."""
    from mnemoseed.storage.ports import TurnRange

    with _client(tmp_path, monkeypatch) as client:
        # a settled watermark creates a "before consolidation" boundary...
        client.portal.call(
            client.app.state.stores.meta.advance_watermark,
            _PROFILE,
            TurnRange(start=0, end=5),
        )
        # ...a graph preference sharing an entity with...
        _write_node(client, _pref("fresh-node", "consolidate me later", entities=("Gh",)))
        # ...a chunk captured AFTER the watermark (turn_start > watermark.end)
        _write_chunk(
            client,
            chunk_id="fresh-evidence",
            text="fresh unconsolidated snippet",
            entities=("Gh",),
            turn_start=6,
        )

        body = _recall(client, "`Gh` 偏好")

        graph_entries = [entry for entry in body["memory"]["entries"] if entry["kind"] == "graph"]
        assert graph_entries
        assert "pending_consolidation" in graph_entries[0]["flags"]
        assert "fresh_evidence" in graph_entries[0]["flags"]
        assert graph_entries[0]["recent_evidence"]
        assert graph_entries[0]["recent_evidence"][0].startswith("fresh unconsolidated snippet")
        assert body["memory"]["coverage"]["pending_marked"] == 1
        assert body["memory"]["coverage"]["fresh_evidence_chunks"] == 1


# ---------------------------------------------------------------- remember


def _remember(client: TestClient, text: str, **over: Any) -> dict[str, Any]:
    body = {"profile_id": _PROFILE, "text": text}
    body.update(over)
    response = client.post("/memory/remember", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def test_remember_writes_explicit_pin_with_user_provenance(tmp_path, monkeypatch) -> None:
    """FR-3.1 remember: a user-origin pin lands as a real chunk carrying
    asserted_by="user" and the explicit-pin source marker, plus an audit row."""
    with _client(tmp_path, monkeypatch) as client:
        body = _remember(client, "用户决定统一用 pnpm 管理依赖")

        assert body["outcome"] == "new_chunk"
        assert body["chunk_id"]
        stored = client.app.state.stores.vector.get_chunk(body["chunk_id"])
        assert stored is not None
        assert stored.provenance.asserted_by == "user"
        assert stored.provenance.source == "memory.remember"
        assert len(_chunks(client)) == 1

        entries = client.app.state.stores.meta.audit_query(
            __import__("mnemoseed.storage.ports", fromlist=["AuditFilter"]).AuditFilter(), Page(limit=100)
        ).items
        assert any(entry.action == "remember" for entry in entries)


def test_remember_identical_repin_reinforces_without_duplicate(tmp_path, monkeypatch) -> None:
    """FR-1.8-backed idempotency: re-pinning the identical text reinforces the
    existing chunk (same id) — no duplicate shard is ever created."""
    with _client(tmp_path, monkeypatch) as client:
        first = _remember(client, "用户决定统一用 pnpm 管理依赖")
        assert first["outcome"] == "new_chunk"

        second = _remember(client, "用户决定统一用 pnpm 管理依赖")
        assert second["outcome"] == "reinforced"
        assert second["chunk_id"] == first["chunk_id"]
        assert len(_chunks(client)) == 1


# ---------------------------------------------------------------- audit / timeline


def _audit(client: TestClient, **target: Any) -> dict[str, Any]:
    body = {"profile_id": _PROFILE}
    body.update(target)
    response = client.post("/memory/audit", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def test_audit_chunk_returns_provenance_and_relevant_audit(tmp_path, monkeypatch) -> None:
    """AC-4 / AC-T4-5: a chunk's provenance is audit-able end-to-end."""
    with _client(tmp_path, monkeypatch) as client:
        remembered = _remember(client, "用户以后都用 vite 打包")
        chunk_id = remembered["chunk_id"]

        body = _audit(client, chunk_id=chunk_id)

        assert body["target"] == {"type": "chunk", "id": chunk_id}
        assert body["provenance"]["asserted_by"] == "user"
        assert body["versions"] == []
        assert body["audit"]
        assert any(
            entry["action"] == "remember" and entry["detail"].get("chunk_id") == chunk_id
            for entry in body["audit"]
        )


def test_audit_node_returns_full_version_chain(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        now = time.time()
        _write_node(client, _pref("aud-v", "v1 statement", entities=("Fmt",), valid_from=now - 200.0))
        take_over = now
        graph_store = client.app.state.stores.graph
        client.portal.call(
            lambda node, at: graph_store.append_version(node, invalidate_at=at),
            _pref("aud-v", "v2 statement", entities=("Fmt",), version=2, valid_from=take_over),
            take_over,
        )

        body = _audit(client, node_id="aud-v")

        assert body["target"] == {"type": "node", "id": "aud-v"}
        assert [version["version"] for version in body["versions"]] == [1, 2]


def test_audit_unknown_target_returns_404(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        response = client.post("/memory/audit", json={"profile_id": _PROFILE, "chunk_id": "nope"})
        assert response.status_code == 404


def test_timeline_node_replays_versions(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        now = time.time()
        _write_node(client, _pref("tl-v", "v1", entities=("Fmt",), valid_from=now - 100.0))
        client.portal.call(
            client.app.state.stores.graph.append_version,
            _pref("tl-v", "v2", entities=("Fmt",), version=2, valid_from=now),
        )

        response = client.post("/memory/timeline", json={"profile_id": _PROFILE, "node_id": "tl-v"})
        assert response.status_code == 200
        body = response.json()
        assert [event["version"] for event in body["events"]] == [1, 2]
        assert all(event["summary"] for event in body["events"])


def test_timeline_profile_wide_recent_order(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        _write_chunk(client, chunk_id="tl-chunk", text="timeline chunk text", entities=("Mnx",))
        _write_node(client, _pref("tl-node", "node summary statement", entities=("Mnx",)))

        response = client.post("/memory/timeline", json={"profile_id": _PROFILE})
        assert response.status_code == 200
        events = response.json()["events"]
        kinds = {event["kind"] for event in events}
        assert kinds == {"chunk", "node"}
        assert {event["id"] for event in events} == {"tl-chunk", "tl-node"}
        # the events come back most-recent-first
        assert events == sorted(events, key=lambda event: event["when"], reverse=True)


# ---------------------------------------------------------------- export


def test_export_profile_dump_stable_shape(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        _write_chunk(client, chunk_id="ex-1", text="export one", entities=("Mnx",))
        _write_chunk(client, chunk_id="ex-2", text="export two", entities=("Mnx",))
        _write_node(client, _pref("ex-node", "exported fact", entities=("Fmt",)))

        response = client.post("/memory/export", json={"profile_id": _PROFILE})
        assert response.status_code == 200
        body = response.json()

        assert body["schema"] == "mnemoseed.memory.export/1"
        assert body["profile_id"] == _PROFILE
        assert len(body["chunks"]) == 2
        assert len(body["nodes"]) == 1
        assert body["paging"]["chunk_total"] == 2
        assert body["paging"]["node_total"] == 1
        assert body["nodes"][0]["provenance"]["asserted_by"] == "dream-engine"


# ---------------------------------------------------------------- forget_this


def _forget(client: TestClient, **target: Any) -> dict[str, Any]:
    body = {"profile_id": _PROFILE}
    body.update(target)
    response = client.post("/memory/forget_this", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def test_forget_this_by_chunk_id_deletes_and_audits(tmp_path, monkeypatch) -> None:
    """GDPR / AC-T4-6: chunk deletion is exact and leaves an audit trail."""
    with _client(tmp_path, monkeypatch) as client:
        remembered = _remember(client, "要删除的临时记忆")
        chunk_id = remembered["chunk_id"]

        body = _forget(client, chunk_id=chunk_id)

        assert body["removed"] == {"chunks": [chunk_id], "nodes": []}
        assert client.app.state.stores.vector.get_chunk(chunk_id) is None
        entries = client.app.state.stores.meta.audit_query(
            __import__("mnemoseed.storage.ports", fromlist=["AuditFilter"]).AuditFilter(), Page(limit=100)
        ).items
        assert any(
            entry.action == "forget_this" and chunk_id in entry.detail.get("chunks", []) for entry in entries
        )


def test_forget_this_by_node_id_tombstones_keeps_history(tmp_path, monkeypatch) -> None:
    """AC-T4-7: node deletion is a tombstone — invisible to reads now, but the
    version chain survives for audit and as_of historical replay."""
    from mnemoseed.storage.ports import NodeFilter

    with _client(tmp_path, monkeypatch) as client:
        # valid_from well in the past so the tombstone closes a node whose
        # pre-deletion window covers a verifiable historical as_of.
        _write_node(
            client,
            _pref("fgt-node", "doomed fact", entities=("Fmt",), valid_from=time.time() - 1000.0),
        )

        body = _forget(client, node_id="fgt-node")

        assert body["removed"] == {"chunks": [], "nodes": ["fgt-node"]}
        graph = client.app.state.stores.graph
        assert graph.get_node("fgt-node") is None
        assert "fgt-node" not in {
            node.node_id for node in graph.as_of(time.time() + 1.0, NodeFilter(profile_id=_PROFILE))
        }
        deleted_at = graph.versions("fgt-node")[0].valid_to
        assert "fgt-node" in {
            node.node_id for node in graph.as_of(deleted_at - 1.0, NodeFilter(profile_id=_PROFILE))
        }
        versions = graph.versions("fgt-node")
        assert len(versions) == 1
        assert "deleted" in {event.action for event in versions[0].provenance.history}


def test_forget_this_by_entity_removes_matching_chunks_and_nodes(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        _write_chunk(client, chunk_id="e-1", text="entity chunk one", entities=("Mnx",))
        _write_chunk(client, chunk_id="e-2", text="entity chunk two", entities=("Mnx",))
        _write_node(client, _pref("e-node", "entity fact", entities=("Mnx",)))

        body = _forget(client, entity="Mnx")

        assert set(body["removed"]["chunks"]) == {"e-1", "e-2"}
        assert body["removed"]["nodes"] == ["e-node"]
        assert client.app.state.stores.vector.get_chunk("e-1") is None
        assert client.app.state.stores.graph.get_node("e-node") is None


def test_forget_this_unknown_target_returns_404(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        response = client.post(
            "/memory/forget_this", json={"profile_id": _PROFILE, "chunk_id": "does-not-exist"}
        )
        assert response.status_code == 404


# ---------------------------------------------------------------- validation


def test_memory_endpoints_reject_blank_profile_id(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        assert client.post("/memory/recall", json={"profile_id": "   ", "query": "x"}).status_code == 422
        assert client.post("/memory/remember", json={"profile_id": "", "text": "x"}).status_code == 422
        assert client.post("/memory/recall", json={"query": "x"}).status_code == 422


def test_memory_remember_rejects_blank_text(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        response = client.post("/memory/remember", json={"profile_id": _PROFILE, "text": "   "})
        assert response.status_code == 422


def test_memory_audit_requires_a_target(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        response = client.post("/memory/audit", json={"profile_id": _PROFILE})
        assert response.status_code == 422


def test_memory_forget_requires_a_target(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        response = client.post("/memory/forget_this", json={"profile_id": _PROFILE})
        assert response.status_code == 422


# ---------------------------------------------------------------- lifecycle


def test_daemon_closes_hybrid_retriever_on_shutdown(tmp_path, monkeypatch) -> None:
    """T4 lifecycle fix: the daemon owns the HybridRetriever and closes it in
    lifespan teardown, so the track-2 executor never outlives the process."""
    from mnemoseed.retrieve.hybrid import HybridRetriever

    with _client(tmp_path, monkeypatch) as client:
        memory = client.app.state.memory
        retriever = memory.retriever
        assert isinstance(retriever, HybridRetriever)
        assert hasattr(retriever, "close")
        # a real recall path spawns the executor's worker threads lazily
        _recall(client, "warm up the tracks")
        assert retriever._executor._shutdown is False

    # lifespan teardown ran: the executor refuses any new work
    assert retriever._executor._shutdown is True
    with pytest.raises(RuntimeError):
        retriever._executor.submit(len)
