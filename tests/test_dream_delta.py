"""Dream delta packing + prompt-cache partition (PRD-02 T5; FR-2.5 / NFR-2.2).

Testable behaviors asserted through the public surface:

- ``estimate_tokens`` is a deterministic local token estimator (chars/4 for
  non-CJK + one token per CJK char), pinned on known strings.
- Delta packing: chunks are packed whole (never split mid-text) in deterministic
  order under a token budget (default 5000); chunks that do not fit are reported
  as overflow, never silently dropped; the cache prefix never counts against the
  delta budget.
- Cache prefix: byte-stable across dreams of the same profile; per-dream data
  (snapshot ids, chunk ids, timestamps) never leaks in; an optional injected
  graph-digest provider slots into the prefix while the null default renders no
  digest section.
- Cost telemetry: ``DeltaReport`` arithmetic from a configurable per-role price
  table (input / cache-read / output USD per million tokens).
- Orchestrator integration: the reflect pass consumes the packed request instead
  of the raw full-snapshot render; overflow is reported and never an error;
  under no budget pressure the packed delta IS the full snapshot render.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from mnemoseed.capture.pool import PoolEvent, PoolEventKind
from mnemoseed.dream import (
    DEFAULT_DELTA_BUDGET_TOKENS,
    DeltaPacker,
    DeltaReport,
    DeltaRequest,
    DreamState,
    DreamTrigger,
    GraphDigest,
    NullGraphDigest,
    NullSnapshotter,
    PriceTable,
    ReflectionResult,
    ReflectOrchestrator,
    ReflectOutcome,
    StubReflectLLM,
    build_cache_prefix,
    estimate_cost_usd,
    estimate_tokens,
    render_chunk_blocks,
    resume_boundary,
)
from mnemoseed.dream.merge import MergeOutcome, Merger
from mnemoseed.dream.pipeline import DreamPipeline
from mnemoseed.dream.snapshot import FileSnapshotter, Snapshot, SnapshotChunk
from mnemoseed.schema.stamp import ChunkStamp, CognitiveTier, Cues, Provenance
from mnemoseed.storage.ports import TurnRange

_RANGE = TurnRange(0, 10)
_DEFAULT_INPUT = PriceTable().input_usd_per_m
_DEFAULT_CACHE = PriceTable().cache_read_usd_per_m


# ---------------------------------------------------------------- helpers


def _stamp(
    chunk_id: str,
    text: str,
    *,
    turn_start: int = 0,
    turn_end: int = 1,
) -> ChunkStamp:
    return ChunkStamp(
        chunk_id=chunk_id,
        profile_id="alice",
        text=text,
        cognitive_tier=CognitiveTier.TIER_1,
        model_id="test-model",
        cues=Cues(entities=[]),
        provenance=Provenance(asserted_by="user", session_id="s1", source="manual"),
        turn_start=turn_start,
        turn_end=turn_end,
    )


def _snap(
    *stamps: ChunkStamp,
    snapshot_id: str = "snap-p1",
    profile_id: str = "alice",
    created_at: float = 1000.0,
    phases: frozenset[str] = frozenset({"snapshot_done"}),
) -> Snapshot:
    return Snapshot(
        snapshot_id=snapshot_id,
        profile_id=profile_id,
        turn_range=_RANGE,
        chunks=tuple(SnapshotChunk.from_stamp(c) for c in stamps),
        created_at=created_at,
        phases=phases,
    )


class _RecordingLLM(StubReflectLLM):
    """Stub that also records the (system, user) segments of each chat call."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, str]] = []

    def chat(self, *, system: str, user: str) -> str:
        self.calls.append((system, user))
        return super().chat(system=system, user=user)


class _FixedDigest:
    """Graph-digest double returning a fixed, profile-independent string."""

    def __init__(self, value: str) -> None:
        self._value = value
        self.calls = 0

    def digest(self, profile_id: str) -> str:
        del profile_id
        self.calls += 1
        return self._value


def _packed_tokens(text: str) -> int:
    return estimate_tokens(text)


# ---------------------------------------------------------------- token estimator


def test_estimate_tokens_pinned_strings() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("a") == 1
    assert estimate_tokens("hello") == 2  # 5 non-CJK chars -> ceil(5/4)
    assert estimate_tokens("hello world") == 3  # 11 non-CJK chars -> ceil(11/4)
    assert estimate_tokens("你好世界") == 4  # one token per CJK char
    assert estimate_tokens("hello你好") == 4  # ceil(5/4) + 2 CJK


