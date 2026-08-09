"""Dream trigger state machine (PRD-02 T1; FR-2.1 / FR-2.4 trigger side).

The trigger consumes ScorePool events (FR-2.1) and drives exactly the design/02
section 2 lifecycle, one per profile:

    IDLE -> ACCUMULATING -> TRIGGERED -> SNAPSHOTTING -> DREAMING -> MERGING -> IDLE
    DREAMING | MERGING -> INTERRUPTED -> (ACCUMULATING | MERGING)

The pool has already evaluated the threshold and the idle window; the trigger
consumes the event and never re-evaluates a threshold. A FORCED_CONSOLIDATION
event triggers regardless of the current state: while a dream is in flight it
queues rather than aborting, and applies to a NEW range once the in-flight
dream finishes (design/02 section 7 overflow rule).

Invariants, enforced by construction:

- Every public method is O(1) state bookkeeping. The snapshot is the trigger's
  only outbound seam call; reflect / merge completion are inbound callbacks. No
  heavy work ever runs inline.
- One dream per profile at a time: an event that arrives while a dream is in
  flight (SNAPSHOTTING/DREAMING/MERGING/INTERRUPTED, or a background dream that
  survived an interrupt) joins the overflow queue and is drained one-per-dream.
- The pool event's turn_range is carried through to the snapshot request
  unchanged, and is re-used as the current_range observability field.

Manual-first discipline (FR-2.8): with ``auto_trigger=False`` (the default)
every pool event is recorded as ``pending_manual`` and drives nothing; the
console's ``dream --once`` (later task) calls ``dream_once`` to run exactly one
cycle. ``notify_activity`` is the interruption seam, wired to a new turn for a
profile; the /ingest hook-up is a later task.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from mnemoseed.capture.pool import PoolEvent
from mnemoseed.storage.ports import TurnRange


class DreamState(StrEnum):
    """One profile's dream-lifecycle state (design/02 section 2)."""

    IDLE = "idle"
    ACCUMULATING = "accumulating"
    TRIGGERED = "triggered"
    SNAPSHOTTING = "snapshotting"
    DREAMING = "dreaming"
    MERGING = "merging"
    INTERRUPTED = "interrupted"


class Snapshotter(Protocol):
    """Read-only snapshot seam (T2 supplies the real implementation).

    The trigger records the request and advances to SNAPSHOTTING; the real
    snapshot reads the hippocampus over the range read-only and reports
    completion through ``DreamTrigger.on_snapshot_ready``.
    """

    def request(self, profile_id: str, turn_range: TurnRange) -> None: ...


class NullSnapshotter:
    """Void seam bound at daemon boot until the real snapshot (T2) exists.

    Records nothing: the trigger still advances through SNAPSHOTTING and the
    completion seam stays free for T2 to attach.
    """

    def request(self, profile_id: str, turn_range: TurnRange) -> None:
        del profile_id, turn_range


@dataclass(frozen=True)
class TriggerStatus:
    """Observability snapshot of one profile's trigger (console reads this)."""

    profile_id: str
    state: DreamState
    pending_queue: int  # events queued while a dream ran (forced / overflows)
    pending_manual: int  # events held while auto_trigger=False (FR-2.8)
    last_event: PoolEvent | None
    current_range: TurnRange | None


@dataclass
class _Profile:
    """Per-profile trigger state (D5 isolation: never shared across profiles)."""

    state: DreamState = DreamState.IDLE
    queued: deque[PoolEvent] = field(default_factory=deque)
    pending_manual: deque[PoolEvent] = field(default_factory=deque)
    last_event: PoolEvent | None = None
    current_range: TurnRange | None = None
    dream_in_flight: bool = False  # a background (post-interrupt) dream still runs


