"""T6 usage reconciliation through the reflect seam + degradation signaling.

T3's ReflectOrchestrator consumes the widened chat seam (str OR ChatResult).
Provider-reported usage, when present on a ChatResult, rides out on the
DeltaReport (additive: existing T3/T5 tests stay green), and an LLMUnavailable
inside chat is surfaced as ReflectOutcome.llm_unavailable plus the injectable
on_unavailable callback — the capture-only seam the daemon can wire later.
"""

from __future__ import annotations

from pathlib import Path

from mnemoseed.dream import DeltaPacker, DeltaReport, ReflectOrchestrator, StubReflectLLM
from mnemoseed.dream.snapshot import Snapshot, SnapshotChunk
from mnemoseed.llm import ChatResult, LLMUnavailable, ReflectLLMAdapter, Usage
from mnemoseed.llm.types import HealthReport
from mnemoseed.schema.stamp import ChunkStamp, CognitiveTier, Cues, Provenance
from mnemoseed.storage.ports import TurnRange

_RANGE = TurnRange(0, 4)


def _stamp(chunk_id: str, text: str) -> ChunkStamp:
    return ChunkStamp(
        chunk_id=chunk_id,
        profile_id="alice",
        text=text,
        cognitive_tier=CognitiveTier.TIER_1,
        model_id="test-model",
        cues=Cues(entities=[]),
        provenance=Provenance(asserted_by="user", session_id="s1", source="manual"),
        turn_start=0,
        turn_end=1,
    )


def _snap(*stamps: ChunkStamp) -> Snapshot:
    return Snapshot(
        snapshot_id="snap-p1",
        profile_id="alice",
        turn_range=_RANGE,
        chunks=tuple(SnapshotChunk.from_stamp(c) for c in stamps),
        created_at=1000.0,
        phases=frozenset({"snapshot_done"}),
    )


class _Sleeper:
    def __init__(self) -> None:
        self.delays: list[float] = []

    def __call__(self, secs: float) -> None:
        self.delays.append(secs)


class _UsageDreamLLM:
    """DreamLLM-shaped double returning a ChatResult with optional usage."""

    def __init__(self, text: str = "[]", usage: Usage | None = None) -> None:
        self.text = text
        self.usage = usage
        self.calls = 0

    def chat(self, *, system: str, user: str) -> ChatResult:
        del system, user
        self.calls += 1
        return ChatResult(text=self.text, usage=self.usage, model="fake-dream", driver="fake")

    def check(self) -> HealthReport:
        return HealthReport(ok=True)


class _DownDreamLLM:
    """DreamLLM-shaped double whose chat always raises the typed unavailable."""

    def __init__(self) -> None:
        self.calls = 0

    def chat(self, *, system: str, user: str) -> ChatResult:
        del system, user
        self.calls += 1
        raise LLMUnavailable("provider down")

    def check(self) -> HealthReport:
        return HealthReport(ok=False, detail={"error": "provider down"})


# ---------------------------------------------------------------- usage reconciliation


def test_reflect_records_provider_usage_on_success(tmp_path: Path) -> None:
    usage = Usage(prompt_tokens=30, completion_tokens=5, cache_read_input_tokens=42)
    llm = _UsageDreamLLM(text="[]", usage=usage)
    outcome = ReflectOrchestrator(llm=llm, directory=tmp_path).reflect(
        _snap(_stamp("c1", "I prefer dark mode"))
    )
    assert outcome.ok
    assert outcome.result is not None
    assert outcome.report is not None
    assert outcome.report.provider_usage is usage


def test_reflect_str_llm_keeps_provider_usage_none(tmp_path: Path) -> None:
    outcome = ReflectOrchestrator(llm=StubReflectLLM(), directory=tmp_path).reflect(
        _snap(_stamp("c1", "I prefer dark mode"))
    )
    assert outcome.ok
    assert outcome.report is not None
    assert outcome.report.provider_usage is None


