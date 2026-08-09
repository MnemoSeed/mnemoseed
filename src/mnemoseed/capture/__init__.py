"""Capture funnel: /ingest intake, Turn segmentation, downstream seam.

F1 (Local Stripper) is wired in as the StrippingPipeline seam default; F2
(persistence classifier) and F3 (scoring) run as ScoringPipeline, which scores
drained turns into a watermark score pool.
"""

from __future__ import annotations

from mnemoseed.capture.lexicon_v1 import (
    EN_LEXICON_V1,
    LEXICON_V1,
    ZH_LEXICON_V1,
    AffectiveEntry,
    Lexicon,
)
from mnemoseed.capture.pipeline import (
    CapturePipeline,
    InMemoryCapturePipeline,
    ScoringPipeline,
    ScoringStats,
    StrippingPipeline,
    WritingPipeline,
    WritingStats,
)
from mnemoseed.capture.pool import (
    PoolBackend,
    PoolEvent,
    PoolEventKind,
    PoolStats,
    ScorePool,
)
from mnemoseed.capture.rulesets_v1 import RULESET_V1
from mnemoseed.capture.scorer import (
    Durability,
    ScoreComponents,
    ScoredTurn,
    ScoringConfig,
    TurnScorer,
)
from mnemoseed.capture.segment import (
    CaptureError,
    ProfileMismatchError,
    SessionSettledError,
    SessionUnknownError,
    TurnSegmenter,
)
from mnemoseed.capture.stamper import (
    ConsistencyVerdict,
    NearDuplicateChecker,
    StampWriter,
    WriteConfig,
    WriteContext,
    WriteOutcome,
    WriteOutcomeKind,
)
from mnemoseed.capture.stripper import (
    ContentTarget,
    Rule,
    RuleSet,
    StripAction,
    StrippedTurn,
    Stripper,
    StripperError,
    StripStats,
)

__all__ = [
    "AffectiveEntry",
    "CaptureError",
    "CapturePipeline",
    "ConsistencyVerdict",
    "ContentTarget",
    "Durability",
    "EN_LEXICON_V1",
    "InMemoryCapturePipeline",
    "LEXICON_V1",
    "Lexicon",
    "NearDuplicateChecker",
    "PoolBackend",
    "PoolEvent",
    "PoolEventKind",
    "PoolStats",
    "ProfileMismatchError",
    "RULESET_V1",
    "Rule",
    "RuleSet",
    "ScoreComponents",
    "ScorePool",
    "ScoredTurn",
    "ScoringConfig",
    "ScoringPipeline",
    "ScoringStats",
    "SessionSettledError",
    "SessionUnknownError",
    "StampWriter",
    "StripAction",
    "StripStats",
    "StrippedTurn",
    "Stripper",
    "StripperError",
    "StrippingPipeline",
    "TurnScorer",
    "TurnSegmenter",
    "WriteConfig",
    "WriteContext",
    "WriteOutcome",
    "WriteOutcomeKind",
    "WritingPipeline",
    "WritingStats",
    "ZH_LEXICON_V1",
]
