"""Optional live-Postgres smoke tests for the pg_* driver family (task 5).

Skipped unless MNEMOSEED_TEST_PG_DSN points at a running server (e.g. the local
`mnemoseed-pg-it` container on :55432, DSN
postgresql://mnemoseed:mnemoseed@localhost:55432/mnemoseed). Each test runs in
its own Postgres schema so runs are independent and repeatable (D6 named
instances are separate schemas).

Coverage: pg_graph round-trip + version chain + atomic rollback, pg_meta pool /
profiles / tokens / config / audit / dream runs, pgvector full VectorStore
surface with the synthetic embedder, and the rollback-first _transaction helper
persisting across consecutive writes.
"""

import asyncio
import os
import time
import uuid

import psycopg
import pytest

from mnemoseed.schema.graph import Edge, GraphNode, NodeType, RelType
from mnemoseed.schema.stamp import ChunkStamp, CognitiveTier, Cues, Provenance
from mnemoseed.storage.drivers.pg_graph import PgGraphDriver
from mnemoseed.storage.drivers.pg_meta import PgMetaDriver
from mnemoseed.storage.drivers.pgvector import PgVectorStore
from mnemoseed.storage.drivers.synthetic_embedder import SyntheticEmbedder
from mnemoseed.storage.ports import (
    AuditEntry,
    AuditFilter,
    ChunkFilter,
    DreamRun,
    DreamRunFilter,
    GraphFlag,
    GraphWeightUpdate,
    IntentionStatus,
    NodeFilter,
    Page,
    PoolState,
    SparseVector,
    StorageError,
    StoredProfile,
    TurnRange,
    WeightUpdate,
)

PG_DSN = os.environ.get("MNEMOSEED_TEST_PG_DSN")
pytestmark = pytest.mark.skipif(
    PG_DSN is None or not PG_DSN.strip(),
    reason="MNEMOSEED_TEST_PG_DSN not set (no live Postgres for integration)",
)

_DIM = 64

_PREF_PROPS: dict = {
    "domain": "coding",
    "statement": "dark mode",
    "valence": 0.8,
    "prior_width": 0.3,
    "trait_anchor": "anima-1",
    "evidence_chain": [{"event": "created", "at": 123.0}],
}


def _schema() -> str:
    return f"it_{uuid.uuid4().hex[:12]}"


def _pref(**over: object) -> GraphNode:
    base: dict[str, object] = dict(
        profile_id="p1",
        node_type=NodeType.PREFERENCE,
        entities=["ui"],
        props=dict(_PREF_PROPS),
        provenance=Provenance(asserted_by="test-agent", source="session://s1"),
        valid_from=time.time() - 100.0,
    )
    base.update(over)
    return GraphNode(**base)


@pytest.fixture
def graph():
    db = PgGraphDriver(dsn=PG_DSN, schema=_schema())
    yield db
    asyncio.run(db.close())


@pytest.fixture
def meta():
    db = PgMetaDriver(dsn=PG_DSN, schema=_schema())
    yield db
    asyncio.run(db.close())


@pytest.fixture
def vector():
    store = PgVectorStore(dsn=PG_DSN, dimensions=_DIM, schema=_schema())
    yield store
    asyncio.run(store.close())


@pytest.fixture
def embedder():
    return SyntheticEmbedder(dimension=_DIM)


def _make(chunk_id: str, text: str, **over: object) -> ChunkStamp:
    base: dict[str, object] = dict(
        chunk_id=chunk_id,
        profile_id="alice",
        text=text,
        cognitive_tier=CognitiveTier.TIER_1,
        model_id="test-model",
        persona_id="p1",
        cues=Cues(project="it", entities=["math"]),
        provenance=Provenance(asserted_by="test-model", session_id="s1", source="manual"),
        ingested_at=1.0,
    )
    base.update(over)
    return ChunkStamp(**base)  # type: ignore[arg-type]