def test_estimate_tokens_deterministic_across_calls() -> None:
    text = "I prefer dark mode and vim, 深色模式是最好的。"
    assert estimate_tokens(text) == estimate_tokens(text)


def test_estimate_tokens_english_accuracy_bounds() -> None:
    """Documented envelope: a BPE tokenizer averages roughly 3-5 chars/token
    for English prose; the chars/4 estimator must land inside that band."""
    text = "a" * 4000
    estimate = estimate_tokens(text)
    assert 4000 // 6 <= estimate <= 4000 // 3
    assert estimate == 1000


def test_estimate_tokens_mixed_cjk_and_ascii() -> None:
    # "你好" = 2 CJK tokens; " world" = 6 non-CJK chars -> ceil(6/4) = 2
    assert estimate_tokens("你好 world") == 4


# ---------------------------------------------------------------- delta packing


def test_delta_pack_all_chunks_when_within_budget() -> None:
    snap = _snap(
        _stamp("c1", "a" * 8, turn_start=2, turn_end=3),
        _stamp("c2", "b" * 8, turn_start=0, turn_end=1),
        _stamp("c3", "c" * 8, turn_start=4, turn_end=5),
    )
    request = DeltaPacker().pack(snap)
    assert request.packed_chunk_ids == ("c2", "c1", "c3")  # deterministic turn order
    assert request.overflow_chunk_ids == ()
    assert request.delta == render_chunk_blocks(snap.chunks)
    assert request.delta_tokens == _packed_tokens(request.delta)


def test_delta_pack_is_deterministic_same_input_same_request() -> None:
    snap = _snap(_stamp("c1", "a" * 40), _stamp("c2", "b" * 40))
    assert DeltaPacker().pack(snap) == DeltaPacker().pack(snap)


def test_delta_order_is_deterministic_regardless_of_input_order() -> None:
    a = _stamp("c1", "a" * 16, turn_start=2, turn_end=3)
    b = _stamp("c2", "b" * 16, turn_start=0, turn_end=1)
    forward = _snap(a, b)
    reversed_snap = _snap(b, a)
    assert DeltaPacker().pack(forward) == DeltaPacker().pack(reversed_snap)


def test_delta_overflow_reported_never_dropped() -> None:
    """Overflow chunk ids are part of the result; every chunk is either packed
    or reported, never silently dropped."""
    snap = _snap(
        *(_stamp(f"c{i}", "z" * 100, turn_start=i, turn_end=i) for i in range(4)),
    )  # each rendered block is ~42 delta tokens
    request = DeltaPacker(budget_tokens=90).pack(snap)
    assert request.packed_chunk_ids == ("c0", "c1")
    assert request.overflow_chunk_ids == ("c2", "c3")
    assert set(request.packed_chunk_ids + request.overflow_chunk_ids) == {"c0", "c1", "c2", "c3"}
    assert request.delta_tokens <= 90


def test_delta_never_splits_a_chunk() -> None:
    snap = _snap(_stamp("c1", "a" * 200, turn_start=0, turn_end=1))  # ~69 block tokens
    request = DeltaPacker(budget_tokens=10).pack(snap)
    assert request.overflow_chunk_ids == ("c1",)
    assert request.delta == ""
    assert request.delta_tokens == 0


def test_delta_prefix_excluded_from_budget() -> None:
    """System instruction + stable context never count against the delta budget:
    a very large cache prefix must not push chunks into overflow (FR-2.5)."""
    chunks = _snap(*(_stamp(f"c{i}", "z" * 40, turn_start=i, turn_end=i) for i in range(3)))
    digest = _FixedDigest("g" * 20000)  # ~5000 cached prefix tokens on its own
    request = DeltaPacker(budget_tokens=100, graph_digest=digest).pack(chunks)
    assert request.prefix_tokens >= 5000
    assert request.overflow_chunk_ids == ()
    assert request.packed_chunk_ids == ("c0", "c1", "c2")


def test_delta_budget_default_is_5000_and_caps_delta() -> None:
    """NFR-2.2 cap proof: twenty-two 1000-char chunks would render to ~5500
    tokens uncapped; the default packing always lands at or below 5000 and the
    excess is reported as overflow."""
    snap = _snap(
        *(_stamp(f"c{i}", "z" * 1000, turn_start=i, turn_end=i) for i in range(22)),
    )
    assert _packed_tokens(render_chunk_blocks(snap.chunks)) > 5500
    request = DeltaPacker().pack(snap)
    assert request.delta_tokens <= 5000
    assert request.overflow_chunk_ids  # a later dream picks these up
    assert len(request.packed_chunk_ids) + len(request.overflow_chunk_ids) == 22


