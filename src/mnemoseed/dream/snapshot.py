"""Dream snapshot & idempotent recovery (PRD-02 T2; FR-2.1 snapshot side, NFR-2.3).

A snapshot is a frozen, read-only copy of a profile's funnel chunks whose turn
bounds overlap a trigger's turn range. Capturing it is a pure read: the source
vector store is never mutated, no lock is taken, and the trigger's ingestion
hot path stays unblocked. The snapshot is persisted atomically (tmp + rename)
under the config directory and registered in MetaStore's dream_runs table using
the existing transport.

Recovery (NFR-2.3) is idempotent and crash-safe: at daemon boot the snapshots
that are not merge-complete are reloaded from disk, and the trigger resumes at
exactly one phase boundary. Phases are recorded as opaque markers inside the
snapshot file (SnapshotPhase), so later tasks (T3 reflect, T4 merge) add new
markers without a schema change; unknown markers are preserved and ignored.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from mnemoseed.config import CONFIG_DIR
from mnemoseed.schema.stamp import ChunkStamp
from mnemoseed.storage.ports import (
    ChunkFilter,
    DreamRun,
    MetaStore,
    TurnRange,
    VectorStore,
)

logger = logging.getLogger("mnemoseed.dream.snapshot")


class SnapshotPhase(StrEnum):
    """Phase markers carried inside a snapshot file.

    Unknown markers (added by later tasks or newer engines) are preserved on
    load and never considered by resume_boundary, so forward- and
    backward-compatible recovery requires no schema version.
    """

    SNAPSHOT_DONE = "snapshot_done"
    REFLECT_DONE = "reflect_done"
    MERGE_DONE = "merge_done"


@dataclass(frozen=True)
class SnapshotChunk:
    """One chunk payload inside a snapshot.

    Immutable by construction: the full ChunkStamp is carried as ``stamp_json``
    (lossless ``model_dump_json`` round-trip) plus denormalized fields for
    ordering, session scoping, and overlap filtering without re-parsing.
    """

    chunk_id: str
    profile_id: str
    text: str
    session_id: str | None
    turn_start: int | None
    turn_end: int | None
    stamp_json: str

    @classmethod
    def from_stamp(cls, stamp: ChunkStamp) -> SnapshotChunk:
        return cls(
            chunk_id=stamp.chunk_id,
            profile_id=stamp.profile_id,
            text=stamp.text,
            session_id=stamp.provenance.session_id,
            turn_start=stamp.turn_start,
            turn_end=stamp.turn_end,
            stamp_json=stamp.model_dump_json(),
        )

    def to_stamp(self) -> ChunkStamp:
        return ChunkStamp.model_validate_json(self.stamp_json)


@dataclass(frozen=True)
class Snapshot:
    """Frozen, immutable view of one profile's dream scope (design/02 section 2)."""

    snapshot_id: str
    profile_id: str
    turn_range: TurnRange
    chunks: tuple[SnapshotChunk, ...]
    created_at: float
    phases: frozenset[str]

    def with_phase(self, phase: str) -> Snapshot:
        return Snapshot(
            snapshot_id=self.snapshot_id,
            profile_id=self.profile_id,
            turn_range=self.turn_range,
            chunks=self.chunks,
            created_at=self.created_at,
            phases=frozenset({*self.phases, phase}),
        )


@dataclass(frozen=True)
class SnapshotResult:
    """Typed outcome of a snapshot request (design/02 section 7).

    ``ok`` is always set; a store/persistence failure yields ``snapshot`` as
    None with ``error`` populated, so the trigger degrades without raising into
    the ingestion hot path.
    """

    snapshot: Snapshot | None
    ok: bool
    error: str | None = None


