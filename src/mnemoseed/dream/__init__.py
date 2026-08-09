"""Dream engine: pool-event trigger, snapshot, reflect, split-write (PRD-02).

T1 ships the trigger: the per-profile lifecycle state machine that consumes
ScorePool events and requests read-only snapshots through the Snapshotter
seam. T2 ships the real snapshotter: frozen capture, atomic disk persistence,
MetaStore registration, and crash-safe idempotent recovery at the phase
boundary. Reflection (T3) and the split writer (T4) land in later tasks.
"""

from __future__ import annotations

from mnemoseed.dream.snapshot import (
    FileSnapshotter,
    Snapshot,
    SnapshotChunk,
    SnapshotPhase,
    SnapshotResult,
    load_snapshot_file,
    recover_snapshots,
    resume_boundary,
    write_snapshot_file,
)
from mnemoseed.dream.trigger import (
    DreamState,
    DreamTrigger,
    NullSnapshotter,
    Snapshotter,
    TriggerStatus,
)

__all__ = [
    "DreamState",
    "DreamTrigger",
    "FileSnapshotter",
    "NullSnapshotter",
    "Snapshot",
    "SnapshotChunk",
    "SnapshotPhase",
    "SnapshotResult",
    "Snapshotter",
    "TriggerStatus",
    "load_snapshot_file",
    "recover_snapshots",
    "resume_boundary",
    "write_snapshot_file",
]
