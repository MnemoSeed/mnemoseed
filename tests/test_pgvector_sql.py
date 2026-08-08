"""PgVectorStore unit surface: DDL generation, filter clauses, sparse similarity
math, chunk row round-trip, and constructor validation. No live Postgres needed.
"""

import math

import pytest
from pgvector import Vector

from mnemoseed.schema.stamp import ChunkStamp, CognitiveTier, Cues, EmotionCue, Provenance
from mnemoseed.storage.drivers.pgvector import (
    PgVectorStore,
    _dense_to_list,
    _filter_clauses,
    _sparse_similarity,
    _to_row,
    _to_stamp,
    chunks_ddl,
    hnsw_index_ddl,
)
from mnemoseed.storage.ports import Capability, ChunkFilter, SparseVector, StorageError
from mnemoseed.storage.registry import VECTOR_DRIVERS, register

_DIM = 64


@pytest.fixture(autouse=True)
def _ensure_registered():
    if not VECTOR_DRIVERS.contains("pgvector"):
        register(VECTOR_DRIVERS)(PgVectorStore)
    yield


def _stamp(**over: object) -> ChunkStamp:
    base: dict[str, object] = dict(
        chunk_id="c1",
        profile_id="alice",
        text="alpha beta gamma",
        cognitive_tier=CognitiveTier.TIER_1,
        model_id="test-model",
        persona_id="p1",
        cues=Cues(
            project="unit-tests",
            tools_used=["pytest"],
            time_bucket="diurnal",
            entities=["math"],
        ),
        provenance=Provenance(
            asserted_by="test-model",
            session_id="s1",
            source="manual",
            confidence=0.8,
            asserted_at=100.0,
        ),
        decay_weight=0.6,
        score=0.7,
        consolidated=True,
        ingested_at=42.0,
    )
    base.update(over)
    return ChunkStamp(**base)


def test_registered_in_shared_registry():
    assert VECTOR_DRIVERS.contains("pgvector")


def test_capabilities_declared():
    caps = PgVectorStore.info.capabilities
    assert Capability.VECTOR_HYBRID_SEARCH in caps
    assert Capability.VECTOR_METADATA_FILTER in caps
    assert Capability.VECTOR_SNAPSHOT in caps
    assert len(caps) == 3


def test_dimensions_validation_before_connection(monkeypatch):
    monkeypatch.delenv("MNEMOSEED_PG_DSN", raising=False)
    for bad in (0, -4, True):
        with pytest.raises(ValueError, match="dimensions"):
            PgVectorStore(dimensions=bad)
    with pytest.raises(StorageError, match="dsn"):
        PgVectorStore(dimensions=64)


def test_chunks_ddl_carries_every_appendix_a1_field():
    sql = chunks_ddl("chunks", _DIM)
    assert "chunk_id TEXT PRIMARY KEY" in sql
    assert "text TEXT NOT NULL" in sql
    assert f"vector_dense vector({_DIM}) NOT NULL" in sql
    assert "vector_sparse JSONB NOT NULL" in sql
    assert "profile_id TEXT NOT NULL" in sql
    assert "session_id TEXT" in sql
    assert "turn_start INTEGER" in sql
    assert "turn_end INTEGER" in sql
    assert "cognitive_tier INTEGER NOT NULL DEFAULT 3" in sql
    assert "model_id TEXT NOT NULL" in sql
    assert "anima_id TEXT" in sql
    assert "cues JSONB NOT NULL" in sql
    assert "provenance JSONB NOT NULL" in sql
    assert "score JSONB NOT NULL" in sql
    assert "decay_weight DOUBLE PRECISION NOT NULL DEFAULT 1.0" in sql
    assert "last_reinforced DOUBLE PRECISION" in sql
    assert "consolidated BOOLEAN NOT NULL DEFAULT FALSE" in sql
    assert "ingested_at DOUBLE PRECISION NOT NULL" in sql
    assert "peripheral_gaps BOOLEAN NOT NULL DEFAULT FALSE" in sql
    assert "needs_reconcile BOOLEAN NOT NULL DEFAULT FALSE" in sql
    assert "hit_count INTEGER NOT NULL DEFAULT 0" in sql
    assert "last_hit_at DOUBLE PRECISION" in sql
    assert "reinforce_count INTEGER NOT NULL DEFAULT 0" in sql