def _raw_chunk(vector: "PgVectorStore", chunk_id: str) -> dict[str, object]:
    """Raw chunks-row read (usage counters are not on the public ChunkStamp)."""
    rows = vector._conn.execute(
        f"SELECT * FROM {vector.table_name} WHERE chunk_id = %s", (chunk_id,)
    ).fetchall()
    return dict(rows[0]) if rows else {}


# ---------------------------------------------------------------- graph


def test_graph_roundtrip_and_version_chain(graph):
    v1 = _pref(node_id="n1")
    graph.upsert_node(v1)
    got = graph.get_node("n1")
    assert got is not None
    assert got.props["statement"] == "dark mode"
    assert got.is_current
    assert got.entities == ["ui"]
    assert graph.list_nodes(NodeFilter(profile_id="p1"), Page(0, 50)).total == 1

    take_over = time.time()
    v2 = _pref(
        node_id="n1",
        version=2,
        valid_from=take_over,
        props={**v1.props, "statement": "dark mode at night"},
    )
    graph.invalidate("n1", take_over)
    graph.append_version(v2)

    current = graph.get_node("n1")
    assert current.version == 2
    versions = graph.versions("n1")
    assert [v.version for v in versions] == [1, 2]
    assert versions[0].valid_to is not None
    assert versions[1].valid_to is None

    before = graph.as_of(take_over - 1.0, NodeFilter(profile_id="p1"))
    assert {n.version for n in before} == {1}
    after = graph.as_of(take_over + 1.0, NodeFilter(profile_id="p1"))
    assert {n.version for n in after} == {2}


def test_graph_same_version_reupsert_keeps_one_version(graph):
    node = _pref(node_id="dup")
    graph.upsert_node(node)
    graph.upsert_node(node)
    versions = graph.versions("dup")
    assert [v.version for v in versions] == [1]


def test_graph_append_version_invalidate_rolls_back_atomically(graph):
    v1 = _pref(node_id="r1")
    graph.upsert_node(v1)
    bad = _pref(node_id="r1", version=2, props={"domain": "coding"})  # invalid payload
    with pytest.raises(ValueError, match="missing required field"):
        graph.append_version(bad, invalidate_at=time.time())
    current = graph.get_node("r1")
    assert current.version == 1
    assert current.valid_to is None


def test_graph_edges_traverse_flags_intentions_weights(graph):
    for node_id in ("hub", "leaf1", "leaf2"):
        graph.upsert_node(_pref(node_id=node_id))
    graph.add_edge(Edge(src="hub", dst="leaf1", rel=RelType.EVIDENCED_BY, profile_id="p1"))
    graph.add_edge(Edge(src="leaf2", dst="hub", rel=RelType.EVIDENCED_BY, profile_id="p1"))
    reached = graph.traverse("hub", depth=1, filter=NodeFilter(profile_id="p1"))
    assert {n.node_id for n in reached} == {"hub", "leaf1", "leaf2"}

    graph.set_flags(["hub", "leaf1"], [GraphFlag.CONFLICT_GROUP])
    g1 = graph.get_node("hub")
    g2 = graph.get_node("leaf1")
    assert g1.conflict_flag and g2.conflict_flag
    assert g1.conflict_group == g2.conflict_group

    due = GraphNode(
        node_id="i1",
        profile_id="p1",
        node_type=NodeType.INTENTION,
        props={"trigger_condition": "when", "action": "act", "status": "pending"},
        valid_from=time.time() - 50.0,
        provenance=Provenance(asserted_by="test-agent", source="session://s1"),
    )
    graph.upsert_node(due)
    hits = graph.query_intentions(IntentionStatus.PENDING, time.time())
    assert {n.node_id for n in hits} == {"i1"}

    graph.batch_update_weights([GraphWeightUpdate(node_id="hub", decay_weight=0.3)])
    assert abs(graph.get_node("hub").decay_weight - 0.3) < 1e-9


# ---------------------------------------------------------------- meta


