"""PRD-02 T7 integration tests: interruption injection + pollution audit.

End-to-end tests that exercise the WHOLE dream chain (daemon app wiring ->
ScorePool event -> _DreamRelay -> DreamTrigger -> FileSnapshotter ->
ReflectOrchestrator -> Merger -> consumed-ids-scoped safe-clear) under
adversarial conditions, over REAL storage drivers.

Section A (interruption injection, NFR-2.3 / FR-2.4):
  - crash-after-reflect resumes the merge boundary with reflect never re-run
    (dual-driver parity: embedded + pg).
  - new turns arriving mid-dream keep the ORIGINAL snapshot scope and survive
    the safe-clear.
  - a FORCED_CONSOLIDATION arriving mid-dream queues and drains to a NEW range.

Section B (pollution audit):
  - tier-3 isolation: the main graph ends with ZERO tier-3-sourced nodes and the
    isolated instance + salvage audit hold every contaminated fixture.
  - never-drop (FR-2.5): a delta budget that forces partial overflow with
    triples produced commits the packed rows and preserves the overflow rows.
  - FR-2.12 evidence boundary: a preference triple whose only evidence is
    agent-rendered output never reaches the graph.

Every scenario that crashes a process does so by constructing the journaled
snapshot state the crashed process would have left and running the recovery
loop daemon/app.py executes at boot; no processes are ever killed.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from mnemoseed.capture.pool import PoolEvent, PoolEventKind, ScorePool
from mnemoseed.config import Config, DecayConfig
from mnemoseed.daemon.app import _DreamRelay
from mnemoseed.decay.sweeper import DecaySweeper
from mnemoseed.dream import (
    DEFAULT_DELTA_BUDGET_TOKENS,
    DeltaPacker,
    DreamPipeline,
    DreamState,
    DreamTrigger,
    FileSnapshotter,
    Merger,
    ReflectOrchestrator,
    Snapshot,
    SnapshotPhase,
    StubReflectLLM,
    load_snapshot_file,
    resume_boundary,
)
from mnemoseed.schema.stamp import ChunkStamp, CognitiveTier, Cues, Provenance
from mnemoseed.storage.drivers.lancedb_embedded import LanceDbEmbeddedStore
from mnemoseed.storage.drivers.pg_graph import PgGraphDriver
from mnemoseed.storage.drivers.pg_meta import PgMetaDriver
from mnemoseed.storage.drivers.pgvector import PgVectorStore
from mnemoseed.storage.drivers.sqlite_graph import SqliteGraphDriver
from mnemoseed.storage.drivers.sqlite_meta import SqliteMetaDriver
from mnemoseed.storage.drivers.synthetic_embedder import SyntheticEmbedder
from mnemoseed.storage.ports import AuditFilter, ChunkFilter, NodeFilter, Page, TurnRange

_DIMENSION = 64
_PROFILE = "alice"
_LOCAL_PG_DSN = "postgresql://mnemoseed:mnemoseed@localhost:55432/mnemoseed"


# ---------------------------------------------------------------- dual-driver stack


@dataclass
class _DreamStack:
    """One fully-wired driver family under integration test.

    graph_main / graph_isolated are the dual-track instances (D6); the pg arm
    gives every store a unique schema so runs are isolated and repeatable.
    """

    backend: str
    vector: Any
    graph_main: Any
    graph_isolated: Any
    meta: Any
    embed: SyntheticEmbedder

    async def close(self) -> None:
        for store in (self.vector, self.graph_main, self.graph_isolated, self.meta):
            closer = getattr(store, "close", None)
            if closer is not None:
                await closer()


def build_embedded(tmp_path: Path) -> _DreamStack:
    return _DreamStack(
        backend="embedded",
        vector=LanceDbEmbeddedStore(uri=tmp_path / "chunks.lance", dimensions=_DIMENSION),
        graph_main=SqliteGraphDriver(path=tmp_path / "cortex.db"),
        graph_isolated=SqliteGraphDriver(path=tmp_path / "isolated.db"),
        meta=SqliteMetaDriver(path=tmp_path / "meta.db"),
        embed=SyntheticEmbedder(dimension=_DIMENSION),
    )


def build_pg(dsn: str) -> _DreamStack:
    tag = uuid.uuid4().hex[:8]
    return _DreamStack(
        backend="pg",
        vector=PgVectorStore(dsn=dsn, dimensions=_DIMENSION, schema=f"itg_v_{tag}"),
        graph_main=PgGraphDriver(dsn=dsn, schema=f"itg_g_{tag}"),
        graph_isolated=PgGraphDriver(dsn=dsn, schema=f"itg_gi_{tag}"),
        meta=PgMetaDriver(dsn=dsn, schema=f"itg_m_{tag}"),
        embed=SyntheticEmbedder(dimension=_DIMENSION),
    )


def _pg_dsn() -> str:
    """The probed live-Postgres DSN; the parametrized arm skips cleanly offline."""
    dsn = os.environ.get("MNEMOSEED_TEST_PG_DSN") or _LOCAL_PG_DSN
    try:
        import psycopg  # lazy: this module must import even where psycopg is absent

        conn = psycopg.connect(dsn, connect_timeout=2)
        conn.close()
    except Exception:
        pytest.skip(f"Postgres unreachable at {dsn}; live-Postgres arm skips cleanly")
    return dsn


@pytest.fixture(params=["embedded", "pg"])
def stack(request: pytest.FixtureRequest, tmp_path: Path) -> _DreamStack:
    """One real driver stack per backend; the pg arm skips offline (NFR-8.2)."""
    if request.param == "pg":
        built = build_pg(_pg_dsn())
    else:
        built = build_embedded(tmp_path)
    yield built
    asyncio.run(built.close())


@pytest.fixture
def emb_stack(tmp_path: Path) -> _DreamStack:
    """Embedded-only stack for the scenarios with no dual-driver requirement."""
    built = build_embedded(tmp_path)
    yield built
    asyncio.run(built.close())


# ---------------------------------------------------------------- seeding + wiring


def _seed_chunk(
    stack: _DreamStack,
    *,
    chunk_id: str,
    text: str,
    turn_start: int,
    turn_end: int,
    session: str,
    profile: str = _PROFILE,
    tier: CognitiveTier = CognitiveTier.TIER_1,
    origin: str = "user",
    ingested_at: float | None = None,
    confidence: float | None = None,
) -> None:
    """Insert one verbatim chunk with a full stamp; ``origin``/``tier`` mirror
    the capture paths: a user turn is TIER_1 asserted by user (core graph), an
    agent render is stamped with a persona (FR-2.12 origin) and can be tier-3.
    ``ingested_at`` / ``confidence`` are deterministic test overrides."""
    asserted_by = "user" if origin == "user" else "anima-model"
    stamp = ChunkStamp(
        chunk_id=chunk_id,
        profile_id=profile,
        text=text,
        cognitive_tier=tier,
        model_id="test-model" if origin == "user" else "anima-1",
        persona_id=None if origin == "user" else "anima-1",
        cues=Cues(entities=[]),
        provenance=Provenance(
            asserted_by=asserted_by,
            session_id=session,
            source="manual",
            confidence=confidence if confidence is not None else 0.5,
        ),
        turn_start=turn_start,
        turn_end=turn_end,
        ingested_at=ingested_at if ingested_at is not None else time.time(),
    )
    vec = stack.embed.embed(text)
    stack.vector.upsert_chunk(stamp, vec.dense, vec.sparse)


def _make_snapshot(stack: _DreamStack, directory: Path, rng: TurnRange) -> Snapshot:
    """Construct the journaled crash state: a fresh SNAPSHOT_DONE file via the
    real snapshotter (registered once, nothing launched)."""
    seeder = FileSnapshotter(store=stack.vector, meta=stack.meta, directory=directory)
    snap = seeder.request(_PROFILE, rng).snapshot
    assert snap is not None
    return snap


def _wire_chain(
    stack: _DreamStack,
    directory: Path,
    *,
    llm: Any | None = None,
    budget: int = DEFAULT_DELTA_BUDGET_TOKENS,
) -> tuple[FileSnapshotter, DreamTrigger, DreamPipeline, ReflectOrchestrator, Merger]:
    """Production-shaped wiring over the real stack: mirrors daemon/app.py's
    dream half (trigger -> snapshotter -> reflect -> merge -> safe-clear)."""
    snapshotter = FileSnapshotter(store=stack.vector, meta=stack.meta, directory=directory)
    trigger = DreamTrigger(snapshotter=snapshotter, auto_trigger=True, purger=snapshotter.purge_snapshot)
    reflector = ReflectOrchestrator(
        llm=llm if llm is not None else StubReflectLLM(),
        directory=directory,
        packer=DeltaPacker(budget_tokens=budget),
        on_done=trigger.on_reflect_complete,
    )
    merger = Merger(
        graph_main=stack.graph_main,
        graph_isolated=stack.graph_isolated,
        meta=stack.meta,
        on_committed=trigger.on_merge_committed,
    )
    pipeline = DreamPipeline(trigger=trigger, snapshotter=snapshotter, reflector=reflector, merger=merger)
    snapshotter.on_ready = pipeline.on_snapshot_ready
    return snapshotter, trigger, pipeline, reflector, merger


def _wire_daemon(
    stack: _DreamStack,
    directory: Path,
    *,
    llm: Any | None = None,
    llm_factory: Any | None = None,
    budget: int = DEFAULT_DELTA_BUDGET_TOKENS,
) -> tuple[FileSnapshotter, DreamTrigger, DreamPipeline, ScorePool, _DreamRelay]:
    """_wire_chain plus the ScorePool -> relay plumbing the daemon wires;
    ``llm_factory`` lets a mid-dream double capture the relay/trigger."""
    snapshotter = FileSnapshotter(store=stack.vector, meta=stack.meta, directory=directory)
    trigger = DreamTrigger(snapshotter=snapshotter, auto_trigger=True, purger=snapshotter.purge_snapshot)
    relay = _DreamRelay(trigger)
    if llm_factory is not None:
        resolved_llm = llm_factory(trigger, stack, relay)
    elif llm is not None:
        resolved_llm = llm
    else:
        resolved_llm = StubReflectLLM()
    reflector = ReflectOrchestrator(
        llm=resolved_llm,
        directory=directory,
        packer=DeltaPacker(budget_tokens=budget),
        on_done=trigger.on_reflect_complete,
    )
    merger = Merger(
        graph_main=stack.graph_main,
        graph_isolated=stack.graph_isolated,
        meta=stack.meta,
        on_committed=trigger.on_merge_committed,
    )
    pipeline = DreamPipeline(trigger=trigger, snapshotter=snapshotter, reflector=reflector, merger=merger)
    snapshotter.on_ready = pipeline.on_snapshot_ready
    pool = ScorePool(clock=time.monotonic, backend=stack.meta, sink=relay.handle)
    return snapshotter, trigger, pipeline, pool, relay


def _fire(pool: ScorePool, relay: _DreamRelay, rng: TurnRange, profile: str = _PROFILE) -> None:
    """Credit enough points to hit the forced cap (idleness-independent) and
    deliver the fired event through the daemon's relay buffer to the trigger."""
    pool.add_points(profile, 60.0, rng)
    relay.flush()


