"""Decay engine (PRD-04 FR-4.1 / FR-4.4, design/01 stage ⑤).

The time-based weight sweep that makes unused memories fade: an
Ebbinghaus-style exponential curve layered per node type, a daemon-owned
periodic sweep over every profile's unreinforced nodes and chunks, batch
weight writes through the existing storage ports, a crash-safe resume cursor,
and one audit entry per sweep pass.
"""

from __future__ import annotations

from mnemoseed.decay.model import (
    DEFAULT_LAMBDA_PER_TYPE,
    LAMBDA_TARGETS,
    SECONDS_PER_DAY,
    decay_weight,
    half_life_days,
    lambda_for,
)
from mnemoseed.decay.sweeper import DecaySweeper, SweepStats

__all__ = [
    "DEFAULT_LAMBDA_PER_TYPE",
    "LAMBDA_TARGETS",
    "SECONDS_PER_DAY",
    "DecaySweeper",
    "SweepStats",
    "decay_weight",
    "half_life_days",
    "lambda_for",
]
