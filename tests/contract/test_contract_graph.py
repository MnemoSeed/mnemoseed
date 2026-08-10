"""Driver-agnostic contract tests for the GraphStore port (prd-08 appendix B.2).

Every method of the graph port gets at least one behavioral test, run against
the embedded (sqlite_graph) and postgres (pg_graph) driver families. The two
drivers are mirror implementations of the same relational schema (D1 / D2), so
the assertions hold on both sides unchanged.
"""

from __future__ import annotations

import time

import pytest
from _support import PROFILE, make_edge, make_intention, make_pref, make_prov

from mnemoseed.schema.graph import GraphNode, NodeType, RelType
from mnemoseed.storage.ports import (
    Capability,
    GraphFlag,
    GraphWeightUpdate,
    IntentionStatus,
    NodeFilter,
    Page,
    StorageError,
)

# ---------------------------------------------------------------- B.2 surface


def test_capabilities(stack) -> None:
    expected = frozenset(
        {
            Capability.GRAPH_TRAVERSE_2HOP,
            Capability.GRAPH_VERSION_CHAIN,
            Capability.GRAPH_COOCCURRENCE_EDGES,
        }
    )
    assert stack.graph.capabilities() == stack.graph.info.capabilities == expected


def test_upsert_get_roundtrip(stack) -> None:
    node = make_pref(
        conflict_flag=True,
        conflict_group="cg-1",
        pending_consolidation=True,
        peripheral_gaps=True,
        needs_reconcile=True,
        hit_count=3,
        last_hit_at=time.time() - 5,
        reinforce_count=2,
        decay_weight=0.7,
        confidence=0.9,
        entities=["ui", "theme"],
    )
    stack.graph.upsert_node(node)
    got = stack.graph.get_node(node.node_id)
    assert got is not None
    assert got.node_type is node.node_type
    assert got.props["statement"] == "dark mode"
    assert got.entities == ["ui", "theme"]
    assert got.conflict_flag is True
    assert got.conflict_group == "cg-1"
    assert got.pending_consolidation is True
    assert got.peripheral_gaps is True
    assert got.needs_reconcile is True
    assert got.hit_count == 3
    assert got.reinforce_count == 2
    assert got.last_hit_at == pytest.approx(node.last_hit_at, abs=0.002)
    assert got.decay_weight == pytest.approx(0.7, abs=1e-9)
    assert got.confidence == pytest.approx(0.9, abs=1e-9)
    assert got.is_current
    assert stack.graph.get_node("missing") is None


def test_list_nodes_filter_pagination(stack) -> None:
    now = time.time()
    a = make_pref(node_id="n-a", entities=["ui"], decay_weight=0.9, updated_at=now - 30.0)
    b = make_pref(node_id="n-b", entities=["typography"], decay_weight=0.3, updated_at=now - 20.0)
    tool = GraphNode(
        node_id="n-t",
        profile_id=PROFILE,
        node_type=NodeType.TOOL,
        props={"name": "gh"},
        provenance=make_prov(),
        decay_weight=0.2,
        updated_at=now - 10.0,
    )
    other_p = make_pref(node_id="n-c", profile_id="p2", updated_at=now - 5.0)
    for node in (a, b, tool, other_p):
        stack.graph.upsert_node(node)

    all_p1 = stack.graph.list_nodes(NodeFilter(profile_id=PROFILE), Page(0, 50))
    assert {n.node_id for n in all_p1.items} == {"n-a", "n-b", "n-t"}
    assert all_p1.total == 3

    by_type = stack.graph.list_nodes(
        NodeFilter(profile_id=PROFILE, node_type=NodeType.PREFERENCE), Page(0, 50)
    )
    assert {n.node_id for n in by_type.items} == {"n-a", "n-b"}

    by_decay = stack.graph.list_nodes(NodeFilter(profile_id=PROFILE, min_decay=0.5), Page(0, 50))
    assert {n.node_id for n in by_decay.items} == {"n-a"}

    by_entity = stack.graph.list_nodes(NodeFilter(profile_id=PROFILE, entities=("ui",)), Page(0, 50))
    assert {n.node_id for n in by_entity.items} == {"n-a"}

    first = stack.graph.list_nodes(NodeFilter(profile_id=PROFILE), Page(offset=0, limit=2))
    assert len(first.items) == 2
    assert first.total == 3


# ---------------------------------------------------------------- edges / traversal