def _boot_recover(
    stack: _DreamStack,
    directory: Path,
    *,
    llm: Any | None = None,
    budget: int = DEFAULT_DELTA_BUDGET_TOKENS,
) -> tuple[FileSnapshotter, DreamTrigger, DreamPipeline]:
    """Replicate the daemon boot-recovery loop from daemon/app.py verbatim:
    recover -> adopt -> resume at the journaled phase boundary -> pipeline.run."""
    snapshotter, trigger, pipeline, _, _ = _wire_chain(stack, directory, llm=llm, budget=budget)
    for snapshot in snapshotter.recover():
        snapshotter.adopt(snapshot)
        boundary = resume_boundary(snapshot)
        if boundary == "reflect":
            trigger.resume(snapshot.profile_id, snapshot.turn_range)
        elif boundary == "merge":
            # reflect already wrote back; resume straight at the merge stage so
            # the merge-commit seam fires the safe-clear once and never re-runs
            # reflect (would duplicate the committed graph writes).
            trigger.resume_merge(snapshot.profile_id, snapshot.turn_range)
        pipeline.run(snapshot)
    return snapshotter, trigger, pipeline


def _main_nodes(stack: _DreamStack, profile: str = _PROFILE) -> list[Any]:
    return stack.graph_main.list_nodes(NodeFilter(profile_id=profile), Page(limit=200)).items


