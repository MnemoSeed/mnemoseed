"""Driver-agnostic contract tests for the MetaStore port (prd-08 appendix B.3).

Every meta method gets at least one behavioral test against the embedded
(sqlite_meta) and postgres (pg_meta) drivers. Both carry the same append-only
audit enforcement at the database level, and the same atomic pool semantics.
"""

from __future__ import annotations

import sqlite3
import time

import psycopg
import pytest
from _support import make_prov, raw_meta_row

from mnemoseed.storage.ports import (
    AuditEntry,
    AuditFilter,
    Capability,
    ConfigEntry,
    DreamRun,
    DreamRunFilter,
    Page,
    PoolState,
    StorageError,
    StoredProfile,
    TurnRange,
)


def _pool_profile(stack, profile_id: str = "u1") -> StoredProfile:
    return StoredProfile(profile_id=profile_id, display_name="Uma", created_at=time.time())


# ---------------------------------------------------------------- B.3 surface


def test_capabilities(stack) -> None:
    expected = frozenset({Capability.META_TRANSACTION, Capability.META_CONCURRENT_READERS})
    assert stack.meta.capabilities() == stack.meta.info.capabilities == expected


def test_pool_add_state_advance_watermark(stack) -> None:
    profile = "u1"
    assert stack.meta.pool_state(profile) == PoolState(balance=0.0)
    stack.meta.pool_add(profile, 10.0, TurnRange(start=0, end=4))
    stack.meta.advance_watermark(profile, TurnRange(start=0, end=4))
    state = stack.meta.pool_state(profile)
    assert state.balance == 10.0
    assert state.watermark == TurnRange(start=0, end=4)

    stack.meta.advance_watermark(profile, TurnRange(start=1, end=8))
    assert stack.meta.pool_state(profile).watermark == TurnRange(start=0, end=8)

    stack.meta.pool_add(profile, 5.0, TurnRange(start=5, end=9))
    assert stack.meta.pool_state(profile).balance == 15.0


def test_pool_per_profile_isolation(stack) -> None:
    stack.meta.pool_add("a", 10.0, TurnRange(start=0, end=4))
    stack.meta.pool_add("b", 3.0, TurnRange(start=0, end=4))
    assert stack.meta.pool_state("a").balance == 10.0
    assert stack.meta.pool_state("b").balance == 3.0
    stack.meta.advance_watermark("a", TurnRange(start=0, end=4))
    assert stack.meta.pool_state("a").watermark == TurnRange(start=0, end=4)
    assert stack.meta.pool_state("b").watermark is None


def test_pool_state_unknown_profile_is_empty(stack) -> None:
    assert stack.meta.pool_state("ghost") == PoolState(balance=0.0)


def test_pool_credit_upserts_row(stack) -> None:
    """pool_credit sets a profile's row absolutely (balance + watermark)."""
    stack.meta.pool_credit("u1", 5.0, TurnRange(start=0, end=2))
    state = stack.meta.pool_state("u1")
    assert state.balance == 5.0
    assert state.watermark == TurnRange(start=0, end=2)
    # a second credit overwrites instead of accumulating
    stack.meta.pool_credit("u1", 3.0, TurnRange(start=3, end=5))
    state = stack.meta.pool_state("u1")
    assert state.balance == 3.0
    assert state.watermark == TurnRange(start=3, end=5)


def test_pool_states_returns_all_rows(stack) -> None:
    stack.meta.pool_credit("u1", 4.0, TurnRange(start=0, end=1))
    stack.meta.pool_add("u2", 7.0, TurnRange(start=2, end=3))
    states = stack.meta.pool_states()
    assert set(states) == {"u1", "u2"}
    assert states["u1"].balance == 4.0
    assert states["u1"].watermark == TurnRange(start=0, end=1)  # pool_credit carries the span
    assert states["u2"].balance == 7.0
    assert states["u2"].watermark is None  # pool_add alone advances no watermark


def test_pool_watermark_gap_raises(stack) -> None:
    stack.meta.advance_watermark("u1", TurnRange(start=0, end=4))
    with pytest.raises(ValueError, match="jumps over unprocessed turns"):
        stack.meta.advance_watermark("u1", TurnRange(start=10, end=12))


