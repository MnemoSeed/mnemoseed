"""Dream snapshot & idempotent recovery (PRD-02 T2, FR-2.1 snapshot side, NFR-2.3).

The real Snapshotter captures a frozen read-only copy of a profile's funnel
chunks over a turn range, persists it to disk (atomic write), and registers it
in the meta store's dream_runs table. Boot recovery loads merge-incomplete
snapshots from disk and resumes the trigger at a phase boundary so a crashed
dream continues, never re-executing completed phases and never double-writing.

Every test asserts behavior through the public surface: FileSnapshotter
(request / purge_snapshot / recover / adopt), Snapshot, and the DreamTrigger
seams. The store and meta fakes are minimal in-memory doublets of the two ports.
"""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from mnemoseed.capture.pool import PoolEvent, PoolEventKind
from mnemoseed.dream import (
    DreamState,
    DreamTrigger,
    FileSnapshotter,
    NullSnapshotter,
    SnapshotPhase,
    load_snapshot_file,
    recover_snapshots,
    resume_boundary,
    write_snapshot_file,
)
from mnemoseed.schema.stamp import ChunkStamp, CognitiveTier, Cues, Provenance
from mnemoseed.storage.ports import (
    Capability,
    ChunkFilter,
    DreamRun,
    StorageError,
    TurnRange,
)

_RANGE = TurnRange(0, 3)


# ---------------------------------------------------------------- fakes


class _FakeStore:
    """VectorStore-shaped in-memory double: snapshot_read + purge_range."""

    def __init__(self, chunks: list[ChunkStamp] | None = None) -> None:
        self.chunks: list[ChunkStamp] = list(chunks or [])
        self.purged: list[tuple[str, int, int]] = []

    def capabilities(self) -> frozenset[Capability]:
        return frozenset({Capability.VECTOR_SNAPSHOT})

    def snapshot_read(self, filter: ChunkFilter) -> list[ChunkStamp]:
        return [c for c in self.chunks if c.profile_id == filter.profile_id]

    def purge_range(self, session_id: str, turn_start: int, turn_end: int) -> int:
        self.purged.append((session_id, turn_start, turn_end))
        before = len(self.chunks)
        disjoint: list[ChunkStamp] = []
        for chunk in self.chunks:
            inside = (
                chunk.provenance.session_id == session_id
                and chunk.turn_start is not None
                and chunk.turn_end is not None
                and chunk.turn_start <= turn_end
                and chunk.turn_end >= turn_start
            )
            if not inside:
                disjoint.append(chunk)
        self.chunks = disjoint
        return before - len(disjoint)


class _ReadFailsStore(_FakeStore):
    """Store double whose snapshot read raises (failure-degradation test)."""

    def snapshot_read(self, filter: ChunkFilter) -> list[ChunkStamp]:
        del filter
        raise StorageError("store exploded")


class _OrderProbeStore(_FakeStore):
    """Purge doublet that snapshots the on-disk journal state at the instant
    each purge_range call happens, so tests can assert the merge marker was
    persisted BEFORE any store mutation (marker-before-purge ordering)."""

    def __init__(self, chunks: list[ChunkStamp] | None = None, *, journal: Path) -> None:
        super().__init__(chunks)
        self._journal = journal
        self.file_states: list[frozenset[str]] = []

    def purge_range(self, session_id: str, turn_start: int, turn_end: int) -> int:
        files = list(self._journal.glob("*.json"))
        if files:
            snapshot = load_snapshot_file(files[0])
            self.file_states.append(snapshot.phases if snapshot is not None else frozenset())
        return super().purge_range(session_id, turn_start, turn_end)


class _CrashPurgeStore(_FakeStore):
    """Purge doublet that dies mid-clear (simulated) so tests can check the
    crash window between journal commit and store purge leaves an idempotent
    journal: the committed merge is never re-executed and survivors are never
    re-written."""

    def __init__(self, chunks: list[ChunkStamp] | None = None) -> None:
        super().__init__(chunks)
        self.purge_calls = 0

    def purge_range(self, session_id: str, turn_start: int, turn_end: int) -> int:
        self.purge_calls += 1
        raise StorageError("crash simulated inside store purge")