def _isolated_nodes(stack: _DreamStack, profile: str = _PROFILE) -> list[Any]:
    return stack.graph_isolated.list_nodes(NodeFilter(profile_id=profile), Page(limit=200)).items


def _packed_split(
    snapshotter: FileSnapshotter, *, budget: int = DEFAULT_DELTA_BUDGET_TOKENS
) -> tuple[set[str], set[str]]:
    """The allow-list split (packed vs overflow) exactly as the engine's
    DeltaPacker produced it for the dream that just committed. The snapshot's
    chunk set is immutable, so packing the active (now merge-done) snapshot
    deterministically reproduces the reflect pass's split without re-running
    anything."""
    snap = snapshotter.active(_PROFILE)
    assert snap is not None
    req = DeltaPacker(budget_tokens=budget).pack(snap)
    return set(req.packed_chunk_ids), set(req.overflow_chunk_ids)


# ---------------------------------------------------------------- LLM test doubles


class _CountingStub(StubReflectLLM):
    """The deterministic offline seam plus a call counter, so a crash scenario
    can probe the LLM seam: at the merge boundary recovery must NEVER re-run
    reflect, so calls stay at zero after the crash simulation's own pass."""

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def chat(self, *, system: str, user: str) -> str:
        self.calls += 1
        return super().chat(system=system, user=user)