# ---------------------------------------------------------------- cache prefix


def test_cache_prefix_byte_stable_across_dreams_of_same_profile() -> None:
    dream_a = _snap(_stamp("c1", "I prefer dark mode"), snapshot_id="snap-a", created_at=1.0)
    dream_b = _snap(_stamp("c9", "I like coffee"), snapshot_id="snap-b", created_at=999.0)
    packer = DeltaPacker()
    assert packer.pack(dream_a).cache_prefix == packer.pack(dream_b).cache_prefix
    assert packer.pack(dream_a).cache_prefix == build_cache_prefix("")


def test_cache_prefix_excludes_per_dream_data() -> None:
    snap = _snap(_stamp("c7", "I prefer dark mode"), snapshot_id="snap-secret", created_at=1234.0)
    prefix = DeltaPacker().pack(snap).cache_prefix
    assert "snap-secret" not in prefix
    assert "c7" not in prefix
    assert "1234" not in prefix
    assert "1000.0" not in prefix


def test_cache_prefix_includes_injected_graph_digest() -> None:
    digest = _FixedDigest("digest-abc")
    snap = _snap(_stamp("c1", "I prefer dark mode"))
    packer = DeltaPacker(graph_digest=digest)
    prefix = packer.pack(snap).cache_prefix
    assert digest.calls == 1
    assert "digest-abc" in prefix
    assert "Known graph digest" in prefix
    # still byte-stable across dreams when the digest is stable
    other = _snap(_stamp("c9", "zzz"), snapshot_id="snap-other", created_at=5.0)
    assert packer.pack(other).cache_prefix == prefix


def test_null_graph_digest_renders_no_section() -> None:
    assert NullGraphDigest().digest("alice") == ""
    assert DeltaPacker(graph_digest=NullGraphDigest()).pack(
        _snap(_stamp("c1", "hi"))
    ).cache_prefix == build_cache_prefix("")


def test_graph_digest_protocol_seam_is_satisfied_by_duck_typed_provider() -> None:
    provider: GraphDigest = _FixedDigest("stable")
    assert provider.digest("alice") == "stable"


# ---------------------------------------------------------------- cost telemetry


def test_estimate_cost_usd_default_short_increment_pricing() -> None:
    assert estimate_cost_usd(delta_tokens=2500, prefix_tokens=1000, price=PriceTable()) == pytest.approx(
        2500 * _DEFAULT_INPUT / 1e6 + 1000 * _DEFAULT_CACHE / 1e6
    )


def test_estimate_cost_usd_is_configurable_per_role() -> None:
    price = PriceTable(input_usd_per_m=4.0, cache_read_usd_per_m=1.0, output_usd_per_m=12.0)
    assert estimate_cost_usd(
        delta_tokens=1000, prefix_tokens=1000, output_tokens=250, price=price
    ) == pytest.approx((4000 + 1000 + 3000) / 1e6)


def test_delta_report_tracks_delta_prefix_overflow() -> None:
    snap = _snap(
        *(_stamp(f"c{i}", "z" * 100, turn_start=i, turn_end=i) for i in range(30)),
    )
    packer = DeltaPacker(budget_tokens=200)
    request = packer.pack(snap)
    report = packer.report(request)
    assert report.delta_tokens == request.delta_tokens
    assert report.prefix_tokens == request.prefix_tokens
    assert report.overflow_count == len(request.overflow_chunk_ids)
    assert report.delta_tokens <= 200
    assert report.overflow_count > 0
    assert report.estimated_cost_usd == pytest.approx(
        estimate_cost_usd(
            delta_tokens=request.delta_tokens,
            prefix_tokens=request.prefix_tokens,
            price=packer.price,
        )
    )
    assert report.prefix_tokens == estimate_tokens(request.cache_prefix)


def test_nfr22_budget_arithmetic_five_thousand_delta_cap() -> None:
    """NFR-2.2 substrate: report the cost projection for the 5k delta budget."""
    request = DeltaRequest(
        version="v1",
        profile_id="alice",
        cache_prefix=build_cache_prefix(""),
        delta="z" * 20000,  # 5000 estimated delta tokens
        packed_chunk_ids=("c1",),
        overflow_chunk_ids=(),
        delta_tokens=5000,
        prefix_tokens=estimate_tokens(build_cache_prefix("")),
    )
    report = DeltaPacker().report(request)
    assert report.delta_tokens == 5000
    assert report.estimated_cost_usd == pytest.approx(
        5000 * _DEFAULT_INPUT / 1e6 + report.prefix_tokens * _DEFAULT_CACHE / 1e6
    )