class _FakeMeta:
    """MetaStore-shaped double recording the dream_runs registration."""

    def __init__(self) -> None:
        self.runs: list[DreamRun] = []

    def record_dream_run(self, run: DreamRun) -> str:
        self.runs.append(run)
        return run.run_id


# ---------------------------------------------------------------- helpers


def _stamp(
    chunk_id: str,
    text: str,
    *,
    profile_id: str = "alice",
    session: str = "s1",
    turn_start: int | None = None,
    turn_end: int | None = None,
) -> ChunkStamp:
    return ChunkStamp(
        chunk_id=chunk_id,
        profile_id=profile_id,
        text=text,
        cognitive_tier=CognitiveTier.TIER_1,
        model_id="test-model",
        cues=Cues(entities=["test"]),
        provenance=Provenance(asserted_by="test-model", session_id=session, source="manual"),
        turn_start=turn_start,
        turn_end=turn_end,
    )


def _event(profile: str = "alice", rng: TurnRange = _RANGE) -> PoolEvent:
    return PoolEvent(
        kind=PoolEventKind.DREAM_TRIGGER,
        profile_id=profile,
        turn_range=rng,
        balance=12.0,
        fired_at=1.0,
    )


def _snapshotter(
    store: _FakeStore,
    meta: _FakeMeta | None = None,
    directory: Path | None = None,
) -> FileSnapshotter:
    return FileSnapshotter(
        store=store,
        meta=meta or _FakeMeta(),
        directory=directory,
        clock=lambda: 1000.0,
    )


# ---------------------------------------------------------------- capture semantics


def test_snapshot_captures_exactly_overlapping_chunks(tmp_path: Path) -> None:
    store = _FakeStore(
        [
            _stamp("a", "in a", turn_start=1, turn_end=3),
            _stamp("b", "in b", turn_start=3, turn_end=5),  # straddles [2, 4]
            _stamp("c", "out c", turn_start=6, turn_end=8),  # fully outside
            _stamp("d", "no bounds", session="s9"),  # no turn bounds: excluded
        ]
    )
    fs = _snapshotter(store, directory=tmp_path)
    result = fs.request("alice", TurnRange(2, 4))

    assert result.ok
    assert result.snapshot is not None
    assert [c.chunk_id for c in result.snapshot.chunks] == ["a", "b"]
    # pure read: source store untouched, no purge, exactly one on-disk snapshot
    assert {c.chunk_id for c in store.chunks} == {"a", "b", "c", "d"}
    assert store.purged == []
    assert len(list(tmp_path.glob("*.json"))) == 1


def test_empty_range_is_empty_snapshot_not_error(tmp_path: Path) -> None:
    store = _FakeStore([_stamp("a", "in a", turn_start=10, turn_end=12)])
    fs = _snapshotter(store, directory=tmp_path)
    result = fs.request("alice", TurnRange(50, 60))
    assert result.ok
    assert result.snapshot is not None
    assert result.snapshot.chunks == ()


def test_snapshot_is_frozen_and_source_untouched(tmp_path: Path) -> None:
    store = _FakeStore([_stamp("a", "verbatim text", turn_start=1, turn_end=1)])
    fs = _snapshotter(store, directory=tmp_path)
    snap = fs.request("alice", TurnRange(0, 5)).snapshot
    assert snap is not None
    with pytest.raises(FrozenInstanceError):
        snap.chunks = snap.chunks[:0]  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        snap.chunks[0].text = "mutated"  # type: ignore[misc]
    assert len(store.chunks) == 1
    assert store.chunks[0].text == "verbatim text"


# ---------------------------------------------------------------- persistence + recovery