def test_profile_crud_and_token_cascade(stack) -> None:
    stack.meta.upsert_profile(_pool_profile(stack))
    assert stack.meta.get_profile("u1").display_name == "Uma"
    stack.meta.upsert_profile(StoredProfile(profile_id="u1", display_name="Uma Updated"))
    assert stack.meta.get_profile("u1").display_name == "Uma Updated"
    stack.meta.upsert_profile(StoredProfile(profile_id="u2", display_name="Bob"))
    assert {p.profile_id for p in stack.meta.list_profiles()} == {"u1", "u2"}

    token = stack.meta.issue_token("u1", ("graph:read",))
    stack.meta.delete_profile("u1")
    assert stack.meta.get_profile("u1") is None
    assert raw_meta_row(stack, "tokens", "token_id", token.token_id) == {}  # FK cascade


def test_issue_token_and_revoke(stack) -> None:
    stack.meta.upsert_profile(_pool_profile(stack))
    token = stack.meta.issue_token("u1", ("graph:read", "graph:write"), expires_at=time.time() + 60.0)
    assert token.profile_id == "u1"
    assert tuple(token.scopes) == ("graph:read", "graph:write")
    assert token.revoked is False
    with pytest.raises(StorageError, match="unknown profile"):
        stack.meta.issue_token("ghost", ("graph:read",))

    stack.meta.revoke_token(token.token_id)
    assert int(raw_meta_row(stack, "tokens", "token_id", token.token_id)["revoked"]) == 1


def test_config_versioned_get_set_rollback(stack) -> None:
    v1 = stack.meta.set_config("theme", {"mode": "dark"})
    assert v1 == 1
    stack.meta.set_config("theme", {"mode": "light"})
    latest = stack.meta.get_config("theme")
    assert isinstance(latest, ConfigEntry)
    assert latest.version == 2
    assert latest.value == {"mode": "light"}
    assert stack.meta.get_config("theme", version=1).value == {"mode": "dark"}
    assert stack.meta.get_config("missing-key") is None

    stack.meta.rollback_config("theme", v1)
    rolled = stack.meta.get_config("theme")
    assert rolled.version == 3
    assert rolled.value == {"mode": "dark"}
    with pytest.raises(StorageError, match="has no version 99"):
        stack.meta.rollback_config("theme", 99)


def test_audit_append_and_query(stack) -> None:
    stack.meta.audit_append(AuditEntry(actor="alice", action="insert", detail={"n": 1}, at=100.0))
    stack.meta.audit_append(AuditEntry(actor="bob", action="read", detail={"n": 2}, at=200.0))
    page = stack.meta.audit_query(AuditFilter(actor="alice"), Page(0, 50))
    assert page.total == 1
    assert page.items[0].detail == {"n": 1}
    assert page.items[0].actor == "alice"
    both = stack.meta.audit_query(AuditFilter(since=0.0, until=250.0), Page(0, 50))
    assert both.total == 2


def test_audit_append_only_enforced_by_database(stack) -> None:
    """Both dialects refuse to mutate audit_log at the database level."""
    stack.meta.audit_append(AuditEntry(actor="alice", action="insert", at=100.0))
    if stack.backend == "embedded":
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            stack.meta._conn.execute("UPDATE audit_log SET action = 'tampered'")
    else:
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            with stack.meta._conn.transaction():
                stack.meta._conn.execute("UPDATE audit_log SET action = 'tampered'")


def test_dream_runs_roundtrip(stack) -> None:
    run_id = stack.meta.record_dream_run(
        DreamRun(
            run_id="run-1",
            session_id="s1",
            turn_range=TurnRange(start=1, end=3),
            model_id="claude",
            tokens=42,
            cost=0.0042,
            interrupted=True,
        )
    )
    assert run_id == "run-1"
    page = stack.meta.list_dream_runs(DreamRunFilter(session_id="s1"), Page(0, 50))
    assert page.total == 1
    run = page.items[0]
    assert run.turn_range == TurnRange(start=1, end=3)
    assert run.tokens == 42
    assert run.interrupted is True

    second = stack.meta.list_dream_runs(DreamRunFilter(interrupted=False), Page(0, 50))
    assert second.total == 0


def test_schema_version_and_migrate_forward_only(stack) -> None:
    """meta's head is v3 (frozen v1 schema + v3 profile_score_pool); migrate is
    idempotent and forward-only."""
    assert stack.meta.schema_version() == 3
    assert stack.meta.migrate() == 3
    assert stack.meta.migrate(target=1) == 3  # back-targeting is a no-op at head
    assert stack.meta.schema_version() == 3


def test_meta_stamp_helpers_used(stack) -> None:
    """The stamp helpers are exercised so ruff never prunes them from the suite."""
    prov = make_prov(session_id="s-meta")
    assert prov.session_id == "s-meta"