# ---------------------------------------------------------------- orchestrator integration


def test_orchestrator_default_preserves_full_render_behavior(tmp_path: Path) -> None:
    """No budget pressure: the deltas the LLM sees ARE the full snapshot render,
    split as (stable cache prefix -> system, chunk blocks -> user)."""
    snap = _snap(
        _stamp("c1", "I prefer dark mode", turn_start=2, turn_end=3),
        _stamp("c2", "I like coffee", turn_start=0, turn_end=1),
    )
    llm = _RecordingLLM()
    outcome = ReflectOrchestrator(llm=llm, directory=tmp_path, packer=DeltaPacker()).reflect(snap)
    assert outcome.ok
    assert outcome.result is not None
    assert len(outcome.result.triples) == 2
    assert len(llm.calls) == 1
    assert llm.calls[0][0] == build_cache_prefix("")
    assert llm.calls[0][1] == render_chunk_blocks(snap.chunks)
    assert outcome.report is not None
    assert outcome.report.overflow_count == 0


def test_orchestrator_outcome_carries_delta_report(tmp_path: Path) -> None:
    snap = _snap(_stamp("c1", "I prefer dark mode"))
    packer = DeltaPacker()
    outcome = ReflectOrchestrator(llm=StubReflectLLM(), directory=tmp_path, packer=packer).reflect(snap)
    assert outcome.ok
    assert outcome.report is not None
    assert isinstance(outcome.report, DeltaReport)
    assert outcome.report.delta_tokens > 0
    assert outcome.report.delta_tokens == estimate_tokens(render_chunk_blocks(snap.chunks))


def test_orchestrator_overflow_reflects_packed_subset_only(tmp_path: Path) -> None:
    """Chunks beyond the budget are deferred (reported as overflow), never an
    error, and never reflected this pass."""
    coffee = (_stamp(f"c{i}", "I prefer coffee", turn_start=i, turn_end=i) for i in range(2, 12))
    snap = _snap(_stamp("c1", "I prefer dark mode", turn_start=0, turn_end=1), *coffee)
    packer = DeltaPacker(budget_tokens=40)
    request = packer.pack(snap)
    assert request.overflow_chunk_ids  # coffee chunks beyond the cap are deferred

    outcome = ReflectOrchestrator(llm=StubReflectLLM(), directory=tmp_path, packer=packer).reflect(snap)
    assert outcome.ok  # overflow is never an error
    assert outcome.report is not None
    assert outcome.report.overflow_count == len(request.overflow_chunk_ids)
    for triple in outcome.result.triples or ():
        for cid in triple.chunk_ids:
            assert cid in request.packed_chunk_ids


def test_orchestrator_marker_gate_skips_packing(tmp_path: Path) -> None:
    snap = _snap(_stamp("c1", "I prefer dark mode"), phases=frozenset({"snapshot_done", "reflect_done"}))
    llm = _RecordingLLM()
    outcome = ReflectOrchestrator(llm=llm, directory=tmp_path).reflect(snap)
    assert outcome.skipped is True
    assert outcome.report is None  # skipped dreams never call the cloud
    assert llm.calls == []


def test_orchestrator_empty_snapshot_reports_zero_delta(tmp_path: Path) -> None:
    snap = _snap()
    packer = DeltaPacker()
    outcome = ReflectOrchestrator(
        llm=StubReflectLLM(),
        directory=tmp_path,
        packer=packer,
        on_done=lambda profile_id: None,
    ).reflect(snap)
    assert outcome.ok
    assert outcome.result is not None
    assert outcome.result.triples == ()
    assert outcome.report is not None
    assert outcome.report.delta_tokens == 0
    assert outcome.report.overflow_count == 0


# ---------------------------------------------------------------- D1 data-loss defenses
#
# The delta layer must never be the reason a source chunk is lost. Without a
# guard, a snapshot whose chunks all exceed the budget produced an empty delta,
# the LLM was still called, zero triples merged, and the safe-clear purged the
# source rows the model never saw. Two defense lines break that chain:
#
# 1. Orchestrator (reflect boundary): when nothing packed but overflow exists,
#    the dream is deferred -- no cloud call, no REFLECT_DONE, the snapshot stays
#    journaled so a later dream (bigger budget / manual run) picks the overflow
#    up.
# 2. Pipeline (merge boundary): a result that is empty BECAUSE the delta was
#    truncated (``overflow_chunk_ids`` non-empty) is never handed to the merger,
#    so the commit callback cannot fire the purge. A genuinely empty result with
#    NO overflow (all-noise session) still merges and safe-clears normally.


