"""CapturePipeline seam — where the F1-F3 capture funnel plugs in.

F1 (Stripper) is wired as StrippingPipeline, the embedded default below; F2
(persistence classifier) and F3 (scoring) are later tasks consuming the same
consumer contract. Submit must never block the ingest hot path: the default is
an O(1) append, and F1 strips as a synchronous drain on read, never inside the
HTTP handler.
"""

from __future__ import annotations

from typing import Protocol

from mnemoseed.capture.rulesets_v1 import RULESET_V1
from mnemoseed.capture.stripper import (
    RuleSet,
    StrippedTurn,
    Stripper,
    StripStats,
)
from mnemoseed.schema.turn import Turn
from mnemoseed.storage.ports import TurnRange


class CapturePipeline(Protocol):
    """Consumer contract for structured turns and session settlements."""

    def submit_turn(self, turn: Turn) -> None: ...

    def end_session(self, session_id: str, turn_range: TurnRange) -> None: ...


class InMemoryCapturePipeline:
    """In-process CapturePipeline (embedded default): buffers per session.

    Ordering per session is submission order. ``settled`` records the closed
    turn range so later stages know the session boundary.
    """

    def __init__(self) -> None:
        self._turns: dict[str, list[Turn]] = {}
        self._settled: dict[str, TurnRange] = {}

    def submit_turn(self, turn: Turn) -> None:
        self._turns.setdefault(turn.session_id, []).append(turn)

    def end_session(self, session_id: str, turn_range: TurnRange) -> None:
        self._settled[session_id] = turn_range

    def turns(self, session_id: str) -> list[Turn]:
        return list(self._turns.get(session_id, []))

    def settled(self, session_id: str) -> TurnRange | None:
        return self._settled.get(session_id)

    def sessions(self) -> tuple[str, ...]:
        return tuple(self._turns)


class StrippingPipeline:
    """F1 seam: O(1) submit on the HTTP path; the stripper drains on read.

    ``submit_turn`` is a plain append to the delegate buffer, so the /ingest
    handler never runs the stripper. The consumer side (``turns`` / ``drain``)
    strips each buffered turn exactly once with the ruleset current at that
    moment — a hot reload therefore takes effect on the next turn, and already
    drained turns are never reprocessed. The delegate keeps the raw provenance
    copy; ``stats`` expose cumulative compression telemetry for the benchmark.
    """

    def __init__(
        self,
        delegate: InMemoryCapturePipeline | None = None,
        stripper: Stripper | None = None,
    ) -> None:
        self._delegate = delegate if delegate is not None else InMemoryCapturePipeline()
        self._stripper = stripper if stripper is not None else Stripper(RULESET_V1)
        self._stripped: dict[str, list[Turn]] = {}
        self._bytes_in = 0
        self._bytes_out = 0
        self._rules_hit: dict[str, int] = {}

    def submit_turn(self, turn: Turn) -> None:
        self._delegate.submit_turn(turn)

    def end_session(self, session_id: str, turn_range: TurnRange) -> None:
        self._delegate.end_session(session_id, turn_range)

    def reload_rules(self, ruleset: RuleSet) -> None:
        """Swap the ruleset; governs turns drained after this call."""
        self._stripper.reload_rules(ruleset)

    def drain(self, session_id: str) -> list[StrippedTurn]:
        """Strip not-yet-processed turns of one session; returns per-turn results."""
        raw = self._delegate.turns(session_id)
        done = len(self._stripped.get(session_id, []))
        results: list[StrippedTurn] = []
        for turn in raw[done:]:
            result = self._stripper.strip_turn(turn)
            self._bytes_in += result.stats.bytes_in
            self._bytes_out += result.stats.bytes_out
            for rule_id, count in result.stats.rules_hit.items():
                self._rules_hit[rule_id] = self._rules_hit.get(rule_id, 0) + count
            results.append(result)
            self._stripped.setdefault(session_id, []).append(result.turn)
        return results

    def turns(self, session_id: str) -> list[Turn]:
        """Drain pending turns lazily and return the stripped versions."""
        self.drain(session_id)
        return list(self._stripped.get(session_id, []))

    def settled(self, session_id: str) -> TurnRange | None:
        return self._delegate.settled(session_id)

    def sessions(self) -> tuple[str, ...]:
        return self._delegate.sessions()

    @property
    def stats(self) -> StripStats:
        """Cumulative stripping telemetry across every drained turn."""
        return StripStats(
            bytes_in=self._bytes_in,
            bytes_out=self._bytes_out,
            rules_hit=dict(self._rules_hit),
        )
