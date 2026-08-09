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
    """The committed fixture strips its noise classes above a loose floor.

    NFR-1.2 (2026 reword) measures the noise-class stripping rate: bytes the
    rules removed over bytes the rules matched, both sides confined to
    rule-hit content. The full-byte ratio stays a reported observation only.
    """
    pipeline = drive_funnel(load_corpus(FIXTURE), scorer=_scorer())
    stats = pipeline.stats
    assert stats.bytes_in > 0
    assert stats.noise_class_rate > 0.9
    full_byte = (stats.bytes_in - stats.bytes_out) / stats.bytes_in
    assert full_byte > 0.5  # observation floor: fixture is noise-heavy


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


# ---------------------------------------------------------------- NFR-1.3 harness funnel
# The harness runs F1 before F2: host-injected artifacts (compaction wrappers,
# task notifications) are stripped first and a fully-stripped turn scores
# disposable, because it carries no user speech.

ARTIFACT_COMPACTION = (
    "This session is being continued from a previous conversation that ran out of context. "
    "The summary below covers the earlier portion of the conversation.\n\n"
    "Summary:\n1. The user prefers concise communication.\n"
    "Continue the conversation from where it left off without asking the user any further questions. "
    "Resume directly — do not acknowledge the summary, do not recap what was happening, do not "
    'preface with "I\'ll continue" or similar. Pick up the last task as if the break never happened.'
)

ARTIFACT_TASK_NOTIFICATION = (
    "<task-notification>\n"
    "<task-id>abc</task-id>\n"
    "<tool-use-id>Agent_1</tool-use-id>\n"
    "<status>completed</status>\n"
    '<summary>Agent "research" finished</summary>\n'
    "<result>the report\n</result>\n"
    "</task-notification>\n"
)

CALIBRATED_FN_TEXTS = [
    "官方云端还需要多一层，就是系统管理员，属于我管理整个系统运行，和查看所有数据，也可以查看销售增长等等，服务是否上线等等的管理。接下去可以开始M0",
    (
        "设计没问题，但我还有一个疑虑，就是用户使用cursor之类的工具时，"
        "你说没有startsession hook的功能，那样要怎么确保AI会自行运用记忆服务呢？"
        "在整个session的对话过程中，又怎么确保AI能有效写入和读取记忆呢？"
    ),
    "claude desktop/codex desktop这类的桌面型应用，也可以探讨一下是否能够接入MnemoSeed，有什么技术壁垒",
    (
        "AI Mode里的反馈并不是绝对的，你可以自行斟酌是否要加入设计。"
        "还有目前我用着mempalace，但是有时候可能触发记忆存储的间隔太远，"
        "一旦中间关闭session重开就失忆，你有办法解决吗？"
        "顺带一提，我刚才说的希望语气拟人一点，沟通更多以说明情况并给出选择和背后原因，"
        "而不是用很多缩写代号等等最后变成无字天书。但我同时要你尽量精简内容，节省token消耗"
    ),
    "如果效能结果都相似，甚至更好，并且没有资安风险，那就选bge-m3",
]


def test_system_artifact_turns_classify_disposable_through_harness() -> None:
    """Host-injected artifacts score disposable through strip + score: the raw
    text carries decision-looking markers, but F1 removes the scaffolding first."""
    rows = [
        _row("fp-1", ARTIFACT_COMPACTION, "durable", "decision-marker", label="disposable"),
        _row("fp-2", ARTIFACT_TASK_NOTIFICATION, "durable", "decision-marker", label="disposable"),
    ]
    report = evaluate_durability(rows, _scorer())
    assert report.tp == 0
    assert report.fp == 0
    assert report.tn == 2
    assert report.fn == 0
    # both human labels are disposable, so there is no positive reference class
    # for human_precision; the noise-free verdict is the point, not precision.
    assert report.human_labeled == 2


def test_calibrated_false_negative_patterns_classify_durable_through_harness() -> None:
    rows = [
        _row(f"fn-{i}", text, "durable", "heuristic", label="durable")
        for i, text in enumerate(CALIBRATED_FN_TEXTS)
    ]
    report = evaluate_durability(rows, _scorer())
    assert report.fp == 0
    assert report.fn == 0
    assert report.tp == len(CALIBRATED_FN_TEXTS)
    assert report.human_precision == pytest.approx(1.0)


@pytest.mark.skipif(not CORPUS.exists(), reason="real corpus not present (local-only benchmark)")
def test_real_corpus_compression_nfr_12() -> None:
    """NFR-1.2: >= 90% noise-class stripping rate on real session logs.

    Denominator is bytes the rules matched as strippable noise (matched
    spans/lines), numerator is bytes actually removed — NOT the whole corpus.
    The full-byte ratio is reported but never gate-asserted (it follows the
    input population, see PRD NFR-1.2).
    """
    pipeline = drive_funnel(load_corpus(CORPUS), scorer=_scorer())
    stats = pipeline.stats
    assert stats.bytes_in > 0
    rate = stats.noise_class_rate
    full_byte = (stats.bytes_in - stats.bytes_out) / stats.bytes_in
    print(
        f"[NFR-1.2] noise_class_rate={rate:.4f} matched={stats.noise_matched_bytes} "
        f"removed={stats.noise_removed_bytes} full_byte={full_byte:.4f}"
    )
    assert rate >= 0.90


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