def test_meta_pool_profiles_tokens_config_audit_dreams(meta):
    assert meta.pool_state() == PoolState(balance=0.0)
    meta.pool_add(10.0, TurnRange(start=0, end=4))
    meta.advance_watermark(TurnRange(start=0, end=4))
    state = meta.pool_state()
    assert state.balance == 10.0
    assert state.watermark == TurnRange(start=0, end=4)

    meta.upsert_profile(StoredProfile(profile_id="u1", display_name="Uma"))
    token = meta.issue_token("u1", ("graph:read",), expires_at=time.time() + 60.0)
    assert token.profile_id == "u1"
    assert meta.get_profile("u1").display_name == "Uma"
    meta.revoke_token(token.token_id)
    assert [p.profile_id for p in meta.list_profiles()] == ["u1"]

    v1 = meta.set_config("theme", {"mode": "dark"})
    meta.set_config("theme", {"mode": "light"})
    assert meta.get_config("theme").version == 2
    meta.rollback_config("theme", v1)
    entry = meta.get_config("theme")
    assert entry.value == {"mode": "dark"}
    assert entry.version == 3
    with pytest.raises(StorageError, match="has no version 99"):
        meta.rollback_config("theme", 99)

    meta.audit_append(AuditEntry(actor="alice", action="insert", detail={"n": 1}, at=100.0))
    page = meta.audit_query(AuditFilter(actor="alice"), Page(0, 50))
    assert page.total == 1
    assert page.items[0].detail == {"n": 1}

    run_id = meta.record_dream_run(
        DreamRun(
            session_id="s1",
            turn_range=TurnRange(start=1, end=3),
            model_id="claude",
            tokens=42,
            cost=0.0042,
            interrupted=True,
        )
    )
    assert len(run_id) == 32
    runs = meta.list_dream_runs(DreamRunFilter(session_id="s1"), Page(0, 50))
    assert runs.total == 1
    assert runs.items[0].tokens == 42
    assert runs.items[0].interrupted is True


def test_meta_issue_token_unknown_profile_raises(meta):
    with pytest.raises(StorageError, match="unknown profile"):
        meta.issue_token("ghost", ("graph:read",))


def test_meta_audit_append_only_enforced_by_db(meta):
    meta.audit_append(AuditEntry(actor="alice", action="insert", at=100.0))
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        with meta._conn.transaction():
            meta._conn.execute("UPDATE audit_log SET action = 'tampered'")


# ---------------------------------------------------------------- vector


def test_vector_full_surface(vector, embedder):
    a = embedder.embed("alpha beta gamma")
    vector.upsert_chunk(_make("a1", "alpha beta gamma"), a.dense, a.sparse)
    vector.upsert_chunk(_make("a2", "alpha beta delta"), a.dense, a.sparse)

    hits = vector.search(a.dense, a.sparse, ChunkFilter(profile_id="alice"), top_k=2)
    assert [hit.chunk.chunk_id for hit in hits] == ["a1", "a2"]
    assert all(hit.chunk.profile_id == "alice" for hit in hits)

    dup = vector.near_duplicate(a.dense, threshold=0.99, profile_id="alice")
    assert {c.chunk_id for c in dup} == {"a1", "a2"}

    snap = vector.snapshot_read(ChunkFilter(profile_id="alice"))
    assert {c.chunk_id for c in snap} == {"a1", "a2"}

    vector.mark_consolidated(["a1"])
    cons = vector.list_chunks(ChunkFilter(profile_id="alice", consolidated=True), Page(limit=10))
    assert [c.chunk_id for c in cons.items] == ["a1"]

    vector.update_weights([WeightUpdate(chunk_id="a2", decay_weight=0.3, reinforce_count=2)])
    assert vector.get_chunk("a2").decay_weight == pytest.approx(0.3)

    assert vector.purge_range("s1", turn_start=1, turn_end=9) == 0  # rows have no turn bounds

    everything = vector.list_chunks(ChunkFilter(profile_id="alice"), Page(offset=0, limit=1))
    assert everything.total == 2
    assert len(everything.items) == 1

    assert vector.get_chunk("missing") is None