class _FixedPayloadLLM(StubReflectLLM):
    """Scripted ChatLLM double implementing the reflect seam protocol (plain
    text JSON mention array) and returning ONE fixed payload per call. The
    model-independent engine guards (routing, FR-2.12, anti-backflow) are what
    the pollution audit checks, so the double never re-derives anything."""

    def __init__(self, payload: str) -> None:
        super().__init__()
        self._payload = payload
        self.calls = 0

    def chat(self, *, system: str, user: str) -> str:
        del system, user
        self.calls += 1
        return self._payload


class _InterruptingStub(StubReflectLLM):
    """Mid-dream mutation double. On its FIRST chat call (the in-flight dream's
    reflect pass) it (a) notifies the trigger of a new turn while DREAMING and
    (b) writes a brand-new chunk into the vector store. Later calls behave like
    the plain stub, so the drained next dream never re-mutates."""

    def __init__(self, *, trigger: DreamTrigger, stack: _DreamStack, relay: _DreamRelay) -> None:
        del relay  # this double interrupts through the trigger only
        super().__init__()
        self._trigger = trigger
        self._stack = stack
        self._mutated = False
        self.calls = 0

    def chat(self, *, system: str, user: str) -> str:
        self.calls += 1
        if not self._mutated:
            self._mutated = True
            self._trigger.notify_activity(_PROFILE)
            _seed_chunk(
                self._stack,
                chunk_id="c9",
                text="I always stretch after running",
                turn_start=9,
                turn_end=9,
                session="s2",
            )
        return super().chat(system=system, user=user)


class _ForcedEventStub(StubReflectLLM):
    """Mid-dream FORCED-event double. On its FIRST chat call it injects a
    FORCED_CONSOLIDATION into the relay (queued behind the in-flight dream),
    writes the new-range chunk, and interrupts. The queued event drains to a NEW
    range once the in-flight dream finishes (design/02 section 7)."""

    def __init__(self, *, trigger: DreamTrigger, stack: _DreamStack, relay: _DreamRelay) -> None:
        super().__init__()
        self._trigger = trigger
        self._stack = stack
        self._relay = relay
        self._injected = False
        self.calls = 0

    def chat(self, *, system: str, user: str) -> str:
        self.calls += 1
        if not self._injected:
            self._injected = True
            self._relay.handle(
                PoolEvent(
                    kind=PoolEventKind.FORCED_CONSOLIDATION,
                    profile_id=_PROFILE,
                    turn_range=TurnRange(7, 9),
                    balance=55.0,
                    fired_at=2000.0,
                )
            )
            self._relay.flush()
            _seed_chunk(
                self._stack,
                chunk_id="c9",
                text="I always stretch after running",
                turn_start=9,
                turn_end=9,
                session="s2",
            )
            self._trigger.notify_activity(_PROFILE)
        return super().chat(system=system, user=user)


# ---------------------------------------------------------------- C. dual-driver parity
# Scenario A2 (crash after REFLECT_DONE) must run against BOTH storage drivers.