class FileSnapshotter:
    """Production Snapshotter: read-only capture, atomic disk persistence, and
    merge-scoped purge. Registered in the MetaStore using existing transports.

    The constructor stores a live VectorStore/MetaStore reference so later
    phases can write back; recovery/adopt call only read-side paths. The purge
    seam is session-scoped (VectorStore.purge_range), so a profile snapshot
    spanning several sessions clears exactly those sessions, only inside the
    snapshot's turn range, and only after its merge committed.
    """

    def __init__(
        self,
        store: VectorStore,
        meta: MetaStore,
        *,
        directory: Path | None = None,
        clock: Callable[[], float] = time.time,
        on_ready: Callable[[str], None] | None = None,
    ) -> None:
        self._store = store
        self._meta = meta
        self._directory = Path(directory) if directory is not None else CONFIG_DIR / "dreams"
        self._clock = clock
        self.on_ready = on_ready
        self._active: dict[str, Snapshot] = {}

    # ------------------------------------------------------------ capture

    def request(self, profile_id: str, turn_range: TurnRange) -> SnapshotResult:
        """Capture, persist, and register a snapshot over ``turn_range``.

        A failed capture never mutates state: nothing is persisted, nothing is
        registered, and the error surfaces as a typed result.
        """
        try:
            snapshot = self._capture(profile_id, turn_range)
            write_snapshot_file(self._directory, snapshot)
            self._register_run(snapshot)
            self._active[profile_id] = snapshot
            if self.on_ready is not None:
                self.on_ready(profile_id)
            return SnapshotResult(snapshot=snapshot, ok=True)
        except Exception as exc:  # noqa: BLE001 - degrade ingestion, never block it
            return SnapshotResult(snapshot=None, ok=False, error=str(exc))

    def _capture(self, profile_id: str, turn_range: TurnRange) -> Snapshot:
        rows = self._store.snapshot_read(ChunkFilter(profile_id=profile_id))
        chunks = [SnapshotChunk.from_stamp(row) for row in rows]
        selected = [c for c in chunks if _overlaps(c, turn_range)]
        selected.sort(
            key=lambda c: (
                c.turn_start if c.turn_start is not None else -1,
                c.turn_end if c.turn_end is not None else -1,
                c.chunk_id,
            )
        )
        return Snapshot(
            snapshot_id=uuid.uuid4().hex,
            profile_id=profile_id,
            turn_range=turn_range,
            chunks=tuple(selected),
            created_at=self._clock(),
            phases=frozenset({SnapshotPhase.SNAPSHOT_DONE.value}),
        )

    def _register_run(self, snapshot: Snapshot) -> None:
        self._meta.record_dream_run(
            DreamRun(
                run_id=snapshot.snapshot_id,
                turn_range=snapshot.turn_range,
                started_at=snapshot.created_at,
            )
        )

    # ------------------------------------------------------------ recovery

    def recover(self) -> list[Snapshot]:
        """Reload merge-incomplete snapshots from disk, oldest first."""
        recovered = recover_snapshots(self._directory)
        recovered.sort(key=lambda s: (s.created_at or 0.0, s.snapshot_id))
        return recovered

    def adopt(self, snapshot: Snapshot) -> None:
        """Register a recovered snapshot as the profile's active read-only
        scope without re-capturing or re-persisting anything."""
        self._active[snapshot.profile_id] = snapshot

    # ------------------------------------------------------------ safe clear

    def purge_snapshot(self, profile_id: str, turn_range: TurnRange) -> int:
        """Safe-clear: remove exactly the snapshot's range from its sessions.

        Guarded so it only advances after merge-commit: the snapshot must be
        the one adopted in this process for ``profile_id``, with a matching
        range, and must not already be merge-complete. The snapshot is marked
        merge-done on disk *before* the store purge, so a crash mid-purge can
        never re-execute a committed merge (NFR-2.3 idempotency); leftover
        source rows are stale but never re-written. Returns the number of rows
        purged across sessions.
        """
        snap = self._active.get(profile_id)
        if snap is None or snap.turn_range != turn_range:
            return 0
        if SnapshotPhase.MERGE_DONE.value in snap.phases:
            return 0
        merged = snap.with_phase(SnapshotPhase.MERGE_DONE.value)
        write_snapshot_file(self._directory, merged)
        self._active[profile_id] = merged
        sessions = sorted({c.session_id for c in snap.chunks if c.session_id is not None})
        removed = 0
        for session in sessions:
            removed += self._store.purge_range(session, turn_range.start, turn_range.end)
        return removed


# ---------------------------------------------------------------- module-level helpers


def _overlaps(chunk: SnapshotChunk, turn_range: TurnRange) -> bool:
    if chunk.turn_start is None or chunk.turn_end is None:
        return False
    return chunk.turn_start <= turn_range.end and chunk.turn_end >= turn_range.start