def test_vector_purge_range_deletes_funnel_chunk_by_turn_window(vector, embedder):
    # Item C: a funnel-written chunk (turn bounds on the stamp) must be
    # targetable by purge_range through the public write path.
    a = embedder.embed("alpha beta gamma")
    vector.upsert_chunk(_make("f1", "funnel chunk", turn_start=2, turn_end=2), a.dense, a.sparse)
    vector.upsert_chunk(_make("f2", "later funnel chunk", turn_start=8, turn_end=8), a.dense, a.sparse)
    vector.upsert_chunk(_make("plain", "no turn bounds"), a.dense, a.sparse)

    assert vector.purge_range("s1", turn_start=2, turn_end=2) == 1

    remaining = [
        chunk.chunk_id for chunk in vector.list_chunks(ChunkFilter(profile_id="alice"), Page(limit=10)).items
    ]
    assert "f1" not in remaining
    assert "f2" in remaining
    assert "plain" in remaining


def test_vector_profile_isolation(vector, embedder):
    a = embedder.embed("alpha beta gamma")
    vector.upsert_chunk(_make("a1", "alpha beta gamma"), a.dense, a.sparse)
    vector.upsert_chunk(_make("b1", "alpha beta gamma", profile_id="bob"), a.dense, a.sparse)

    alice_hits = vector.search(a.dense, a.sparse, ChunkFilter(profile_id="alice"), top_k=5)
    assert [hit.chunk.chunk_id for hit in alice_hits] == ["a1"]

    only_bob = vector.near_duplicate(a.dense, threshold=0.8, profile_id="bob")
    assert [c.chunk_id for c in only_bob] == ["b1"]

    carol = vector.search(a.dense, a.sparse, ChunkFilter(profile_id="carol"), top_k=5)
    assert carol == []


def test_vector_hybrid_ranking_sparse_breaks_dense_tie(vector):
    query = ChunkFilter(profile_id="alice")
    dense = [1.0] + [0.0] * (_DIM - 1)
    shared = SparseVector((1, 2, 3), (0.7, 0.3, 0.2))
    disjoint = SparseVector((10, 11, 12), (0.8, 0.6, 0.4))

    vector.upsert_chunk(_make("q1", "shared lexicon"), dense, shared)
    vector.upsert_chunk(_make("q2", "disjoint lexicon"), dense, disjoint)

    hits = vector.search(dense, shared, query, top_k=2)
    assert hits[0].chunk.chunk_id == "q1"
    assert hits[0].similarity > hits[1].similarity


def test_vector_update_chunk_state_hits_and_flag(vector, embedder):
    a = embedder.embed("alpha beta gamma")
    vector.upsert_chunk(_make("s1", "alpha beta gamma"), a.dense, a.sparse)
    vector.update_chunk_state(["s1"], hit_increment=4, needs_reconcile=True)
    row = _raw_chunk(vector, "s1")
    assert row["hit_count"] == 4
    assert row["last_hit_at"] is not None and row["last_hit_at"] > 0.0
    assert row["needs_reconcile"] is True

    vector.update_chunk_state(["s1"], needs_reconcile=False)
    assert _raw_chunk(vector, "s1")["needs_reconcile"] is False


def test_vector_update_chunk_state_batch_and_unknown(vector, embedder):
    a = embedder.embed("alpha beta gamma")
    vector.upsert_chunk(_make("s2", "alpha beta gamma"), a.dense, a.sparse)
    vector.upsert_chunk(_make("s3", "alpha beta delta"), a.dense, a.sparse)
    vector.update_chunk_state(["s2", "s3", "ghost"], hit_increment=1, needs_reconcile=True)
    for cid in ("s2", "s3"):
        row = _raw_chunk(vector, cid)
        assert row["hit_count"] == 1
        assert row["needs_reconcile"] is True

    vector.update_chunk_state(["s2"])  # neither argument -> no-op
    assert _raw_chunk(vector, "s2")["hit_count"] == 1


# ---------------------------------------------------------------- transaction helper


def test_transaction_helper_persists_across_consecutive_writes(graph):
    graph.upsert_node(_pref(node_id="txn1"))
    graph.upsert_node(_pref(node_id="txn2"))
    page = graph.list_nodes(NodeFilter(profile_id="p1"), Page(0, 50))
    assert page.total == 2
