"""Console REST surface (PRD-07 T1): /api/v1 read core + M1 review writes.

Drives the REAL daemon app (embedded preset, synthetic embedder) through
TestClient, seeding through the daemon's own stores on the portal thread so the
tests assert the wire contract only -- never internal helpers. Covers, per
FR/AC:

- FR-7.2 / /api/v1/status dashboard shape (daemon drivers+health, per-profile
  dream state / pool / counts / token usage / reconcile queue).
- FR-7.4 / Memory Browser: chunk + node paging, driver filters (entity /
  consolidated / needs_reconcile), client-side overlay filters (project / host /
  tier / decay ceiling / reconcile / conflict), pagination edges.
- FR-7.5 / dossiers: verbatim channel, full provenance history, version chain,
  weights, flags.
- FR-7.6 / Dream panel + writes: trigger status, run history, ``dream_once``
  and the persisted ``auto_trigger`` toggle, both audit-logged.
- FR-7.1 / NFR-7.1 auth gate: localhost implicit trust; non-localhost needs the
  admin token (Bearer or X-Admin-Token); unconfigured token refuses remote.
- The /console SPA shell behind the same gate (served from its static dir).
"""

from __future__ import annotations

import json
import secrets
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mnemoseed.capture import PoolEvent, PoolEventKind
from mnemoseed.daemon.app import create_app
from mnemoseed.schema.graph import GraphNode, NodeType
from mnemoseed.schema.stamp import ChunkStamp, CognitiveTier, Cues, Provenance, ProvenanceEvent
from mnemoseed.storage.drivers import lancedb_embedded, sqlite_graph, sqlite_meta
from mnemoseed.storage.drivers.synthetic_embedder import SyntheticEmbedder
from mnemoseed.storage.ports import (
    AuditFilter,
    GraphFlag,
    NodeFilter,
    Page,
    StoredProfile,
    TurnRange,
)
from mnemoseed.storage.registry import (
    EMBED_DRIVERS,
    GRAPH_DRIVERS,
    META_DRIVERS,
    VECTOR_DRIVERS,
    register,
)

_SESSION = "sess-console-1"
_PROFILE = "prof-console"
_ADMIN_TOKEN = "console-test-token"


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


def _config_toml(tmp_path: Path, *, dream: bool = False) -> Path:
    # as_posix(): Windows backslashes are invalid escapes in TOML strings
    cfg = tmp_path / "config.toml"
    body = (
        'preset = "embedded"\n'
        f'[storage.vector]\nuri = "{(tmp_path / "chunks.lance").as_posix()}"\ndimensions = 64\n'
        f'[storage.graph]\npath = "{(tmp_path / "cortex.db").as_posix()}"\n'
        f'[storage.meta]\npath = "{(tmp_path / "meta.db").as_posix()}"\n'
        f'[storage.embed]\ndriver = "synthetic"\ndimension = 64\n'
    )
    if dream:
        body += "[dream]\ntoken_budget_usd = 5.0\n"
    cfg.write_text(body, encoding="utf-8")
    return cfg


def _client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    cfg: Path | None = None,
    token: str | None = _ADMIN_TOKEN,
    loopback: bool = True,
) -> TestClient:
    """Boot the real daemon with a throwaway config + the console token env.

    Defaults to a loopback source (the live console is local-first); the
    remote-auth tests opt out with ``loopback=False``.
    """
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    monkeypatch.setattr("mnemoseed.config.CONFIG_PATH", cfg if cfg is not None else _config_toml(tmp_path))
    monkeypatch.setattr("mnemoseed.dream.snapshot.CONFIG_DIR", tmp_path)
    if token is None:
        monkeypatch.delenv("MNEMOSEED_CONSOLE_ADMIN_TOKEN", raising=False)
    else:
        monkeypatch.setenv("MNEMOSEED_CONSOLE_ADMIN_TOKEN", token)
    kwargs = {"client": ("127.0.0.1", 50057)} if loopback else {}
    return TestClient(create_app(), **kwargs)


def _seed_profile(client: TestClient, profile_id: str = _PROFILE) -> None:
    client.portal.call(client.app.state.stores.meta.upsert_profile, StoredProfile(profile_id=profile_id))


def _write_chunk(
    client: TestClient,
    *,
    chunk_id: str,
    text: str,
    entities: tuple[str, ...] = (),
    profile_id: str = _PROFILE,
    decay: float = 1.0,
    ingested_at: float | None = None,
    project: str | None = None,
    host: str | None = None,
    tier: CognitiveTier = CognitiveTier.TIER_1,
    consolidated: bool = False,
) -> str:
    now = time.time() if ingested_at is None else ingested_at
    stamp = ChunkStamp(
        chunk_id=chunk_id,
        profile_id=profile_id,
        text=text,
        cognitive_tier=tier,
        model_id="test-model",
        cues=Cues(entities=list(entities), project=project, host=host),
        provenance=Provenance(
            asserted_by="test-agent",
            session_id=_SESSION,
            source="seed",
            asserted_at=now,
            history=[
                ProvenanceEvent(at=now - 1.0, action="created", actor="test", detail={"round": 0}),
            ],
        ),
        decay_weight=decay,
        score=0.0,
        consolidated=consolidated,
        ingested_at=now,
        turn_start=0,
        turn_end=1,
    )
    embedded = client.app.state.stores.embed.embed(text)
    client.portal.call(client.app.state.stores.vector.upsert_chunk, stamp, embedded.dense, embedded.sparse)
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
    needs_reconcile: bool = False,
    pending_consolidation: bool = False,
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
        needs_reconcile=needs_reconcile,
        pending_consolidation=pending_consolidation,
    )


def _write_node(client: TestClient, node: GraphNode) -> str:
    client.portal.call(client.app.state.stores.graph.upsert_node, node)
    return node.node_id


def _fire_dream_event(client: TestClient, profile_id: str = _PROFILE) -> None:
    """Sink one pool decision into the trigger (manual-first: pending_manual)."""
    client.app.state.dream.handle_event(
        PoolEvent(
            kind=PoolEventKind.DREAM_TRIGGER,
            profile_id=profile_id,
            turn_range=TurnRange(0, 1),
            balance=5.0,
            fired_at=time.time(),
        )
    )


def _audit(client: TestClient, action: str) -> list[object]:
    return client.app.state.stores.meta.audit_query(
        AuditFilter(actor="console", action=action), Page(limit=10)
    ).items


# ---------------------------------------------------------------- dashboard (FR-7.2)


