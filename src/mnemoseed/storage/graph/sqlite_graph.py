"""SQLite GraphStore driver (embedded default cortex).

Graph emulated with two tables (nodes + edges); BFS implements N-hop
traversal. A version chain is a sequence of nodes linked by prev_version_id,
with valid_to pinned on superseded versions.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections import deque
from pathlib import Path

from mnemoseed.config import CONFIG_DIR
from mnemoseed.schema.graph import Edge, GraphNode, NodeType
from mnemoseed.storage.ports import GRAPH_DRIVERS, Capability, DriverInfo, GraphStore, register

_DDL = """
CREATE TABLE IF NOT EXISTS nodes (
    node_id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL,
    node_type TEXT NOT NULL,
    entities TEXT NOT NULL DEFAULT '[]',
    payload TEXT NOT NULL,          -- full GraphNode JSON
    valid_from REAL NOT NULL,
    valid_to REAL,
    decay_weight REAL NOT NULL DEFAULT 1.0,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_nodes_profile ON nodes(profile_id, node_type);
CREATE INDEX IF NOT EXISTS idx_nodes_valid ON nodes(profile_id, valid_to);
CREATE TABLE IF NOT EXISTS edges (
    src TEXT NOT NULL,
    dst TEXT NOT NULL,
    rel TEXT NOT NULL,
    weight REAL NOT NULL DEFAULT 1.0,
    profile_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (src, dst, rel)
);
CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst);
"""


_NODE_COLS = (
    "node_id, profile_id, node_type, entities, payload, "
    "valid_from, valid_to, decay_weight, updated_at"
)


@register(GRAPH_DRIVERS)
class SqliteGraph(GraphStore):
    info = DriverInfo(
        name="sqlite_graph",
        capabilities=frozenset(
            {
                Capability.VERSION_CHAIN,
                Capability.TRANSACTIONS,
                Capability.TRAVERSAL,
                Capability.PERSIST,
                # no SNAPSHOT: dream snapshots degrade to logical turn-range
                # isolation (explicit degradation at the startup gate)
            }
        ),
        description="Embedded SQLite graph (default)",
    )

    def __init__(self, path: str | None = None) -> None:
        db_path = Path(path).expanduser() if path else CONFIG_DIR / "graph.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_DDL)
        self._conn.commit()

    def _insert(self, node: GraphNode) -> None:
        self._conn.execute(
            f"INSERT OR REPLACE INTO nodes ({_NODE_COLS}) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
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
        self._conn.commit()

    async def get_node(self, node_id: str, as_of: float | None = None) -> GraphNode | None:
        anchor = self._conn.execute(
            "SELECT payload FROM nodes WHERE node_id=?", (node_id,)
        ).fetchone()
        if not anchor:
            return None
        node = GraphNode.model_validate_json(anchor["payload"])
        if as_of is None:
            return node
        # point-in-time: walk the version chain to the version live at as_of
        seen = node
        while True:
            if seen.valid_from <= as_of and (seen.valid_to is None or as_of < seen.valid_to):
                return seen
            if seen.prev_version_id is None:
                return None
            row = self._conn.execute(
                "SELECT payload FROM nodes WHERE node_id=?", (seen.prev_version_id,)
            ).fetchone()
            if not row:
                return None
            seen = GraphNode.model_validate_json(row["payload"])

    async def find_nodes(
        self,
        profile_id: str,
        node_type: NodeType | None = None,
        entity: str | None = None,
        min_decay: float = 0.0,
        limit: int = 50,
    ) -> list[GraphNode]:
        sql = (
            "SELECT payload FROM nodes WHERE profile_id=? AND valid_to IS NULL "
            "AND decay_weight>=?"
        )
        params: list = [profile_id, min_decay]
        if node_type is not None:
            sql += " AND node_type=?"
            params.append(node_type.value)
        if entity is not None:
            sql += " AND entities LIKE ?"
            params.append(f'%"{entity}"%')
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        return [GraphNode.model_validate_json(r["payload"]) for r in self._conn.execute(sql, params)]

    async def put_edge(self, edge: Edge) -> None:
        self._conn.execute(
            "INSERT INTO edges (src, dst, rel, weight, profile_id, created_at) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(src, dst, rel) DO UPDATE SET weight=weight+excluded.weight",
            (edge.src, edge.dst, edge.rel.value, edge.weight, edge.profile_id, edge.created_at),
        )
        self._conn.commit()

    async def neighbors(self, node_id: str, hops: int = 2) -> list[GraphNode]:
        """BFS N-hop traversal (the carrier of spreading activation)."""
        visited = {node_id}
        queue: deque[tuple[str, int]] = deque([(node_id, 0)])
        out: list[GraphNode] = []
        while queue:
            current, depth = queue.popleft()
            if depth >= hops:
                continue
            rows = self._conn.execute(
                "SELECT src, dst FROM edges WHERE src=? OR dst=?", (current, current)
            ).fetchall()
            for r in rows:
                nxt = r["dst"] if r["src"] == current else r["src"]
                if nxt in visited:
                    continue
                visited.add(nxt)
                row = self._conn.execute(
                    "SELECT payload FROM nodes WHERE node_id=? AND valid_to IS NULL", (nxt,)
                ).fetchone()
                if row:
                    node = GraphNode.model_validate_json(row["payload"])
                    out.append(node)
                    queue.append((nxt, depth + 1))
        return out

    async def supersede(self, old_id: str, new_node: GraphNode) -> None:
        """Reconsolidation rewrite: old version pinned, new version chained
        (single atomic transaction)."""
        with self._conn:
            row = self._conn.execute(
                "SELECT payload FROM nodes WHERE node_id=?", (old_id,)
            ).fetchone()
            if not row:
                raise KeyError(f"supersede target not found: {old_id}")
            old = GraphNode.model_validate_json(row["payload"])
            old.valid_to = time.time()
            old.updated_at = time.time()
            self._insert(old)

            new_node.prev_version_id = old_id
            new_node.version = old.version + 1
            self._insert(new_node)

    async def history(self, node_id: str) -> list[GraphNode]:
        """Full version-chain history, newest first."""
        out: list[GraphNode] = []
        current_id: str | None = node_id
        while current_id:
            row = self._conn.execute(
                "SELECT payload FROM nodes WHERE node_id=?", (current_id,)
            ).fetchone()
            if not row:
                break
            node = GraphNode.model_validate_json(row["payload"])
            out.append(node)
            current_id = node.prev_version_id
        return out

    async def close(self) -> None:
        self._conn.close()
