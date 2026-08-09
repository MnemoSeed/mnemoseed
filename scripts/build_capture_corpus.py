"""Build the local capture benchmark corpus from Claude Code session logs.

The corpus never enters the repo: this script reads real session transcripts
from a configurable source directory (default ``~/.claude/projects``), samples
turns across sessions, normalizes them into the funnel's Turn shape (through
the same TurnSegmenter the daemon uses), and writes a deterministic JSONL
corpus to a gitignored ``--out`` path (default ``.bench/capture_corpus.jsonl``).

Determinism: given the same seed and source, the output is byte-identical.
Files are visited in sorted path order, each transcript is consumed in append
order, and only the final sampling step uses the seeded RNG.

Handled edge cases:

- missing source dir -> clean error and exit code 2;
- very large sessions -> only the first ``--max-session-bytes`` bytes of each
  transcript are read (truncated at a line boundary), so a giant transcript
  cannot dominate the sample; the truncation tail is deliberately dropped;
- non-text / malformed content -> non-text block types and unparseable lines
  are skipped (counted and summarized).
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from mnemoseed.capture.benchmark import write_corpus
from mnemoseed.capture.pipeline import InMemoryCapturePipeline
from mnemoseed.capture.segment import CaptureError, TurnSegmenter
from mnemoseed.schema.turn import (
    HostId,
    IngestEvent,
    IngestEventType,
    MessageContent,
    ToolContent,
    Turn,
)

DEFAULT_SOURCE = Path("~/.claude/projects").expanduser()
DEFAULT_OUT = Path(".bench/capture_corpus.jsonl")
DEFAULT_MAX_SESSION_BYTES = 4 * 1024 * 1024
DEFAULT_TURNS = 500
DEFAULT_SEED = 0


class CorpusError(Exception):
    """A build-input problem the user must fix (bad source dir, ...)."""


@dataclass
class ParseStats:
    """Per-transcript parsing telemetry."""

    lines_total: int = 0
    lines_malformed: int = 0
    events: int = 0
    skipped_non_text: int = 0


@dataclass
class BuildStats:
    """Summary telemetry for the build run."""

    files_read: int = 0
    sessions: set[str] = field(default_factory=set)
    lines_total: int = 0
    lines_malformed: int = 0
    events_ingested: int = 0
    turns_built: int = 0
    bytes_total: int = 0


def _timestamp(obj: dict) -> float:
    raw = obj.get("timestamp", "")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


def _model(obj: dict) -> str | None:
    model = obj.get("message", {}).get("model")
    return model if isinstance(model, str) else None


def _blocks(obj: dict) -> list[dict]:
    content = obj.get("message", {}).get("content")
    if isinstance(content, list):
        return [block for block in content if isinstance(block, dict)]
    return []


def _tool_result_text(block: dict) -> str:
    content = block.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(parts)
    return ""


def _pop_tool_use(pending: list[tuple[str, str, dict]], tool_use_id: str) -> tuple[str, dict]:
    for index, (tid, name, input_) in enumerate(pending):
        if tid == tool_use_id:
            pending.pop(index)
            return name, input_
    name = tool_use_id.rsplit("_", 1)[0] if "_" in tool_use_id else "tool"
    return name or "tool", {}


def _read_head(path: Path, max_bytes: int) -> str:
    """Read the first ``max_bytes`` bytes of a transcript, cut at a newline."""
    with path.open("rb") as handle:
        data = handle.read(max_bytes)
    text = data.decode("utf-8", errors="replace")
    boundary = text.rfind("\n")
    if boundary > 0:
        text = text[:boundary]
    return text


def events_from_transcript(
    text: str,
    session_id: str,
    profile_id: str,
    stats: ParseStats,
) -> list[IngestEvent]:
    """Convert a Claude Code transcript into a daemon-shaped ingest stream."""
    events: list[IngestEvent] = []
    pending_tool_uses: list[tuple[str, str, dict]] = []
    for line in text.splitlines():
        strip = line.strip()
        if not strip:
            continue
        try:
            obj = json.loads(strip)
        except json.JSONDecodeError:
            stats.lines_malformed += 1
            continue
        stats.lines_total += 1
        etype = obj.get("type")
        if etype == "assistant":
            for block in _blocks(obj):
                btype = block.get("type")
                if btype == "text":
                    events.append(
                        IngestEvent(
                            host=HostId.CLAUDE_CODE,
                            event=IngestEventType.ASSISTANT_MESSAGE,
                            session_id=session_id,
                            profile_id=profile_id,
                            ts=_timestamp(obj),
                            content=MessageContent(text=block.get("text", ""), model_id=_model(obj)),
                        )
                    )
                elif btype == "tool_use":
                    input_ = block.get("input", {})
                    tool_input = input_ if isinstance(input_, dict) else {}
                    pending_tool_uses.append((block.get("id", ""), block.get("name", "tool"), tool_input))
        elif etype == "user":
            if obj.get("isMeta"):
                continue
            content = obj.get("message", {}).get("content")
            if isinstance(content, str):
                if content.strip():
                    events.append(
                        IngestEvent(
                            host=HostId.CLAUDE_CODE,
                            event=IngestEventType.USER_PROMPT,
                            session_id=session_id,
                            profile_id=profile_id,
                            ts=_timestamp(obj),
                            content=MessageContent(text=content),
                        )
                    )
            elif isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "text":
                        text_block = block.get("text", "")
                        if text_block.strip():
                            events.append(
                                IngestEvent(
                                    host=HostId.CLAUDE_CODE,
                                    event=IngestEventType.USER_PROMPT,
                                    session_id=session_id,
                                    profile_id=profile_id,
                                    ts=_timestamp(obj),
                                    content=MessageContent(text=text_block),
                                )
                            )
                    elif btype == "tool_result":
                        tool_use_id = block.get("tool_use_id", "")
                        name, input_ = _pop_tool_use(pending_tool_uses, tool_use_id)
                        events.append(
                            IngestEvent(
                                host=HostId.CLAUDE_CODE,
                                event=IngestEventType.TOOL_USE,
                                session_id=session_id,
                                profile_id=profile_id,
                                ts=_timestamp(obj),
                                content=ToolContent(
                                    tool_name=name,
                                    input=input_,
                                    output=_tool_result_text(block),
                                ),
                            )
                        )
                    else:
                        stats.skipped_non_text += 1  # images / other non-text blocks
    stats.events = len(events)
    return events


def build_corpus(
    source: Path,
    *,
    max_session_bytes: int,
    turns: int,
    seed: int,
) -> tuple[Sequence[Turn], BuildStats]:
    """Read transcripts, segment into Turns, and deterministically sample."""
    if not source.is_dir():
        raise CorpusError(
            f"source directory not found: {source}\n"
            "Set --source to a Claude Code projects directory (one JSONL per session)."
        )
    files = sorted(
        path for path in source.rglob("*.jsonl") if not any(part == "subagents" for part in path.parts)
    )
    all_turns: list[Turn] = []
    stats = BuildStats()
    for path in files:
        stats.files_read += 1
        session_id = path.stem
        profile_id = path.parent.name
        stats.sessions.add(session_id)
        pipeline = InMemoryCapturePipeline()
        segmenter = TurnSegmenter(pipeline)
        head = _read_head(path, max_session_bytes)
        parse = ParseStats()
        for event in events_from_transcript(head, session_id, profile_id, parse):
            try:
                segmenter.ingest(event)
                stats.events_ingested += 1
            except CaptureError:
                continue
        stats.lines_total += parse.lines_total
        stats.lines_malformed += parse.lines_malformed
        try:
            segmenter.end_session(session_id, profile_id)
        except CaptureError:
            pass
        built = pipeline.turns(session_id)
        for turn in built:
            # end_session stamps the turn still open at end-of-file with the wall
            # clock; neutralize it so the corpus is byte-deterministic for a given
            # seed + source (ended_at is metadata the funnel never reads).
            turn.ended_at = turn.started_at
        stats.turns_built += len(built)
        all_turns.extend(built)
    rng = random.Random(seed)
    sampled = rng.sample(all_turns, min(turns, len(all_turns)))
    sampled.sort(key=lambda turn: (turn.session_id, turn.turn_index))
    stats.bytes_total = sum(len(turn.model_dump_json().encode("utf-8")) for turn in sampled)
    return sampled, stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the local capture benchmark corpus.")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE, help="Claude Code projects dir")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="corpus JSONL output path")
    parser.add_argument("--turns", type=int, default=DEFAULT_TURNS, help="target turn count")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="sampling seed")
    parser.add_argument(
        "--max-session-bytes", type=int, default=DEFAULT_MAX_SESSION_BYTES, help="bytes cap per transcript"
    )
    args = parser.parse_args(argv)
    try:
        sampled, stats = build_corpus(
            args.source,
            max_session_bytes=args.max_session_bytes,
            turns=args.turns,
            seed=args.seed,
        )
    except CorpusError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    write_corpus(args.out, sampled)
    print(f"wrote {len(sampled)} turns -> {args.out}")
    print(
        f"source files={stats.files_read} sessions={len(stats.sessions)} "
        f"lines={stats.lines_total} malformed={stats.lines_malformed} "
        f"events={stats.events_ingested} turns_built={stats.turns_built} bytes={stats.bytes_total}"
    )
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    raise SystemExit(main())
