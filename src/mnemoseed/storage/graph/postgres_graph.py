"""Postgres GraphStore driver (alternative; optional extra).

Same semantics as the SQLite driver: nodes/edges tables, full GraphNode JSON
in a JSONB payload column, BFS traversal, transactional supersede. Adds the
SNAPSHOT capability (consistent snapshots), so dream-engine snapshot isolation
does not degrade.
"""

from __future__ import annotations

import json
import time
from collections import deque
from typing import Any

from mnemoseed.schema.graph import Edge, GraphNode, NodeType
from mnemoseed.storage.ports import GRAPH_DRIVERS, Capability, DriverInfo, GraphStore, register


def _loads(payload: Any) -> str:
    """psycopg JSONB may come back as str or already-parsed dict; normalize to
    a JSON string."""
    return payload if isinstance(payload, str) else json.dumps(payload)


_NODE_COLS = (
    "node_id, profile_id, node_type, entities, payload, valid_from, valid_to, decay_weight, updated_at"
)


_DDL = """
CREATE TABLE IF NOT EXISTS mnemo_nodes (
    node_id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL,
    node_type TEXT NOT NULL,
    entities JSONB NOT NULL DEFAULT '[]',
    payload JSONB NOT NULL,
    valid_from DOUBLE PRECISION NOT NULL,
    valid_to DOUBLE PRECISION,
    decay_weight DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    updated_at DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mnemo_nodes_profile ON mnemo_nodes(profile_id, node_type);
CREATE INDEX IF NOT EXISTS idx_mnemo_nodes_valid ON mnemo_nodes(profile_id, valid_to);
CREATE TABLE IF NOT EXISTS mnemo_edges (
    src TEXT NOT NULL,
    dst TEXT NOT NULL,
    rel TEXT NOT NULL,
    weight DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    profile_id TEXT NOT NULL,
    created_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (src, dst, rel)
);
CREATE INDEX IF NOT EXISTS idx_mnemo_edges_src ON mnemo_edges(src);
CREATE INDEX IF NOT EXISTS idx_mnemo_edges_dst ON mnemo_edges(dst);
"""