def test_round_trip_persist_recover_identical(tmp_path: Path) -> None:
    store = _FakeStore(
        [
            _stamp("a", "hello world", turn_start=0, turn_end=2, session="s1"),
            _stamp("b", "second chunk", turn_start=2, turn_end=4, session="s2"),
        ]
    )
    meta = _FakeMeta()
    captured = _snapshotter(store, meta, directory=tmp_path).request("alice", TurnRange(0, 4)).snapshot
    assert captured is not None

    restored = _snapshotter(store, meta, directory=tmp_path).recover()
    assert len(restored) == 1
    assert restored[0] == captured
    assert [c.text for c in restored[0].chunks] == ["hello world", "second chunk"]


def test_recovery_resumes_snapshot_ready_for_reflect(tmp_path: Path) -> None:
    """Crash simulation: a persed snapshot mid-dream reboots into DREAMING."""
    store = _FakeStore([_stamp("a", "text a", turn_start=0, turn_end=3)])
    fs1 = _snapshotter(store, directory=tmp_path)
    fs1.request("alice", TurnRange(0, 3))

    trigger = DreamTrigger(snapshotter=NullSnapshotter())
    fs2 = _snapshotter(store, directory=tmp_path)
    recovered = fs2.recover()
    assert len(recovered) == 1
    fs2.adopt(recovered[0])
    assert trigger.resume(recovered[0].profile_id, recovered[0].turn_range) is True
    assert trigger.status("alice").state is DreamState.DREAMING
    assert trigger.status("alice").current_range == TurnRange(0, 3)
    assert store.purged == []  # recovery never purges


def test_merge_completed_snapshots_are_not_recovered(tmp_path: Path) -> None:
    store = _FakeStore([_stamp("a", "text a", turn_start=0, turn_end=3, session="s1")])
    fs = _snapshotter(store, directory=tmp_path)
    fs.request("alice", TurnRange(0, 3))
    assert len(fs.recover()) == 1

    # the merge commits and safe-clear marks the snapshot merge-done
    fs.purge_snapshot("alice", TurnRange(0, 3))
    assert fs.recover() == []


def test_recovery_is_idempotent_no_double_execution(tmp_path: Path) -> None:
    store = _FakeStore([_stamp("a", "text a", turn_start=0, turn_end=2)])
    fs1 = _snapshotter(store, directory=tmp_path)
    fs1.request("alice", TurnRange(0, 2))

    fs2 = _snapshotter(store, directory=tmp_path)
    first = fs2.recover()
    assert fs2.recover() == first  # reading again changes nothing, no new files
    assert len(list(tmp_path.glob("*.json"))) == 1

    trigger = DreamTrigger(snapshotter=NullSnapshotter())
    fs2.adopt(first[0])
    assert trigger.resume(first[0].profile_id, first[0].turn_range) is True
    assert trigger.resume(first[0].profile_id, first[0].turn_range) is False
    assert trigger.status("alice").state is DreamState.DREAMING


# ---------------------------------------------------------------- safe clear seam


def test_safe_clear_only_runs_after_merge_commit(tmp_path: Path) -> None:
    store = _FakeStore(
        [
            _stamp("a", "in a", turn_start=0, turn_end=2, session="s1"),
            _stamp("b", "in b", turn_start=1, turn_end=3, session="s2"),
            _stamp("c", "out c", turn_start=9, turn_end=9, session="s2"),
        ]
    )
    meta = _FakeMeta()
    fs = _snapshotter(store, meta, directory=tmp_path)
    trigger = DreamTrigger(snapshotter=fs, auto_trigger=True, purger=fs.purge_snapshot)
    fs.on_ready = trigger.on_snapshot_ready

    trigger.handle_event(_event(profile="alice", rng=TurnRange(0, 4)))
    assert trigger.status("alice").state is DreamState.DREAMING
    assert store.purged == []  # not before the merge commits

    fs2 = _snapshotter(store, meta, directory=tmp_path)
    recovered = fs2.recover()
    assert recovered
    fs2.adopt(recovered[0])
    assert store.purged == []  # recovery of an unmerged snapshot never purges

    trigger.on_reflect_complete("alice")
    trigger.on_merge_committed("alice")
    # purge ran exactly once per snapshot session, scoped to the snapshot range
    assert sorted(store.purged) == sorted([("s1", 0, 4), ("s2", 0, 4)])
    # nothing outside the snapshot's range was removed
    assert {c.chunk_id for c in store.chunks} == {"c"}

    # a re-delivered merge commit purges nothing more (idempotent)
    assert fs.purge_snapshot("alice", TurnRange(0, 4)) == 0
    assert len(store.purged) == 2


