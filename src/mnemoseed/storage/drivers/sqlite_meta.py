"""SQLite meta driver: profiles, tokens, score pool, config, audit, dream runs.

Every pool mutation (pool_add / advance_watermark) is a transaction: the WAL
journal makes concurrent writer waits safe (busy_timeout). audit_log is
append-only at the database level via BEFORE UPDATE/DELETE triggers, not just
by driver convention.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from collections.abc import Iterator
from collections.abc import Sequence as CSeq
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from mnemoseed.config import CONFIG_DIR
from mnemoseed.storage.drivers._migrations import apply_migrations
from mnemoseed.storage.drivers._time import epoch_from_iso, iso8601_utc
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
class SqliteMetaDriver:
    """MetaStore over a single SQLite file."""

    info = DriverInfo(
        name="sqlite_meta",
        capabilities=_CAPABILITIES,
        description="profiles/tokens/score-pool/config/audit/dream-runs over SQLite",
    )

    def __init__(self, path: str | os.PathLike[str] | None = None, **kwargs: Any) -> None:
        self.params: dict[str, Any] = kwargs
        self._path = Path(os.path.expanduser(str(path))) if path is not None else CONFIG_DIR / "meta.db"
        extra = kwargs.get("path")
        if extra is not None and path is None:
            self._path = Path(os.path.expanduser(str(extra)))
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        apply_migrations(self._conn, "meta")

    def capabilities(self) -> frozenset[Capability]:
        return self.info.capabilities

    async def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------ score pool

    def pool_add(self, points: float, turn_range: TurnRange) -> None:
        with _transaction(self._conn):
            self._conn.execute(
                "INSERT INTO score_pool (id, balance, watermark_start, watermark_end, "
                "last_event_start, last_event_end) "
                "VALUES (1, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "balance = balance + ?, last_event_start = ?, last_event_end = ?",
                (points, 0, 0, turn_range.start, turn_range.end, points, turn_range.start, turn_range.end),
            )

    def pool_state(self) -> PoolState:
        row = self._conn.execute(
            "SELECT balance, watermark_start, watermark_end FROM score_pool WHERE id = 1"
        ).fetchone()
        if row is None or float(row["balance"]) == 0.0 and int(row["watermark_end"]) == 0:
            return PoolState(balance=0.0)
        watermark = TurnRange(start=int(row["watermark_start"]), end=int(row["watermark_end"]))
        return PoolState(balance=float(row["balance"]), watermark=watermark)

    def advance_watermark(self, turn_range: TurnRange) -> None:
        current = self._conn.execute(
            "SELECT watermark_start, watermark_end FROM score_pool WHERE id = 1"
        ).fetchone()
        with _transaction(self._conn):
            if current is None or int(current["watermark_end"]) == 0:
                self._conn.execute(
                    "INSERT INTO score_pool (id, balance, watermark_start, watermark_end, "
                    "last_event_start, last_event_end) "
                    "VALUES (1, 0.0, ?, ?, 0, 0) "
                    "ON CONFLICT(id) DO UPDATE SET watermark_start = excluded.watermark_start, "
                    "watermark_end = excluded.watermark_end",
                    (turn_range.start, turn_range.end),
                )
                return
            start = int(current["watermark_start"])
            end = int(current["watermark_end"])
            new_start, new_end = _merge_watermark((start, end), (turn_range.start, turn_range.end))
            self._conn.execute(
                "UPDATE score_pool SET watermark_start = ?, watermark_end = ? WHERE id = 1",
                (new_start, new_end),
            )

    # ------------------------------------------------------------ profiles

    def upsert_profile(self, profile: StoredProfile) -> None:
        created = profile.created_at if profile.created_at else time.time()
        self._conn.execute(
            "INSERT INTO profiles (profile_id, display_name, created_at) VALUES (?, ?, ?) "
            "ON CONFLICT(profile_id) DO UPDATE SET display_name = excluded.display_name",
            (profile.profile_id, profile.display_name, iso8601_utc(created)),
        )

    def get_profile(self, profile_id: str) -> StoredProfile | None:
        row = self._conn.execute(
            "SELECT profile_id, display_name, created_at FROM profiles WHERE profile_id = ?",
            (profile_id,),
        ).fetchone()
        if row is None:
            return None
        return StoredProfile(
            profile_id=str(row["profile_id"]),
            display_name=str(row["display_name"]),
            created_at=epoch_from_iso(str(row["created_at"])),
        )

    def delete_profile(self, profile_id: str) -> None:
        # tokens cascade via FK
        self._conn.execute("DELETE FROM profiles WHERE profile_id = ?", (profile_id,))

    def list_profiles(self) -> list[StoredProfile]:
        rows = self._conn.execute(
            "SELECT profile_id, display_name, created_at FROM profiles ORDER BY created_at"
        ).fetchall()
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
            profile = self._conn.execute(
                "SELECT profile_id FROM profiles WHERE profile_id = ?", (profile_id,)
            ).fetchone()
            if profile is None:
                raise StorageError(f"cannot issue token for unknown profile {profile_id!r}")
            self._conn.execute(
                "INSERT INTO tokens (token_id, profile_id, scopes, issued_at, expires_at, revoked) "
                "VALUES (?, ?, ?, ?, ?, 0)",
                (
                    token_id,
                    profile_id,
                    json.dumps(list(scopes)),
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
        self._conn.execute("UPDATE tokens SET revoked = 1 WHERE token_id = ?", (token_id,))

    # ------------------------------------------------------------ config

    def get_config(self, key: str, version: int | None = None) -> ConfigEntry | None:
        if version is None:
            row = self._conn.execute(
                "SELECT key, value, version, updated_at FROM config "
                "WHERE key = ? ORDER BY version DESC LIMIT 1",
                (key,),
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT key, value, version, updated_at FROM config WHERE key = ? AND version = ?",
                (key, version),
            ).fetchone()
        if row is None:
            return None
        return ConfigEntry(
            key=str(row["key"]),
            value=json.loads(str(row["value"])),
            version=int(row["version"]),
            updated_at=epoch_from_iso(str(row["updated_at"])),
        )

    def set_config(self, key: str, value: dict[str, Any]) -> int:
        with _transaction(self._conn):
            row = self._conn.execute(
                "SELECT COALESCE(MAX(version), 0) AS v FROM config WHERE key = ?", (key,)
            ).fetchone()
            version = int(row["v"]) + 1 if row is not None else 1
            self._conn.execute(
                "INSERT INTO config (key, value, version, updated_at) VALUES (?, ?, ?, ?)",
                (key, json.dumps(value), version, iso8601_utc(time.time())),
            )
        return version

    def rollback_config(self, key: str, version: int) -> None:
        row = self._conn.execute(
            "SELECT value FROM config WHERE key = ? AND version = ?", (key, version)
        ).fetchone()
        if row is None:
            raise StorageError(f"config key {key!r} has no version {version}")
        with _transaction(self._conn):
            current = self._conn.execute(
                "SELECT COALESCE(MAX(version), 0) AS v FROM config WHERE key = ?", (key,)
            ).fetchone()
            next_version = int(current["v"]) + 1 if current is not None else 1
            self._conn.execute(
                "INSERT INTO config (key, value, version, updated_at) VALUES (?, ?, ?, ?)",
                (key, str(row["value"]), next_version, iso8601_utc(time.time())),
            )

    # ------------------------------------------------------------ audit

    def audit_append(self, entry: AuditEntry) -> None:
        self._conn.execute(
            "INSERT INTO audit_log (actor, action, detail, at) VALUES (?, ?, ?, ?)",
            (
                entry.actor,
                entry.action,
                json.dumps(entry.detail),
                iso8601_utc(entry.at if entry.at else time.time()),
            ),
        )

    def audit_query(self, filter: AuditFilter, page: Page) -> PageResult[AuditEntry]:
        clauses: list[str] = []
        params: list[Any] = []
        if filter.actor is not None:
            clauses.append("actor = ?")
            params.append(filter.actor)
        if filter.action is not None:
            clauses.append("action = ?")
            params.append(filter.action)
        if filter.since is not None:
            clauses.append("at >= ?")
            params.append(iso8601_utc(filter.since))
        if filter.until is not None:
            clauses.append("at <= ?")
            params.append(iso8601_utc(filter.until))
        where = " AND ".join(clauses) if clauses else "1 = 1"
        count_row = self._conn.execute(f"SELECT COUNT(*) FROM audit_log WHERE {where}", params).fetchone()
        total = int(count_row[0]) if count_row is not None else 0
        rows = self._conn.execute(
            f"SELECT id, actor, action, detail, at FROM audit_log WHERE {where} ORDER BY id LIMIT ? OFFSET ?",
            [*params, page.limit, page.offset],
        ).fetchall()
        items = [_decode_audit(r) for r in rows]
        return PageResult(items=items, total=total, offset=page.offset, limit=page.limit)

    # ------------------------------------------------------------ dream runs

    def record_dream_run(self, run: DreamRun) -> str:
        run_id = run.run_id if run.run_id else uuid.uuid4().hex
        with _transaction(self._conn):
            self._conn.execute(
                "INSERT INTO dream_runs (run_id, session_id, turn_start, turn_end, model_id, "
                "started_at, finished_at, tokens, cost, interrupted, dropped_count) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
            clauses.append("session_id = ?")
            params.append(filter.session_id)
        if filter.since is not None:
            clauses.append("started_at >= ?")
            params.append(iso8601_utc(filter.since))
        if filter.until is not None:
            clauses.append("started_at <= ?")
            params.append(iso8601_utc(filter.until))
        if filter.interrupted is not None:
            clauses.append("interrupted = ?")
            params.append(int(filter.interrupted))
        where = " AND ".join(clauses) if clauses else "1 = 1"
        count_row = self._conn.execute(f"SELECT COUNT(*) FROM dream_runs WHERE {where}", params).fetchone()
        total = int(count_row[0]) if count_row is not None else 0
        rows = self._conn.execute(
            f"SELECT * FROM dream_runs WHERE {where} ORDER BY started_at DESC LIMIT ? OFFSET ?",
            [*params, page.limit, page.offset],
        ).fetchall()
        items = [_decode_dream_run(r) for r in rows]
        return PageResult(items=items, total=total, offset=page.offset, limit=page.limit)

    # ------------------------------------------------------------ migrations

    def schema_version(self) -> int:
        from mnemoseed.storage.drivers._migrations import current_schema_version

        return current_schema_version(self._conn, "meta")

    def migrate(self, target: int | None = None) -> int:
        return apply_migrations(self._conn, "meta", target)


# ---------------------------------------------------------------- module helpers


@contextmanager
def _transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """Explicit BEGIN IMMEDIATE / COMMIT transaction, ROLLBACK on any error."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _merge_watermark(current: tuple[int, int], incoming: tuple[int, int]) -> tuple[int, int]:
    """Monotonic forward merge of watermark ranges; gaps raise."""
    cur_start, cur_end = current
    new_start, new_end = incoming
    if cur_end == 0:
        return new_start, new_end
    if new_start > cur_end + 1:
        raise ValueError(
            f"watermark advance jumps over unprocessed turns "
            f"(current end {cur_end}, incoming start {new_start})"
        )
    return min(cur_start, new_start), max(cur_end, new_end)


def _decode_audit(row: sqlite3.Row) -> AuditEntry:
    return AuditEntry(
        actor=str(row["actor"]),
        action=str(row["action"]),
        detail=json.loads(str(row["detail"])),
        at=epoch_from_iso(str(row["at"])),
        id=int(row["id"]),
    )


def _decode_dream_run(row: sqlite3.Row) -> DreamRun:
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


def _turn_range_or_none(start: Any, end: Any) -> TurnRange | None:
    if start is None or end is None:
        return None
    return TurnRange(start=int(start), end=int(end))


def _maybe_epoch(value: Any) -> float | None:
    return epoch_from_iso(str(value)) if value is not None else None
