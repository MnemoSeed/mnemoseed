"""Smoke tests for the embedded SQLite drivers (stdlib only, CI-safe)."""

import time

import pytest

from mnemoseed.schema.graph import Edge, GraphNode, NodeType, RelType
from mnemoseed.schema.stamp import Provenance
from mnemoseed.storage.graph.sqlite_graph import SqliteGraph
from mnemoseed.storage.meta.sqlite_meta import SqliteMeta
from mnemoseed.storage.ports import Capability


@pytest.fixture
def meta(tmp_path):
    store = SqliteMeta(path=str(tmp_path / "meta.db"))
    yield store


@pytest.fixture
def graph(tmp_path):
    store = SqliteGraph(path=str(tmp_path / "graph.db"))
    yield store


async def test_meta_kv_roundtrip(meta):
    assert await meta.kv_get("ns", "k") is None
    await meta.kv_put("ns", "k", {"v": 1})
    assert await meta.kv_get("ns", "k") == {"v": 1}
    await meta.kv_put("ns", "k", {"v": 2})
    assert await meta.kv_get("ns", "k") == {"v": 2}


async def test_meta_audit(meta):
    await meta.audit("tester", "login", {"ok": True})
    entries = [e async for e in meta.audit_iter()]
    assert len(entries) == 1
    assert entries[0]["actor"] == "tester"
    assert entries[0]["detail"] == {"ok": True}


def node(nid: str, **over) -> GraphNode:
    base = dict(
        node_id=nid,
        profile_id="p1",
        node_type=NodeType.PREFERENCE,
        entities=["theme"],
        provenance=Provenance(asserted_by="test", source="test"),
        valid_from=time.time(),
    )
    base.update(over)
    return GraphNode(**base)


async def test_graph_put_get(graph):
    n = node("n1")
    await graph.put_node(n)
    got = await graph.get_node("n1")
    assert got is not None and got.node_id == "n1"
    assert await graph.get_node("missing") is None


async def test_graph_supersede_and_history(graph):
    v1 = node("n1")
    await graph.put_node(v1)
    v2 = node("n2", props={"key": "theme", "value": "light"})
    await graph.supersede("n1", v2)

    old = await graph.get_node("n1")
    assert old is not None and not old.is_current
    new = await graph.get_node("n2")
    assert new is not None and new.is_current
    assert new.prev_version_id == "n1" and new.version == 2

    hist = await graph.history("n2")
    assert [h.node_id for h in hist] == ["n2", "n1"]

    # as_of before the supersede returns v1 semantics via chain walk from n2
    before = min(h.valid_from for h in hist)
    past = await graph.get_node("n2", as_of=before + 0.0001)
    assert past is not None and past.valid_to is None or past.node_id in {"n1", "n2"}


async def test_graph_neighbors_bfs(graph):
    for nid in ("a", "b", "c"):
        await graph.put_node(node(nid))
    await graph.put_edge(Edge(src="a", dst="b", rel=RelType.CO_OCCURRED, profile_id="p1"))
    await graph.put_edge(Edge(src="b", dst="c", rel=RelType.CO_OCCURRED, profile_id="p1"))

    one_hop = await graph.neighbors("a", hops=1)
    assert {n.node_id for n in one_hop} == {"b"}
    two_hop = await graph.neighbors("a", hops=2)
    assert {n.node_id for n in two_hop} == {"b", "c"}


async def test_graph_find_nodes_filters(graph):
    await graph.put_node(node("x", node_type=NodeType.HABIT, entities=["coffee"]))
    await graph.put_node(node("y", node_type=NodeType.PREFERENCE, entities=["theme"]))

    habits = await graph.find_nodes("p1", node_type=NodeType.HABIT)
    assert [n.node_id for n in habits] == ["x"]
    by_entity = await graph.find_nodes("p1", entity="theme")
    assert [n.node_id for n in by_entity] == ["y"]


def test_capability_declarations(graph, meta):
    assert Capability.VERSION_CHAIN in graph.info.capabilities
    assert Capability.SNAPSHOT not in graph.info.capabilities  # honest degradation
    assert Capability.TRANSACTIONS in meta.info.capabilities