def test_hnsw_index_ddl_uses_cosine_ops():
    sql = hnsw_index_ddl("chunks")
    assert "chunks_dense_hnsw" in sql
    assert "USING hnsw (vector_dense vector_cosine_ops)" in sql


def test_filter_clauses_always_profile_scoped():
    clauses, params = _filter_clauses(ChunkFilter(profile_id="p1"))
    assert clauses == ["profile_id = %s"]
    assert params == ["p1"]


def test_filter_clauses_metadata_windows():
    f = ChunkFilter(
        profile_id="p1",
        min_decay=0.3,
        ingested_after=10.0,
        ingested_before=20.0,
        session_id="s1",
        turn_start=5,
        turn_end=9,
        entities=("a", "b"),
        consolidated=True,
    )
    clauses, params = _filter_clauses(f)
    assert clauses[0] == "profile_id = %s"
    assert "decay_weight >= %s" in clauses
    assert "ingested_at >= %s" in clauses
    assert "ingested_at <= %s" in clauses
    assert "session_id = %s" in clauses
    assert "turn_start IS NOT NULL AND turn_start >= %s" in clauses
    assert "turn_end IS NOT NULL AND turn_end <= %s" in clauses
    assert any("jsonb_array_elements_text(cues->'entities')" in c for c in clauses)
    assert "consolidated = %s" in clauses
    assert params[0] == "p1"
    assert params[-1] is True
    assert ["a", "b"] in params


def test_sparse_similarity_dot_over_norms():
    query = SparseVector((1, 2, 3), (0.2, 0.4, 0.6))
    stored = {"indices": [2, 3], "values": [0.4, 0.6]}
    expected = (0.4 * 0.4 + 0.6 * 0.6) / (math.sqrt(0.04 + 0.16 + 0.36) * math.sqrt(0.16 + 0.36))
    assert _sparse_similarity(query, stored) == pytest.approx(expected)
    assert _sparse_similarity(query, None) == 0.0
    assert _sparse_similarity(query, {"indices": [], "values": []}) == 0.0


def test_dense_to_list_handles_pgvector_vector():
    assert _dense_to_list(Vector([1.0, 2.0, 3.0])) == [1.0, 2.0, 3.0]
    assert _dense_to_list([1.0, 2.0]) == [1.0, 2.0]
    assert _dense_to_list(None) is None


def test_to_row_to_stamp_round_trip():
    stamp = _stamp()
    sparse = SparseVector((1, 2), (0.5, 0.3))
    row = _to_row(stamp, [0.1, 0.2, 0.3], sparse)
    # sparse stored as a struct, never a dense array
    assert row["vector_sparse"] == {"indices": [1, 2], "values": [0.5, 0.3]}
    assert row["provenance"]["history"] == []

    got = _to_stamp(row)
    assert got.chunk_id == stamp.chunk_id
    assert got.profile_id == stamp.profile_id
    assert got.text == stamp.text
    assert got.persona_id == stamp.persona_id
    assert got.cues.entities == ["math"]
    assert got.cues.project == "unit-tests"
    assert got.provenance.session_id == "s1"
    assert got.decay_weight == pytest.approx(0.6)
    assert got.score == pytest.approx(0.7)
    assert got.consolidated is True
    assert got.ingested_at == pytest.approx(42.0)


def test_to_row_emotion_cue_round_trip():
    stamp = _stamp(cues=Cues(emotion=EmotionCue(valence=0.4, peripheral_gaps=True)))
    row = _to_row(stamp, [0.1], None)
    assert row["cues"]["emotion_valence"] == 0.4
    assert row["peripheral_gaps"] is True

    got = _to_stamp(row)
    assert got.cues.emotion is not None
    assert got.cues.emotion.valence == pytest.approx(0.4)
    assert got.cues.emotion.peripheral_gaps is True
