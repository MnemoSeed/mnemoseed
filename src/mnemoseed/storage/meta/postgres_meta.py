"""Postgres MetaStore driver (alternative; optional extra)."""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from typing import Any

from mnemoseed.storage.ports import META_DRIVERS, Capability, DriverInfo, MetaStore, register

_DDL = """
CREATE TABLE IF NOT EXISTS mnemo_kv (
    ns TEXT NOT NULL,
    key TEXT NOT NULL,
    value JSONB NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (ns, key)
);
CREATE TABLE IF NOT EXISTS mnemo_audit (
    id BIGSERIAL PRIMARY KEY,
    at DOUBLE PRECISION NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    detail JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mnemo_audit_at ON mnemo_audit(at);
"""


@register(META_DRIVERS)
class PostgresMeta(MetaStore):
    info = DriverInfo(
        name="postgres_meta",
        capabilities=frozenset({Capability.TRANSACTIONS, Capability.PERSIST}),
        description="Postgres metadata store",
    )

    def __init__(self, dsn: str) -> None:
        import psycopg  # lazy import: optional dependency

        self._conn = psycopg.connect(dsn, autocommit=True)
        with self._conn.cursor() as cur:
            cur.execute(_DDL)

    async def kv_get(self, ns: str, key: str) -> Any | None:
        with self._conn.cursor() as cur:
            cur.execute("SELECT value FROM mnemo_kv WHERE ns=%s AND key=%s", (ns, key))
            row = cur.fetchone()
        return row[0] if row else None

    async def kv_put(self, ns: str, key: str, value: Any) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO mnemo_kv (ns, key, value, updated_at) VALUES (%s,%s,%s,%s) "
                "ON CONFLICT (ns, key) DO UPDATE SET value=EXCLUDED.value, updated_at=EXCLUDED.updated_at",
                (ns, key, json.dumps(value, ensure_ascii=False), time.time()),
            )

    async def audit(self, actor: str, action: str, detail: dict[str, Any]) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO mnemo_audit (at, actor, action, detail) VALUES (%s,%s,%s,%s)",
                (time.time(), actor, action, json.dumps(detail, ensure_ascii=False)),
            )

    async def audit_iter(self, ns: str | None = None) -> AsyncIterator[dict[str, Any]]:
        with self._conn.cursor() as cur:
            cur.execute("SELECT at, actor, action, detail FROM mnemo_audit ORDER BY at DESC")
            for at, actor, action, detail in cur.fetchall():
                yield {"at": at, "actor": actor, "action": action, "detail": detail}

    async def close(self) -> None:
        self._conn.close()