class _VectorFake:
    """VectorStore/GraphStore-shaped double: snapshot_read + purge_range plus
    the merger's idempotent-write seams (exercised only when triples exist)."""

    def __init__(self, chunks: list[ChunkStamp] | None = None) -> None:
        self.chunks = list(chunks or [])
        self.purged: list[tuple[str, int, int]] = []
        self.deleted: list[str] = []  # per-id safe-clear (consumed-ids-scoped)

    def capabilities(self) -> frozenset[object]:
        return frozenset()

    def snapshot_read(self, filter: object) -> list[ChunkStamp]:
        return [c for c in self.chunks if c.profile_id == getattr(filter, "profile_id", None)]

    def delete_chunk(self, chunk_id: str) -> None:
        self.deleted.append(chunk_id)
        self.chunks = [c for c in self.chunks if c.chunk_id != chunk_id]

    def purge_range(self, session_id: str, turn_start: int, turn_end: int) -> int:
        self.purged.append((session_id, turn_start, turn_end))
        before = len(self.chunks)
        self.chunks = [
            c
            for c in self.chunks
            if not (
                c.provenance.session_id == session_id
                and c.turn_start is not None
                and c.turn_end is not None
                and c.turn_start <= turn_end
                and c.turn_end >= turn_start
            )
        ]
        return before - len(self.chunks)

    def find_same_predicate(self, subject: str, predicate: str, profile_id: str) -> list[Any]:
        del subject, predicate, profile_id
        return []

    def upsert_node(self, node: Any) -> None:
        del node


class _MetaFake:
    """MetaStore-shaped double: the FileSnapshotter's dream-run registration."""

    def __init__(self) -> None:
        self.runs: list[Any] = []

    def record_dream_run(self, run: Any) -> str:
        self.runs.append(run)
        return str(getattr(run, "run_id", ""))