@register(GRAPH_DRIVERS)
class PostgresGraph(GraphStore):
    info = DriverInfo(
        name="postgres_graph",
        capabilities=frozenset(
            {
                Capability.VERSION_CHAIN,
                Capability.TRANSACTIONS,
                Capability.TRAVERSAL,
                Capability.PERSIST,
                Capability.SNAPSHOT,
            }
        ),
        description="Postgres graph store (consistent snapshots)",
    )

    def __init__(self, dsn: str) -> None:
        import psycopg  # lazy import: optional dependency

        self._dsn = dsn
        self._conn = psycopg.connect(dsn, autocommit=True)
        with self._conn.cursor() as cur:
            cur.execute(_DDL)

    def _insert(self, node: GraphNode) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO mnemo_nodes ({_NODE_COLS}) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (node_id) DO UPDATE SET payload=EXCLUDED.payload, "
                "valid_to=EXCLUDED.valid_to, decay_weight=EXCLUDED.decay_weight, "
                "updated_at=EXCLUDED.updated_at",
                (
                    node.node_id,
                    node.profile_id,
                    node.node_type.value,
                    json.dumps(node.entities, ensure_ascii=False),
                    node.model_dump_json(),
                    node.valid_from,
                    node.valid_to,
                    node.decay_weight,
                    time.time(),
                ),
            )

    async def put_node(self, node: GraphNode) -> None:
        self._insert(node)

    async def get_node(self, node_id: str, as_of: float | None = None) -> GraphNode | None:
        with self._conn.cursor() as cur:
            cur.execute("SELECT payload FROM mnemo_nodes WHERE node_id=%s", (node_id,))
            row = cur.fetchone()
        if not row:
            return None
        node = GraphNode.model_validate_json(_loads(row[0]))
        if as_of is None:
            return node
        # point-in-time: walk the version chain to the version live at as_of
        seen = node
        while True:
            if seen.valid_from <= as_of and (seen.valid_to is None or as_of < seen.valid_to):
                return seen
            if seen.prev_version_id is None:
                return None
            with self._conn.cursor() as cur:
                cur.execute("SELECT payload FROM mnemo_nodes WHERE node_id=%s", (seen.prev_version_id,))
                row = cur.fetchone()
            if not row:
                return None
            seen = GraphNode.model_validate_json(_loads(row[0]))

    async def find_nodes(
        self,
        profile_id: str,
        node_type: NodeType | None = None,
        entity: str | None = None,
        min_decay: float = 0.0,
        limit: int = 50,
    ) -> list[GraphNode]:
        sql = (
            "SELECT payload FROM mnemo_nodes WHERE profile_id=%s AND valid_to IS NULL AND decay_weight >= %s"
        )
        params: list = [profile_id, min_decay]
        if node_type is not None:
            sql += " AND node_type=%s"
            params.append(node_type.value)
        if entity is not None:
            sql += " AND entities @> %s"
            params.append(json.dumps([entity]))
        sql += " ORDER BY updated_at DESC LIMIT %s"
        params.append(limit)

        out: list[GraphNode] = []
        with self._conn.cursor() as cur:
            cur.execute(sql, params)
            for (payload,) in cur.fetchall():
                out.append(GraphNode.model_validate_json(_loads(payload)))
        return out

    async def put_edge(self, edge: Edge) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO mnemo_edges (src, dst, rel, weight, profile_id, created_at) "
                "VALUES (%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (src, dst, rel) DO UPDATE SET weight=mnemo_edges.weight+EXCLUDED.weight",
                (edge.src, edge.dst, edge.rel.value, edge.weight, edge.profile_id, edge.created_at),
            )

    async def neighbors(self, node_id: str, hops: int = 2) -> list[GraphNode]:
        """BFS N-hop traversal (the carrier of spreading activation)."""
        visited = {node_id}
        queue: deque[tuple[str, int]] = deque([(node_id, 0)])
        out: list[GraphNode] = []
        while queue:
            current, depth = queue.popleft()
            if depth >= hops:
                continue
            with self._conn.cursor() as cur:
                cur.execute("SELECT src, dst FROM mnemo_edges WHERE src=%s OR dst=%s", (current, current))
                pairs = cur.fetchall()
            for src, dst in pairs:
                nxt = dst if src == current else src
                if nxt in visited:
                    continue
                visited.add(nxt)
                node = await self.get_node(nxt)
                if node and node.is_current:
                    out.append(node)
                    queue.append((nxt, depth + 1))
        return out

    async def supersede(self, old_id: str, new_node: GraphNode) -> None:
        """Transactional reconsolidation rewrite."""
        import psycopg

        conn = psycopg.connect(self._dsn)
        try:
            with conn, conn.cursor() as cur:
                cur.execute("SELECT payload FROM mnemo_nodes WHERE node_id=%s", (old_id,))
                row = cur.fetchone()
                if not row:
                    raise KeyError(f"supersede target not found: {old_id}")
                old = GraphNode.model_validate_json(_loads(row[0]))
                old.valid_to = time.time()
                old.updated_at = time.time()
                cur.execute(
                    "UPDATE mnemo_nodes SET payload=%s, valid_to=%s, updated_at=%s WHERE node_id=%s",
                    (old.model_dump_json(), old.valid_to, old.updated_at, old_id),
                )
                new_node.prev_version_id = old_id
                new_node.version = old.version + 1
                cur.execute(
                    f"INSERT INTO mnemo_nodes ({_NODE_COLS}) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        new_node.node_id,
                        new_node.profile_id,
                        new_node.node_type.value,
                        json.dumps(new_node.entities, ensure_ascii=False),
                        new_node.model_dump_json(),
                        new_node.valid_from,
                        new_node.valid_to,
                        new_node.decay_weight,
                        time.time(),
                    ),
                )
        finally:
            conn.close()

    async def history(self, node_id: str) -> list[GraphNode]:
        """Full version-chain history, newest first."""
        out: list[GraphNode] = []
        current_id: str | None = node_id
        while current_id:
            node = await self.get_node(current_id)
            if not node:
                break
            out.append(node)
            current_id = node.prev_version_id
        return out

    async def close(self) -> None:
        self._conn.close()