def test_status_dashboard_shapes_daemon_and_profile_row(tmp_path, monkeypatch) -> None:
    """FR-7.2: /api/v1/status aggregates daemon health + one row per known
    profile with dream state, pool, counts, and the monthly token ledger."""
    with _client(tmp_path, monkeypatch) as client:
        _seed_profile(client)
        _write_chunk(client, chunk_id="c1", text="hello world", entities=("world",))
        _write_node(client, _pref("n1", "prefers tabs"))

        response = client.get("/api/v1/status")
        assert response.status_code == 200
        body = response.json()

        assert body["daemon"]["version"]
        assert body["daemon"]["preset"] == "embedded"
        assert body["daemon"]["drivers"]["vector"]
        assert body["daemon"]["gate"]["ok"] is True

        row = next(p for p in body["profiles"] if p["profile_id"] == _PROFILE)
        assert row["dream"]["state"] in {"idle", "accumulating"}
        assert row["pool"]["balance"] == 0.0
        assert row["pool"]["watermark"] is None
        assert row["counts"]["chunks"] == 1
        assert row["counts"]["nodes"] == 1
        assert row["counts"]["needs_reconcile"] == 0
        assert row["counts"]["pending_consolidation"] == 0
        assert row["tokens"]["today"] == 0
        assert row["tokens"]["this_week"] == 0
        assert row["tokens"]["ledger"]["year_month"]
        assert row["tokens"]["ledger"]["used_usd"] == 0.0
        assert row["tokens"]["ledger"]["budget_usd"] == 5.0
        assert row["tokens"]["ledger"]["remaining_usd"] == 5.0


def test_status_dashboard_counts_reconcile_and_pending(tmp_path, monkeypatch) -> None:
    """FR-7.2: needs_reconcile and pending_consolidation surface on the
    dashboard (chunk flag via the update_chunk_state seam, node flags direct)."""
    with _client(tmp_path, monkeypatch) as client:
        _seed_profile(client)
        _write_chunk(client, chunk_id="ok-1", text="clean")
        chunk_id = _write_chunk(client, chunk_id="rec-1", text="flagged")
        # positional args only: anyio portal.call does not forward keywords
        client.portal.call(client.app.state.stores.vector.update_chunk_state, [chunk_id], None, True)
        _write_node(client, _pref("pc-1", "needs reconcile", needs_reconcile=True))
        _write_node(client, _pref("pc-2", "needs consolidation", pending_consolidation=True))

        row = next(p for p in client.get("/api/v1/status").json()["profiles"] if p["profile_id"] == _PROFILE)
        assert row["counts"]["needs_reconcile"] == 2  # 1 chunk + 1 node
        assert row["counts"]["pending_consolidation"] == 1


# ---------------------------------------------------------------- memory browse (FR-7.4)


def test_list_chunks_orders_newest_first(tmp_path, monkeypatch) -> None:
    """FR-7.4: the chunk browse page is newest-first with honest totals."""
    with _client(tmp_path, monkeypatch) as client:
        now = time.time()
        _write_chunk(client, chunk_id="c1", text="old", ingested_at=now - 300.0)
        _write_chunk(client, chunk_id="c2", text="mid", ingested_at=now - 200.0)
        _write_chunk(client, chunk_id="c3", text="new", ingested_at=now - 100.0)

        body = client.get("/api/v1/chunks", params={"profile_id": _PROFILE}).json()
        assert [i["chunk_id"] for i in body["items"]] == ["c3", "c2", "c1"]
        assert body["paging"] == {"total": 3, "offset": 0, "limit": 50}


def test_list_chunks_project_host_tier_and_decay_overlay(tmp_path, monkeypatch) -> None:
    """FR-7.4: project / host / tier / decay-ceiling filters are the console's
    own overlay over the scan (fast-path paging behaviour is exercised by the
    entity filter below)."""
    with _client(tmp_path, monkeypatch) as client:
        _write_chunk(client, chunk_id="p1", text="alpha", project="core", host="host-a", decay=0.9)
        _write_chunk(client, chunk_id="p2", text="beta", project="core", host="host-b", decay=0.4)
        _write_chunk(client, chunk_id="p3", text="gamma", project="cli", host="host-a", decay=0.7)

        by_project = client.get("/api/v1/chunks", params={"profile_id": _PROFILE, "project": "core"}).json()
        assert {i["chunk_id"] for i in by_project["items"]} == {"p1", "p2"}
        assert by_project["paging"]["total"] == 2

        by_host = client.get("/api/v1/chunks", params={"profile_id": _PROFILE, "host": "host-a"}).json()
        assert {i["chunk_id"] for i in by_host["items"]} == {"p1", "p3"}

        combined = client.get(
            "/api/v1/chunks",
            params={"profile_id": _PROFILE, "project": "core", "host": "host-a"},
        ).json()
        assert [i["chunk_id"] for i in combined["items"]] == ["p1"]

        decay_cap = client.get("/api/v1/chunks", params={"profile_id": _PROFILE, "max_decay": "0.5"}).json()
        assert [i["chunk_id"] for i in decay_cap["items"]] == ["p2"]

        tier = client.get("/api/v1/chunks", params={"profile_id": _PROFILE, "tier": "1"}).json()
        assert {i["chunk_id"] for i in tier["items"]} == {"p1", "p2", "p3"}


def test_list_chunks_entity_consolidated_and_reconcile_driver_filters(tmp_path, monkeypatch) -> None:
    """FR-7.4: entity, consolidated and needs_reconcile are pushed down to the
    vector SQL index (fast-path paging, no scan overhead)."""
    with _client(tmp_path, monkeypatch) as client:
        _write_chunk(client, chunk_id="e1", text="planet", entities=("Pluto",))
        _write_chunk(client, chunk_id="e2", text="planet", entities=("Mars",))
        _write_chunk(client, chunk_id="con-1", text="consolidated", consolidated=True)
        rec_id = _write_chunk(client, chunk_id="rec-1", text="flagged")
        # positional args only: anyio portal.call does not forward keywords
        client.portal.call(client.app.state.stores.vector.update_chunk_state, [rec_id], None, True)

        by_entity = client.get("/api/v1/chunks", params={"profile_id": _PROFILE, "entity": ["Pluto"]}).json()
        assert [i["chunk_id"] for i in by_entity["items"]] == ["e1"]

        consolidated = client.get(
            "/api/v1/chunks", params={"profile_id": _PROFILE, "consolidated": "true"}
        ).json()
        assert [i["chunk_id"] for i in consolidated["items"]] == ["con-1"]

        reconcile = client.get(
            "/api/v1/chunks", params={"profile_id": _PROFILE, "needs_reconcile": "true"}
        ).json()
        assert [i["chunk_id"] for i in reconcile["items"]] == ["rec-1"]


def test_list_chunks_pagination_edges(tmp_path, monkeypatch) -> None:
    """FR-7.4 pagination: a page beyond the end is honest-empty, never an
    error; a limit/offset window slices correctly."""
    with _client(tmp_path, monkeypatch) as client:
        for index in range(3):
            _write_chunk(client, chunk_id=f"p{index}", text=f"page {index}")

        beyond = client.get(
            "/api/v1/chunks", params={"profile_id": _PROFILE, "offset": 10, "limit": 2}
        ).json()
        assert beyond["items"] == []
        assert beyond["paging"]["total"] == 3

        window = client.get("/api/v1/chunks", params={"profile_id": _PROFILE, "offset": 1, "limit": 2}).json()
        assert len(window["items"]) == 2
        assert window["paging"]["total"] == 3