class _RecordingMerger(Merger):
    """The real Merger plus a call counter, so tests can distinguish 'blocked
    before merge' from 'merge ran and committed'."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.call_count = 0

    def merge(self, snapshot: Snapshot, result: ReflectionResult) -> MergeOutcome:
        self.call_count += 1
        return super().merge(snapshot, result)


class _NeverReflector:
    """Reflector double that records, and must never be handed a merge-boundary
    snapshot (reflect is never re-run once REFLECT_DONE is journaled)."""

    def __init__(self) -> None:
        self.calls: list[Snapshot] = []

    def reflect(self, snapshot: Snapshot) -> ReflectOutcome:
        self.calls.append(snapshot)
        return ReflectOutcome(ok=True, result=None)


def _event(profile: str = "alice", rng: TurnRange = _RANGE) -> PoolEvent:
    return PoolEvent(
        kind=PoolEventKind.DREAM_TRIGGER,
        profile_id=profile,
        turn_range=rng,
        balance=12.0,
        fired_at=1.0,
    )


def _chain(
    tmp_path: Path,
    store: _VectorFake,
    *,
    budget: int = DEFAULT_DELTA_BUDGET_TOKENS,
) -> tuple[FileSnapshotter, DreamTrigger, _RecordingLLM, _RecordingMerger]:
    """Production-shaped wiring: trigger intake -> FileSnapshotter capture ->
    real ReflectOrchestrator -> real Merger -> trigger safe-clear purger."""
    meta = _MetaFake()
    fs = FileSnapshotter(store=store, meta=meta, directory=tmp_path / "dreams")
    trigger = DreamTrigger(snapshotter=fs, auto_trigger=True, purger=fs.purge_snapshot)
    llm = _RecordingLLM()
    reflector = ReflectOrchestrator(
        llm=llm,
        directory=tmp_path / "dreams",
        packer=DeltaPacker(budget_tokens=budget),
        on_done=trigger.on_reflect_complete,
    )
    merger = _RecordingMerger(
        graph_main=store,
        graph_isolated=None,
        meta=meta,
        on_committed=trigger.on_merge_committed,
    )
    pipeline = DreamPipeline(trigger=trigger, snapshotter=fs, reflector=reflector, merger=merger)
    fs.on_ready = pipeline.on_snapshot_ready
    return fs, trigger, llm, merger


def test_d1_full_overflow_reflect_defers_and_never_calls_cloud(tmp_path: Path) -> None:
    """Defense line 1 (orchestrator): a snapshot whose every chunk is over the
    delta budget is deferred, not reflected. Nothing hits the LLM, no REFLECT_DONE
    is persisted, and the report still carries the overflow count."""
    snap = _snap(_stamp("huge", "I prefer dark mode. " * 1000))
    packer = DeltaPacker()  # default 5000-token budget: the single chunk overflows
    assert packer.pack(snap).overflow_chunk_ids == ("huge",)
    llm = _RecordingLLM()
    done: list[str] = []
    outcome = ReflectOrchestrator(
        llm=llm,
        directory=tmp_path / "dreams",
        packer=packer,
        on_done=lambda p: done.append(p),
    ).reflect(snap)
    assert outcome.ok is False
    assert outcome.result is None
    assert "delta budget" in (outcome.error or "")
    assert outcome.report is not None
    assert outcome.report.overflow_count == 1
    assert outcome.report.delta_tokens == 0
    assert llm.calls == []  # the cloud call is skipped entirely
    assert done == []  # on_done never fired: the snapshot stays at the reflect boundary
    assert list(tmp_path.glob("*.json")) == []  # nothing journaled for a deferred dream


def test_d1_verifier_repro_over_budget_chunk_survives_then_later_dream_completes(
    tmp_path: Path,
) -> None:
    """Verifier repro (the data-loss chain is broken): a snapshot whose only
    chunk does not fit the default budget survives the full reflect -> merge ->
    commit -> safe-clear chain untouched, stays journaled at the reflect
    boundary, and a later dream with a bigger budget picks it up and completes
    the commit + purge normally."""
    store = _VectorFake([_stamp("huge", "I prefer dark mode. " * 1000)])
    fs, trigger, llm, merger = _chain(tmp_path, store)
    trigger.handle_event(_event())

    # defense 1 engages at the reflect boundary: nothing to pack -> no cloud call
    assert llm.calls == []
    assert merger.call_count == 0  # the merger is never reached
    assert store.purged == []  # the safe-clear purge never fired
    assert store.deleted == []  # no per-id delete either
    assert [c.chunk_id for c in store.chunks] == ["huge"]  # the chunk survives

    snapshot = fs.active("alice")
    assert snapshot is not None
    pending = FileSnapshotter(store=store, meta=_MetaFake(), directory=tmp_path / "dreams").recover()
    assert [s.snapshot_id for s in pending] == [snapshot.snapshot_id]
    assert resume_boundary(pending[0]) == "reflect"  # re-pickable by a later dream

    # a later dream (same profile, bigger budget) re-processes the retained chunk:
    # it packs, reflects one triple, commits, and the safe-clear completes
    llm2 = _RecordingLLM()
    pipeline2 = DreamPipeline(
        trigger=trigger,
        snapshotter=fs,
        reflector=ReflectOrchestrator(
            llm=llm2,
            directory=tmp_path / "dreams",
            packer=DeltaPacker(budget_tokens=6000),
            on_done=trigger.on_reflect_complete,
        ),
        merger=_RecordingMerger(
            graph_main=store,
            graph_isolated=None,
            meta=_MetaFake(),
            on_committed=trigger.on_merge_committed,
        ),
    )
    pipeline2.run(snapshot)

    assert len(llm2.calls) == 1
    assert "I prefer dark mode" in llm2.calls[0][1]  # the retained chunk reached the model
    # the safe-clear is id-scoped now: exactly the consumed row, never a range delete
    assert store.deleted == ["huge"]
    assert store.purged == []
    assert store.chunks == []  # the over-budget chunk was processed and cleared, not lost


def test_d1_partial_overflow_with_empty_triples_defers_merge(tmp_path: Path) -> None:
    """Defense line 2 (pipeline): the packed delta reflects fine, but with the
    overflow chunk flagged and ZERO triples extracted, the snapshot is NOT handed
    to the merger -- committing would purge source chunks the model never saw."""
    noise = _stamp("noise", "lorem ipsum dolor sit amet", turn_start=0, turn_end=1)
    huge = _stamp("huge", "z" * 20000, turn_start=0, turn_end=1)
    store = _VectorFake([noise, huge])
    fs, trigger, llm, merger = _chain(tmp_path, store)
    trigger.handle_event(_event())

    assert len(llm.calls) == 1  # the packed (noise-only) delta WAS reflected
    assert "huge" not in llm.calls[0][1]  # the overflow chunk never reached the model
    assert merger.call_count == 0  # engine-side insurance: merge blocked
    assert store.purged == []  # no commit, no purge
    assert store.deleted == []  # and no per-id delete either
    assert {c.chunk_id for c in store.chunks} == {"noise", "huge"}  # nothing dropped


def test_d1_merge_boundary_recovery_respects_persisted_overflow(tmp_path: Path) -> None:
    """The overflow flag survives the journal round-trip: a crashed dream that
    reflected a truncated delta and crashed before merge resumes at the MERGE
    boundary with the guard active -- reflect is never re-run and the deferred
    merge cannot purge the overflow chunk."""
    noise = _stamp("noise", "lorem ipsum dolor sit amet", turn_start=0, turn_end=1)
    huge = _stamp("huge", "z" * 20000, turn_start=0, turn_end=1)
    store = _VectorFake([noise, huge])
    fs1 = FileSnapshotter(store=store, meta=_MetaFake(), directory=tmp_path / "dreams")
    snap = fs1.request("alice", _RANGE).snapshot
    assert snap is not None
    outcome = ReflectOrchestrator(
        llm=_RecordingLLM(),
        directory=tmp_path / "dreams",
        packer=DeltaPacker(),
    ).reflect(snap)
    assert outcome.ok
    assert outcome.result is not None
    assert outcome.result.triples == ()
    assert outcome.result.overflow_chunk_ids == ("huge",)

    # a fresh boot recovers at the merge boundary with the payload intact
    fs2 = FileSnapshotter(store=store, meta=_MetaFake(), directory=tmp_path / "dreams")
    pending = fs2.recover()
    assert len(pending) == 1
    assert resume_boundary(pending[0]) == "merge"
    fs2.adopt(pending[0])
    assert pending[0].turn_range == _RANGE

    reflector = _NeverReflector()
    merger = _RecordingMerger(
        graph_main=store,
        graph_isolated=None,
        meta=_MetaFake(),
        on_committed=lambda p: fs2.purge_snapshot(p, _RANGE),
    )
    pipeline = DreamPipeline(
        trigger=DreamTrigger(snapshotter=NullSnapshotter(), purger=fs2.purge_snapshot),
        snapshotter=fs2,
        reflector=reflector,  # type: ignore[arg-type]
        merger=merger,
    )
    pipeline.run(pending[0])

    assert reflector.calls == []  # reflect must never re-run at the merge boundary
    assert merger.call_count == 0  # the persisted overflow held the guard
    assert store.purged == []
    assert store.deleted == []
    assert {c.chunk_id for c in store.chunks} == {"noise", "huge"}


def test_d1_control_all_noise_without_overflow_still_commits_and_purges(tmp_path: Path) -> None:
    """Control: a legitimately empty result with NO overflow (an all-noise
    session) must still merge, commit, and safe-clear exactly as it did before
    the delta layer -- the guard only defers overflow-truncated empties."""
    store = _VectorFake(
        [
            _stamp("noise-a", "lorem ipsum dolor sit amet", turn_start=0, turn_end=1),
            _stamp("noise-b", "consectetur adipiscing elit sed do eiusmod", turn_start=0, turn_end=1),
        ]
    )
    fs, trigger, llm, merger = _chain(tmp_path, store)
    trigger.handle_event(_event())

    assert len(llm.calls) == 1
    assert merger.call_count == 1  # merge ran and committed (empty result, no overflow)
    # no overflow: the allow-list equals every chunk, so the id-scoped purge is
    # behavior-equivalent to the old full-range purge -- both rows are cleared
    assert sorted(store.deleted) == ["noise-a", "noise-b"]
    assert store.purged == []
    assert store.chunks == []  # all-noise rows cleared, exactly as before T5
    assert trigger.status("alice").state is DreamState.IDLE
    assert FileSnapshotter(store=store, meta=_MetaFake(), directory=tmp_path / "dreams").recover() == []


def test_purge_snapshot_explicit_consumed_allow_list_is_id_scoped(tmp_path: Path) -> None:
    """The purge seam accepts an explicit id allow-list (or equivalent mechanism):
    only those rows are deleted, never-seen rows survive, and a merge-complete
    snapshot never re-purges."""
    store = _VectorFake(
        [
            _stamp("c1", "a" * 8, turn_start=0, turn_end=1),
            _stamp("c2", "b" * 8, turn_start=2, turn_end=3),
        ]
    )
    fs = FileSnapshotter(store=store, meta=_MetaFake(), directory=tmp_path / "dreams")
    assert fs.request("alice", _RANGE).ok
    assert fs.purge_snapshot("alice", _RANGE, consumed_chunk_ids=["c1"]) == 1
    assert [c.chunk_id for c in store.chunks] == ["c2"]  # the never-seen row survives
    assert store.purged == []  # id-scoped: no range purge fired
    assert store.deleted == ["c1"]
    # marker guard: a re-drive is a no-op, never a double delete
    assert fs.purge_snapshot("alice", _RANGE, consumed_chunk_ids=["c1", "c2"]) == 0
    assert [c.chunk_id for c in store.chunks] == ["c2"]


def test_d1_verifier_probe_partial_overflow_with_triples_keeps_overflow_chunks(
    tmp_path: Path,
) -> None:
    """Verifier probe (the HIGH data-loss residual, fixed end-to-end): 22 chunks
    over a 60-turn window at the default 5000-token budget pack 9 (c0-c8) and
    overflow 13 (c9-c21, ~59% of the window). One triple is extracted so the
    merge commits -- but the safe-clear now deletes ONLY the consumed rows, so
    the 13 chunks the model never saw stay in the store for a later dream
    instead of being silently purged."""
    store = _VectorFake(
        [
            _stamp(f"c{i}", "I prefer dark mode. " * 100, turn_start=i * 2, turn_end=i * 2 + 1)
            for i in range(22)
        ]
    )
    fs, trigger, llm, merger = _chain(tmp_path, store)
    trigger.handle_event(_event(rng=TurnRange(0, 60)))

    assert len(llm.calls) == 1
    assert llm.calls[0][1].count("<chunk>") == 9  # exactly the packed window reached the model
    assert "c0" in llm.calls[0][1] and "c8" in llm.calls[0][1]
    assert "c9" not in llm.calls[0][1] and "c21" not in llm.calls[0][1]
    assert merger.call_count == 1  # one triple extracted -> the commit goes through
    assert sorted(store.deleted) == [f"c{i}" for i in range(9)]  # consumed rows only
    assert store.purged == []  # id-scoped, never range-scoped
    assert {c.chunk_id for c in store.chunks} == {f"c{i}" for i in range(9, 22)}  # 13 overflow chunks survive
    assert trigger.status("alice").state is DreamState.IDLE


def test_d1_recovery_partial_overflow_with_triples_purges_only_consumed(tmp_path: Path) -> None:
    """Merge-boundary recovery WITH triples: the journaled consumed allow-list
    survives the boot round-trip, so the resumed committed merge purges ONLY the
    packed rows -- the never-reflected overflow chunk survives for a later dream
    (verifier ask: packed ids read back from the journal at resume)."""
    chunks = [
        _stamp(f"c{i}", "I prefer dark mode", turn_start=i * 2, turn_end=i * 2 + 1) for i in range(5)
    ] + [_stamp("huge", "z" * 20000, turn_start=50, turn_end=51)]
    store = _VectorFake(chunks)
    fs1 = FileSnapshotter(store=store, meta=_MetaFake(), directory=tmp_path / "dreams")
    snap = fs1.request("alice", TurnRange(0, 60)).snapshot
    assert snap is not None
    outcome = ReflectOrchestrator(
        llm=_RecordingLLM(),
        directory=tmp_path / "dreams",
        packer=DeltaPacker(),
    ).reflect(snap)
    assert outcome.ok
    assert outcome.result is not None
    assert len(outcome.result.triples) == 1  # a real triple is waiting to commit
    assert outcome.result.overflow_chunk_ids == ("huge",)

    # crash after reflect, before merge: a fresh boot recovers at the merge boundary
    fs2 = FileSnapshotter(store=store, meta=_MetaFake(), directory=tmp_path / "dreams")
    pending = fs2.recover()
    assert len(pending) == 1
    assert resume_boundary(pending[0]) == "merge"
    fs2.adopt(pending[0])
    assert pending[0].reflect_result is not None  # the journal payload came back

    reflector = _NeverReflector()
    merger = _RecordingMerger(
        graph_main=store,
        graph_isolated=None,
        meta=_MetaFake(),
        on_committed=lambda p: fs2.purge_snapshot(p, pending[0].turn_range),
    )
    pipeline = DreamPipeline(
        trigger=DreamTrigger(snapshotter=NullSnapshotter(), purger=fs2.purge_snapshot),
        snapshotter=fs2,
        reflector=reflector,  # type: ignore[arg-type]
        merger=merger,
    )
    pipeline.run(pending[0])

    assert reflector.calls == []  # reflect never re-runs at the merge boundary
    assert merger.call_count == 1  # triples present -> the committed merge proceeds
    assert sorted(store.deleted) == ["c0", "c1", "c2", "c3", "c4"]  # consumed rows only
    assert store.purged == []
    assert [c.chunk_id for c in store.chunks] == ["huge"]  # the overflow chunk survives