def test_dual_driver_merge_boundary_crash_resumes_merge_never_reruns_reflect(
    stack: _DreamStack, tmp_path: Path
) -> None:
    """NFR-2.3 idempotent recovery at the merge boundary, over both drivers: a
    crash after reflect (REFLECT_DONE + journaled payload) must resume at merge
    ONLY — the restart's LLM seam is never called — then commit, mark exactly
    once, and never re-recover or duplicate graph rows on a second boot."""
    dreams = tmp_path / "dreams"
    _seed_chunk(stack, chunk_id="c1", text="I prefer dark mode", turn_start=0, turn_end=1, session="s1")
    snap = _make_snapshot(stack, dreams, TurnRange(0, 20))

    # the crashed process: its reflect pass journaled REFLECT_DONE + payload, then died
    crash_llm = _CountingStub()
    outcome = ReflectOrchestrator(llm=crash_llm, directory=dreams).reflect(snap)
    assert outcome.ok and outcome.result is not None
    assert crash_llm.calls == 1

    # the simulated restart runs app.py's recovery loop; reflect must NOT re-run
    boot_llm = _CountingStub()
    _, trigger, _ = _boot_recover(stack, dreams, llm=boot_llm)
    assert boot_llm.calls == 0  # merge boundary resumes the write-back, never re-reflects
    assert trigger.status(_PROFILE).state is DreamState.IDLE

    nodes = _main_nodes(stack)
    assert len(nodes) == 1
    assert nodes[0].props["object"] == "dark mode"
    consumed = stack.vector.get_chunk("c1")
    assert consumed is not None  # the consumed row was marked, never deleted
    assert consumed.consolidated is True
    assert consumed.text == "I prefer dark mode"  # verbatim channel never lossy
    assert len(list(dreams.glob("*.json"))) == 1

    # a second boot finds the journal terminated: nothing recovers, graph stays one row
    _boot_recover(stack, dreams, llm=_CountingStub())
    assert len(_main_nodes(stack)) == 1


# Scenario B7 (never-drop partial overflow) must run against BOTH storage drivers.


def test_never_drop_partial_overflow_preserves_overflow_commits_consumed(stack, tmp_path) -> None:
    """FR-2.5 never-drop invariant end-to-end, over both drivers: with the delta
    budget forcing a PARTIAL overflow AND triples produced, the committed merge
    marks exactly the 9 packed rows consolidated and the 13 overflow rows stay
    unmarked in the vector store for a later dream (consumed-ids-scoped
    safe-clear-as-mark)."""
    dreams = tmp_path / "dreams"
    for index in range(22):
        _seed_chunk(
            stack,
            chunk_id=f"c{index}",
            text="I prefer dark mode. " * 100,
            turn_start=index * 2,
            turn_end=index * 2 + 1,
            session="s1",
        )
    snapshotter, trigger, _, pool, relay = _wire_daemon(stack, dreams)
    _fire(pool, relay, TurnRange(0, 60))

    assert trigger.status(_PROFILE).state is DreamState.IDLE
    nodes = _main_nodes(stack)
    assert len(nodes) == 1  # one triple extracted from the packed window -> commit goes through
    assert nodes[0].props["object"] == "dark mode"

    consumed, overflow = _packed_split(snapshotter)
    assert overflow  # the budget DID force a partial overflow
    assert consumed and not (consumed & overflow)
    assert consumed | overflow == {f"c{i}" for i in range(22)}
    remaining = {
        c.chunk_id: c.consolidated for c in stack.vector.snapshot_read(ChunkFilter(profile_id=_PROFILE))
    }
    assert set(remaining) == {f"c{i}" for i in range(22)}  # every row retained (never dropped)
    assert all(remaining[c] for c in consumed)  # every packed row marked consolidated
    assert not any(remaining[c] for c in overflow)  # overflow rows stay unmarked


# ---------------------------------------------------------------- A. interruption injection


def test_interrupt_new_turns_mid_dream_keep_snapshot_scope_and_survive_clear(
    emb_stack: _DreamStack, tmp_path: Path
) -> None:
    """FR-2.4 / NFR-2.1: turns arriving mid-dream (notify_activity during
    DREAMING) interrupt the dream but the snapshot scope stays fixed at the
    ORIGINAL range; the new chunk is NOT in the snapshot and stays unmarked
    after the consumed-ids-scoped clear-as-mark, ready for the next dream."""
    dreams = tmp_path / "dreams"
    _seed_chunk(
        emb_stack, chunk_id="c0", text="I always stretch after waking", turn_start=0, turn_end=1, session="s1"
    )
    _seed_chunk(
        emb_stack, chunk_id="c1", text="I prefer cold showers", turn_start=2, turn_end=3, session="s1"
    )
    snapshotter, trigger, _, pool, relay = _wire_daemon(
        emb_stack,
        dreams,
        llm_factory=lambda trigger, stack, relay: _InterruptingStub(
            trigger=trigger, stack=stack, relay=relay
        ),
    )
    _fire(pool, relay, TurnRange(0, 3))

    assert trigger.status(_PROFILE).state is DreamState.IDLE
    # the ORIGINAL snapshot scope was consumed and marked; the mid-dream chunk
    # survives unmarked (it was never in the snapshot)
    assert emb_stack.vector.get_chunk("c0").consolidated is True
    assert emb_stack.vector.get_chunk("c1").consolidated is True
    mid_dream = emb_stack.vector.get_chunk("c9")
    assert mid_dream is not None
    assert mid_dream.consolidated is False
    snap = snapshotter.active(_PROFILE)
    assert snap is not None
    assert {c.chunk_id for c in snap.chunks} == {"c0", "c1"}  # c9 never entered the snapshot
    assert SnapshotPhase.MERGE_DONE.value in snap.phases
    assert {n.props.get("object") for n in _main_nodes(emb_stack)} == {
        "stretch after waking",
        "cold showers",
    }