def test_list_chunks_typed_422_on_bad_filters(tmp_path, monkeypatch) -> None:
    """Typed validation: decay bounds and limit live in a closed range."""
    with _client(tmp_path, monkeypatch) as client:
        assert (
            client.get("/api/v1/chunks", params={"profile_id": _PROFILE, "min_decay": "2.0"}).status_code
            == 422
        )
        assert client.get("/api/v1/chunks", params={"profile_id": _PROFILE, "limit": "0"}).status_code == 422
        assert client.get("/api/v1/chunks", params={"profile_id": _PROFILE, "tier": "9"}).status_code == 422


def test_list_nodes_type_and_overlay_filters(tmp_path, monkeypatch) -> None:
    """FR-7.4: node browse filters by node type (driver) and by reconcile /
    pending / conflict (console overlay), newest-first."""
    with _client(tmp_path, monkeypatch) as client:
        _write_node(client, _pref("a1", "prefers tabs", entities=("Fmt",)))
        _write_node(client, _pref("a2", "prefers spaces", entities=("Fmt",), conflict_flag=True))
        _write_node(client, _pref("a3", "needs reconcile", entities=("Fmt",), needs_reconcile=True))

        all_nodes = client.get("/api/v1/nodes", params={"profile_id": _PROFILE}).json()
        assert all_nodes["paging"]["total"] == 3

        by_type = client.get(
            "/api/v1/nodes", params={"profile_id": _PROFILE, "node_type": "PREFERENCE"}
        ).json()
        assert by_type["paging"]["total"] == 3

        conflicts = client.get("/api/v1/nodes", params={"profile_id": _PROFILE, "conflict": "true"}).json()
        assert [i["node_id"] for i in conflicts["items"]] == ["a2"]

        reconcile = client.get(
            "/api/v1/nodes", params={"profile_id": _PROFILE, "needs_reconcile": "true"}
        ).json()
        assert [i["node_id"] for i in reconcile["items"]] == ["a3"]


def test_list_nodes_pagination_edges(tmp_path, monkeypatch) -> None:
    """FR-7.4 node pagination edge: empty page beyond the end, sliced window."""
    with _client(tmp_path, monkeypatch) as client:
        for index in range(3):
            _write_node(client, _pref(f"n{index}", f"pref {index}"))

        beyond = client.get("/api/v1/nodes", params={"profile_id": _PROFILE, "offset": 10}).json()
        assert beyond["items"] == []
        assert beyond["paging"]["total"] == 3

        window = client.get("/api/v1/nodes", params={"profile_id": _PROFILE, "offset": 1, "limit": 2}).json()
        assert len(window["items"]) == 2


# ---------------------------------------------------------------- dossiers (FR-7.5)


def test_get_chunk_dossier_verbatim_provenance_weights_flags(tmp_path, monkeypatch) -> None:
    """FR-7.5: a chunk dossier carries the verbatim channel, the full
    provenance history, the decay weights, and the flags (chunk usage counters
    are hidden by the vector ports, so they surface as null -- a documented
    schema boundary the console reports honestly)."""
    with _client(tmp_path, monkeypatch) as client:
        _write_chunk(
            client,
            chunk_id="c-dossier",
            text="the verbatim body",
            entities=("Body",),
            project="core",
            host="host-a",
            decay=0.42,
        )

        body = client.get("/api/v1/chunks/c-dossier", params={"profile_id": _PROFILE}).json()

        assert body["type"] == "chunk"
        assert body["content"]["verbatim"] == "the verbatim body"
        assert body["cues"]["project"] == "core"
        assert body["cues"]["host"] == "host-a"
        assert body["provenance"]["asserted_by"] == "test-agent"
        assert body["provenance"]["history"][0]["action"] == "created"
        assert body["weights"]["decay_weight"] == pytest.approx(0.42)
        assert body["weights"]["score"] == 0.0
        assert body["weights"]["confidence"] == 0.5
        assert body["weights"]["reinforce_count"] is None  # ports hide chunk counters
        assert body["flags"]["consolidated"] is False
        assert body["flags"]["needs_reconcile"] is None
        assert body["usage"]["hit_count"] is None


def test_get_chunk_404_unknown_and_foreign_profile(tmp_path, monkeypatch) -> None:
    """FR-7.5 typed 404: an unknown id and a foreign-profile id both 404."""
    with _client(tmp_path, monkeypatch) as client:
        _write_chunk(client, chunk_id="mine-1", text="mine", profile_id="prof-other")

        unknown = client.get("/api/v1/chunks/nope", params={"profile_id": _PROFILE})
        assert unknown.status_code == 404

        foreign = client.get("/api/v1/chunks/mine-1", params={"profile_id": _PROFILE})
        assert foreign.status_code == 404


def test_get_node_dossier_version_chain_weights_flags(tmp_path, monkeypatch) -> None:
    """FR-7.5: a node dossier exposes the triple, the historical version chain
    (both revisions), the weights/flags/usage, and the timeline."""
    with _client(tmp_path, monkeypatch) as client:
        now = time.time()
        _write_node(client, _pref("fmt-v", "prefers tabs", version=1, valid_from=now - 200.0))
        _write_node(client, _pref("fmt-v", "prefers spaces", version=2, valid_from=now - 50.0))

        body = client.get("/api/v1/nodes/fmt-v", params={"profile_id": _PROFILE}).json()

        assert body["type"] == "node"
        assert body["node_type"] == "PREFERENCE"
        assert body["content"]["statement"] == "prefers spaces"
        # a preference node carries no subject/predicate/object triple -- those
        # props belong to structured node types, and the dossier reports null
        assert body["content"]["subject"] is None
        assert body["content"]["predicate"] is None
        assert body["version"]["number"] == 2
        assert body["version"]["current"] is True
        assert len(body["version_chain"]) == 2  # historical revision retained
        assert body["version_chain"][0]["props"]["statement"] == "prefers tabs"
        assert body["version_chain"][1]["props"]["statement"] == "prefers spaces"
        assert body["weights"]["confidence"] == 0.9
        assert body["weights"]["reinforce_count"] == 0
        assert body["flags"]["conflict_flag"] is False
        assert body["flags"]["pending_consolidation"] is False
        assert body["usage"]["hit_count"] == 0
        assert body["promotion_status"] == "promoted"
        assert len(body["timeline"]) == 2


def test_get_node_404_unknown(tmp_path, monkeypatch) -> None:
    """FR-7.5 typed 404 for an unknown node id."""
    with _client(tmp_path, monkeypatch) as client:
        response = client.get("/api/v1/nodes/nope", params={"profile_id": _PROFILE})
        assert response.status_code == 404


# ---------------------------------------------------------------- dream panel (FR-7.6)


