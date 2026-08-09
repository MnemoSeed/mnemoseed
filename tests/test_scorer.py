"""F2 persistence classifier + F3 importance scorer (FR-1.3 / FR-1.4 / FR-1.9).

All behaviour goes through TurnScorer.score_turn: durability label + confidence
for F2, then saturated arousal / novelty / causal-chain and the combined S for
F3, with valence restricted to the cues field and the importance_hint max-merge.
"""

from __future__ import annotations

import pytest

from mnemoseed.capture.scorer import (
    Durability,
    ScoredTurn,
    ScoringConfig,
    TurnScorer,
)
from mnemoseed.schema.turn import HostId, Turn, TurnRole, TurnStep
from mnemoseed.storage.drivers.synthetic_embedder import SyntheticEmbedder


def _turn(text: str, *, index: int = 0, profile: str = "prof-main") -> Turn:
    return Turn(
        turn_index=index,
        session_id="sess-score-1",
        profile_id=profile,
        host=HostId.GENERIC,
        started_at=0.0,
        steps=[TurnStep(role=TurnRole.USER, content=text)],
    )


def _scorer(config: ScoringConfig | None = None) -> TurnScorer:
    return TurnScorer(embedder=SyntheticEmbedder(), config=config)


# ---------------------------------------------------------------- F2 label


def test_ac2_bug_venting_class_rejected() -> None:
    result = _scorer().score_turn(_turn("这 bug 烦死了"))
    assert result.durability.durability is Durability.DISPOSABLE
    assert 0.0 <= result.durability.confidence <= 1.0
    assert result.durability.reasons


def test_ac2_review_preference_accepted() -> None:
    result = _scorer().score_turn(_turn("我 review 喜欢简洁"))
    assert result.durability.durability is Durability.DURABLE
    assert result.durability.confidence >= 0.7
    assert "pref-marker" in result.durability.reasons


def test_decision_sentence_accepted() -> None:
    result = _scorer().score_turn(_turn("以后都用 pnpm 管理依赖"))
    assert result.durability.durability is Durability.DURABLE


def test_pure_venting_rejected() -> None:
    result = _scorer().score_turn(_turn("今天累死了 真是受不了"))
    assert result.durability.durability is Durability.DISPOSABLE


def test_cjk_en_mixed_preference_accepted() -> None:
    result = _scorer().score_turn(_turn("每次 code review 我都要简洁 别寒暄"))
    assert result.durability.durability is Durability.DURABLE


def test_phatic_interjection_rejected_as_conservative_default() -> None:
    result = _scorer().score_turn(_turn("好的"))
    assert result.durability.durability is Durability.DISPOSABLE


def test_markerless_neutral_text_defaults_to_reject() -> None:
    result = _scorer().score_turn(_turn("下午三点开会"))
    assert result.durability.durability is Durability.DISPOSABLE


def test_verbatim_session_repeat_is_rejected_as_repetition() -> None:
    text = "我 review 喜欢简洁"
    scorer = _scorer()
    first = scorer.score_turn(_turn(text))
    assert first.durability.durability is Durability.DURABLE
    repeat = scorer.score_turn(_turn(text), recent_texts=[text])
    assert repeat.durability.durability is Durability.DISPOSABLE
    assert "session-repetition" in repeat.durability.reasons


def test_embedding_fallback_accepts_markerless_durable_anchor() -> None:
    result = _scorer().score_turn(_turn("我用模板管理复用代码"))
    assert result.durability.durability is Durability.DURABLE
    assert "embedding-durable" in result.durability.reasons


def test_embedding_fallback_rejects_markerless_disposable_anchor() -> None:
    result = _scorer().score_turn(_turn("老是无缘无故卡住"))
    assert result.durability.durability is Durability.DISPOSABLE
    assert "embedding-disposable" in result.durability.reasons


# ---------------------------------------------------------------- F3 arousal


def test_arousal_saturates_at_cap() -> None:
    scorer = _scorer()
    extreme = scorer.score_turn(_turn("崩溃极了"))
    mild = scorer.score_turn(_turn("有点烦"))
    assert extreme.components.arousal == pytest.approx(10.0)
    assert mild.components.arousal == pytest.approx(0.6 / 0.75 * 10.0)
    assert extreme.components.arousal > mild.components.arousal


