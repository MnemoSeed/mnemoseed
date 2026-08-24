"""PgGraphDriver unit surface: SQL-generation helpers, capability declarations,
constructor validation, and decode round-trips. No live Postgres required.
"""

import time

import pytest

from mnemoseed.schema.graph import GraphNode, NodeType
from mnemoseed.schema.stamp import Provenance
from mnemoseed.storage.drivers.pg_graph import (
    PgGraphDriver,
    _decode_version,
    _node_filter_clauses,
    _same_predicate_sql,
    _traverse_neighbor_sql,
    _upsert_sql,
)
from mnemoseed.storage.ports import Capability, NodeFilter, StorageError
from mnemoseed.storage.registry import GRAPH_DRIVERS, register

_PREF_PROPS: dict = {
    "domain": "coding",
    "statement": "dark mode",
    "valence": 0.8,
    "prior_width": 0.3,
    "trait_anchor": "anima-1",
    "evidence_chain": [{"event": "created", "at": 123.0}],
}


@pytest.fixture(autouse=True)
def _ensure_registered():
    if not GRAPH_DRIVERS.contains("pg_graph"):
        register(GRAPH_DRIVERS)(PgGraphDriver)
    yield


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


def test_registered_in_shared_registry():
    assert GRAPH_DRIVERS.contains("pg_graph")


def test_capabilities_declared_exactly_four():
    caps = PgGraphDriver.info.capabilities
    assert caps == frozenset(
        {
            Capability.GRAPH_TRAVERSE_2HOP,
            Capability.GRAPH_VERSION_CHAIN,
            Capability.GRAPH_COOCCURRENCE_EDGES,
            Capability.GRAPH_EDGE_LIST,
        }
    )


def test_constructor_requires_dsn_or_conn(monkeypatch):
    monkeypatch.delenv("MNEMOSEED_PG_DSN", raising=False)
    with pytest.raises(StorageError, match="dsn"):
        PgGraphDriver()


def test_upsert_sql_is_on_conflict_do_update():
    sql = _upsert_sql("nodes", ("node_id", "payload"), ("node_id",))
    assert sql == (
        "INSERT INTO nodes (node_id, payload) VALUES (%s, %s) "
        "ON CONFLICT (node_id) DO UPDATE SET "
        "node_id = EXCLUDED.node_id, payload = EXCLUDED.payload"
    )
    assert "INSERT OR REPLACE" not in sql


def test_node_filter_clauses_always_profile_scoped():
    clauses, params = _node_filter_clauses(NodeFilter(profile_id="p1"))
    assert clauses == ["valid_to IS NULL", "profile_id = %s"]
    assert params == ["p1"]


def test_node_filter_clauses_type_decay_entities():
    clauses, params = _node_filter_clauses(
        NodeFilter(profile_id="p1", node_type=NodeType.PREFERENCE, min_decay=0.5, entities=("ui", "theme"))
    )
    assert "node_type = %s" in clauses
    assert "decay_weight >= %s" in clauses
    assert any("jsonb_array_elements_text(entities)" in c for c in clauses)
    assert params[0] == "p1"
    assert params[-1] == ["ui", "theme"]


def test_same_predicate_sql_uses_jsonb_accessors():
    sql = _same_predicate_sql()
    assert "payload->>'subject' = %s" in sql
    assert "payload->>'predicate' = %s" in sql
    assert "valid_to IS NULL" in sql


def test_traverse_neighbor_sql_profile_scoping():
    plain = _traverse_neighbor_sql(False)
    scoped = _traverse_neighbor_sql(True)
    assert plain.endswith("(src = %s OR dst = %s)")
    assert scoped.endswith("(src = %s OR dst = %s) AND profile_id = %s")


def test_node_row_matches_column_order():
    node = _pref()
    row = PgGraphDriver._node_row(None, node)
    assert len(row) == 26
    assert row[0] == node.node_id
    # payload goes in as a plain dict for JSONB (never a serialized string)
    assert isinstance(row[3], dict)
    # v5 carrier column: defaulted to promoted when the caller does not say so
    assert row[-1] == "promoted"


def test_decode_version_roundtrip():
    row = {
        "payload": _pref().model_dump(),
        "version": 2,
        "valid_from": "2026-01-01T00:00:00.000Z",
        "valid_to": "2026-01-02T00:00:00.000Z",
    }
    node = _decode_version(row)
    assert node.version == 2
    assert node.props["statement"] == "dark mode"
    assert node.valid_to is not None