def test_dream_status_endpoint_reports_trigger_and_queue(tmp_path, monkeypatch) -> None:
    """FR-7.6: the dream panel reports the live trigger state and the pending
    manual queue depth for one profile."""
    with _client(tmp_path, monkeypatch) as client:
        response = client.get("/api/v1/dream/status", params={"profile_id": _PROFILE})
        assert response.status_code == 200
        body = response.json()
        assert body["profile_id"] == _PROFILE
        assert body["state"] in {"idle", "accumulating"}
        assert body["pending_manual"] == 0

        _fire_dream_event(client)
        after = client.get("/api/v1/dream/status", params={"profile_id": _PROFILE}).json()
        assert after["pending_manual"] == 1
        assert after["queue_depth"] == 1


def test_dream_runs_empty_before_any_cycle(tmp_path, monkeypatch) -> None:
    """FR-7.6: the run history starts honest-empty."""
    with _client(tmp_path, monkeypatch) as client:
        body = client.get("/api/v1/dream/runs").json()
        assert body["runs"] == []
        assert body["paging"]["total"] == 0


def test_dream_once_launches_registers_run_and_audits(tmp_path, monkeypatch) -> None:
    """FR-7.6 write: ``dream_once`` consumes the pending manual event, launches
    a real snapshot through the daemon's trigger, registers the run, and lands
    an audit entry -- with honest 0 token/cost until a later task records
    per-run LM usage."""
    with _client(tmp_path, monkeypatch) as client:
        _seed_profile(client)
        _write_chunk(client, chunk_id="c1", text="hello world")
        _fire_dream_event(client)

        response = client.post("/api/v1/dream/once", json={"profile_id": _PROFILE})
        assert response.status_code == 200
        body = response.json()
        assert body["profile_id"] == _PROFILE
        assert body["launched"] is True

        runs = client.get("/api/v1/dream/runs").json()["runs"]
        assert len(runs) == 1
        assert runs[0]["turn_range"] == {"start": 0, "end": 1}
        assert runs[0]["tokens"] == 0  # not yet persisted per run (schema gap)
        assert runs[0]["interrupted"] is False

        entries = _audit(client, "dream_once")
        assert len(entries) == 1
        assert entries[0].detail["profile_id"] == _PROFILE
        assert entries[0].detail["launched"] is True

        # nothing left pending: a second manual trigger does not launch
        again = client.post("/api/v1/dream/once", json={"profile_id": _PROFILE}).json()
        assert again["launched"] is False


def test_dream_once_422_on_bad_body(tmp_path, monkeypatch) -> None:
    """FR-7.6 typed 422: dream_once without a profile_id is rejected."""
    with _client(tmp_path, monkeypatch) as client:
        response = client.post("/api/v1/dream/once", json={})
        assert response.status_code == 422
        assert client.post("/api/v1/dream/once", json={"profile_id": ""}).status_code == 422


def test_auto_trigger_toggle_persists_config_and_audits(tmp_path, monkeypatch) -> None:
    """FR-7.6 write: the auto_trigger toggle flips the live trigger flag, writes
    ``auto_trigger = true`` back into the config TOML, and audits the change."""
    cfg = _config_toml(tmp_path, dream=True)
    with _client(tmp_path, monkeypatch, cfg=cfg, loopback=True) as client:
        response = client.post("/api/v1/dream/auto_trigger", json={"enabled": True})
        assert response.status_code == 200
        assert response.json()["enabled"] is True
        assert client.app.state.dream.auto_trigger_enabled is True

        text = cfg.read_text(encoding="utf-8")
        assert "auto_trigger = true" in text

        entries = _audit(client, "console.auto_trigger")
        assert len(entries) == 1
        assert entries[0].detail["enabled"] is True
        assert Path(entries[0].detail["persisted_to"]) == cfg


def test_auto_trigger_in_place_rewrite_and_no_dream_section(tmp_path, monkeypatch) -> None:
    """FR-7.6: an existing ``auto_trigger = false`` line is rewritten in place
    (never duplicated); a config without a [dream] table gains one."""
    cfg = _config_toml(tmp_path, dream=True)
    cfg.write_text(cfg.read_text(encoding="utf-8") + "auto_trigger = false\n", encoding="utf-8")
    with _client(tmp_path, monkeypatch, cfg=cfg, loopback=True) as client:
        client.post("/api/v1/dream/auto_trigger", json={"enabled": True})
        # exactly one TOML key line survives; the in-place line was rewritten,
        # never duplicated (a path-in-name substring must not confuse the count)
        text = cfg.read_text(encoding="utf-8")
        keys = [line for line in text.splitlines() if line.startswith("auto_trigger =")]
        assert keys == ["auto_trigger = true"]

    with _client(tmp_path, monkeypatch, loopback=True) as client:
        client.post("/api/v1/dream/auto_trigger", json={"enabled": False})
        source = client.app.state.config.source
        assert source is not None
        assert "auto_trigger = false" in source.read_text(encoding="utf-8")


def test_auto_trigger_422_on_non_boolean(tmp_path, monkeypatch) -> None:
    """FR-7.6 typed 422: the toggle body must carry a boolean."""
    with _client(tmp_path, monkeypatch) as client:
        response = client.post("/api/v1/dream/auto_trigger", json={"enabled": "yes"})
        assert response.status_code == 422


# ---------------------------------------------------------------- auth (FR-7.1 / NFR-7.1)


def test_console_requires_admin_token_for_non_loopback(tmp_path, monkeypatch) -> None:
    """FR-7.1: a non-localhost request is refused without the token and with a
    wrong token; either authorized header shape passes."""
    with _client(tmp_path, monkeypatch, loopback=False) as client:
        assert client.get("/api/v1/status").status_code == 401
        assert client.get("/api/v1/status", headers={"Authorization": "Bearer nope"}).status_code == 401
        ok_bearer = client.get("/api/v1/status", headers={"Authorization": f"Bearer {_ADMIN_TOKEN}"})
        assert ok_bearer.status_code == 200
        ok_x = client.get("/api/v1/status", headers={"X-Admin-Token": _ADMIN_TOKEN})
        assert ok_x.status_code == 200


def test_console_refuses_remote_when_token_unconfigured(tmp_path, monkeypatch) -> None:
    """NFR-7.1: no admin token configured -> remote access refused outright."""
    with _client(tmp_path, monkeypatch, token=None, loopback=False) as client:
        assert client.get("/api/v1/status").status_code == 401


def test_console_loopback_passes_without_token(tmp_path, monkeypatch) -> None:
    """FR-7.1: a loopback request is implicitly trusted -- no credential."""
    with _client(tmp_path, monkeypatch, token=None, loopback=True) as client:
        assert client.get("/api/v1/status").status_code == 200


def test_console_static_shell_served_and_guarded(tmp_path, monkeypatch) -> None:
    """FR-7.7 shell: /console serves the SPA from its static dir, behind the
    same auth gate a remote request hits."""
    with _client(tmp_path, monkeypatch, loopback=True) as client:
        response = client.get("/console/")
        assert response.status_code == 200
        assert "MnemoSeed console" in response.text