def test_purge_guarded_against_wrong_scope(tmp_path: Path) -> None:
    store = _FakeStore([_stamp("a", "text a", turn_start=0, turn_end=2, session="s1")])
    fs = _snapshotter(store, directory=tmp_path)
    fs.request("alice", TurnRange(0, 2))

    # a purge for a different range is refused: scope is exact
    assert fs.purge_snapshot("alice", TurnRange(3, 5)) == 0
    assert store.purged == []
    assert {c.chunk_id for c in store.chunks} == {"a"}


# ---------------------------------------------------------------- phase markers


def test_unknown_phase_marker_recovery_is_graceful(tmp_path: Path) -> None:
    """T3/T4 add markers without touching recovery: unknown markers must not
    crash a load and must not change the resume boundary."""
    store = _FakeStore([_stamp("a", "text a", turn_start=0, turn_end=2)])
    fs = _snapshotter(store, directory=tmp_path)
    snap = fs.request("alice", TurnRange(0, 2)).snapshot
    assert snap is not None
    future = snap.with_phase("future_unknown_marker")
    write_snapshot_file(tmp_path, future)  # a newer engine overwrote the file

    recovered = _snapshotter(store, directory=tmp_path).recover()
    assert len(recovered) == 1
    assert "future_unknown_marker" in recovered[0].phases
    # the unknown marker neither crashes recovery nor shifts the boundary:
    # a fresh snapshot is still restorable at the reflect barrier
    assert resume_boundary(recovered[0]) == "reflect"


def test_resume_boundary_marks_each_phase(tmp_path: Path) -> None:
    store = _FakeStore([_stamp("a", "text a", turn_start=0, turn_end=2)])
    fs = _snapshotter(store, directory=tmp_path)
    snap = fs.request("alice", TurnRange(0, 2)).snapshot
    assert snap is not None
    assert resume_boundary(snap) == "reflect"
    reflected = snap.with_phase(SnapshotPhase.REFLECT_DONE.value)
    assert resume_boundary(reflected) == "merge"
    merged = reflected.with_phase(SnapshotPhase.MERGE_DONE.value)
    assert resume_boundary(merged) is None


# ---------------------------------------------------------------- multi profile


def test_multi_profile_snapshots_do_not_mix(tmp_path: Path) -> None:
    store = _FakeStore(
        [
            _stamp("a1", "alice text", profile_id="alice", turn_start=0, turn_end=2, session="sa"),
            _stamp("b1", "bob text", profile_id="bob", turn_start=0, turn_end=2, session="sb"),
        ]
    )
    meta = _FakeMeta()
    fs = _snapshotter(store, meta, directory=tmp_path)
    ra = fs.request("alice", TurnRange(0, 2)).snapshot
    rb = fs.request("bob", TurnRange(0, 2)).snapshot
    assert ra is not None and [c.chunk_id for c in ra.chunks] == ["a1"]
    assert rb is not None and [c.chunk_id for c in rb.chunks] == ["b1"]

    # purging alice's committed merge leaves bob's sources untouched
    assert fs.purge_snapshot("alice", TurnRange(0, 2)) == 1
    assert {c.chunk_id for c in store.chunks} == {"b1"}

    # only bob's unmerged snapshot survives recovery
    recovered = _snapshotter(store, meta, directory=tmp_path).recover()
    assert {s.profile_id for s in recovered} == {"bob"}


# ---------------------------------------------------------------- failure degradation


