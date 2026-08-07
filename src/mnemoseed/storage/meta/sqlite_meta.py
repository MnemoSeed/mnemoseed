"""SQLite MetaStore driver (embedded default)."""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from mnemoseed.config import CONFIG_DIR
from mnemoseed.storage.ports import META_DRIVERS, Capability, DriverInfo, MetaStore, register

_DDL = """
CREATE TABLE IF NOT EXISTS kv (
    ns TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (ns, key)
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at REAL NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    detail TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_at ON audit(at);
"""


@register(META_DRIVERS)
class SqliteMeta(MetaStore):
    info = DriverInfo(
        name="sqlite_meta",
        capabilities=frozenset({Capability.TRANSACTIONS, Capability.PERSIST}),
        description="Embedded SQLite metadata store (default)",
    )

    def __init__(self, path: str | None = None) -> None:
        db_path = Path(path).expanduser() if path else CONFIG_DIR / "meta.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_DDL)
        self._conn.commit()

    async def kv_get(self, ns: str, key: str) -> Any | None:
        row = self._conn.execute(
            "SELECT value FROM kv WHERE ns=? AND key=?", (ns, key)
        ).fetchone()
        return json.loads(row["value"]) if row else None

    async def kv_put(self, ns: str, key: str, value: Any) -> None:
        self._conn.execute(
            "INSERT INTO kv (ns, key, value, updated_at) VALUES (?,?,?,?) "
            "ON CONFLICT(ns, key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (ns, key, json.dumps(value, ensure_ascii=False), time.time()),
        )
        self._conn.commit()

    async def audit(self, actor: str, action: str, detail: dict[str, Any]) -> None:
        self._conn.execute(
            "INSERT INTO audit (at, actor, action, detail) VALUES (?,?,?,?)",
            (time.time(), actor, action, json.dumps(detail, ensure_ascii=False)),
        )
        self._conn.commit()

    async def audit_iter(self, ns: str | None = None) -> AsyncIterator[dict[str, Any]]:
        cur = self._conn.execute("SELECT * FROM audit ORDER BY at DESC")
        for row in cur:
            yield {
                "at": row["at"],
                "actor": row["actor"],
                "action": row["action"],
                "detail": json.loads(row["detail"]),
            }

    async def close(self) -> None:
        self._conn.close()
