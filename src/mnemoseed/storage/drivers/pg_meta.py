"""Postgres meta driver: profiles, tokens, score pool, config, audit, dream runs.

Mirror of the sqlite_meta driver over the same relational tables (prd-08
appendix A.3). Every pool mutation (pool_add / advance_watermark) runs in one
psycopg transaction so the balance and watermark stay atomic; audit_log is
append-only at the database level via plpgsql triggers created by the shared
migration runner. Temporal columns stay ISO8601 TEXT exactly like SQLite so the
two backends compare byte-for-byte (AC-5 parity).

The dedicated integer identity on audit_log / score_pool mirrors SQLite's
rowid alias; all JSON columns are JSONB carrying the same values the SQLite
columns carry as TEXT.
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

from mnemoseed.storage.drivers._migrations import (
    apply_postgres_migrations,
    current_postgres_schema_version,
)
from mnemoseed.storage.drivers._time import epoch_from_iso, iso8601_utc
from mnemoseed.storage.drivers.sqlite_meta import _maybe_epoch, _merge_watermark, _turn_range_or_none
from mnemoseed.storage.ports import (
    AuditEntry,
    AuditFilter,
    Capability,
    ConfigEntry,
    DreamRun,
    DreamRunFilter,
    DriverInfo,
    Page,
    PageResult,
    PoolState,
    StorageError,
    StoredProfile,
    Token,
    TurnRange,
)
from mnemoseed.storage.registry import META_DRIVERS, register

_CAPABILITIES = frozenset({Capability.META_TRANSACTION, Capability.META_CONCURRENT_READERS})


@register(META_DRIVERS)
class PgMetaDriver:
    """MetaStore over a managed Postgres database."""

    info = DriverInfo(
        name="pg_meta",
        capabilities=_CAPABILITIES,
        description="profiles/tokens/score-pool/config/audit/dream-runs over Postgres",
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
                raise StorageError("pg_meta requires a 'dsn' connection string (or a 'conn' connection)")
            conn = psycopg.connect(dsn)
            self._owns_conn = True
        else:
            self._owns_conn = False
        self._conn = conn
        self._conn.row_factory = dict_row
        register_default_adapters(self._conn)
        # psycopg3 dumps dict/list to JSON only when wrapped; this connection
        # dumps bare dicts as JSONB so config values / audit details go through
        # unwrapped (the token scopes list is wrapped explicitly).
        self._conn.adapters.register_dumper(dict, JsonbDumper)
        apply_postgres_migrations(self._conn, "meta", schema=self._schema)

    def capabilities(self) -> frozenset[Capability]:
        return self.info.capabilities

    async def close(self) -> None:
        if self._owns_conn:
            self._conn.close()

    # ------------------------------------------------------------ helpers

    def _exec_rows(self, sql: str, params: CSeq[Any] | None = None) -> list[Any]:
        return self._conn.execute(sql, params).fetchall()

    def _exec_row(self, sql: str, params: CSeq[Any] | None = None) -> Any | None:
        return self._conn.execute(sql, params).fetchone()

    # ------------------------------------------------------------ score pool

    def pool_add(self, profile_id: str, points: float, turn_range: TurnRange) -> None:
        with _transaction(self._conn):
            self._conn.execute(
                "INSERT INTO profile_score_pool (profile_id, balance, watermark_start, "
                "watermark_end, last_event_start, last_event_end) "
                "VALUES (%s, %s, 0, 0, %s, %s) "
                "ON CONFLICT (profile_id) DO UPDATE SET "
                "balance = profile_score_pool.balance + EXCLUDED.balance, "
                "last_event_start = EXCLUDED.last_event_start, "
                "last_event_end = EXCLUDED.last_event_end",
                (profile_id, points, turn_range.start, turn_range.end),
            )

    def pool_credit(self, profile_id: str, balance: float, turn_range: TurnRange) -> None:
        with _transaction(self._conn):
            self._conn.execute(
                "INSERT INTO profile_score_pool (profile_id, balance, watermark_start, "
                "watermark_end, last_event_start, last_event_end) "
                "VALUES (%s, %s, %s, %s, 0, 0) "
                "ON CONFLICT (profile_id) DO UPDATE SET "
                "balance = EXCLUDED.balance, watermark_start = EXCLUDED.watermark_start, "
                "watermark_end = EXCLUDED.watermark_end",
                (profile_id, balance, turn_range.start, turn_range.end),
            )

    def pool_state(self, profile_id: str) -> PoolState:
        row = self._exec_row(
            "SELECT balance, watermark_start, watermark_end FROM profile_score_pool WHERE profile_id = %s",
            (profile_id,),
        )
        if row is None or int(row["watermark_end"]) == 0:
            # no watermark advanced yet: balance may still be un-filed points
            balance = float(row["balance"]) if row is not None else 0.0
            return PoolState(balance=balance)
        watermark = TurnRange(start=int(row["watermark_start"]), end=int(row["watermark_end"]))
        return PoolState(balance=float(row["balance"]), watermark=watermark)

    def pool_states(self) -> dict[str, PoolState]:
        rows = self._exec_rows(
            "SELECT profile_id, balance, watermark_start, watermark_end FROM profile_score_pool"
        )
        states: dict[str, PoolState] = {}
        for row in rows:
            balance = float(row["balance"])
            watermark: TurnRange | None = None
            if int(row["watermark_end"]) != 0:
                watermark = TurnRange(start=int(row["watermark_start"]), end=int(row["watermark_end"]))
            states[str(row["profile_id"])] = PoolState(balance=balance, watermark=watermark)
        return states

    def advance_watermark(self, profile_id: str, turn_range: TurnRange) -> None:
        current = self._exec_row(
            "SELECT watermark_start, watermark_end FROM profile_score_pool WHERE profile_id = %s",
            (profile_id,),
        )
        with _transaction(self._conn):
            if current is None or int(current["watermark_end"]) == 0:
                self._conn.execute(
                    "INSERT INTO profile_score_pool (profile_id, balance, watermark_start, "
                    "watermark_end, last_event_start, last_event_end) "
                    "VALUES (%s, 0.0, %s, %s, 0, 0) "
                    "ON CONFLICT (profile_id) DO UPDATE SET watermark_start = EXCLUDED.watermark_start, "
                    "watermark_end = EXCLUDED.watermark_end",
                    (profile_id, turn_range.start, turn_range.end),
                )
                return
            start = int(current["watermark_start"])
            end = int(current["watermark_end"])
            new_start, new_end = _merge_watermark((start, end), (turn_range.start, turn_range.end))
            self._conn.execute(
                "UPDATE profile_score_pool SET watermark_start = %s, watermark_end = %s "
                "WHERE profile_id = %s",
                (new_start, new_end, profile_id),
            )

    # ------------------------------------------------------------ profiles

    def upsert_profile(self, profile: StoredProfile) -> None:
        created = profile.created_at if profile.created_at else time.time()
        with _transaction(self._conn):
            self._conn.execute(
                "INSERT INTO profiles (profile_id, display_name, created_at) VALUES (%s, %s, %s) "
                "ON CONFLICT (profile_id) DO UPDATE SET display_name = EXCLUDED.display_name",
                (profile.profile_id, profile.display_name, iso8601_utc(created)),
            )

    def get_profile(self, profile_id: str) -> StoredProfile | None:
        row = self._exec_row(
            "SELECT profile_id, display_name, created_at FROM profiles WHERE profile_id = %s",
            (profile_id,),
        )
        if row is None:
            return None
        return StoredProfile(
            profile_id=str(row["profile_id"]),
            display_name=str(row["display_name"]),
            created_at=epoch_from_iso(str(row["created_at"])),
        )

    def delete_profile(self, profile_id: str) -> None:
        # tokens cascade via FK, exactly like SQLite's ON DELETE CASCADE
        with _transaction(self._conn):
            self._conn.execute("DELETE FROM profiles WHERE profile_id = %s", (profile_id,))

    def list_profiles(self) -> list[StoredProfile]:
        rows = self._exec_rows(
            "SELECT profile_id, display_name, created_at FROM profiles ORDER BY created_at"
        )
        return [
            StoredProfile(
                profile_id=str(r["profile_id"]),
                display_name=str(r["display_name"]),
                created_at=epoch_from_iso(str(r["created_at"])),
            )
            for r in rows
        ]

    # ------------------------------------------------------------ tokens

    def issue_token(
        self,
        profile_id: str,
        scopes: CSeq[str],
        expires_at: float | None = None,
    ) -> Token:
        token_id = uuid.uuid4().hex
        issued_at = time.time()
        with _transaction(self._conn):
            profile = self._exec_row("SELECT profile_id FROM profiles WHERE profile_id = %s", (profile_id,))
            if profile is None:
                raise StorageError(f"cannot issue token for unknown profile {profile_id!r}")
            self._conn.execute(
                "INSERT INTO tokens (token_id, profile_id, scopes, issued_at, expires_at, revoked) "
                "VALUES (%s, %s, %s, %s, %s, 0)",
                (
                    token_id,
                    profile_id,
                    Jsonb(list(scopes)),
                    iso8601_utc(issued_at),
                    iso8601_utc(expires_at) if expires_at is not None else None,
                ),
            )
        return Token(
            token_id=token_id,
            profile_id=profile_id,
            scopes=tuple(scopes),
            issued_at=issued_at,
            expires_at=expires_at,
            revoked=False,
        )

    def revoke_token(self, token_id: str) -> None:
        with _transaction(self._conn):
            self._conn.execute("UPDATE tokens SET revoked = 1 WHERE token_id = %s", (token_id,))

    # ------------------------------------------------------------ config

    def get_config(self, key: str, version: int | None = None) -> ConfigEntry | None:
        if version is None:
            row = self._exec_row(
                "SELECT key, value, version, updated_at FROM config "
                "WHERE key = %s ORDER BY version DESC LIMIT 1",
                (key,),
            )
        else:
            row = self._exec_row(
                "SELECT key, value, version, updated_at FROM config WHERE key = %s AND version = %s",
                (key, version),
            )
        if row is None:
            return None
        return ConfigEntry(
            key=str(row["key"]),
            value=_json_value(row["value"]),
            version=int(row["version"]),
            updated_at=epoch_from_iso(str(row["updated_at"])),
        )

    def set_config(self, key: str, value: dict[str, Any]) -> int:
        with _transaction(self._conn):
            row = self._exec_row("SELECT COALESCE(MAX(version), 0) AS v FROM config WHERE key = %s", (key,))
            version = int(row["v"]) + 1 if row is not None else 1
            self._conn.execute(
                "INSERT INTO config (key, value, version, updated_at) VALUES (%s, %s, %s, %s)",
                (key, value, version, iso8601_utc(time.time())),
            )
        return version

    def rollback_config(self, key: str, version: int) -> None:
        row = self._exec_row("SELECT value FROM config WHERE key = %s AND version = %s", (key, version))
        if row is None:
            raise StorageError(f"config key {key!r} has no version {version}")
        with _transaction(self._conn):
            current = self._exec_row(
                "SELECT COALESCE(MAX(version), 0) AS v FROM config WHERE key = %s", (key,)
            )
            next_version = int(current["v"]) + 1 if current is not None else 1
            self._conn.execute(
                "INSERT INTO config (key, value, version, updated_at) VALUES (%s, %s, %s, %s)",
                (key, _json_value(row["value"]), next_version, iso8601_utc(time.time())),
            )

    # ------------------------------------------------------------ audit

    def audit_append(self, entry: AuditEntry) -> None:
        with _transaction(self._conn):
            self._conn.execute(
                "INSERT INTO audit_log (actor, action, detail, at) VALUES (%s, %s, %s, %s)",
                (
                    entry.actor,
                    entry.action,
                    entry.detail,
                    iso8601_utc(entry.at if entry.at else time.time()),
                ),
            )

    def audit_query(self, filter: AuditFilter, page: Page) -> PageResult[AuditEntry]:
        clauses: list[str] = []
        params: list[Any] = []
        if filter.actor is not None:
            clauses.append("actor = %s")
            params.append(filter.actor)
        if filter.action is not None:
            clauses.append("action = %s")
            params.append(filter.action)
        if filter.since is not None:
            clauses.append("at >= %s")
            params.append(iso8601_utc(filter.since))
        if filter.until is not None:
            clauses.append("at <= %s")
            params.append(iso8601_utc(filter.until))
        where = " AND ".join(clauses) if clauses else "true"
        count_row = self._exec_row(f"SELECT COUNT(*) FROM audit_log WHERE {where}", params)
        total = _count_value(count_row)
        rows = self._exec_rows(
            f"SELECT id, actor, action, detail, at FROM audit_log WHERE {where} "
            "ORDER BY id LIMIT %s OFFSET %s",
            [*params, page.limit, page.offset],
        )
        items = [_decode_audit(r) for r in rows]
        return PageResult(items=items, total=total, offset=page.offset, limit=page.limit)

    # ------------------------------------------------------------ dream runs

    def record_dream_run(self, run: DreamRun) -> str:
        run_id = run.run_id if run.run_id else uuid.uuid4().hex
        with _transaction(self._conn):
            self._conn.execute(
                "INSERT INTO dream_runs (run_id, session_id, turn_start, turn_end, model_id, "
                "started_at, finished_at, tokens, cost, interrupted, dropped_count) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    run_id,
                    run.session_id,
                    run.turn_range.start if run.turn_range is not None else None,
                    run.turn_range.end if run.turn_range is not None else None,
                    run.model_id,
                    iso8601_utc(run.started_at if run.started_at else time.time()),
                    iso8601_utc(run.finished_at) if run.finished_at is not None else None,
                    run.tokens,
                    run.cost,
                    int(run.interrupted),
                    run.dropped_count,
                ),
            )
        return run_id

    def list_dream_runs(self, filter: DreamRunFilter, page: Page) -> PageResult[DreamRun]:
        clauses: list[str] = []
        params: list[Any] = []
        if filter.session_id is not None:
            clauses.append("session_id = %s")
            params.append(filter.session_id)
        if filter.since is not None:
            clauses.append("started_at >= %s")
            params.append(iso8601_utc(filter.since))
        if filter.until is not None:
            clauses.append("started_at <= %s")
            params.append(iso8601_utc(filter.until))
        if filter.interrupted is not None:
            clauses.append("interrupted = %s")
            params.append(int(filter.interrupted))
        where = " AND ".join(clauses) if clauses else "true"
        count_row = self._exec_row(f"SELECT COUNT(*) FROM dream_runs WHERE {where}", params)
        total = _count_value(count_row)
        rows = self._exec_rows(
            f"SELECT * FROM dream_runs WHERE {where} ORDER BY started_at DESC LIMIT %s OFFSET %s",
            [*params, page.limit, page.offset],
        )
        items = [_decode_dream_run(r) for r in rows]
        return PageResult(items=items, total=total, offset=page.offset, limit=page.limit)

    # ------------------------------------------------------------ migrations

    def schema_version(self) -> int:
        return current_postgres_schema_version(self._conn, "meta", schema=self._schema)

    def migrate(self, target: int | None = None) -> int:
        return apply_postgres_migrations(self._conn, "meta", target, schema=self._schema)


# ---------------------------------------------------------------- module helpers


def _count_value(row: Any) -> int:
    """Extract a COUNT(*) scalar regardless of the connection row factory."""
    if row is None:
        return 0
    return int(row["count"]) if isinstance(row, dict) else int(row[0])


def _json_value(value: Any) -> Any:
    """JSONB comes back parsed by psycopg; tolerate raw text like SQLite."""
    if isinstance(value, str):
        return json.loads(value) if value else None
    return value


def _decode_audit(row: Any) -> AuditEntry:
    return AuditEntry(
        actor=str(row["actor"]),
        action=str(row["action"]),
        detail=_json_value(row["detail"]),
        at=epoch_from_iso(str(row["at"])),
        id=int(row["id"]),
    )


def _decode_dream_run(row: Any) -> DreamRun:
    return DreamRun(
        run_id=str(row["run_id"]),
        session_id=row["session_id"],
        turn_range=_turn_range_or_none(row["turn_start"], row["turn_end"]),
        model_id=str(row["model_id"]) if row["model_id"] is not None else "",
        started_at=epoch_from_iso(str(row["started_at"])),
        finished_at=_maybe_epoch(row["finished_at"]),
        tokens=int(row["tokens"]),
        cost=float(row["cost"]),
        interrupted=bool(int(row["interrupted"])),
        dropped_count=int(row["dropped_count"]),
    )


@contextmanager
def _transaction(conn: Any) -> Iterator[None]:
    """Begin a real outer transaction; roll back any lingering read transaction
    first so psycopg3 does not nest into a savepoint."""
    conn.rollback()
    with conn.transaction():
        yield
