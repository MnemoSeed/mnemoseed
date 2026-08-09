"""Dream engine: pool-event trigger, snapshot, reflect, split-write (PRD-02).

T1 ships the trigger: the per-profile lifecycle state machine that consumes
ScorePool events and requests read-only snapshots through the Snapshotter
seam. T2 ships the real snapshotter: frozen capture, atomic disk persistence,
MetaStore registration, and crash-safe idempotent recovery at the phase
boundary. T3 ships the reflection orchestrator: the de-biasing prompt template
and the deterministic offline ReflectLLM seam. The split writer (T4) lands in
a later task.
"""

from __future__ import annotations

from mnemoseed.dream.prompts import PROMPT_VERSION, ChunkBlock, ReflectPrompt, build_reflect_prompt
from mnemoseed.dream.reflect import (
    STRIP_TOKENS,
    ReflectedTriple,
    ReflectionResult,
    ReflectLLM,
    ReflectOrchestrator,
    ReflectOutcome,
    Route,
    StubReflectLLM,
)
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
    "PROMPT_VERSION",
    "ChunkBlock",
    "DreamState",
    "DreamTrigger",
    "FileSnapshotter",
    "NullSnapshotter",
    "ReflectLLM",
    "ReflectOrchestrator",
    "ReflectOutcome",
    "ReflectPrompt",
    "ReflectedTriple",
    "ReflectionResult",
    "Route",
    "STRIP_TOKENS",
    "Snapshot",
    "SnapshotChunk",
    "SnapshotPhase",
    "SnapshotResult",
    "Snapshotter",
    "StubReflectLLM",
    "TriggerStatus",
    "build_reflect_prompt",
    "load_snapshot_file",
    "recover_snapshots",
    "resume_boundary",
    "write_snapshot_file",
]