def test_console_static_assets_served_with_content_types(tmp_path, monkeypatch) -> None:
    """FR-7.1: every static asset the SPA references (html shell, css, js,
    banner) is served with the correct content-type — the page must not need a
    build step, so these are served raw from the static dir."""
    # (path, expected content-type prefix or None for a JS dual check)
    expected = [
        ("/console/", "text/html"),
        ("/console/styles.css", "text/css"),
        ("/console/app.js", None),
        ("/console/banner.png", "image/png"),
    ]
    with _client(tmp_path, monkeypatch, loopback=True) as client:
        for path, ctype in expected:
            response = client.get(path)
            assert response.status_code == 200, path
            content_type = response.headers["content-type"]
            if path.endswith(".js"):
                assert content_type in (
                    "application/javascript",
                    "text/javascript",
                ), (path, content_type)
            else:
                assert content_type.startswith(ctype), (path, content_type)
        # the SPA shell must reference the assets we assert on (so a rename
        # cannot silently break the offline console)
        shell = client.get("/console/").text
        assert "/console/styles.css" in shell
        assert "/console/app.js" in shell
        assert "/console/banner.png" in shell


def test_console_static_assets_guarded_for_remote(tmp_path, monkeypatch) -> None:
    """NFR-7.1: the static assets sit behind the same admin-token gate as the
    API — a remote request without the token gets 401, never content."""
    with _client(tmp_path, monkeypatch, loopback=False) as client:
        for path in ("/console/", "/console/styles.css", "/console/app.js", "/console/banner.png"):
            assert client.get(path).status_code == 401, path
        with_token = client.get("/console/styles.css", headers={"X-Admin-Token": _ADMIN_TOKEN})
        assert with_token.status_code == 200


def test_console_admin_token_check_uses_compare_digest(tmp_path, monkeypatch) -> None:
    """The admin-token match must run through secrets.compare_digest
    (constant-time), never a plain a == b -- swapping the call for ``==`` must
    leave this test red (mutation pin)."""
    calls: list[tuple[str, str]] = []
    real_compare = secrets.compare_digest

    def _spy(left: str, right: str) -> bool:
        calls.append((left, right))
        return real_compare(left, right)

    monkeypatch.setattr(secrets, "compare_digest", _spy)
    with _client(tmp_path, monkeypatch, loopback=False) as client:
        response = client.get("/api/v1/status", headers={"X-Admin-Token": _ADMIN_TOKEN})
        assert response.status_code == 200

    assert calls, "the admin-token check bypassed secrets.compare_digest"
    assert (_ADMIN_TOKEN, _ADMIN_TOKEN) in calls  # (supplied, expected)

    with _client(tmp_path, monkeypatch, loopback=False) as client:
        assert client.get("/console/").status_code == 401


# ---------------------------------------------------------------- dream review (FR-7.6 T3)


def _write_user_chunk(
    client: TestClient,
    *,
    chunk_id: str,
    text: str,
    entities: tuple[str, ...] = (),
    turn_start: int = 0,
    turn_end: int = 1,
) -> str:
    """Seed a verbatim chunk stamped as user-originated (FR-2.12: preference
    extraction requires origin=user, which persona_id/asserted_by gate)."""
    now = time.time()
    stamp = ChunkStamp(
        chunk_id=chunk_id,
        profile_id=_PROFILE,
        text=text,
        cognitive_tier=CognitiveTier.TIER_1,
        model_id="test-model",
        cues=Cues(entities=list(entities)),
        provenance=Provenance(
            asserted_by="user",
            session_id=_SESSION,
            source="seed",
            asserted_at=now,
            history=[
                ProvenanceEvent(at=now - 1.0, action="created", actor="test", detail={"round": 0}),
            ],
        ),
        decay_weight=1.0,
        score=0.0,
        consolidated=False,
        ingested_at=now,
        turn_start=turn_start,
        turn_end=turn_end,
    )
    embedded = client.app.state.stores.embed.embed(text)
    client.portal.call(client.app.state.stores.vector.upsert_chunk, stamp, embedded.dense, embedded.sparse)
    return chunk_id


def _dream_run_id(client: TestClient, profile_id: str = _PROFILE) -> str:
    """Fire one manual dream through the real daemon (reflect + merge + safe
    clear run synchronously) and return the journaled run id."""
    _fire_dream_event(client, profile_id)
    launched = client.post("/api/v1/dream/once", json={"profile_id": profile_id}).json()
    assert launched["launched"] is True
    runs = client.get("/api/v1/dream/runs").json()["runs"]
    assert runs, "the manual dream must register a run"
    return runs[0]["run_id"]


def test_dream_review_404_unknown_run_and_foreign_profile(tmp_path, monkeypatch) -> None:
    """FR-7.6 typed 404: an unknown run id, and a run of another profile, are
    both 404 for the review view."""
    with _client(tmp_path, monkeypatch) as client:
        assert client.get("/api/v1/dream/review/nope", params={"profile_id": _PROFILE}).status_code == 404
        _seed_profile(client)
        run_id = _dream_run_id(client)
        foreign = client.get(f"/api/v1/dream/review/{run_id}", params={"profile_id": "other"})
        assert foreign.status_code == 404


def test_dream_review_lists_triples_pairing_source_chunks(tmp_path, monkeypatch) -> None:
    """FR-7.6 AC-1: a completed run review view lists its triples side-by-side
    with the verbatim source chunks they were distilled from, verdict pending."""
    with _client(tmp_path, monkeypatch) as client:
        _seed_profile(client)
        _write_user_chunk(client, chunk_id="tabs-1", text="I prefer tabs for indentation")
        _write_user_chunk(client, chunk_id="coffee-1", text="I like coffee in the morning")
        run_id = _dream_run_id(client)

        body = client.get(f"/api/v1/dream/review/{run_id}", params={"profile_id": _PROFILE}).json()
        assert body["run_id"] == run_id
        assert body["profile_id"] == _PROFILE
        assert body["reflected"] is True
        assert body["turn_range"] == {"start": 0, "end": 1}

        by_object = {t["object"]: t for t in body["triples"]}
        assert set(by_object) == {"tabs for indentation", "coffee in the morning"}
        for triple in body["triples"]:
            assert triple["subject"] == "user"
            assert triple["predicate"] == "prefers"
            assert triple["confidence"] == 0.7
            assert triple["route"] == "core"
            assert triple["polarity"] == "positive"
            assert triple["preference"] is True
            assert triple["verdict"] is None
            # diff-style pairing: every triple carries its verbatim source chunks
            assert triple["chunk_ids"] and triple["chunks"]
            assert triple["chunk_ids"][0] == triple["chunks"][0]["chunk_id"]
            assert triple["chunks"][0]["text"].startswith("I ")


