"""F1 Local Stripper — ordered rule engine for mechanical noise (FR-1.2).

The engine walks an ordered, data-driven ruleset over Turn content. Every rule
names a target content kind (tool output / message text / both), an action
(strip a whole line, redact a span, collapse a repeated block) and either a
regex pattern or a per-unit predicate. The ruleset itself is plain data
(capture/rulesets_v1.py), so the daemon can hot-swap it via reload_rules
without a restart; a swap governs the next stripped turn.

Red line (design/01 stage 1): F1 never touches prose. Rules match only
mechanical shapes, a pure-prose turn exits byte-identical, and strip_turn
returns copies — the input Turn is never mutated, so the raw provenance copy
stays available.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from mnemoseed.schema.turn import Turn, TurnRole, TurnStep


class ContentTarget(StrEnum):
    """Which content kind a rule may touch."""

    TOOL_OUTPUT = "tool_output"  # TOOL step content
    MESSAGE_TEXT = "message_text"  # USER and ASSISTANT step content
    BOTH = "both"


class StripAction(StrEnum):
    """What a rule does to matching content."""

    STRIP_LINE = "strip_line"  # drop whole matching lines
    REDACT_SPAN = "redact_span"  # remove matching spans, keep surroundings
    COLLAPSE_RUNS = "collapse_runs"  # dedupe repeated blocks


class StripperError(Exception):
    """A ruleset that cannot be applied (bad regex, missing fields)."""


@dataclass(frozen=True)
class Rule:
    """One data-driven stripping rule.

    ``pattern`` is matched against each line unit (content plus its original
    terminator) for STRIP_LINE and against the whole text for REDACT_SPAN.
    ``predicate`` is an alternative to ``pattern`` for STRIP_LINE.
    ``min_run`` only applies to COLLAPSE_RUNS.
    """

    id: str
    target: ContentTarget
    action: StripAction
    pattern: str = ""
    predicate: Callable[[str], bool] | None = None
    min_run: int = 2


@dataclass(frozen=True)
class RuleSet:
    """An ordered collection of rules; the unit a hot reload swaps in."""

    rules: tuple[Rule, ...] = ()


@dataclass(frozen=True)
class StripStats:
    """Per-turn or cumulative stripping telemetry for the benchmark harness."""

    bytes_in: int = 0
    bytes_out: int = 0
    rules_hit: dict[str, int] = field(default_factory=dict)

    @property
    def compression_ratio(self) -> float:
        """Fraction of input bytes saved (0.0 .. 1.0); 0 for empty input."""
        if self.bytes_in <= 0:
            return 0.0
        return (self.bytes_in - self.bytes_out) / self.bytes_in


@dataclass(frozen=True)
class StrippedTurn:
    """A stripped Turn plus the statistics that describe the strip."""

    turn: Turn
    stats: StripStats


@dataclass(frozen=True)
class _CompiledRule:
    rule: Rule
    pattern: re.Pattern[str] | None


class Stripper:
    """Walks an ordered ruleset over Turn / text content."""

    def __init__(self, ruleset: RuleSet) -> None:
        self._compiled = self._compile(ruleset)
        self._ruleset = ruleset

    @property
    def ruleset(self) -> RuleSet:
        return self._ruleset

    def reload_rules(self, ruleset: RuleSet) -> None:
        """Swap the active ruleset; a bad ruleset is rejected and the old one
        stays in force (the compiled set is only replaced on success)."""
        compiled = self._compile(ruleset)
        self._ruleset = ruleset
        self._compiled = compiled

    def strip_turn(self, turn: Turn) -> StrippedTurn:
        """Strip every text-bearing step of a Turn; the input is never mutated."""
        new_steps: list[TurnStep] = []
        bytes_in = 0
        bytes_out = 0
        hits: dict[str, int] = {}
        for step in turn.steps:
            target = ContentTarget.TOOL_OUTPUT if step.role is TurnRole.TOOL else ContentTarget.MESSAGE_TEXT
            text, step_hits = self.strip_text(step.content, target)
            bytes_in += len(step.content.encode("utf-8"))
            bytes_out += len(text.encode("utf-8"))
            for rule_id, count in step_hits.items():
                hits[rule_id] = hits.get(rule_id, 0) + count
            new_steps.append(step.model_copy(update={"content": text}))
        stripped = turn.model_copy(update={"steps": new_steps})
        return StrippedTurn(
            turn=stripped,
            stats=StripStats(bytes_in=bytes_in, bytes_out=bytes_out, rules_hit=hits),
        )

    def strip_text(self, text: str, target: ContentTarget) -> tuple[str, dict[str, int]]:
        """Apply the ruleset to one content blob; returns (text, rule hits)."""
        hits: dict[str, int] = {}
        for compiled in self._compiled:
            rule = compiled.rule
            if rule.target is not ContentTarget.BOTH and rule.target is not target:
                continue
            if rule.action is StripAction.REDACT_SPAN:
                pattern = compiled.pattern
                assert pattern is not None
                text, count = pattern.subn("", text)
            elif rule.action is StripAction.STRIP_LINE:
                text, count = _strip_lines(text, rule, compiled.pattern)
            else:  # COLLAPSE_RUNS
                text, count = _collapse_runs(text, rule.min_run)
            if count:
                hits[rule.id] = hits.get(rule.id, 0) + count
        return text, hits

    @staticmethod
    def _compile(ruleset: RuleSet) -> tuple[_CompiledRule, ...]:
        seen: set[str] = set()
        compiled: list[_CompiledRule] = []
        for rule in ruleset.rules:
            if rule.id in seen:
                raise StripperError(f"duplicate rule id {rule.id!r}")
            seen.add(rule.id)
            if rule.action is StripAction.REDACT_SPAN:
                if not rule.pattern:
                    raise StripperError(f"rule {rule.id!r}: redact-span requires a pattern")
            elif rule.action is StripAction.STRIP_LINE:
                if not rule.pattern and rule.predicate is None:
                    raise StripperError(f"rule {rule.id!r}: strip-line requires a pattern or predicate")
            elif rule.action is StripAction.COLLAPSE_RUNS:
                if rule.pattern or rule.predicate is not None:
                    raise StripperError(f"rule {rule.id!r}: collapse-runs takes no pattern or predicate")
                if rule.min_run < 2:
                    raise StripperError(f"rule {rule.id!r}: collapse-runs min_run must be >= 2")
            else:  # pragma: no cover - exhaustive StrEnum
                raise StripperError(f"rule {rule.id!r}: unknown action {rule.action!r}")
            pattern = None
            if rule.pattern:
                try:
                    pattern = re.compile(rule.pattern)
                except re.error as exc:
                    raise StripperError(f"rule {rule.id!r}: invalid pattern: {exc}") from exc
            compiled.append(_CompiledRule(rule=rule, pattern=pattern))
        return tuple(compiled)


# ---------------------------------------------------------------- line machinery


def _split_terminated(text: str) -> list[tuple[str, str]]:
    """Split into (content, terminator) units, preserving every terminator byte.
    Terminators are one of \r\n / \r / \n; the final unit carries ''."""
    units: list[tuple[str, str]] = []
    start = 0
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char in ("\r", "\n"):
            term = "\r\n" if char == "\r" and index + 1 < length and text[index + 1] == "\n" else char
            units.append((text[start:index], term))
            index += len(term)
            start = index
        else:
            index += 1
    if start == length and not units and text == "":
        units.append(("", ""))
    elif start < length:
        units.append((text[start:length], ""))
    return units


def _join_terminated(units: list[tuple[str, str]]) -> str:
    return "".join(content + term for content, term in units)


def _strip_lines(text: str, rule: Rule, pattern: re.Pattern[str] | None) -> tuple[str, int]:
    """Drop every unit the rule matches; counts the removed units."""
    units = _split_terminated(text)
    kept: list[tuple[str, str]] = []
    removed = 0
    for content, term in units:
        unit = content + term
        if rule.predicate is not None:
            matched = rule.predicate(unit)
        elif pattern is not None:
            matched = pattern.match(unit) is not None
        else:  # pragma: no cover - _compile rejects this shape
            matched = False
        if matched:
            removed += 1
        else:
            kept.append((content, term))
    if removed == 0:
        return text, 0
    return _join_terminated(kept), removed


def _collapse_runs(text: str, min_run: int) -> tuple[str, int]:
    """Collapse repeated blocks: adjacent duplicate units, then a full-coverage
    periodic repetition of the whole unit sequence, to a single occurrence."""
    units = _split_terminated(text)
    if len(units) < min_run:
        return text, 0
    collapsed, removed = _collapse_adjacent(units, min_run)
    collapsed, removed_blocks = _collapse_periodic(collapsed, min_run)
    total = removed + removed_blocks
    if total == 0:
        return text, 0
    return _join_terminated(collapsed), total


def _collapse_adjacent(units: list[tuple[str, str]], min_run: int) -> tuple[list[tuple[str, str]], int]:
    kept: list[tuple[str, str]] = []
    removed = 0
    index = 0
    length = len(units)
    while index < length:
        end = index + 1
        while end < length and units[end] == units[index]:
            end += 1
        run = end - index
        if run >= min_run:
            kept.append(units[index])
            removed += run - 1
        else:
            kept.extend(units[index:end])
        index = end
    return kept, removed


def _collapse_periodic(units: list[tuple[str, str]], min_run: int) -> tuple[list[tuple[str, str]], int]:
    length = len(units)
    if length < min_run * 2:
        return units, 0
    for block in range(1, length):
        if length % block:
            continue
        repeats = length // block
        if repeats < min_run:
            break  # repeats fall as block grows; nothing left to try
        if units[:block] * repeats == units:
            return units[:block], length - block
    return units, 0