def test_interrupt_forced_event_mid_dream_queues_and_drains_to_new_range(
    emb_stack: _DreamStack, tmp_path: Path
) -> None:
    """FR-2.4 / design/02 section 7: a FORCED_CONSOLIDATION arriving while a
    dream is in flight queues (never aborts), the in-flight dream completes over
    its own scope, and the queued event drains to a NEW range with its own
    snapshot + journal termination."""
    dreams = tmp_path / "dreams"
    _seed_chunk(
        emb_stack, chunk_id="c0", text="I always stretch after waking", turn_start=0, turn_end=1, session="s1"
    )
    _seed_chunk(
        emb_stack, chunk_id="c1", text="I prefer cold showers", turn_start=2, turn_end=3, session="s1"
    )
    _, trigger, _, pool, relay = _wire_daemon(
        emb_stack,
        dreams,
        llm_factory=lambda trigger, stack, relay: _ForcedEventStub(trigger=trigger, stack=stack, relay=relay),
    )
    _fire(pool, relay, TurnRange(0, 3))

    assert trigger.status(_PROFILE).state is DreamState.IDLE
    assert trigger.status(_PROFILE).pending_queue == 0

    # two terminated journals: the original (0-3) then the FORCED (7-9) range
    files = sorted(dreams.glob("*.json"))
    assert len(files) == 2
    journals = [load_snapshot_file(path) for path in files]
    assert all(s is not None for s in journals)
    ranges = sorted((s.turn_range.start, s.turn_range.end) for s in journals if s is not None)  # type: ignore[arg-type]
    assert ranges == [(0, 3), (7, 9)]
    assert all(SnapshotPhase.MERGE_DONE.value in s.phases for s in journals if s is not None)  # type: ignore[union-attr]

    # both scopes committed and cleared-as-mark; c9 was consumed by the second
    # dream (marked consolidated), every chunk retained
    assert emb_stack.vector.get_chunk("c0").consolidated is True
    assert emb_stack.vector.get_chunk("c1").consolidated is True
    assert emb_stack.vector.get_chunk("c9").consolidated is True
    assert {n.props.get("object") for n in _main_nodes(emb_stack)} == {
        "stretch after waking",
        "cold showers",
        "stretch after running",
    }


# ---------------------------------------------------------------- B. pollution audit