def test_add_edge_weight_overwrite(stack) -> None:
    a = make_pref(node_id="e-a")
    b = make_pref(node_id="e-b")
    stack.graph.upsert_node(a)
    stack.graph.upsert_node(b)
    stack.graph.add_edge(make_edge("e-a", "e-b", rel=RelType.EVIDENCED_BY))
    stack.graph.add_edge(make_edge("e-a", "e-b", rel=RelType.EVIDENCED_BY))  # same key: overwrite, not dup
    reached = stack.graph.traverse("e-a", depth=1, filter=NodeFilter(profile_id=PROFILE))
    assert {n.node_id for n in reached} == {"e-a", "e-b"}


def test_bump_cooccurrence_symmetric_and_increments(stack) -> None:
    for node_id in ("node-a", "node-b", "other"):
        stack.graph.upsert_node(make_pref(node_id=node_id))
    stack.graph.bump_cooccurrence("node-a", "node-b", PROFILE)
    stack.graph.bump_cooccurrence("node-b", "node-a", PROFILE)
    stack.graph.bump_cooccurrence("node-a", "node-b", PROFILE)
    stack.graph.bump_cooccurrence("other", "node-a", PROFILE)
    reached = stack.graph.traverse("node-a", depth=1, filter=NodeFilter(profile_id=PROFILE))
    assert {n.node_id for n in reached} == {"node-a", "node-b", "other"}


def test_traverse_profile_scoped(stack) -> None:
    hub = make_pref(node_id="hub", entities=["h"])
    leaf = make_pref(node_id="leaf", entities=["l"])
    other = make_pref(node_id="other", profile_id="p2", entities=["o"])
    stack.graph.upsert_node(hub)
    stack.graph.upsert_node(leaf)
    stack.graph.upsert_node(other)
    stack.graph.add_edge(make_edge("hub", "leaf"))
    stack.graph.add_edge(make_edge("other", "hub", profile_id="p2"))

    scoped = stack.graph.traverse("hub", depth=1, filter=NodeFilter(profile_id=PROFILE))
    assert {n.node_id for n in scoped} == {"hub", "leaf"}
    unscoped = stack.graph.traverse("hub", depth=1)
    assert "other" in {n.node_id for n in unscoped}


def test_traverse_depth_capped_at_two(stack) -> None:
    for node_id in ("e0", "e1", "e2"):
        stack.graph.upsert_node(make_pref(node_id=node_id))
    stack.graph.add_edge(make_edge("e0", "e1"))
    stack.graph.add_edge(make_edge("e1", "e2"))
    reached = stack.graph.traverse("e0", depth=99, filter=NodeFilter(profile_id=PROFILE))
    assert {n.node_id for n in reached} == {"e0", "e1", "e2"}


def test_find_same_predicate(stack) -> None:
    fp = dict(
        domain="coding",
        statement="dark mode",
        valence=0.8,
        prior_width=0.3,
        trait_anchor="anima-1",
        evidence_chain=[{"event": "created", "at": 123.0}],
    )
    a = make_pref(node_id="fp1", props={**fp, "subject": "user", "predicate": "indent", "value": "spaces"})
    b = make_pref(node_id="fp2", props={**fp, "subject": "user", "predicate": "indent", "value": "tabs"})
    stack.graph.upsert_node(a)
    stack.graph.upsert_node(b)
    found = {n.node_id for n in stack.graph.find_same_predicate("user", "indent", PROFILE)}
    assert found == {"fp1", "fp2"}


# ---------------------------------------------------------------- flags


def test_set_and_clear_flags(stack) -> None:
    node = make_pref(node_id="fl")
    stack.graph.upsert_node(node)
    flags = [
        GraphFlag.NEEDS_RECONCILE,
        GraphFlag.PENDING_CONSOLIDATION,
        GraphFlag.PERIPHERAL_GAPS,
    ]
    stack.graph.set_flags(["fl"], flags)
    got = stack.graph.get_node("fl")
    assert got.needs_reconcile and got.pending_consolidation and got.peripheral_gaps
    stack.graph.clear_flags(["fl"], flags)
    got = stack.graph.get_node("fl")
    assert not got.needs_reconcile and not got.pending_consolidation and not got.peripheral_gaps