def resume_boundary(snapshot: Snapshot) -> str | None:
    """The phase boundary at which an interrupted dream resumes.

    None when the dream fully completed (merge committed); otherwise the phase
    that produced the snapshot still needs to run: ``"reflect"`` for a fresh
    snapshot, ``"merge"`` once reflect wrote back. Unknown markers never
    influence the boundary.
    """
    if SnapshotPhase.MERGE_DONE.value in snapshot.phases:
        return None
    if SnapshotPhase.REFLECT_DONE.value in snapshot.phases:
        return "merge"
    return "reflect"


def write_snapshot_file(directory: Path, snapshot: Snapshot) -> None:
    """Atomic write: tmp file + os.replace under the config directory."""
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{snapshot.snapshot_id}.json"
    tmp = directory / f"{snapshot.snapshot_id}.json.tmp"
    tmp.write_text(_snapshot_to_json(snapshot), encoding="utf-8")
    os.replace(tmp, target)


def load_snapshot_file(path: Path) -> Snapshot | None:
    """Load one snapshot file; None on corrupt/unrecognized content.

    Never raises: JSON that fails to parse, or that parses but carries wrong
    types (bad created_at/turn bounds), is skipped and logged so one damaged
    file cannot take a daemon boot down (design/02 section 7 degradation).
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("skipping corrupt snapshot file %s", path)
        return None
    try:
        snapshot = _snapshot_from_dict(data)
    except (TypeError, ValueError, KeyError):
        logger.warning("skipping corrupt snapshot file %s", path)
        return None
    if snapshot is None:
        logger.warning("skipping unrecognized snapshot file %s", path)
    return snapshot


def recover_snapshots(directory: Path) -> list[Snapshot]:
    """Collect merge-incomplete snapshots from a directory, ignoring temp
    residue and corrupt files. Unknown phase markers are preserved."""
    recovered = [s for s in (load_snapshot_file(p) for p in directory.glob("*.json")) if s is not None]
    return [s for s in recovered if resume_boundary(s) is not None]


# ---------------------------------------------------------------- serialization


def _snapshot_to_json(snapshot: Snapshot) -> str:
    payload = {
        "snapshot_id": snapshot.snapshot_id,
        "profile_id": snapshot.profile_id,
        "turn_range": {"start": snapshot.turn_range.start, "end": snapshot.turn_range.end},
        "chunks": [
            {
                "chunk_id": c.chunk_id,
                "profile_id": c.profile_id,
                "text": c.text,
                "session_id": c.session_id,
                "turn_start": c.turn_start,
                "turn_end": c.turn_end,
                "stamp_json": c.stamp_json,
            }
            for c in snapshot.chunks
        ],
        "created_at": snapshot.created_at,
        "phases": sorted(snapshot.phases),
    }
    return json.dumps(payload, indent=2, sort_keys=True)


def _snapshot_from_dict(data: Any) -> Snapshot | None:
    if not isinstance(data, dict):
        return None
    turn_range = data.get("turn_range")
    if not isinstance(turn_range, dict) or "start" not in turn_range or "end" not in turn_range:
        return None
    chunks: list[SnapshotChunk] = []
    for raw in data.get("chunks") or ():
        chunk = _chunk_from_dict(raw)
        if chunk is None:
            return None
        chunks.append(chunk)
    return Snapshot(
        snapshot_id=str(data.get("snapshot_id", "")),
        profile_id=str(data.get("profile_id", "")),
        turn_range=TurnRange(int(turn_range["start"]), int(turn_range["end"])),
        chunks=tuple(chunks),
        created_at=float(data.get("created_at", 0.0)),
        phases=frozenset(str(p) for p in data.get("phases") or ()),
    )


def _chunk_from_dict(raw: Any) -> SnapshotChunk | None:
    if not isinstance(raw, dict):
        return None
    turn_start = raw.get("turn_start")
    turn_end = raw.get("turn_end")
    session_id = raw.get("session_id")
    return SnapshotChunk(
        chunk_id=str(raw.get("chunk_id", "")),
        profile_id=str(raw.get("profile_id", "")),
        text=str(raw.get("text", "")),
        session_id=str(session_id) if session_id is not None else None,
        turn_start=int(turn_start) if turn_start is not None else None,
        turn_end=int(turn_end) if turn_end is not None else None,
        stamp_json=str(raw.get("stamp_json", "")),
    )