def test_pollution_tier3_isolated_never_pollutes_main_graph_and_salvage_audited(
    emb_stack: _DreamStack, tmp_path: Path
) -> None:
    """AC-2 tier-3 fixture isolation end-to-end: a mixed session whose scripted
    DreamLLM returns core + isolated/conflict payloads ends with ZERO
    tier-3-sourced nodes in graph.main, every contaminated fixture locked into
    the isolated instance, and the salvage review channel audited for the
    durable entries."""
    dreams = tmp_path / "dreams"
    _seed_chunk(emb_stack, chunk_id="c1", text="I prefer dark mode", turn_start=0, turn_end=1, session="s1")
    _seed_chunk(
        emb_stack,
        chunk_id="c2",
        text="The answer is definitely option B",
        turn_start=2,
        turn_end=3,
        session="s1",
        tier=CognitiveTier.TIER_3,
        origin="agent",
    )
    payload = json.dumps(
        [
            {
                "subject": "user",
                "predicate": "prefers",
                "object": "dark mode",
                "tiers": [1],
                "chunk_ids": ["c1"],
                "confidence": 0.8,
                "route": "core",
                "preference": True,
                "polarity": "positive",
            },
            {
                "subject": "assistant",
                "predicate": "asserts",
                "object": "option B",
                "tiers": [3],
                "chunk_ids": ["c2"],
                "confidence": 0.5,
                "route": "isolated",
                "preference": False,
                "polarity": "positive",
            },
            {
                "subject": "user",
                "predicate": "prefers",
                "object": "option B",
                "tiers": [3],
                "chunk_ids": ["c2"],
                "confidence": 0.6,
                "route": "salvage",
                "preference": False,
                "polarity": "positive",
            },
            {
                # hostile: tier-3 claimed core by the model; the engine re-routes it
                "subject": "user",
                "predicate": "prefers",
                "object": "hostile",
                "tiers": [3],
                "chunk_ids": ["c2"],
                "confidence": 0.7,
                "route": "core",
                "preference": False,
                "polarity": "positive",
            },
        ]
    )
    _, trigger, _, pool, relay = _wire_daemon(
        emb_stack, dreams, llm_factory=lambda trigger, stack, relay: _FixedPayloadLLM(payload)
    )
    _fire(pool, relay, TurnRange(0, 5))

    assert trigger.status(_PROFILE).state is DreamState.IDLE
    main = _main_nodes(emb_stack)
    isolated = _isolated_nodes(emb_stack)

    # main holds exactly the one core triple and ZERO tier-3-sourced nodes
    assert len(main) == 1
    assert main[0].props["object"] == "dark mode"
    assert main[0].cognitive_tier == int(CognitiveTier.TIER_1)

    # every tier-3 fixture is locked into the isolated instance
    assert len(isolated) == 3
    assert {n.props["object"] for n in isolated} == {"option B", "hostile"}

    # the durable salvage entries were queued into the append-only audit
    salvage = emb_stack.meta.audit_query(
        AuditFilter(actor=_PROFILE, action="salvage_queued"), Page(limit=200)
    )
    assert {entry.detail.get("object") for entry in salvage.items} == {"option B", "hostile"}
    assert salvage.total == 2


def test_pollution_never_drop_partial_overflow_preserves_over_commits_consumed_embedded(
    emb_stack: _DreamStack, tmp_path: Path
) -> None:
    """Embedded-arm control for scenario B7 (the dual-driver test above runs the
    same invariants against both stacks); kept separate so the never-drop
    invariant is pinned even when the pg container is offline."""
    dreams = tmp_path / "dreams"
    for index in range(22):
        _seed_chunk(
            emb_stack,
            chunk_id=f"c{index}",
            text="I prefer dark mode. " * 100,
            turn_start=index * 2,
            turn_end=index * 2 + 1,
            session="s1",
        )
    snapshotter, trigger, _, pool, relay = _wire_daemon(emb_stack, dreams)
    _fire(pool, relay, TurnRange(0, 60))

    assert trigger.status(_PROFILE).state is DreamState.IDLE
    assert {n.props.get("object") for n in _main_nodes(emb_stack)} == {"dark mode"}
    consumed, overflow = _packed_split(snapshotter)
    assert overflow and consumed and not (consumed & overflow)
    remaining = {
        c.chunk_id: c.consolidated for c in emb_stack.vector.snapshot_read(ChunkFilter(profile_id=_PROFILE))
    }
    assert set(remaining) == {f"c{i}" for i in range(22)}  # every row retained, never dropped
    assert all(remaining[c] for c in consumed)  # the consumed rows are marked consolidated
    assert not any(remaining[c] for c in overflow)  # the overflow rows stay unmarked
    assert len(list(dreams.glob("*.json"))) == 1