def test_dream_review_verdict_records_audits_and_is_idempotent(tmp_path, monkeypatch) -> None:
    """FR-7.6 write: a per-triple verdict lands one audit row, the review view
    reflects it, and re-submitting the identical verdict writes nothing twice."""
    with _client(tmp_path, monkeypatch) as client:
        _seed_profile(client)
        _write_user_chunk(client, chunk_id="tabs-1", text="I prefer tabs for indentation")
        run_id = _dream_run_id(client)
        triple = client.get(f"/api/v1/dream/review/{run_id}", params={"profile_id": _PROFILE}).json()[
            "triples"
        ][0]

        def _send(verdict: str) -> dict:
            return client.post(
                "/api/v1/dream/review",
                json={
                    "run_id": run_id,
                    "profile_id": _PROFILE,
                    "subject": triple["subject"],
                    "predicate": triple["predicate"],
                    "object": triple["object"],
                    "route": triple["route"],
                    "verdict": verdict,
                },
            ).json()

        recorded = _send("accept")
        assert recorded["recorded"] is True
        assert recorded["verdict"] == "accept"

        refreshed = client.get(f"/api/v1/dream/review/{run_id}", params={"profile_id": _PROFILE}).json()
        assert refreshed["triples"][0]["verdict"]["action"] == "accept"
        assert refreshed["triples"][0]["verdict"]["at"] == pytest.approx(recorded["at"])

        # idempotent double-submit: same verdict, no duplicate write
        again = _send("accept")
        assert again["recorded"] is False
        assert again["at"] == pytest.approx(recorded["at"])

        entries = _audit(client, "review_verdict")
        assert len(entries) == 1
        assert entries[0].detail["run_id"] == run_id
        assert entries[0].detail["subject"] == triple["subject"]
        assert entries[0].detail["object"] == triple["object"]
        assert entries[0].detail["verdict"] == "accept"


def test_dream_review_verdict_typed_errors(tmp_path, monkeypatch) -> None:
    """FR-7.6 typed errors: invalid verdict/route/missing fields are 422,
    unknown runs and triples outside the run are 404."""
    with _client(tmp_path, monkeypatch) as client:
        _seed_profile(client)
        _write_user_chunk(client, chunk_id="tabs-1", text="I prefer tabs for indentation")
        run_id = _dream_run_id(client)
        triple = client.get(f"/api/v1/dream/review/{run_id}", params={"profile_id": _PROFILE}).json()[
            "triples"
        ][0]

        base = {
            "run_id": run_id,
            "profile_id": _PROFILE,
            "subject": triple["subject"],
            "predicate": triple["predicate"],
            "object": triple["object"],
            "route": triple["route"],
            "verdict": "accept",
        }

        def _with(**patch):
            payload = dict(base)
            payload.update(patch)
            return payload

        assert client.post("/api/v1/dream/review", json=_with(verdict="maybe")).status_code == 422
        assert client.post("/api/v1/dream/review", json=_with(route="ghost")).status_code == 422
        assert client.post("/api/v1/dream/review", json=_with(object="")).status_code == 422
        assert client.post("/api/v1/dream/review", json=_with(run_id="nope")).status_code == 404
        # a triple that is not part of this run is a typed 404, not a silent no-op
        status = client.post("/api/v1/dream/review", json=_with(object="coffee in the morning")).status_code
        assert status == 404


def test_dream_review_run_id_cannot_escape_journal_dir(tmp_path, monkeypatch) -> None:
    """F2: a crafted run_id ('../x') must never resolve to a snapshot outside
    the journal dir. A decoy file planted one level above journal_dir would be
    loaded by a path-traversal run_id and accepted here; the service must
    reject it as an unknown run (404) instead of reading outside its sandbox."""
    with _client(tmp_path, monkeypatch) as client:
        _seed_profile(client)
        # journal_dir is tmp_path / "dreams"; the decoy lives in tmp_path itself,
        # exactly what "../escape" normalizes to when concatenated unsafely.
        journal_dir = tmp_path / "dreams"
        decoy = {
            "snapshot_id": "escape",
            "profile_id": _PROFILE,
            "turn_range": {"start": 0, "end": 1},
            "chunks": [],
            "created_at": time.time(),
            "phases": ["consolidate"],
            "reflect_result": {
                "snapshot_id": "escape",
                "profile_id": _PROFILE,
                "turn_range": {"start": 0, "end": 1},
                "prompt_version": "test",
                "triples": [
                    {
                        "subject": "alice",
                        "predicate": "prefers",
                        "object": "tea",
                        "tiers": [1],
                        "chunk_ids": [],
                        "turn_range": {"start": 0, "end": 1},
                        "confidence": 0.8,
                        "route": "core",
                        "preference": False,
                        "polarity": "positive",
                    }
                ],
                "conflicts": [],
                "delta_overflow": [],
                "consumed_chunk_ids": [],
            },
        }
        (tmp_path / "escape.json").write_text(json.dumps(decoy), encoding="utf-8")
        assert not (journal_dir / "escape.json").exists()

        # traversal: '../escape' would read tmp_path/escape.json — a 200 here
        # means the sandbox was escaped; it must be an unknown-run 404 instead.
        response = client.post(
            "/api/v1/dream/review",
            json={
                "run_id": "../escape",
                "profile_id": _PROFILE,
                "subject": "alice",
                "predicate": "prefers",
                "object": "tea",
                "route": "core",
                "verdict": "accept",
            },
        )
        assert response.status_code == 404
        assert "not found" in response.json()["detail"]


# ---------------------------------------------------------------- conflicts inbox (FR-7.7 T4)


def _seed_conflict_pair(client: TestClient, *, group_id: str = "cg-tabs-vs-spaces") -> tuple[str, str, str]:
    node_a = _pref("cn-tabs", "prefers tabs", entities=("Fmt",), conflict_flag=True, conflict_group=group_id)
    node_b = _pref(
        "cn-spaces", "prefers spaces", entities=("Fmt",), conflict_flag=True, conflict_group=group_id
    )
    _write_node(client, node_a)
    _write_node(client, node_b)
    return node_a.node_id, node_b.node_id, group_id


def test_conflicts_inbox_pairs_both_sides_with_provenance(tmp_path, monkeypatch) -> None:
    """FR-7.7 inbox: flag_conflict pairs surface both sides with their statement,
    domain, entities, weights, version and provenance summary."""
    with _client(tmp_path, monkeypatch) as client:
        _seed_profile(client)
        node_a, node_b, group_id = _seed_conflict_pair(client)

        body = client.get("/api/v1/conflicts", params={"profile_id": _PROFILE}).json()
        assert len(body["groups"]) == 1
        group = body["groups"][0]
        assert group["group_id"] == group_id
        sides = {side["node_id"]: side for side in group["sides"]}
        assert set(sides) == {node_a, node_b}

        side_a = sides[node_a]
        assert side_a["statement"] == "prefers tabs"
        assert side_a["node_type"] == "PREFERENCE"
        assert side_a["domain"] == "coding"
        assert side_a["entities"] == ["Fmt"]
        assert side_a["decay_weight"] == 1.0
        assert side_a["confidence"] == 0.9
        assert side_a["version"] == 1
        assert side_a["provenance"]["asserted_by"] == "dream-engine"
        assert side_a["provenance"]["source"] == "seed"
        assert sides[node_b]["statement"] == "prefers spaces"
        # nothing ungrouped leaks in: a conflict flag without a group id is skipped
        loner = _write_node(client, _pref("cn-loner", "prefers tabs", conflict_flag=True))
        assert loner
        assert len(client.get("/api/v1/conflicts", params={"profile_id": _PROFILE}).json()["groups"]) == 1