class DreamTrigger:
    """Consumes pool events and drives one dream lifecycle per profile."""

    def __init__(
        self,
        snapshotter: Snapshotter,
        *,
        auto_trigger: bool = False,
    ) -> None:
        self._snapshotter = snapshotter
        self._auto_trigger = auto_trigger
        self._profiles: dict[str, _Profile] = {}

    # ------------------------------------------------------------ pool intake

    def handle_event(self, event: PoolEvent) -> None:
        """Consume one pool event (bound directly as the ScorePool sink)."""
        rec = self._profiles.setdefault(event.profile_id, _Profile())
        rec.last_event = event
        if not self._auto_trigger:
            rec.pending_manual.append(event)
            return
        self._deliver(event)

    def __call__(self, event: PoolEvent) -> None:
        self.handle_event(event)

    def _deliver(self, event: PoolEvent) -> None:
        """Auto path: launch a dream, or queue it behind an in-flight one."""
        rec = self._profiles.setdefault(event.profile_id, _Profile())
        if rec.dream_in_flight or rec.state in (
            DreamState.TRIGGERED,
            DreamState.SNAPSHOTTING,
            DreamState.DREAMING,
            DreamState.MERGING,
            DreamState.INTERRUPTED,
        ):
            rec.queued.append(event)
            return
        self._launch(event.profile_id, rec, event)

    # ------------------------------------------------------------ interruption

    def notify_activity(self, profile_id: str) -> None:
        """A new turn arrived for ``profile_id``.

        IDLE starts accumulating; a dream or merge in progress is interrupted
        (the background extends, snapshot scope stays fixed); an interrupted
        profile resumes accumulating on further turns, 0-latency.
        """
        rec = self._profiles.setdefault(profile_id, _Profile())
        if rec.state is DreamState.IDLE:
            rec.state = DreamState.ACCUMULATING
        elif rec.state in (DreamState.DREAMING, DreamState.MERGING):
            rec.state = DreamState.INTERRUPTED
            rec.dream_in_flight = True
        elif rec.state is DreamState.INTERRUPTED:
            rec.state = DreamState.ACCUMULATING

    # ------------------------------------------------------------ manual (FR-2.8)

    def dream_once(self, profile_id: str) -> bool:
        """Run exactly one manual cycle; True if a dream was launched.

        Consumes the oldest pending-manual event (falling back to an overflow
        queued event) and never overlaps a dream already in flight.
        """
        rec = self._profiles.setdefault(profile_id, _Profile())
        if rec.dream_in_flight or rec.state not in (DreamState.IDLE, DreamState.ACCUMULATING):
            return False
        if rec.pending_manual:
            event = rec.pending_manual.popleft()
        elif rec.queued:
            event = rec.queued.popleft()
        else:
            return False
        self._launch(profile_id, rec, event)
        return True

    # ------------------------------------------------------------ seam callbacks

    def on_snapshot_ready(self, profile_id: str) -> None:
        """Snapshot seam completion: the read-only copy is ready."""
        rec = self._profiles.get(profile_id)
        if rec is None or rec.state is not DreamState.SNAPSHOTTING:
            return
        rec.state = DreamState.DREAMING

    def on_reflect_complete(self, profile_id: str) -> None:
        """Reflect seam completion: write-back of snapshot-scoped deltas starts.

        Also carries INTERRUPTED (and a background dream resumed under
        ACCUMULATING) into MERGING, the design/02 "write-back complete, only
        covering the snapshot range" edge.
        """
        rec = self._profiles.get(profile_id)
        if rec is None:
            return
        if rec.state in (DreamState.DREAMING, DreamState.INTERRUPTED):
            rec.state = DreamState.MERGING
        elif rec.state is DreamState.ACCUMULATING and rec.dream_in_flight:
            rec.state = DreamState.MERGING

    def on_merge_committed(self, profile_id: str) -> None:
        """Merge seam completion: write-back committed + safe-clear of the
        snapshot range. The dream ends; one queued overflow event drains next."""
        rec = self._profiles.get(profile_id)
        if rec is None:
            return
        if rec.state in (DreamState.MERGING, DreamState.INTERRUPTED):
            self._finish(profile_id, rec)
        elif rec.state is DreamState.ACCUMULATING and rec.dream_in_flight:
            self._finish(profile_id, rec)

    # ------------------------------------------------------------ internals

    def _launch(self, profile_id: str, rec: _Profile, event: PoolEvent) -> None:
        """Eligible -- trigger the dream: request the snapshot over the event's
        range and enter SNAPSHOTTING (TRIGGERED is the transient instant of
        eligibility before the snapshot request)."""
        rec.dream_in_flight = True
        rec.state = DreamState.TRIGGERED
        rec.current_range = event.turn_range
        self._snapshotter.request(profile_id, event.turn_range)
        rec.state = DreamState.SNAPSHOTTING

    def _finish(self, profile_id: str, rec: _Profile) -> None:
        rec.state = DreamState.IDLE
        rec.dream_in_flight = False
        rec.current_range = None
        # one dream per profile: a queued overflow launches only after the
        # in-flight dream fully finishes, never alongside it
        if rec.queued:
            next_event = rec.queued.popleft()
            self._launch(profile_id, rec, next_event)

    # ------------------------------------------------------------ observability

    def status(self, profile_id: str) -> TriggerStatus:
        """Snapshot state, pending queue depths, and the last pool event."""
        rec = self._profiles.get(profile_id, _Profile())
        return TriggerStatus(
            profile_id=profile_id,
            state=rec.state,
            pending_queue=len(rec.queued),
            pending_manual=len(rec.pending_manual),
            last_event=rec.last_event,
            current_range=rec.current_range,
        )