def test_usage_survives_chat_then_unparseable_output(tmp_path: Path) -> None:
    # the provider call succeeded (usage reported) but the text was not a JSON
    # array: the typed failure report still carries the provider usage
    llm = _UsageDreamLLM(text="not a json array", usage=Usage(prompt_tokens=7, completion_tokens=1))
    sleeper = _Sleeper()
    outcome = ReflectOrchestrator(llm=llm, directory=tmp_path, sleep=sleeper).reflect(
        _snap(_stamp("c1", "I prefer dark mode"))
    )
    assert not outcome.ok
    assert outcome.report is not None
    assert outcome.report.provider_usage == Usage(prompt_tokens=7, completion_tokens=1)
    assert sleeper.delays == [1.0, 2.0, 4.0]


# ---------------------------------------------------------------- FR-2.6 degradation signal


def test_llm_unavailable_typed_callback_fired_and_retried(tmp_path: Path) -> None:
    llm = _DownDreamLLM()
    heard: list[str] = []
    sleeper = _Sleeper()
    outcome = ReflectOrchestrator(
        llm=llm,
        directory=tmp_path,
        on_unavailable=heard.append,
        sleep=sleeper,
    ).reflect(_snap(_stamp("c1", "I prefer dark mode")))
    assert not outcome.ok
    assert outcome.llm_unavailable is True
    assert outcome.error == "provider down"
    assert heard == ["provider down"] * 4  # initial call + 3 exponential-backoff retries
    assert sleeper.delays == [1.0, 2.0, 4.0]
    assert llm.calls == 4


def test_plain_runtime_error_not_marked_unavailable(tmp_path: Path) -> None:
    class _BoomLLM:
        def chat(self, *, system: str, user: str) -> str:
            del system, user
            raise ValueError("bad output")

    sleeper = _Sleeper()
    outcome = ReflectOrchestrator(llm=_BoomLLM(), directory=tmp_path, sleep=sleeper).reflect(
        _snap(_stamp("c1", "I prefer dark mode"))
    )
    assert not outcome.ok
    assert outcome.llm_unavailable is False
    assert sleeper.delays == [1.0, 2.0, 4.0]


def test_skipped_reflect_reports_no_usage(tmp_path: Path) -> None:
    snap = _snap(
        _stamp("c1", "I prefer dark mode"),
    ).with_phase("reflect_done")
    llm = _UsageDreamLLM(usage=Usage(prompt_tokens=1, completion_tokens=1))
    outcome = ReflectOrchestrator(llm=llm, directory=tmp_path).reflect(snap)
    assert outcome.skipped is True
    assert outcome.report is None
    assert llm.calls == 0


# ---------------------------------------------------------------- adapter (ReflectLLM compat)


def test_reflect_adapter_makes_dream_llm_callable_as_reflect_llm() -> None:
    llm = _UsageDreamLLM(text="[]")
    adapter = ReflectLLMAdapter(llm)
    assert adapter.chat(system="sys", user="usr") == "[]"
    assert llm.calls == 1


# ---------------------------------------------------------------- delta report additive


def test_delta_report_with_provider_usage_is_additive() -> None:
    packer = DeltaPacker()
    request = packer.pack(_snap(_stamp("c1", "hi")))
    report = packer.report(request)
    assert isinstance(report, DeltaReport)
    assert report.provider_usage is None
    usage = Usage(prompt_tokens=1, completion_tokens=9)
    updated = report.with_provider_usage(usage)
    assert updated is not report
    assert updated.provider_usage is usage
    # untouched fields preserved exactly (frozen source)
    assert updated.delta_tokens == report.delta_tokens
    assert updated.prefix_tokens == report.prefix_tokens
    assert updated.overflow_count == report.overflow_count
    assert updated.estimated_cost_usd == report.estimated_cost_usd
    assert report.provider_usage is None


def test_delta_report_with_provider_usage_none_clears() -> None:
    packer = DeltaPacker()
    request = packer.pack(_snap(_stamp("c1", "hi")))
    report = packer.report(request).with_provider_usage(Usage(prompt_tokens=1, completion_tokens=1))
    cleared = report.with_provider_usage(None)
    assert cleared.provider_usage is None
    assert cleared is not report