def test_conflicts_resolve_coexist_annotates_scope_and_clears_pair(tmp_path, monkeypatch) -> None:
    """FR-7.7 AC-3: coexist writes the scope annotation onto both sides version
    chain, clears the pair conflict flags, and leaves one audit row."""
    with _client(tmp_path, monkeypatch) as client:
        _seed_profile(client)
        node_a, node_b, group_id = _seed_conflict_pair(client)
        scope = "go projects use tabs, python projects use spaces"

        resolved = client.post(
            f"/api/v1/conflicts/{group_id}/resolve",
            json={"profile_id": _PROFILE, "branch": "coexist", "scope": scope},
        ).json()
        assert resolved["already_resolved"] is False
        assert resolved["branch"] == "coexist"
        assert set(resolved["written"]) == {node_a, node_b}
        assert resolved["scope"] == scope

        for node_id in (node_a, node_b):
            dossier = client.get(f"/api/v1/nodes/{node_id}", params={"profile_id": _PROFILE}).json()
            assert dossier["flags"]["conflict_flag"] is False
            assert dossier["flags"]["conflict_group"] is None
            # the scope annotation is written into the version chain payload
            assert dossier["version_chain"][-1]["props"]["scope"] == scope
            assert dossier["version_chain"][-1]["provenance"]["history"][-1]["action"] == "scope_annotated"
            # a resolution APPENDS a revision (design/01 section 4): the chain
            # grows, the old revision is closed, and it stays readable
            # temporally with the pre-resolution state (never an in-place rewrite).
            versions = client.portal.call(client.app.state.stores.graph.versions, node_id)
            assert len(versions) == 2
            assert versions[0].version == 1
            assert versions[0].valid_to is not None
            assert versions[0].props.get("scope") is None
            assert versions[1].version == 2
            assert versions[1].valid_to is None
            assert versions[1].props.get("scope") == scope
            pre = client.portal.call(
                client.app.state.stores.graph.as_of,
                resolved["at"] - 1.0,
                NodeFilter(profile_id=_PROFILE),
            )
            pre_node = next(n for n in pre if n.node_id == node_id)
            assert pre_node.version == 1
            assert pre_node.props.get("scope") is None
            assert pre_node.confidence == 0.9

        assert client.get("/api/v1/conflicts", params={"profile_id": _PROFILE}).json()["groups"] == []

        entries = _audit(client, "conflict.resolve")
        assert len(entries) == 1
        assert entries[0].detail["branch"] == "coexist"
        assert entries[0].detail["group_id"] == group_id
        assert entries[0].detail["scope"] == scope


def test_conflicts_resolve_reinforce_boosts_kept_side(tmp_path, monkeypatch) -> None:
    """FR-7.7 reinforce: the kept side gains confidence + decay-weight rebound
    (design/01 4a) plus a pinned provenance event; the pair clears the inbox."""
    with _client(tmp_path, monkeypatch) as client:
        _seed_profile(client)
        node_a, node_b, group_id = _seed_conflict_pair(client)

        resolved = client.post(
            f"/api/v1/conflicts/{group_id}/resolve",
            json={"profile_id": _PROFILE, "branch": "reinforce", "node_id": node_a},
        ).json()
        assert resolved["already_resolved"] is False
        assert resolved["reinforced"] == [node_a]
        assert set(resolved["written"]) == {node_a, node_b}

        kept = client.get(f"/api/v1/nodes/{node_a}", params={"profile_id": _PROFILE}).json()
        assert kept["weights"]["confidence"] == pytest.approx(0.95)  # 0.9 + 0.05, capped
        assert kept["weights"]["reinforce_count"] == 1
        assert kept["weights"]["decay_weight"] == 1.0
        assert kept["version_chain"][-1]["confidence"] == pytest.approx(0.95)
        assert kept["version_chain"][-1]["provenance"]["history"][-1]["action"] == "conflict_reinforced"

        # the resolution appends, it never rewrites in place (design/01 section
        # 4): the kept side grows its chain, the pre-resolution revision stays
        # temporally readable as_of with the original strength (0.9).
        versions = client.portal.call(client.app.state.stores.graph.versions, node_a)
        assert len(versions) == 2
        assert versions[0].version == 1
        assert versions[0].valid_to is not None
        assert versions[0].confidence == 0.9
        assert versions[1].version == 2
        assert versions[1].valid_to is None
        assert versions[1].confidence == pytest.approx(0.95)
        pre = client.portal.call(
            client.app.state.stores.graph.as_of,
            resolved["at"] - 1.0,
            NodeFilter(profile_id=_PROFILE),
        )
        pre_kept = next(n for n in pre if n.node_id == node_a)
        assert pre_kept.version == 1
        assert pre_kept.confidence == 0.9

        # the other side is untouched, but the pair still resolves out of the inbox
        other = client.get(f"/api/v1/nodes/{node_b}", params={"profile_id": _PROFILE}).json()
        assert other["weights"]["confidence"] == 0.9
        assert other["weights"]["reinforce_count"] == 0
        assert client.get("/api/v1/conflicts", params={"profile_id": _PROFILE}).json()["groups"] == []


def test_conflicts_resolve_invalidate_closes_revision(tmp_path, monkeypatch) -> None:
    """FR-7.7 invalidate: the dropped side current revision is closed (valid_to
    set), it stops being a current node, and the survivor stays."""
    with _client(tmp_path, monkeypatch) as client:
        _seed_profile(client)
        node_a, node_b, group_id = _seed_conflict_pair(client)
        before = client.portal.call(client.app.state.stores.graph.get_node, node_a)

        resolved = client.post(
            f"/api/v1/conflicts/{group_id}/resolve",
            json={"profile_id": _PROFILE, "branch": "invalidate", "node_id": node_a},
        ).json()
        assert resolved["already_resolved"] is False
        assert resolved["invalidated"] == [node_a]

        # the invalidated party is no longer a current node (typed 404)...
        assert client.get(f"/api/v1/nodes/{node_a}", params={"profile_id": _PROFILE}).status_code == 404
        # ...but its version chain is closed, not deleted: the invalidation is
        # traced for audit replay
        versions = client.portal.call(client.app.state.stores.graph.versions, node_a)
        assert versions
        assert versions[-1].valid_to is not None
        assert before is not None

        surviving = client.get(f"/api/v1/nodes/{node_b}", params={"profile_id": _PROFILE})
        assert surviving.status_code == 200
        assert client.get("/api/v1/conflicts", params={"profile_id": _PROFILE}).json()["groups"] == []

        entries = _audit(client, "conflict.resolve")
        assert len(entries) == 1
        assert entries[0].detail["invalidated"] == [node_a]
        assert entries[0].detail["written"] == []


