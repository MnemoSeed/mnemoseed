"""Watermark score-pool state machine (FR-1.5 / AC-3).

The pool accumulates per-profile S points; pool >= 10.0 plus idle >= 5s emits
a dream-trigger event, and pool >= 50.0 forces a micro-consolidation event
regardless of idleness. Idle is computed from an injected clock only — no
wall-clock sleeps anywhere.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from mnemoseed.capture.pool import PoolEvent, PoolEventKind, PoolStats, ScorePool
from mnemoseed.storage.ports import PoolState, TurnRange


class _Clock:
    """Deterministic injected clock."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _sink() -> tuple[list[PoolEvent], Callable[[PoolEvent], None]]:
    events: list[PoolEvent] = []

    def sink(event: PoolEvent) -> None:
        events.append(event)

    return events, sink


class _FakeBackend:
    """MetaStore-shaped persistence stub satisfying the PoolBackend seam."""

    def __init__(self) -> None:
        self.credits: list[tuple[float, TurnRange]] = []

    def pool_add(self, points: float, turn_range: TurnRange) -> None:
        self.credits.append((points, turn_range))

    def pool_state(self) -> PoolState:
        return PoolState(balance=sum(points for points, _ in self.credits))


def test_no_trigger_below_threshold() -> None:
    clock = _Clock()
    events, sink = _sink()
    pool = ScorePool(clock=clock, sink=sink)
    pool.add_points("p", 4.0, TurnRange(0, 0))
    pool.add_points("p", 4.0, TurnRange(1, 1))
    assert events == []


def test_no_trigger_below_threshold_even_after_idle() -> None:
    # Pins the dream threshold: balance < 10.0 must NEVER trigger, even once
    # the 5s idle window has fully elapsed (kills a threshold=mutant).
    clock = _Clock()
    events, sink = _sink()
    pool = ScorePool(clock=clock, sink=sink)
    pool.add_points("p", 4.0, TurnRange(0, 0))
    pool.add_points("p", 4.0, TurnRange(1, 1))  # balance 8.0, below threshold
    clock.advance(6.0)
    assert pool.evaluate() == ()
    assert events == []
    assert pool.stats("p").balance == pytest.approx(8.0)


def test_no_trigger_while_busy_even_above_threshold() -> None:
    clock = _Clock()
    events, sink = _sink()
    pool = ScorePool(clock=clock, sink=sink)
    for index in range(4):
        clock.advance(1.0)  # active conversation, never idle 5s
        pool.add_points("p", 4.0, TurnRange(index, index))
    assert events == []  # balance >= 10 but idle < 5s


def test_trigger_after_idle_window_with_correct_turn_range() -> None:
    clock = _Clock()
    events, sink = _sink()
    pool = ScorePool(clock=clock, sink=sink)
    for index in range(3):
        pool.add_points("p", 4.0, TurnRange(index, index))
    clock.advance(5.0)
    pool.add_points("p", 1.0, TurnRange(3, 3))
    event = events[0]
    assert event.kind is PoolEventKind.DREAM_TRIGGER
    assert event.turn_range == TurnRange(0, 3)  # AC-3: window spans pooled turns
    assert event.balance == pytest.approx(13.0)
    assert event.fired_at == pytest.approx(clock.t)
    # the pool drained after the event
    assert pool.stats("p") is not None
    assert pool.stats("p").balance == pytest.approx(0.0)


def test_evaluate_fires_quietly_accumulated_balance() -> None:
    clock = _Clock()
    events, sink = _sink()
    pool = ScorePool(clock=clock, sink=sink)
    pool.add_points("p", 4.0, TurnRange(0, 0))
    pool.add_points("p", 4.0, TurnRange(1, 1))
    pool.add_points("p", 4.0, TurnRange(2, 2))
    clock.advance(6.0)
    fired = pool.evaluate()
    assert len(fired) == 1
    assert fired[0].kind is PoolEventKind.DREAM_TRIGGER
    assert fired[0].turn_range == TurnRange(0, 2)


def test_forced_micro_consolidation_at_cap_ignores_idle() -> None:
    clock = _Clock()
    events, sink = _sink()
    pool = ScorePool(clock=clock, sink=sink)
    for index in range(6):
        clock.advance(1.0)  # busy the whole time
        fired = pool.add_points("p", 9.0, TurnRange(index, index))
        if index < 5:
            assert fired == ()
        else:
            assert len(fired) == 1
            assert fired[0].kind is PoolEventKind.FORCED_CONSOLIDATION
            assert fired[0].balance == pytest.approx(54.0)
    assert len(events) == 1
    assert events[0].kind is PoolEventKind.FORCED_CONSOLIDATION


def test_dream_trigger_takes_precedence_below_cap_only() -> None:
    clock = _Clock()
    events, sink = _sink()
    pool = ScorePool(clock=clock, sink=sink)
    # reach 49 busy -> nothing; then idle + big jump straight past 50
    for index in range(5):
        pool.add_points("p", 9.0, TurnRange(index, index))
    clock.advance(6.0)
    fired = pool.add_points("p", 9.0, TurnRange(5, 5))
    assert len(fired) == 1
    assert fired[0].kind is PoolEventKind.FORCED_CONSOLIDATION


def test_per_profile_isolation() -> None:
    clock = _Clock()
    events, sink = _sink()
    pool = ScorePool(clock=clock, sink=sink)
    pool.add_points("a", 4.0, TurnRange(0, 0))
    pool.add_points("a", 4.0, TurnRange(1, 1))
    pool.add_points("b", 4.0, TurnRange(0, 0))
    clock.advance(6.0)
    fired = pool.add_points("a", 4.0, TurnRange(2, 2))
    assert len(fired) == 1
    assert fired[0].profile_id == "a"
    assert len(events) == 1
    # profile b never triggered and keeps its own balance
    stats_b = pool.stats("b")
    assert stats_b is not None
    assert stats_b.balance == pytest.approx(4.0)
    assert stats_b.dream_triggers == 0
    assert pool.stats("a").balance == pytest.approx(0.0)


def test_stats_observability() -> None:
    clock = _Clock()
    pool = ScorePool(clock=clock)
    pool.add_points("p", 4.0, TurnRange(0, 0))
    pool.add_points("p", 4.0, TurnRange(1, 1))
    stats = pool.stats("p")
    assert isinstance(stats, PoolStats)
    assert stats.profile_id == "p"
    assert stats.balance == pytest.approx(8.0)
    assert stats.points_added == pytest.approx(8.0)
    assert stats.turns_pooled == 2
    assert stats.dream_triggers == 0
    assert pool.stats("ghost") is None


def test_meta_backend_persists_events() -> None:
    clock = _Clock()
    backend = _FakeBackend()
    pool = ScorePool(clock=clock, backend=backend)
    for index in range(3):
        pool.add_points("p", 4.0, TurnRange(index, index))
    clock.advance(5.0)
    pool.add_points("p", 1.0, TurnRange(3, 3))
    assert backend.credits == [(13.0, TurnRange(0, 3))]
    assert backend.pool_state().balance == pytest.approx(13.0)


def test_balances_snapshot() -> None:
    clock = _Clock()
    pool = ScorePool(clock=clock)
    pool.add_points("a", 3.0, TurnRange(0, 0))
    pool.add_points("b", 5.0, TurnRange(0, 0))
    assert pool.balances() == {"a": 3.0, "b": 5.0}
