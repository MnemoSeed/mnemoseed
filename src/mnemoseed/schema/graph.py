"""Cortical graph schema — node types, version chains, edges.

A node is a fact/preference/habit/episode/skill-sequence/decision. The version
chain is the engineering form of reconsolidation: a rewrite pins valid_to on
the old version and links the new one in — nothing is ever overwritten.
"""

from __future__ import annotations

import time
import uuid
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from mnemoseed.schema.stamp import Provenance


class NodeType(StrEnum):
    USER = "USER"
    HABIT = "HABIT"
    PREFERENCE = "PREFERENCE"
    CONSTRAINT = "CONSTRAINT"
    EPISODE = "EPISODE"
    SKILL_SEQUENCE = "SKILL_SEQUENCE"
    DECISION = "DECISION"


class RelType(StrEnum):
    HAS = "has"
    HOLDS = "holds"
    BOUND_BY = "bound_by"
    EVIDENCED_BY = "evidenced_by"
    CONTAINS = "contains"
    SUPERSEDES = "supersedes"
    USED_IN = "used_in"
    MASTERED = "mastered"
    CO_OCCURRED = "co_occurred"  # co-activation edge for spreading activation


class GraphNode(BaseModel):
    """Cortical node. props carries per-type fields (statement/tool_chain/...)."""

    node_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    profile_id: str
    node_type: NodeType
    entities: list[str] = Field(default_factory=list)  # traversal entry points
    props: dict[str, Any] = Field(default_factory=dict)

    # weights and state
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    decay_weight: float = Field(default=1.0, ge=0.0, le=1.0)
    never_decay: bool = False  # e.g. hard constraints
    last_reinforced: float = Field(default_factory=time.time)
    reinforcement_count: int = 0

    # freshness / reconcile flags
    needs_reconcile: bool = False
    conflict_flag: bool = False
    conflict_with: str | None = None

    # version chain (bi-temporal)
    version: int = 1
    prev_version_id: str | None = None
    valid_from: float = Field(default_factory=time.time)
    valid_to: float | None = None  # None = currently in effect

    cognitive_tier: int = 1
    provenance: Provenance
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)

    @property
    def is_current(self) -> bool:
        return self.valid_to is None


class Edge(BaseModel):
    src: str
    dst: str
    rel: RelType
    weight: float = 1.0  # co-occurrence edges: +1 per shared-session activation
    profile_id: str
    created_at: float = Field(default_factory=time.time)
