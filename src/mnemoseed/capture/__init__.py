"""Capture funnel: /ingest intake, Turn segmentation, downstream seam.

F1 (Stripper), F2 (persistence classifier) and F3 (scoring) land here as
later tasks; they consume structured Turns through the CapturePipeline seam.
"""

from __future__ import annotations

from mnemoseed.capture.pipeline import CapturePipeline, InMemoryCapturePipeline
from mnemoseed.capture.segment import (
    CaptureError,
    ProfileMismatchError,
    SessionSettledError,
    SessionUnknownError,
    TurnSegmenter,
)

__all__ = [
    "CaptureError",
    "CapturePipeline",
    "InMemoryCapturePipeline",
    "ProfileMismatchError",
    "SessionSettledError",
    "SessionUnknownError",
    "TurnSegmenter",
]
