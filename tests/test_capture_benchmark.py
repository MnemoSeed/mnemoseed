"""Capture benchmark regression gate (NFR-1.2 / NFR-1.3 / AC-1 / AC-2).

NFR-1.2 (stripper compression >= 90% on real session logs) and NFR-1.3
(durability precision >= 0.9 on the labeled set) are measured against a local,
gitignored corpus under ``.bench/``. CI never has the corpus, so those tests
skip there; the committed synthetic fixture under ``tests/fixtures/`` keeps a
looser sanity assertion and the AC-2 funnel behavior exercised in every run.

Real-corpus tests go through the same public funnel surface the synthetic ones
use, so a production refactor must not rewrite the benchmark.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mnemoseed.capture.benchmark import (
    DurabilityLabelRow,
    drive_funnel,
    evaluate_durability,
    load_corpus,
    load_labels,
    prelabel,
    prelabel_reason,
    write_corpus,
)
from mnemoseed.capture.scorer import TurnScorer
from mnemoseed.schema.turn import TurnRole
from mnemoseed.storage.drivers.synthetic_embedder import SyntheticEmbedder

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / ".bench" / "capture_corpus.jsonl"
LABELS = ROOT / ".bench" / "durability_labels.jsonl"
FIXTURE = ROOT / "tests" / "fixtures" / "synthetic_capture_corpus.jsonl"

AC2_REJECTED = "这 bug 烦死了"
AC2_KEPT = "我 review 喜欢简洁"


def _scorer() -> TurnScorer:
    """Deterministic scorer for every benchmark run (synthetic embedder)."""
    return TurnScorer(embedder=SyntheticEmbedder())


def _user_text(turn) -> str:
    """Join the USER steps of a turn (the scanner's raw input)."""
    parts = [step.content for step in turn.steps if step.role is TurnRole.USER]
    return " ".join(parts)


def _kept(pipeline) -> list:
    return [st for sid in pipeline.sessions() for st in pipeline.turns(sid)]


def test_synthetic_corpus_roundtrip_write_and_load() -> None:
    """The corpus JSONL contract round-trips Turn objects losslessly."""
    turns = load_corpus(FIXTURE)
    assert turns, "synthetic fixture must not be empty"
    tmp = Path(__file__).with_name("fixtures") / "_roundtrip_tmp.jsonl"
    try:
        write_corpus(tmp, turns)
        assert load_corpus(tmp) == turns
    finally:
        tmp.unlink(missing_ok=True)


def test_synthetic_corpus_compression_sanity() -> None:
    """The committed fixture compresses above a loose floor on every run."""
    pipeline = drive_funnel(load_corpus(FIXTURE), scorer=_scorer())
    stats = pipeline.stats
    assert stats.bytes_in > 0
    ratio = (stats.bytes_in - stats.bytes_out) / stats.bytes_in
    assert ratio > 0.5


def test_ac2_sentences_through_funnel() -> None:
    """'这 bug 烦死了' is rejected; '我 review 喜欢简洁' is kept with cues."""
    pipeline = drive_funnel(load_corpus(FIXTURE), scorer=_scorer())
    kept = _kept(pipeline)
    kept_texts = [_user_text(st.turn) for st in kept]
    assert AC2_REJECTED not in kept_texts
    matched = [st for st in kept if _user_text(st.turn) == AC2_KEPT]
    assert len(matched) == 1
    (scored,) = matched
    assert scored.durability.durability.value == "durable"
    assert scored.importance > 0.0
    assert scored.emotion is not None


def test_prelabel_heuristic_on_seed_sentences() -> None:
    """The documented independent heuristic matches the AC-2 gate semantics."""
    assert prelabel(AC2_KEPT) == "durable"
    assert prelabel(AC2_REJECTED) == "disposable"
    assert prelabel("好的") == "disposable"
    assert prelabel("明天下午开会") == "disposable"
    assert prelabel("以后都用 pnpm") == "durable"
    assert prelabel("每次 code review 我都要简洁 别寒暄") == "durable"
    assert prelabel_reason(AC2_KEPT) == "pref-marker"


def _row(id: str, text: str, prelabel: str, reason: str, label: str = "") -> DurabilityLabelRow:
    return DurabilityLabelRow(id=id, text=text, prelabel=prelabel, prelabel_reason=reason, label=label)


def test_evaluate_precision_confusion_on_handcrafted_rows() -> None:
    rows = [
        _row("a", AC2_KEPT, "durable", "pref-marker", label="durable"),
        _row("b", "好的", "disposable", "interjection", label="disposable"),
        _row("c", AC2_REJECTED, "disposable", "venting-marker", label="disposable"),
        _row("d", "以后都用 pnpm", "durable", "decision-marker", label="durable"),
        _row("e", "每次 code review 我都要简洁 别寒暄", "durable", "rule-marker", label="disposable"),
    ]
    report = evaluate_durability(rows, _scorer())
    assert report.total == 5
    assert report.human_labeled == 5
    assert report.prelabel_fallback == 0
    assert report.tp == 2
    assert report.fp == 1
    assert report.tn == 2
    assert report.fn == 0
    assert report.precision == pytest.approx(2 / 3)
    assert report.recall == pytest.approx(1.0)
    assert report.accuracy == pytest.approx(4 / 5)
    assert report.human_precision == pytest.approx(1.0)
    assert report.used_prelabels is False


def test_evaluate_falls_back_to_prelabel_and_reports_it() -> None:
    rows = [
        _row("a", AC2_KEPT, "durable", "pref-marker"),
        _row("b", "好的", "disposable", "interjection"),
    ]
    report = evaluate_durability(rows, _scorer())
    assert report.prelabel_fallback == 2
    assert report.used_prelabels is True


@pytest.mark.skipif(not CORPUS.exists(), reason="real corpus not present (local-only benchmark)")
def test_real_corpus_compression_nfr_12() -> None:
    """NFR-1.2: >= 90% compression on the real session-log corpus."""
    pipeline = drive_funnel(load_corpus(CORPUS), scorer=_scorer())
    stats = pipeline.stats
    assert stats.bytes_in > 0
    ratio = (stats.bytes_in - stats.bytes_out) / stats.bytes_in
    assert ratio >= 0.90


@pytest.mark.skipif(not LABELS.exists(), reason="label set not present (local-only benchmark)")
def test_durability_prelabel_smoke_reports_without_asserting_precision() -> None:
    """Runs the harness against prelabels and checks the report is well-formed."""
    rows = load_labels(LABELS)
    assert rows
    report = evaluate_durability(rows, _scorer())
    assert report.total == len(rows)
    assert report.tp + report.fp + report.tn + report.fn == report.total


@pytest.mark.skipif(not LABELS.exists(), reason="label set not present (local-only benchmark)")
def test_durability_precision_against_human_labels_nfr_13() -> None:
    """NFR-1.3: precision >= 0.9 on the durable class, human labels only."""
    rows = load_labels(LABELS)
    report = evaluate_durability(rows, _scorer())
    if report.human_labeled == 0:
        pytest.skip("no human labels filled yet — precision gate applies once reviewed")
    assert report.human_precision is not None
    assert report.human_precision >= 0.9
