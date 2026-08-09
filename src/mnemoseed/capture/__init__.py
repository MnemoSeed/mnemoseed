"""Capture funnel: /ingest intake, Turn segmentation, downstream seam.

F1 (Local Stripper) is wired in as the StrippingPipeline seam default; F2
(persistence classifier) and F3 (scoring) land here as later tasks consuming
the same CapturePipeline contract.
"""

from __future__ import annotations

from mnemoseed.capture.pipeline import (
    CapturePipeline,
    InMemoryCapturePipeline,
    StrippingPipeline,
)
from mnemoseed.capture.rulesets_v1 import RULESET_V1
from mnemoseed.capture.segment import (
    CaptureError,
    ProfileMismatchError,
    SessionSettledError,
    SessionUnknownError,
    TurnSegmenter,
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
    "CaptureError",
    "CapturePipeline",
    "ContentTarget",
    "InMemoryCapturePipeline",
    "ProfileMismatchError",
    "RULESET_V1",
    "Rule",
    "RuleSet",
    "SessionSettledError",
    "SessionUnknownError",
    "StripAction",
    "StripStats",
    "StrippedTurn",
    "Stripper",
    "StripperError",
    "StrippingPipeline",
    "TurnSegmenter",
]
