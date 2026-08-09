"""Dream engine: pool-event trigger, snapshot, reflect, split-write (PRD-02).

T1 ships the trigger only: the per-profile lifecycle state machine that
consumes ScorePool events and requests read-only snapshots through the
Snapshotter seam. Snapshot (T2), reflection (T3) and the split writer (T4)
land in later tasks.
"""

from __future__ import annotations

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
    "NullSnapshotter",
    "Snapshotter",
    "TriggerStatus",
]