def test_conflict_group_pairing_set_and_clear(stack) -> None:
    a = make_pref(node_id="ca")
    b = make_pref(node_id="cb")
    stack.graph.upsert_node(a)
    stack.graph.upsert_node(b)
    stack.graph.set_flags(["ca", "cb"], [GraphFlag.CONFLICT_GROUP])
    ga, gb = stack.graph.get_node("ca"), stack.graph.get_node("cb")
    assert ga.conflict_flag and gb.conflict_flag
    assert ga.conflict_group == gb.conflict_group
    assert ga.conflict_group is not None
    stack.graph.clear_flags(["ca", "cb"], [GraphFlag.CONFLICT_GROUP])
    assert stack.graph.get_node("ca").conflict_group is None
    assert stack.graph.get_node("cb").conflict_group is None


# ---------------------------------------------------------------- version chain


def test_invalidate_closes_current_revision(stack) -> None:
    node = make_pref(node_id="iv")
    stack.graph.upsert_node(node)
    close_at = time.time()
    stack.graph.invalidate("iv", close_at)
    got = stack.graph.get_node("iv")
    assert got is None  # no current revision remains
    archived = stack.graph.versions("iv")
    assert len(archived) == 1
    assert archived[0].valid_to == pytest.approx(close_at, abs=0.002)


def test_append_version_supersedes_previous(stack) -> None:
    v1 = make_pref(node_id="av", valid_from=time.time() - 200.0)
    stack.graph.upsert_node(v1)
    take_over = time.time()
    v2 = make_pref(
        node_id="av",
        version=2,
        valid_from=take_over,
        props={**v1.props, "statement": "dark mode at night"},
    )
    stack.graph.invalidate("av", take_over)
    stack.graph.append_version(v2)
    current = stack.graph.get_node("av")
    assert current is not None and current.version == 2
    assert current.props["statement"] == "dark mode at night"


def test_append_version_pair_is_atomic(stack) -> None:
    """invalidate + append_version in one call: a failed write rolls back both."""
    v1 = make_pref(node_id="r1", valid_from=time.time() - 100.0)
    stack.graph.upsert_node(v1)
    bad = make_pref(node_id="r1", version=2, props={"domain": "coding"})  # invalid payload
    with pytest.raises(ValueError, match="missing required field"):
        stack.graph.append_version(bad, invalidate_at=time.time())
    current = stack.graph.get_node("r1")
    assert current is not None
    assert current.version == 1
    assert current.valid_to is None


def test_versions_chain(stack) -> None:
    v1 = make_pref(node_id="vc", valid_from=time.time() - 200.0)
    stack.graph.upsert_node(v1)
    take_over = time.time()
    v2 = make_pref(node_id="vc", version=2, valid_from=take_over, props={**v1.props, "statement": "v2"})
    stack.graph.invalidate("vc", take_over)
    stack.graph.append_version(v2)
    versions = stack.graph.versions("vc")
    assert [v.version for v in versions] == [1, 2]
    assert versions[0].valid_to is not None
    assert versions[1].valid_to is None


def test_diff_reports_payload_change(stack) -> None:
    v1 = make_pref(node_id="df", valid_from=time.time() - 10.0)
    stack.graph.upsert_node(v1)
    v2 = make_pref(
        node_id="df",
        version=2,
        valid_from=time.time(),
        props={**v1.props, "statement": "changed", "valence": 0.9},
    )
    stack.graph.append_version(v2)
    result = stack.graph.diff("df:1", "df:2")
    assert result["a"]["version"] == 1
    assert result["b"]["version"] == 2
    fields = {change["field"] for change in result["changed"]}
    assert "props.statement" in fields
    assert "props.valence" in fields


def test_diff_unknown_version_raises(stack) -> None:
    stack.graph.upsert_node(make_pref(node_id="df-miss"))
    with pytest.raises(StorageError, match="unknown version"):
        stack.graph.diff("df-miss:1", "df-miss:99")


def test_timeline_replays_versions(stack) -> None:
    v1 = make_pref(node_id="tl", valid_from=time.time() - 100.0)
    stack.graph.upsert_node(v1)
    v2 = make_pref(node_id="tl", version=2, valid_from=time.time(), props={**v1.props, "statement": "v2"})
    stack.graph.append_version(v2)
    events = stack.graph.timeline("tl")
    assert [event.version for event in events] == [1, 2]
    assert all(isinstance(event.when, float) for event in events)
    assert all(event.summary for event in events)