def test_snapshot_read_failure_surfaces_typed_result(tmp_path: Path) -> None:
    store = _ReadFailsStore([])
    meta = _FakeMeta()
    fs = _snapshotter(store, meta, directory=tmp_path)
    trigger = DreamTrigger(snapshotter=fs, auto_trigger=True)
    fs.on_ready = trigger.on_snapshot_ready

    trigger.handle_event(_event(profile="alice", rng=TurnRange(0, 3)))
    # the dream is dropped, the profile stays accumulating: ingestion is unblocked
    assert trigger.status("alice").state is DreamState.ACCUMULATING
    assert trigger.status("alice").current_range is None
    # nothing persisted, nothing registered, no stray file
    assert meta.runs == []
    assert list(tmp_path.glob("*.json")) == []


def test_empty_store_capture_is_valid(tmp_path: Path) -> None:
    fs = _snapshotter(_FakeStore([]), directory=tmp_path)
    result = fs.request("alice", TurnRange(0, 8))
    assert result.ok
    assert result.snapshot is not None and result.snapshot.chunks == ()
    assert len(recover_snapshots(tmp_path)) == 1


# ---------------------------------------------------------------- defect 1: corrupt files


def test_corrupt_snapshot_files_are_skipped_not_crashing(tmp_path: Path) -> None:
    """A single malformed snapshot file (valid JSON, wrong types, or broken
    structure) must never crash recovery or daemon boot: it is skipped, the
    good snapshots still recover, and nothing raises."""
    store = _FakeStore([_stamp("a", "good text", turn_start=0, turn_end=2)])
    fs = _snapshotter(store, directory=tmp_path)
    good = fs.request("alice", TurnRange(0, 2)).snapshot
    assert good is not None
    malformed = [
        {
            "snapshot_id": "bad-created",
            "profile_id": "a",
            "turn_range": {"start": 0, "end": 2},
            "chunks": [],
            "created_at": None,
            "phases": ["snapshot_done"],
        },
        {
            "snapshot_id": "bad-range",
            "profile_id": "a",
            "turn_range": {"start": "abc", "end": 2},
            "chunks": [],
            "created_at": 1.0,
            "phases": [],
        },
        {
            "snapshot_id": "bad-chunk",
            "profile_id": "a",
            "turn_range": {"start": 0, "end": 2},
            "chunks": [
                {
                    "chunk_id": "x",
                    "profile_id": "a",
                    "text": "t",
                    "session_id": "s1",
                    "turn_start": "xyz",
                    "turn_end": 2,
                    "stamp_json": "{}",
                }
            ],
            "created_at": 1.0,
            "phases": [],
        },
        {"snapshot_id": "bad-structure", "not_a_snapshot": True},
    ]
    for variant in malformed:
        (tmp_path / f"{variant['snapshot_id']}.json").write_text(json.dumps(variant), encoding="utf-8")
    (tmp_path / "broken-syntax.json").write_text('{"snapshot_id": ', encoding="utf-8")

    restored = fs.recover()
    assert [s.snapshot_id for s in restored] == [good.snapshot_id]
    # concrete proof each malformed variant is individually tolerated
    for variant in malformed:
        assert load_snapshot_file(tmp_path / f"{variant['snapshot_id']}.json") is None
    assert load_snapshot_file(tmp_path / "broken-syntax.json") is None


# ---------------------------------------------------------------- defect 2: merge-boundary resume