def test_conflicts_resolve_pending_keeps_pair_in_inbox(tmp_path, monkeypatch) -> None:
    """FR-7.7 pending: recorded but the pair stays flagged for a later decision."""
    with _client(tmp_path, monkeypatch) as client:
        _seed_profile(client)
        node_a, node_b, group_id = _seed_conflict_pair(client)

        resolved = client.post(
            f"/api/v1/conflicts/{group_id}/resolve",
            json={"profile_id": _PROFILE, "branch": "pending"},
        ).json()
        assert resolved["already_resolved"] is False
        assert resolved["written"] == []
        assert resolved["reinforced"] == []
        assert resolved["invalidated"] == []

        # still flagged, still paired in the inbox
        inbox = client.get("/api/v1/conflicts", params={"profile_id": _PROFILE}).json()
        assert len(inbox["groups"]) == 1
        assert {s["node_id"] for s in inbox["groups"][0]["sides"]} == {node_a, node_b}

        entries = _audit(client, "conflict.resolve")
        assert len(entries) == 1
        assert entries[0].detail["branch"] == "pending"
        assert entries[0].detail["node_id"] is None


def test_conflicts_resolve_idempotent_on_resubmit(tmp_path, monkeypatch) -> None:
    """FR-7.7: re-submitting a resolution for an already-resolved group returns
    the recorded branch without re-writing or duplicating the audit row."""
    with _client(tmp_path, monkeypatch) as client:
        _seed_profile(client)
        _, _, group_id = _seed_conflict_pair(client)
        scope = "python only for data tasks"

        first = client.post(
            f"/api/v1/conflicts/{group_id}/resolve",
            json={"profile_id": _PROFILE, "branch": "coexist", "scope": scope},
        ).json()
        assert first["already_resolved"] is False

        again = client.post(
            f"/api/v1/conflicts/{group_id}/resolve",
            json={"profile_id": _PROFILE, "branch": "coexist", "scope": scope},
        ).json()
        assert again["already_resolved"] is True
        assert again["branch"] == "coexist"
        assert again["at"] == pytest.approx(first["at"])
        assert len(_audit(client, "conflict.resolve")) == 1


def test_conflicts_resolve_typed_errors(tmp_path, monkeypatch) -> None:
    """FR-7.7 typed errors: 422 on unknown branch / missing node_id / missing
    scope; 404 on unknown groups and node_id outside the pair."""
    with _client(tmp_path, monkeypatch) as client:
        _seed_profile(client)
        node_a, node_b, group_id = _seed_conflict_pair(client)
        assert node_a and node_b

        def _resolve(**patch):
            payload = {"profile_id": _PROFILE, "branch": "pending"}
            payload.update(patch)
            return client.post(f"/api/v1/conflicts/{group_id}/resolve", json=payload).status_code

        assert _resolve(branch="nuke") == 422
        assert _resolve(branch="reinforce") == 422
        assert _resolve(branch="invalidate") == 422
        assert _resolve(branch="coexist") == 422
        # unknown group
        status = client.post(
            "/api/v1/conflicts/nope/resolve", json={"profile_id": _PROFILE, "branch": "pending"}
        ).status_code
        assert status == 404
        # node_id that is not part of the pair
        assert _resolve(branch="reinforce", node_id="stranger-node") == 404
        # scope of the wrong type
        assert _resolve(branch="coexist", scope=7) == 422


# ---------------------------------------------------------------- e2e: full dream -> review -> conflicts loop


def test_dream_conflict_full_loop_e2e(tmp_path, monkeypatch) -> None:
    """AC-3 end-to-end: two user preference chunks dream into contradictory
    nodes, the review view pairs triples with their source chunks and takes a
    verdict, the conflict-flag surface pairs the two nodes, coexist resolves
    them, and the version chain + audit log both reflect the reconciliation."""
    with _client(tmp_path, monkeypatch) as client:
        _seed_profile(client)
        _write_user_chunk(client, chunk_id="tabs-1", text="I prefer tabs for indentation")
        _write_user_chunk(client, chunk_id="spaces-1", text="I prefer spaces for indentation")
        run_id = _dream_run_id(client)

        # 1. FR-7.6: the run triples pair side-by-side with their source chunks
        review = client.get(f"/api/v1/dream/review/{run_id}", params={"profile_id": _PROFILE}).json()
        assert review["reflected"] is True
        assert {t["object"] for t in review["triples"]} == {
            "tabs for indentation",
            "spaces for indentation",
        }
        for triple in review["triples"]:
            assert triple["chunks"], "each distilled triple shows its verbatim source"
            assert triple["verdict"] is None
        first = review["triples"][0]
        verdict = client.post(
            "/api/v1/dream/review",
            json={
                "run_id": run_id,
                "profile_id": _PROFILE,
                "subject": first["subject"],
                "predicate": first["predicate"],
                "object": first["object"],
                "route": first["route"],
                "verdict": "accept",
            },
        ).json()
        assert verdict["recorded"] is True

        # 2. the dream distilled exactly the two contradictory preference nodes
        nodes = client.get("/api/v1/nodes", params={"profile_id": _PROFILE}).json()["items"]
        by_statement = {n["statement"]: n["node_id"] for n in nodes}
        assert set(by_statement) == {"tabs for indentation", "spaces for indentation"}
        node_a, node_b = by_statement["tabs for indentation"], by_statement["spaces for indentation"]

        # 3. the conflict-flag pair surface links both sides into one group
        client.portal.call(
            client.app.state.stores.graph.set_flags, [node_a, node_b], [GraphFlag.CONFLICT_GROUP]
        )
        group_id = client.get("/api/v1/nodes", params={"profile_id": _PROFILE, "conflict": "true"}).json()[
            "items"
        ][0]["conflict_group"]
        assert group_id

        # 4. FR-7.7: the inbox pairs the two sides of the contradiction
        inbox = client.get("/api/v1/conflicts", params={"profile_id": _PROFILE}).json()
        assert len(inbox["groups"]) == 1
        group = inbox["groups"][0]
        assert group["group_id"] == group_id
        assert {s["node_id"] for s in group["sides"]} == {node_a, node_b}

        # 5. FR-7.7: coexist resolves with a scope; version chain + audit reflect it
        scope = "prefer tabs in go projects, spaces in python projects"
        resolved = client.post(
            f"/api/v1/conflicts/{group_id}/resolve",
            json={"profile_id": _PROFILE, "branch": "coexist", "scope": scope},
        ).json()
        assert resolved["already_resolved"] is False
        assert set(resolved["written"]) == {node_a, node_b}

        for node_id in (node_a, node_b):
            dossier = client.get(f"/api/v1/nodes/{node_id}", params={"profile_id": _PROFILE}).json()
            assert dossier["flags"]["conflict_flag"] is False
            assert dossier["version_chain"][-1]["props"]["scope"] == scope

        assert client.get("/api/v1/conflicts", params={"profile_id": _PROFILE}).json()["groups"] == []
        entries = _audit(client, "conflict.resolve")
        assert len(entries) == 1
        assert entries[0].detail["branch"] == "coexist"
        assert entries[0].detail["group_id"] == group_id
        assert entries[0].detail["scope"] == scope