def test_peripheral_gaps_flag_on_extreme_arousal() -> None:
    scorer = _scorer()
    assert scorer.score_turn(_turn("崩溃极了")).emotion is not None
    assert scorer.score_turn(_turn("崩溃极了")).emotion.peripheral_gaps is True
    assert scorer.score_turn(_turn("有点烦")).emotion.peripheral_gaps is False


# ---------------------------------------------------------------- F3 valence red line


def test_valence_never_enters_s_score_or_confidence() -> None:
    # two turns with equal arousal, equal novelty (self-reference), opposite valence
    scorer = _scorer()
    negative = scorer.score_turn(_turn("真气啊 我气坏了"), recent_texts=["真气啊 我气坏了"])
    positive = scorer.score_turn(_turn("太爽了 真爽"), recent_texts=["太爽了 真爽"])
    assert negative.emotion is not None and negative.emotion.valence is not None
    assert positive.emotion is not None and positive.emotion.valence is not None
    assert negative.emotion.valence < 0.0 < positive.emotion.valence
    assert negative.emotion.arousal == pytest.approx(positive.emotion.arousal)
    assert negative.importance == pytest.approx(positive.importance)
    assert negative.importance > 0.0  # negative valence did not reduce S


# ---------------------------------------------------------------- F3 novelty


def test_novelty_higher_for_distant_topics() -> None:
    scorer = _scorer()
    text = "我 review 喜欢简洁"
    distant = scorer.score_turn(_turn(text), recent_texts=["部署 K8s 集群时的网络配置"])
    repeated = scorer.score_turn(_turn(text), recent_texts=[text])
    assert distant.components.novelty > repeated.components.novelty + 5.0
    assert repeated.components.novelty == pytest.approx(0.0)
    assert 0.0 <= distant.components.novelty <= 10.0


def test_novelty_ten_when_nothing_recent() -> None:
    result = _scorer().score_turn(_turn("我 review 喜欢简洁"))
    assert result.components.novelty == pytest.approx(10.0)


# ---------------------------------------------------------------- F3 causal chain


def test_causal_chain_counts_connectives_and_decisions() -> None:
    scorer = _scorer()
    text = "因为接口变了 导致全部报错 我决定以后都用这个库"
    result = scorer.score_turn(_turn(text))
    # connectives: 因为, 导致; decisions: 决定, 以后
    assert result.components.causal_chain == pytest.approx(min(4, 5) * 2.0)
    assert result.causal_reasons
    assert _scorer().score_turn(_turn("好的")).components.causal_chain == pytest.approx(0.0)


# ---------------------------------------------------------------- S combo / config


def test_weights_are_configurable() -> None:
    text = "因为接口变了 导致全部报错 我决定以后都用这个库"
    arousal_only = _scorer(ScoringConfig(weights=(1.0, 0.0, 0.0))).score_turn(_turn(text))
    causal_only = _scorer(ScoringConfig(weights=(0.0, 0.0, 1.0))).score_turn(_turn(text))
    assert arousal_only.importance == pytest.approx(arousal_only.components.arousal)
    assert causal_only.importance == pytest.approx(causal_only.components.causal_chain)


def test_importance_hint_max_merges() -> None:
    scorer = _scorer()
    plain = scorer.score_turn(_turn("好的"))
    hinted_low = scorer.score_turn(_turn("好的"), importance_hint=0.1)
    hinted_full = scorer.score_turn(_turn("好的"), importance_hint=1.0)
    assert hinted_low.importance == pytest.approx(plain.importance)
    assert hinted_full.importance == pytest.approx(10.0)


def test_importance_hint_never_reduces_score() -> None:
    scorer = _scorer()
    text = "我 review 喜欢简洁"
    plain = scorer.score_turn(_turn(text))
    # auto-S here is already above 0.5; the hint must not pull it down
    hinted = scorer.score_turn(_turn(text), importance_hint=0.4)
    assert hinted.importance >= plain.importance


def test_score_bounds_and_result_shape() -> None:
    result: ScoredTurn = _scorer().score_turn(_turn("我 review 喜欢简洁"))
    assert 0.0 <= result.importance <= 10.0
    assert result.turn.turn_index == 0
    assert result.durability.confidence <= 1.0
