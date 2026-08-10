"""Postgres graph driver: cortex over plain relational tables (prd-08 FR-8.4, D2).

Mirror of the sqlite_graph driver: the same three tables (nodes / edges /
node_versions per appendix A.2), the same query semantics, and the same
version-chain atomicity. No graph engine (D2) — any managed Postgres can run
it.

D6 named instances: a schema per instance (graph.main defaults to "public",
graph.isolated passes ``schema="isolated"``), so the two graphs are physically
separate Postgres schemas. Version-chain writes (invalidate / append_version)
run in one psycopg transaction so reconsolidation never leaves the graph
half-written.

Design note: payload/provenance live in JSONB columns; the PSQL accessors used
in filters are the jsonb arrows (->> / ::double precision) which mirror the
SQLite json_extract/json_each calls.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Iterator
from collections.abc import Sequence as CSeq
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb, JsonbDumper, register_default_adapters

from mnemoseed.schema.graph import Edge, GraphNode, RelType, validate_node_payload
from mnemoseed.storage.drivers._migrations import apply_postgres_migrations
from mnemoseed.storage.drivers._time import epoch_from_iso, iso8601_utc
from mnemoseed.storage.drivers.sqlite_graph import _deep_changes, _summarize_version
from mnemoseed.storage.ports import (
    Capability,
    DriverInfo,
    GraphFlag,
    GraphWeightUpdate,
    IntentionStatus,
    NodeFilter,
    Page,
    PageResult,
    StorageError,
    TimelineEvent,
)
from mnemoseed.storage.registry import GRAPH_DRIVERS, register

_CAPABILITIES = frozenset(
    {
        Capability.GRAPH_TRAVERSE_2HOP,
        Capability.GRAPH_VERSION_CHAIN,
        Capability.GRAPH_COOCCURRENCE_EDGES,
    }
)

_NODE_COLUMNS = (
    "node_id",
    "node_type",
    "profile_id",
    "payload",
    "entities",
    "confidence",
    "decay_weight",
    "never_decay",
    "conflict_flag",
    "conflict_group",
    "needs_reconcile",
    "pending_consolidation",
    "peripheral_gaps",
    "valid_from",
    "valid_to",
    "last_reinforced",
    "hit_count",
    "last_hit_at",
    "reinforce_count",
    "cognitive_tier",
    "provenance",
    "created_at",
    "updated_at",
    "version",
    "prev_version_id",
)

_NODE_VERSIONS_COLUMNS = (
    "node_id",
    "version",
    "profile_id",
    "valid_from",
    "valid_to",
    "superseded_by",
    "changed_at",
    "payload",
)


def _upsert_sql(table: str, columns: tuple[str, ...], conflict: tuple[str, ...]) -> str:
    """INSERT .. ON CONFLICT .. DO UPDATE — the PG spelling of INSERT OR REPLACE.

    DO UPDATE over every column is a full overwrite, exactly matching SQLite's
    REPLACE semantics for a complete row.
    """
    cols = ", ".join(columns)
    placeholders = ", ".join(["%s"] * len(columns))
    targets = ", ".join(conflict)
    updates = ", ".join(f"{column} = EXCLUDED.{column}" for column in columns)
    return (
        f"INSERT INTO {table} ({cols}) VALUES ({placeholders}) "
        f"ON CONFLICT ({targets}) DO UPDATE SET {updates}"
    )


_NODE_UPSERT_SQL = _upsert_sql("nodes", _NODE_COLUMNS, ("node_id",))
# node_versions rows are minimum-column inserts (unique constraint on
# (node_id, version) enforced by the migration), not full-row upserts.


def _node_filter_clauses(filter: NodeFilter) -> tuple[list[str], list[Any]]:
    """WHERE fragments for current-revision node reads plus their params."""
    clauses = ["valid_to IS NULL", "profile_id = %s"]
    params: list[Any] = [filter.profile_id]
    if filter.node_type is not None:
        clauses.append("node_type = %s")
        params.append(filter.node_type.value)
    if filter.min_decay > 0.0:
        clauses.append("decay_weight >= %s")
        params.append(filter.min_decay)
    if filter.entities:
        clauses.append("EXISTS (SELECT 1 FROM jsonb_array_elements_text(entities) e WHERE e = ANY(%s))")
        params.append(list(filter.entities))
    return clauses, params


def _same_predicate_sql() -> str:
    return (
        "SELECT * FROM nodes WHERE profile_id = %s AND valid_to IS NULL "
        "AND payload->>'subject' = %s AND payload->>'predicate' = %s"
    )


def _traverse_neighbor_sql(profile_scoped: bool) -> str:
    base = (
        "SELECT CASE WHEN src = %s THEN dst ELSE src END AS neighbor FROM edges WHERE (src = %s OR dst = %s)"
    )
    if profile_scoped:
        return base + " AND profile_id = %s"
    return base


def _intention_sql() -> str:
    return (
        "SELECT * FROM nodes WHERE valid_to IS NULL AND node_type = 'INTENTION' "
        "AND payload->>'status' = %s AND valid_from <= %s ORDER BY valid_from"
    )


def _json_value(value: Any) -> Any:
    """JSONB comes back parsed by psycopg; tolerate raw text like SQLite."""
    if isinstance(value, str):
        return json.loads(value) if value else None
    return value


def _maybe_epoch(value: Any) -> float | None:
    return epoch_from_iso(str(value)) if value is not None else None


def _decode_version(row: Any) -> GraphNode:
    node = GraphNode.model_validate(_json_value(row["payload"]))
    valid_from = row["valid_from"]
    valid_to = row["valid_to"]
    if valid_from is not None:
        node.valid_from = epoch_from_iso(str(valid_from))
    if valid_to is not None:
        node.valid_to = epoch_from_iso(str(valid_to))
    node.version = int(row["version"])
    return node


@register(GRAPH_DRIVERS)
class PgGraphDriver:
    """GraphStore over plain relational Postgres tables."""

    info = DriverInfo(
        name="pg_graph",
        capabilities=_CAPABILITIES,
        description="nodes/edges/node_versions over relational Postgres (docker preset)",
    )

    def __init__(
        self,
        dsn: str | None = None,
        conn: Any | None = None,
        schema: str = "public",
        **kwargs: Any,
    ) -> None:
        self.params: dict[str, Any] = kwargs
        if dsn is None:
            dsn = kwargs.get("dsn")
        if schema == "public":
            schema = kwargs.get("schema", schema)
        self._schema = schema or "public"
        if dsn is None:
            dsn = os.environ.get("MNEMOSEED_PG_DSN")
        if conn is None:
            if dsn is None:
                raise StorageError("pg_graph requires a 'dsn' connection string (or a 'conn' connection)")
            conn = psycopg.connect(dsn)
            self._owns_conn = True
        else:
            self._owns_conn = False
        self._conn = conn
        self._conn.row_factory = dict_row
        register_default_adapters(self._conn)
        # psycopg3 dumps dict/list to JSON only when wrapped; this connection
        # dumps bare dicts as JSONB so payloads go through unwrapped (the
        # entity list below is wrapped explicitly).
        self._conn.adapters.register_dumper(dict, JsonbDumper)
        self._init_schema()

    def capabilities(self) -> frozenset[Capability]:
        return self.info.capabilities

    async def close(self) -> None:
        if self._owns_conn:
            self._conn.close()

    # ------------------------------------------------------------ internals

    def _init_schema(self) -> None:
        apply_postgres_migrations(self._conn, "graph", schema=self._schema)

    def _exec_rows(self, sql: str, params: CSeq[Any] | None = None) -> list[Any]:
        return self._conn.execute(sql, params).fetchall()

    def _exec_row(self, sql: str, params: CSeq[Any] | None = None) -> Any | None:
        return self._conn.execute(sql, params).fetchone()

    def _node_row(self, node: GraphNode) -> tuple[Any, ...]:
        row: dict[str, Any] = {
            "node_id": node.node_id,
            "node_type": node.node_type.value,
            "profile_id": node.profile_id,
            "payload": node.props,
            "entities": Jsonb(list(node.entities)),
            "confidence": node.confidence,
            "decay_weight": node.decay_weight,
            "never_decay": int(node.never_decay),
            "conflict_flag": int(node.conflict_flag),
            "conflict_group": node.conflict_group,
            "needs_reconcile": int(node.needs_reconcile),
            "pending_consolidation": int(node.pending_consolidation),
            "peripheral_gaps": int(node.peripheral_gaps),
            "valid_from": iso8601_utc(node.valid_from),
            "valid_to": iso8601_utc(node.valid_to) if node.valid_to is not None else None,
            "last_reinforced": iso8601_utc(node.last_reinforced),
            "hit_count": node.hit_count,
            "last_hit_at": iso8601_utc(node.last_hit_at) if node.last_hit_at is not None else None,
            "reinforce_count": node.reinforce_count,
            "cognitive_tier": node.cognitive_tier,
            "provenance": node.provenance.model_dump(),
            "created_at": iso8601_utc(node.created_at),
            "updated_at": iso8601_utc(node.updated_at),
            "version": node.version,
            "prev_version_id": node.prev_version_id,
        }
        return tuple(row[column] for column in _NODE_COLUMNS)

    def _decode_node(self, row: Any) -> GraphNode:
        data: dict[str, Any] = {
            "node_id": str(row["node_id"]),
            "node_type": str(row["node_type"]),
            "profile_id": str(row["profile_id"]),
            "props": _json_value(row["payload"]),
            "entities": _json_value(row["entities"]) or [],
            "confidence": float(row["confidence"]),
            "decay_weight": float(row["decay_weight"]),
            "never_decay": bool(int(row["never_decay"])),
            "conflict_flag": bool(int(row["conflict_flag"])),
            "conflict_group": row["conflict_group"],
            "needs_reconcile": bool(int(row["needs_reconcile"])),
            "pending_consolidation": bool(int(row["pending_consolidation"])),
            "peripheral_gaps": bool(int(row["peripheral_gaps"])),
            "valid_from": epoch_from_iso(str(row["valid_from"])),
            "valid_to": _maybe_epoch(row["valid_to"]),
            "last_reinforced": epoch_from_iso(str(row["last_reinforced"])),
            "hit_count": int(row["hit_count"]),
            "last_hit_at": _maybe_epoch(row["last_hit_at"]),
            "reinforce_count": int(row["reinforce_count"]),
            "cognitive_tier": int(row["cognitive_tier"]),
            "provenance": _json_value(row["provenance"]),
            "created_at": epoch_from_iso(str(row["created_at"])),
            "updated_at": epoch_from_iso(str(row["updated_at"])),
            "version": int(row["version"]),
            "prev_version_id": row["prev_version_id"],
        }
        return GraphNode.model_validate(data)

    def _get_current_version(self, node_id: str) -> int | None:
        row = self._exec_row("SELECT version FROM nodes WHERE node_id = %s AND valid_to IS NULL", (node_id,))
        return int(row["version"]) if row is not None else None

    def _write_revision(self, node: GraphNode, invalidate_at: float | None = None) -> None:
        with _transaction(self._conn):
            validate_node_payload(node.node_type, node.props)
            current_version = self._get_current_version(node.node_id)
            if current_version is not None:
                if invalidate_at is not None:
                    self._invalidate_locked(node.node_id, invalidate_at)
                    self._conn.execute(
                        "UPDATE node_versions SET superseded_by = %s WHERE node_id = %s AND version = %s",
                        (node.version, node.node_id, current_version),
                    )
                elif current_version < node.version:
                    self._supersede_snapshot(node.node_id, current_version, node.version, node.valid_from)
            self._conn.execute(_NODE_UPSERT_SQL, self._node_row(node))
            self._conn.execute(
                "INSERT INTO node_versions (node_id, version, profile_id, valid_from, valid_to, "
                "superseded_by, changed_at, payload) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (node_id, version) DO UPDATE SET "
                "profile_id = EXCLUDED.profile_id, valid_from = EXCLUDED.valid_from, "
                "valid_to = EXCLUDED.valid_to, superseded_by = EXCLUDED.superseded_by, "
                "changed_at = EXCLUDED.changed_at, payload = EXCLUDED.payload",
                (
                    node.node_id,
                    node.version,
                    node.profile_id,
                    iso8601_utc(node.valid_from),
                    iso8601_utc(node.valid_to) if node.valid_to is not None else None,
                    None,  # re-upsert rewrites the whole row, like INSERT OR REPLACE
                    iso8601_utc(node.updated_at),
                    node.model_dump(),  # JSONB wants the object, not a serialized string
                ),
            )

    def _supersede_snapshot(
        self, node_id: str, old_version: int, new_version: int, took_over_at: float
    ) -> None:
        row = self._exec_row(
            "SELECT payload, valid_to FROM node_versions WHERE node_id = %s AND version = %s",
            (node_id, old_version),
        )
        if row is None:
            return
        payload = _json_value(row["payload"])
        if row["valid_to"] is None and payload.get("valid_to") is None:
            payload["valid_to"] = took_over_at
            self._conn.execute(
                "UPDATE node_versions SET payload = %s, valid_to = %s WHERE node_id = %s AND version = %s",
                (payload, iso8601_utc(took_over_at), node_id, old_version),
            )
        self._conn.execute(
            "UPDATE node_versions SET superseded_by = %s WHERE node_id = %s AND version = %s",
            (new_version, node_id, old_version),
        )

    # ------------------------------------------------------------ node CRUD

    def upsert_node(self, node: GraphNode) -> None:
        self._write_revision(node)

    def get_node(self, node_id: str) -> GraphNode | None:
        row = self._exec_row("SELECT * FROM nodes WHERE node_id = %s AND valid_to IS NULL", (node_id,))
        return self._decode_node(row) if row is not None else None

    def list_nodes(self, filter: NodeFilter, page: Page) -> PageResult[GraphNode]:
        clauses, params = _node_filter_clauses(filter)
        where = " AND ".join(clauses)
        total = _count(self._conn, f"SELECT COUNT(*) FROM nodes WHERE {where}", params)
        rows = self._exec_rows(
            f"SELECT * FROM nodes WHERE {where} ORDER BY updated_at DESC LIMIT %s OFFSET %s",
            [*params, page.limit, page.offset],
        )
        items = [self._decode_node(row) for row in rows]
        return PageResult(items=items, total=total, offset=page.offset, limit=page.limit)

    def find_same_predicate(self, subject: str, predicate: str, profile_id: str) -> list[GraphNode]:
        rows = self._exec_rows(_same_predicate_sql(), (profile_id, subject, predicate))
        return [self._decode_node(row) for row in rows]

    # ------------------------------------------------------------ edges

    def add_edge(self, edge: Edge) -> None:
        with _transaction(self._conn):
            existing = self._exec_row(
                "SELECT id FROM edges WHERE src = %s AND dst = %s AND rel = %s AND profile_id = %s",
                (edge.src, edge.dst, edge.rel.value, edge.profile_id),
            )
            if existing is not None:
                self._conn.execute(
                    "UPDATE edges SET weight = %s WHERE id = %s", (edge.weight, str(existing["id"]))
                )
            else:
                self._conn.execute(
                    "INSERT INTO edges (id, src, dst, rel, weight, profile_id, provenance, created_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        uuid.uuid4().hex,
                        edge.src,
                        edge.dst,
                        edge.rel.value,
                        edge.weight,
                        edge.profile_id,
                        {},
                        iso8601_utc(edge.created_at),
                    ),
                )

    def bump_cooccurrence(self, node_a: str, node_b: str, profile_id: str) -> None:
        with _transaction(self._conn):
            existing = self._exec_row(
                "SELECT id, weight FROM edges WHERE rel = %s AND profile_id = %s "
                "AND ((src = %s AND dst = %s) OR (src = %s AND dst = %s))",
                (RelType.CO_OCCURRED.value, profile_id, node_a, node_b, node_b, node_a),
            )
            if existing is not None:
                self._conn.execute(
                    "UPDATE edges SET weight = %s WHERE id = %s",
                    (float(existing["weight"]) + 1.0, str(existing["id"])),
                )
            else:
                self._conn.execute(
                    "INSERT INTO edges (id, src, dst, rel, weight, profile_id, provenance, created_at) "
                    "VALUES (%s, %s, %s, %s, 1.0, %s, %s, %s)",
                    (
                        uuid.uuid4().hex,
                        node_a,
                        node_b,
                        RelType.CO_OCCURRED.value,
                        profile_id,
                        {},
                        iso8601_utc(time.time()),
                    ),
                )

    def traverse(self, node_id: str, depth: int = 2, filter: NodeFilter | None = None) -> list[GraphNode]:
        if depth < 0:
            raise ValueError("traverse depth must be non-negative")
        depth = min(depth, 2)
        order: list[str] = []
        visited: set[str] = set()
        frontier: list[str] = [node_id]
        for hop in range(depth + 1):
            next_frontier: list[str] = []
            for nid in frontier:
                if nid in visited:
                    continue
                visited.add(nid)
                order.append(nid)
                if hop == depth:
                    continue
                params: list[Any] = [nid, nid, nid]
                if filter is not None:
                    params.append(filter.profile_id)
                neighbors = self._exec_rows(_traverse_neighbor_sql(filter is not None), params)
                next_frontier.extend(str(n["neighbor"]) for n in neighbors)
            frontier = next_frontier
        if not order:
            return []
        rows = self._exec_rows("SELECT * FROM nodes WHERE node_id = ANY(%s) AND valid_to IS NULL", (order,))
        decoded = {node.node_id: node for node in (self._decode_node(row) for row in rows)}
        result = [decoded[nid] for nid in order if nid in decoded]
        if filter is not None:
            result = [
                n
                for n in result
                if (filter.node_type is None or n.node_type is filter.node_type)
                and n.decay_weight >= filter.min_decay
            ]
        return result

    # ------------------------------------------------------------ flags

    def set_flags(self, nodes: CSeq[str], flags: CSeq[GraphFlag]) -> None:
        self._apply_flags(nodes, flags, set_to=True)

    def clear_flags(self, nodes: CSeq[str], flags: CSeq[GraphFlag]) -> None:
        self._apply_flags(nodes, flags, set_to=False)

    def _apply_flags(self, nodes: CSeq[str], flags: CSeq[GraphFlag], set_to: bool) -> None:
        if not nodes:
            return
        nodes_list = list(nodes)
        with _transaction(self._conn):
            if GraphFlag.CONFLICT_GROUP in flags:
                if set_to:
                    existing = self._exec_rows(
                        "SELECT conflict_group FROM nodes WHERE node_id = ANY(%s) AND valid_to IS NULL",
                        (nodes_list,),
                    )
                    group_id = next(
                        (str(r["conflict_group"]) for r in existing if r["conflict_group"] is not None),
                        None,
                    )
                    if group_id is None:
                        group_id = uuid.uuid4().hex
                    self._conn.execute(
                        "UPDATE nodes SET conflict_flag = 1, conflict_group = %s "
                        "WHERE node_id = ANY(%s) AND valid_to IS NULL",
                        (group_id, nodes_list),
                    )
                else:
                    self._conn.execute(
                        "UPDATE nodes SET conflict_flag = 0, conflict_group = NULL "
                        "WHERE node_id = ANY(%s) AND valid_to IS NULL",
                        (nodes_list,),
                    )
            column_map: dict[GraphFlag, str] = {
                GraphFlag.NEEDS_RECONCILE: "needs_reconcile",
                GraphFlag.PENDING_CONSOLIDATION: "pending_consolidation",
                GraphFlag.PERIPHERAL_GAPS: "peripheral_gaps",
            }
            for flag, column in column_map.items():
                if flag not in flags:
                    continue
                value = 1 if set_to else 0
                self._conn.execute(
                    f"UPDATE nodes SET {column} = %s WHERE node_id = ANY(%s) AND valid_to IS NULL",
                    (value, nodes_list),
                )

    # ------------------------------------------------------------ version chain

    def invalidate(self, node_id: str, valid_to: float) -> None:
        with _transaction(self._conn):
            self._invalidate_locked(node_id, valid_to)

    def _invalidate_locked(self, node_id: str, valid_to: float) -> None:
        current_version = self._get_current_version(node_id)
        if current_version is None:
            return
        row = self._exec_row(
            "SELECT payload FROM node_versions WHERE node_id = %s AND version = %s",
            (node_id, current_version),
        )
        if row is not None:
            payload = _json_value(row["payload"])
            if isinstance(payload, dict) and payload.get("valid_to") is None:
                payload["valid_to"] = valid_to
                self._conn.execute(
                    "UPDATE node_versions SET valid_to = %s, payload = %s "
                    "WHERE node_id = %s AND version = %s",
                    (iso8601_utc(valid_to), payload, node_id, current_version),
                )
            else:
                self._conn.execute(
                    "UPDATE node_versions SET valid_to = %s WHERE node_id = %s AND version = %s",
                    (iso8601_utc(valid_to), node_id, current_version),
                )
        self._conn.execute(
            "UPDATE nodes SET valid_to = %s, updated_at = %s WHERE node_id = %s AND valid_to IS NULL",
            (iso8601_utc(valid_to), iso8601_utc(time.time()), node_id),
        )

    def tombstone(self, node_id: str, deleted_at: float | None = None) -> bool:
        """Tombstone the current revision (design/03 2.4); postgres mirror of the
        sqlite driver. Close the current revision at ``deleted_at`` and append a
        ``deleted`` provenance event to its version-chain payload. Returns False
        when the node has no current revision to tombstone."""
        at = time.time() if deleted_at is None else deleted_at
        with _transaction(self._conn):
            current_version = self._get_current_version(node_id)
            if current_version is None:
                return False
            self._invalidate_locked(node_id, at)
            row = self._exec_row(
                "SELECT payload FROM node_versions WHERE node_id = %s AND version = %s",
                (node_id, current_version),
            )
            if row is not None:
                payload = _json_value(row["payload"])
                if isinstance(payload, dict):
                    provenance = payload.setdefault("provenance", {})
                    history = provenance.setdefault("history", [])
                    if not any(event.get("action") == "deleted" for event in history):
                        history.append({"at": at, "action": "deleted", "actor": "user", "detail": {}})
                    self._conn.execute(
                        "UPDATE node_versions SET payload = %s WHERE node_id = %s AND version = %s",
                        (payload, node_id, current_version),
                    )
            return True

    def append_version(self, node: GraphNode, *, invalidate_at: float | None = None) -> None:
        self._write_revision(node, invalidate_at=invalidate_at)

    def versions(self, node_id: str) -> list[GraphNode]:
        rows = self._exec_rows(
            "SELECT payload, version, valid_from, valid_to FROM node_versions "
            "WHERE node_id = %s ORDER BY version",
            (node_id,),
        )
        return [_decode_version(row) for row in rows]

    def diff(self, version_a: str, version_b: str) -> dict[str, Any]:
        node_a = self._fetch_version(version_a)
        node_b = self._fetch_version(version_b)
        if node_a is None or node_b is None:
            missing = version_a if node_a is None else version_b
            raise StorageError(f"unknown version identifier {missing!r}")
        changes = _deep_changes(node_a.model_dump(), node_b.model_dump())
        return {
            "a": {"node_id": node_a.node_id, "version": node_a.version},
            "b": {"node_id": node_b.node_id, "version": node_b.version},
            "changed": changes,
        }

    def timeline(self, node_id: str) -> list[TimelineEvent]:
        rows = self._exec_rows(
            "SELECT version, changed_at, payload FROM node_versions WHERE node_id = %s ORDER BY version",
            (node_id,),
        )
        events: list[TimelineEvent] = []
        for row in rows:
            version = int(row["version"])
            node = GraphNode.model_validate(_json_value(row["payload"]))
            node.version = version
            events.append(
                TimelineEvent(
                    when=epoch_from_iso(str(row["changed_at"])),
                    version=version,
                    summary=_summarize_version(node, version),
                )
            )
        return events

    def as_of(self, timestamp: float, filter: NodeFilter) -> list[GraphNode]:
        clauses = ["profile_id = %s", "valid_from <= %s", "(valid_to IS NULL OR valid_to > %s)"]
        params: list[Any] = [filter.profile_id, iso8601_utc(timestamp), iso8601_utc(timestamp)]
        if filter.node_type is not None:
            clauses.append("payload->>'node_type' = %s")
            params.append(filter.node_type.value)
        if filter.min_decay > 0.0:
            clauses.append("(payload->>'decay_weight')::double precision >= %s")
            params.append(filter.min_decay)
        where = " AND ".join(clauses)
        rows = self._exec_rows(
            f"SELECT payload, version, valid_from, valid_to FROM node_versions "
            f"WHERE {where} ORDER BY valid_from",
            params,
        )
        return [_decode_version(row) for row in rows]

    # ------------------------------------------------------------ weights / intentions

    def batch_update_weights(self, updates: CSeq[GraphWeightUpdate]) -> None:
        if not updates:
            return
        now = iso8601_utc(time.time())
        with _transaction(self._conn):
            for update in updates:
                self._conn.execute(
                    "UPDATE nodes SET decay_weight = %s, updated_at = %s "
                    "WHERE node_id = %s AND valid_to IS NULL",
                    (update.decay_weight, now, update.node_id),
                )

    def query_intentions(self, status: IntentionStatus, due_before: float) -> list[GraphNode]:
        rows = self._exec_rows(_intention_sql(), (status.value, iso8601_utc(due_before)))
        return [self._decode_node(row) for row in rows]

    # ------------------------------------------------------------ internals

    def _fetch_version(self, version_id: str) -> GraphNode | None:
        node_id, _, version_str = version_id.partition(":")
        if not node_id or not version_str.isdigit():
            return None
        row = self._exec_row(
            "SELECT payload, version, valid_from, valid_to FROM node_versions "
            "WHERE node_id = %s AND version = %s",
            (node_id, int(version_str)),
        )
        return _decode_version(row) if row is not None else None


# ---------------------------------------------------------------- module helpers


def _count(conn: Any, sql: str, params: CSeq[Any] | None) -> int:
    row = conn.execute(sql, params).fetchone()
    if row is None:
        return 0
    return int(row["count"]) if isinstance(row, dict) else int(row[0])


@contextmanager
def _transaction(conn: Any) -> Iterator[None]:
    """Begin a real outer transaction; roll back any lingering read transaction
    first so psycopg3 does not nest into a savepoint."""
    conn.rollback()
    with conn.transaction():
        yield