def test_reflect_done_snapshot_resumes_at_merge_not_reflect(tmp_path: Path) -> None:
    """A snapshot that already ran reflect resumes at the MERGE boundary: the
    trigger positions in MERGING (never DREAMING, which would re-run reflect
    and duplicate graph writes), and the merge-commit seam fires the safe-clear
    exactly once with the snapshot's scope, after which recovery terminates."""
    store = _FakeStore([_stamp("a", "text a", turn_start=0, turn_end=2, session="s1")])
    fs = _snapshotter(store, directory=tmp_path)
    snap = fs.request("alice", TurnRange(0, 2)).snapshot
    assert snap is not None
    # T3 wrote back and marked reflect done; the daemon crashed before merge.
    # Overwrite the same snapshot_id: the journal now carries the marker.
    write_snapshot_file(tmp_path, snap.with_phase(SnapshotPhase.REFLECT_DONE.value))

    fs3 = _snapshotter(store, directory=tmp_path)
    pending = fs3.recover()
    assert len(pending) == 1
    fs3.adopt(pending[0])
    trigger = DreamTrigger(snapshotter=NullSnapshotter(), purger=fs3.purge_snapshot)

    assert trigger.resume_merge(pending[0].profile_id, pending[0].turn_range) is True
    assert trigger.status("alice").state is DreamState.MERGING
    assert trigger.status("alice").current_range == TurnRange(0, 2)

    # reflect is never re-run: its completion seam is a no-op from MERGING
    trigger.on_reflect_complete("alice")
    assert trigger.status("alice").state is DreamState.MERGING

    # the (future T4) merge completion fires the safe-clear exactly once
    trigger.on_merge_committed("alice")
    assert sorted(store.purged) == [("s1", 0, 2)]
    assert trigger.status("alice").state is DreamState.IDLE
    assert trigger.status("alice").current_range is None
    # terminal: the journal now marks the dream complete, recovery ends
    assert fs3.recover() == []


def test_resume_merge_is_idempotent(tmp_path: Path) -> None:
    store = _FakeStore([_stamp("a", "text a", turn_start=0, turn_end=2)])
    fs = _snapshotter(store, directory=tmp_path)
    snap = fs.request("alice", TurnRange(0, 2)).snapshot
    assert snap is not None
    write_snapshot_file(tmp_path, snap.with_phase(SnapshotPhase.REFLECT_DONE.value))
    pending = fs.recover()
    assert len(pending) == 1
    trigger = DreamTrigger(snapshotter=NullSnapshotter())
    fs.adopt(pending[0])
    assert trigger.resume_merge("alice", TurnRange(0, 2)) is True
    assert trigger.resume_merge("alice", TurnRange(0, 2)) is False
    assert trigger.status("alice").state is DreamState.MERGING


# ---------------------------------------------------------------- defect 3: marker-before-purge


def test_purge_persists_merge_marker_before_store_clear(tmp_path: Path) -> None:
    """Ordering lock: safer-clear must commit the MERGE_DONE journal BEFORE the
    first purge_range hit. A regression that purged first would leave the
    journal unmarked at purge time and this test would go red."""
    store = _OrderProbeStore(
        [_stamp("a", "text a", turn_start=0, turn_end=2, session="s1")],
        journal=tmp_path,
    )
    fs = _snapshotter(store, directory=tmp_path)
    fs.request("alice", TurnRange(0, 2))
    assert fs.purge_snapshot("alice", TurnRange(0, 2)) == 1
    assert store.file_states
    assert all(SnapshotPhase.MERGE_DONE.value in states for states in store.file_states)


def test_crash_between_marker_and_purge_does_not_reexecute(tmp_path: Path) -> None:
    """A crash (simulated store failure) between the journal commit and the
    store clear leaves an idempotent journal: recovery does NOT re-execute the
    committed merge, and leftover rows are never double-purged."""
    store = _CrashPurgeStore([_stamp("a", "text a", turn_start=0, turn_end=2, session="s1")])
    fs = _snapshotter(store, directory=tmp_path)
    fs.request("alice", TurnRange(0, 2))

    with pytest.raises(StorageError):
        fs.purge_snapshot("alice", TurnRange(0, 2))
    # the journal was committed before the store clear began
    on_disk = load_snapshot_file(next(tmp_path.glob("*.json")))
    assert on_disk is not None
    assert SnapshotPhase.MERGE_DONE.value in on_disk.phases
    # the committed merge is never re-executed by recovery
    assert fs.recover() == []
    # a later retry is a no-op: no double clear, survivors untouched
    assert fs.purge_snapshot("alice", TurnRange(0, 2)) == 0
    assert store.purge_calls == 1
    assert {c.chunk_id for c in store.chunks} == {"a"}