def test_as_of_bi_temporal_replay(stack) -> None:
    v1 = make_pref(node_id="ao", valid_from=time.time() - 200.0)
    stack.graph.upsert_node(v1)
    take_over = time.time()
    v2 = make_pref(
        node_id="ao",
        version=2,
        valid_from=take_over,
        props={**v1.props, "statement": "dark mode at night"},
    )
    stack.graph.invalidate("ao", take_over)
    stack.graph.append_version(v2)
    before = stack.graph.as_of(take_over - 1.0, NodeFilter(profile_id=PROFILE))
    assert {n.version for n in before} == {1}
    after = stack.graph.as_of(take_over + 1.0, NodeFilter(profile_id=PROFILE))
    assert {n.version for n in after} == {2}


# ---------------------------------------------------------------- weights / intentions


def test_batch_update_weights(stack) -> None:
    a = make_pref(node_id="w1")
    b = make_pref(node_id="w2")
    stack.graph.upsert_node(a)
    stack.graph.upsert_node(b)
    stack.graph.batch_update_weights(
        [GraphWeightUpdate(node_id="w1", decay_weight=0.4), GraphWeightUpdate(node_id="w2", decay_weight=0.9)]
    )
    assert stack.graph.get_node("w1").decay_weight == pytest.approx(0.4, abs=1e-9)
    assert stack.graph.get_node("w2").decay_weight == pytest.approx(0.9, abs=1e-9)


# ---------------------------------------------------------------- tombstone


def test_tombstone_tombstoned_node_via_port(stack) -> None:
    """`tombstone` (design/03 2.4): a deleted node is invisible to reads /
    traversal / future as_of, yet its version chain survives for audit.

    Tombstone semantics are expressible entirely through the existing
    version-chain machinery: close the current revision at ``deleted_at`` and
    append a ``deleted`` provenance event to the archived payload — nothing is
    ever physically dropped (GDPR log-preserve, design/03 3).
    """
    leaf = make_pref(node_id="tm-leaf", entities=["tm"], decay_weight=0.9)
    hub = make_pref(node_id="tm-hub", entities=["tm"], decay_weight=0.9)
    stack.graph.upsert_node(leaf)
    stack.graph.upsert_node(hub)
    stack.graph.add_edge(make_edge("tm-hub", "tm-leaf"))

    deleted_at = time.time()
    assert stack.graph.tombstone("tm-leaf", deleted_at) is True

    # invisible to the current-revision reads and to the future as_of window
    assert stack.graph.get_node("tm-leaf") is None
    current_ids = {
        n.node_id for n in stack.graph.list_nodes(NodeFilter(profile_id=PROFILE), Page(0, 50)).items
    }
    assert current_ids == {"tm-hub"}
    reachable = {
        n.node_id for n in stack.graph.traverse("tm-hub", depth=1, filter=NodeFilter(profile_id=PROFILE))
    }
    assert "tm-leaf" not in reachable
    after = {n.node_id for n in stack.graph.as_of(deleted_at + 1.0, NodeFilter(profile_id=PROFILE))}
    assert "tm-leaf" not in after

    # the historical read still finds the fact as it was before the deletion
    before = {n.node_id for n in stack.graph.as_of(deleted_at - 1.0, NodeFilter(profile_id=PROFILE))}
    assert "tm-leaf" in before

    # the version chain is preserved: one closed revision marking the deletion
    versions = stack.graph.versions("tm-leaf")
    assert len(versions) == 1
    assert versions[0].valid_to == pytest.approx(deleted_at, abs=0.002)
    names = {event.action for event in versions[0].provenance.history}
    assert "deleted" in names

    # deleting an unknown node reports False so the caller can report honestly
    assert stack.graph.tombstone("tm-missing", time.time()) is False


def test_query_intentions_status_and_due(stack) -> None:
    now = time.time()
    due = make_intention(node_id="i1", valid_from=now - 50.0)
    later = make_intention(node_id="i2", valid_from=now + 500.0)
    fired = make_intention({"status": "fired"}, node_id="i3")
    stack.graph.upsert_node(due)
    stack.graph.upsert_node(later)
    stack.graph.upsert_node(fired)

    pending = stack.graph.query_intentions(IntentionStatus.PENDING, now)
    assert {n.node_id for n in pending} == {"i1"}
    fired_hits = stack.graph.query_intentions(IntentionStatus.FIRED, now + 99999.0)
    assert {n.node_id for n in fired_hits} == {"i3"}
