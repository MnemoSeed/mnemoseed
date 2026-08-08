"""PgMetaDriver unit surface: capabilities, constructor validation, and the
pure decode helpers. No live Postgres required.
"""

import pytest

from mnemoseed.storage.drivers.pg_meta import (
    PgMetaDriver,
    _decode_audit,
    _decode_dream_run,
    _json_value,
)
from mnemoseed.storage.ports import Capability, StorageError
from mnemoseed.storage.registry import META_DRIVERS, register


@pytest.fixture(autouse=True)
def _ensure_registered():
    if not META_DRIVERS.contains("pg_meta"):
        register(META_DRIVERS)(PgMetaDriver)
    yield


def test_registered_in_shared_registry():
    assert META_DRIVERS.contains("pg_meta")


def test_capabilities_declared():
    caps = PgMetaDriver.info.capabilities
    assert Capability.META_TRANSACTION in caps
    assert Capability.META_CONCURRENT_READERS in caps
    assert len(caps) == 2


def test_constructor_requires_dsn_or_conn(monkeypatch):
    monkeypatch.delenv("MNEMOSEED_PG_DSN", raising=False)
    with pytest.raises(StorageError, match="dsn"):
        PgMetaDriver()


def test_json_value_tolerates_text():
    assert _json_value({"a": 1}) == {"a": 1}
    assert _json_value("[1, 2]") == [1, 2]
    assert _json_value("") is None
    assert _json_value(None) is None


def test_decode_audit():
    row = {
        "id": 3,
        "actor": "alice",
        "action": "insert",
        "detail": {"n": 1},
        "at": "2026-01-01T00:00:00.000Z",
    }
    entry = _decode_audit(row)
    assert entry.id == 3
    assert entry.actor == "alice"
    assert entry.detail == {"n": 1}
    assert isinstance(entry.at, float)


def test_decode_dream_run():
    row = {
        "run_id": "r1",
        "session_id": "s1",
        "turn_start": 1,
        "turn_end": 3,
        "model_id": "claude",
        "started_at": "2026-01-01T00:00:00.000Z",
        "finished_at": "2026-01-01T00:00:10.000Z",
        "tokens": 42,
        "cost": 0.0042,
        "interrupted": 1,
        "dropped_count": 1,
    }
    run = _decode_dream_run(row)
    assert run.session_id == "s1"
    assert run.turn_range == (1, 3) or run.turn_range.start == 1
    assert run.model_id == "claude"
    assert run.tokens == 42
    assert run.interrupted is True
    assert run.dropped_count == 1


def test_decode_dream_run_no_turn_range():
    row = {
        "run_id": "r2",
        "session_id": None,
        "turn_start": None,
        "turn_end": None,
        "model_id": None,
        "started_at": "2026-01-01T00:00:00.000Z",
        "finished_at": None,
        "tokens": 0,
        "cost": 0.0,
        "interrupted": 0,
        "dropped_count": 0,
    }
    run = _decode_dream_run(row)
    assert run.turn_range is None
    assert run.model_id == ""