def test_pollution_evidence_boundary_agent_rendered_preference_never_reaches_graph(
    emb_stack: _DreamStack, tmp_path: Path
) -> None:
    """FR-2.12 end-to-end: the scripted DreamLLM returns a preference triple
    whose object appears ONLY in agent-rendered output (persona chunk, never a
    user turn) and a genuine user preference. The engine-side guard drops the
    agent-evidenced triple; it never reaches graph.main or graph.isolated."""
    dreams = tmp_path / "dreams"
    _seed_chunk(
        emb_stack, chunk_id="c1", text="I prefer black coffee", turn_start=0, turn_end=1, session="s1"
    )
    _seed_chunk(
        emb_stack,
        chunk_id="c2",
        text="I prefer violet prose",
        turn_start=2,
        turn_end=3,
        session="s2",
        origin="agent",
    )
    payload = json.dumps(
        [
            {
                "subject": "user",
                "predicate": "prefers",
                "object": "violet prose",
                "tiers": [1],
                "chunk_ids": ["c2"],  # the ONLY evidence is an agent-rendered chunk
                "confidence": 0.8,
                "route": "core",
                "preference": True,
                "polarity": "positive",
            },
            {
                "subject": "user",
                "predicate": "prefers",
                "object": "black coffee",
                "tiers": [1],
                "chunk_ids": ["c1"],
                "confidence": 0.8,
                "route": "core",
                "preference": True,
                "polarity": "positive",
            },
        ]
    )
    _, trigger, _, pool, relay = _wire_daemon(
        emb_stack, dreams, llm_factory=lambda trigger, stack, relay: _FixedPayloadLLM(payload)
    )
    _fire(pool, relay, TurnRange(0, 5))

    assert trigger.status(_PROFILE).state is DreamState.IDLE
    main_objects = {n.props["object"] for n in _main_nodes(emb_stack)}
    isolated_objects = {n.props["object"] for n in _isolated_nodes(emb_stack)}
    assert main_objects == {"black coffee"}
    assert "violet prose" not in main_objects
    assert "violet prose" not in isolated_objects
    # both source rows were consumed by the delta (the model saw them); only the
    # graph was kept clean — the verbatim channel stays neutral, never lossy.
    assert emb_stack.vector.get_chunk("c1").consolidated is True
    assert emb_stack.vector.get_chunk("c2").consolidated is True
    assert emb_stack.vector.get_chunk("c1").text == "I prefer black coffee"


class _SweepStores:
    """Stores-shaped view over a _DreamStack for the D1 decay sweeper."""

    def __init__(self, stack: _DreamStack) -> None:
        self._stack = stack

    @property
    def vector(self):
        return self._stack.vector

    @property
    def graph(self):
        return self._stack.graph_main

    @property
    def meta(self):
        return self._stack.meta


# ---------------------------------------------------------------- defect 1 e2e proof


def test_consolidated_chunks_survive_merge_marked_and_swept_at_triple_rate(
    emb_stack: _DreamStack, tmp_path: Path
) -> None:
    """QA defect 1 e2e proof: ingest -> dream --once (stub) leaves the consumed
    chunks RETAINED and marked consolidated=true (evidence scene, design/03
    section 4), and the D1 sweeper's next pass decays them at 3x the chunk rate
    while an unconsolidated control chunk keeps the base rate."""
    dreams = tmp_path / "dreams"
    days = 10.0
    base_time = 1_800_000_000.0
    old = base_time - days * 86400.0
    _seed_chunk(
        emb_stack,
        chunk_id="c1",
        text="I prefer dark mode",
        turn_start=0,
        turn_end=1,
        session="s1",
        ingested_at=old,
        confidence=1.0,
    )
    snapshotter, trigger, _, pool, relay = _wire_daemon(emb_stack, dreams)
    _fire(pool, relay, TurnRange(0, 20))
    assert trigger.status(_PROFILE).state is DreamState.IDLE
    assert snapshotter.recover() == []  # the journal terminated

    # the consumed source chunk survived the dream: retained + consolidated
    merged = emb_stack.vector.get_chunk("c1")
    assert merged is not None
    assert merged.consolidated is True
    assert merged.text == "I prefer dark mode"  # verbatim channel never lossy

    # a fresh unconsolidated control chunk for the same profile (same baseline)
    _seed_chunk(
        emb_stack,
        chunk_id="c2",
        text="I prefer cold showers",
        turn_start=30,
        turn_end=31,
        session="s2",
        ingested_at=old,
        confidence=1.0,
    )

    config = Config(decay=DecayConfig(sweep_interval_s=1.0, min_apply_delta=0.0))
    clock = [base_time]
    sweeper = DecaySweeper(_SweepStores(emb_stack), config, clock=lambda: clock[0])
    stats = sweeper.run_once()

    assert len(stats) == 1
    assert stats[0].chunks_scanned == 2
    assert stats[0].chunks_updated == 2
    control = emb_stack.vector.get_chunk("c2")
    merged = emb_stack.vector.get_chunk("c1")
    assert control.decay_weight == pytest.approx(math.exp(-0.03 * days), abs=1e-6)
    assert merged.decay_weight == pytest.approx(math.exp(-0.03 * 3.0 * days), abs=1e-6)
    assert merged.consolidated is True  # the marker survived the sweep
