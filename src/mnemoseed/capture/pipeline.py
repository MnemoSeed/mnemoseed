"""CapturePipeline seam — where the F1-F3 capture funnel plugs in.

F1 (Stripper), F2 (persistence classifier) and F3 (scoring) are later tasks;
they consume the structured Turns the daemon segments. The protocol below is
their consumer contract; the in-memory buffer is the embedded default. Submit
must never block the ingest hot path, so the defaults are O(1) appends that
later stages drain on their own worker.
"""

from __future__ import annotations

from typing import Protocol

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
