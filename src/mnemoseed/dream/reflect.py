"""Reflection orchestrator + de-biasing seam (PRD-02 T3; FR-2.2 / FR-2.12, §7).

The orchestrator consumes an adopted Snapshot (from the trigger's DREAMING
state), renders the versioned de-biasing prompt, drives the narrow ReflectLLM
seam, folds duplicate mentions (AC-3), persists the REFLECT_DONE marker into
the snapshot file BEFORE reporting completion (crash-safe, NFR-2.3), and on
model failure degrades with exponential-backoff retry x3 into a typed outcome:
never a raise, never a block on ingestion (design/02 section 7).

The output contract is the T4 seam: a frozen ReflectionResult carrying
deduplicated triples, each with per-triple provenance (tiers, chunk ids, turn
range), confidence, and a route (core | isolated | salvage) per dual-track
rules. The deterministic StubReflectLLM implements the same de-biasing contract
offline, so the whole pipeline is exercisable without any network (the M1
manual-first phase and tests). No graph writes happen here (T4 owns them).
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from mnemoseed.config import CONFIG_DIR
from mnemoseed.dream.prompts import (
    ChunkBlock,
    build_reflect_prompt,
    origin_of,
    parse_chunk_blocks,
)
from mnemoseed.dream.snapshot import (
    Snapshot,
    SnapshotPhase,
    write_snapshot_file,
)
from mnemoseed.schema.stamp import CognitiveTier
from mnemoseed.storage.ports import TurnRange

logger = logging.getLogger("mnemoseed.dream.reflect")


# ---------------------------------------------------------------- output contract


class Route(StrEnum):
    """Dual-track routing of one reflected triple (design/02 section 4)."""

    CORE = "core"
    ISOLATED = "isolated"
    SALVAGE = "salvage"


@dataclass(frozen=True)
class ReflectedTriple:
    """One deduplicated entity triple plus its provenance and route."""

    subject: str
    predicate: str
    object: str
    tiers: tuple[CognitiveTier, ...]  # source tier(s) across the evidence chunks
    chunk_ids: tuple[str, ...]  # provenance refs pinning the exact chunks
    turn_range: TurnRange  # the snapshot scope the evidence came from
    confidence: float  # 0..1, reinforced by dedup folding (AC-3)
    route: Route  # core | isolated | salvage; tier-3 provenance never yields core
    preference: bool = False  # preference-type extraction (FR-2.12 boundary)


@dataclass(frozen=True)
class ReflectionResult:
    """The T4 seam: everything the splitter needs to route and write back."""

    snapshot_id: str
    profile_id: str
    turn_range: TurnRange
    prompt_version: str
    triples: tuple[ReflectedTriple, ...]


@dataclass(frozen=True)
class ReflectOutcome:
    """Typed result of one reflect pass. ``ok`` is always set."""

    ok: bool
    result: ReflectionResult | None = None
    error: str | None = None
    skipped: bool = False  # marker gate: reflect had already completed


# ---------------------------------------------------------------- the LLM seam


class ReflectLLM(Protocol):
    """Narrow chat-completion seam. T6's full DreamLLM port can satisfy this
    single method; the deterministic StubReflectLLM satisfies it offline."""

    def chat(self, *, system: str, user: str) -> str: ...


# Stripped personal color: emotional/flavor intensifiers, sentence-final tone
# particles, and honorific/role-play mannerisms an anima renders. These are
# never stored with a fact (design/02 section 5).
STRIP_TOKENS: frozenset[str] = frozenset(
    {
        "really",
        "very",
        "so",
        "super",
        "absolutely",
        "extremely",
        "totally",
        "literally",
        "honestly",
        "definitely",
        "just",
        "超级",
        "非常",
        "特别",
        "极其",
        "简直",
        "真的",
        "啦",
        "呀",
        "呢",
        "嘛",
        "哈",
        "喔",
        "哦",
        "呗",
        "喵",
        "嘻嘻",
        "哈哈",
        "嘿嘿",
        "陛下",
        "殿下",
        "主人",
        "大人",
        "master",
        "亲爱",
        "亲爱的",
        "人家",
        "奴家",
        "本座",
        "咱家",
        "超",
    }
)

_STRIP_RE = re.compile(
    "|".join(re.escape(token) for token in sorted(STRIP_TOKENS, key=len, reverse=True)),
    re.IGNORECASE,
)
_NON_WORD_RE = re.compile(r"[^\w\s一-鿿-]")

_PREF_EN = re.compile(
    r"\b(?:i|we)\b[^.!?\n]{0,25}?\b(?:like|love|prefer|enjoy|value|favour|favor)\b"
    r"(?P<obj>[^.!?\n]{1,60})",
    re.IGNORECASE,
)
_PREF_ZH = re.compile(
    r"我[^。！？\n]{0,12}?(?:喜欢|爱|偏爱|偏好|欣赏|倾向于|钟意|认可|推崇)(?P<obj>[^。！？\n]{1,30})"
)
_HABIT_EN = re.compile(
    r"\b(?:i|we)\b[^.!?\n]{0,25}?\b(?:always|never|usually|typically|habitually)\b"
    r"(?P<obj>[^.!?\n]{1,60})",
    re.IGNORECASE,
)
_HABIT_ZH = re.compile(r"我[^。！？\n]{0,10}?(?:每次|总是|通常|习惯)(?:都)?(?P<obj>[^。！？\n]{1,30})")
_DECIDE_EN = re.compile(
    r"\b(?:i|we)\b[^.!?\n]{0,25}?\b(?:decided|switched to|committed to)\b"
    r"(?P<obj>[^.!?\n]{1,60})",
    re.IGNORECASE,
)
_DECIDE_ZH = re.compile(r"我(?:决定|打算|以后都|从今往后)(?P<obj>[^。！？\n]{1,30})")
_STANCE_EN = re.compile(
    r"\b(?:i|we)\b[^.!?\n]{0,25}?\b(?:believe|think|support|oppose)\b(?P<obj>[^.!?\n]{1,60})",
    re.IGNORECASE,
)
_STANCE_ZH = re.compile(r"我(?:认为|觉得|相信|坚持|反对|支持)(?P<obj>[^。！？\n]{1,30})")
_ASSERT_PATTERN = re.compile(
    r"\b(?:definitely|absolutely|certainly|surely|no doubt)\b(?P<obj>[^.!?\n]{1,60})",
    re.IGNORECASE,
)

_CANONICAL_PREDICATE: tuple[tuple[re.Pattern[str], str, bool], ...] = (
    (_PREF_EN, "prefers", True),
    (_PREF_ZH, "prefers", True),
    (_HABIT_EN, "has_habit", False),
    (_HABIT_ZH, "has_habit", False),
    (_DECIDE_EN, "decided", False),
    (_DECIDE_ZH, "decided", False),
    (_STANCE_EN, "believes", False),
    (_STANCE_ZH, "believes", False),
)

_BASE_CONFIDENCE: dict[str, float] = {
    "prefers": 0.7,
    "has_habit": 0.65,
    "decided": 0.7,
    "believes": 0.6,
    "asserts": 0.5,
}

_ROUTE_ORDER: dict[Route, int] = {Route.CORE: 1, Route.ISOLATED: 2, Route.SALVAGE: 3}


class StubReflectLLM:
    """Deterministic, offline ReflectLLM for tests and the M1 manual-first phase.

    Implements the same de-biasing contract the prompt demands: rule-based
    triple extraction over the prompt's chunk blocks, personal color stripped
    from every component, speaking style never emitted, preference-type
    extractions restricted to user-originated chunks (FR-2.12), and tier-3
    evidence routed to salvage (durable) or isolated (noise claim), never core.
    """

    def chat(self, *, system: str, user: str) -> str:
        del system
        mentions: list[dict[str, Any]] = []
        for block in parse_chunk_blocks(user):
            mentions.extend(self._extract_block(block))
        return json.dumps(mentions, ensure_ascii=False)

    def _extract_block(self, block: ChunkBlock) -> list[dict[str, Any]]:
        tier = CognitiveTier(block.tier)
        mentions: list[dict[str, Any]] = []
        for pattern, predicate, is_preference in _CANONICAL_PREDICATE:
            for match in pattern.finditer(block.text):
                obj = _clean_components(match.group("obj"))
                if not obj:
                    continue
                mentions.append(
                    {
                        "subject": "user",
                        "predicate": predicate,
                        "object": obj,
                        "tiers": [int(tier)],
                        "chunk_ids": [block.chunk_id],
                        "confidence": _BASE_CONFIDENCE[predicate],
                        "route": _route_for(tier, predicate),
                        "preference": is_preference,
                    }
                )
        # tier-3 low-value noise: confident-but-unverifiable claims from a
        # non-user source, routed to the physical isolation track (AC-2 audit)
        if tier is CognitiveTier.TIER_3 and block.origin != "user":
            for match in _ASSERT_PATTERN.finditer(block.text):
                obj = _clean_components(match.group("obj"))
                if not obj:
                    continue
                mentions.append(
                    {
                        "subject": "assistant",
                        "predicate": "asserts",
                        "object": obj,
                        "tiers": [int(tier)],
                        "chunk_ids": [block.chunk_id],
                        "confidence": _BASE_CONFIDENCE["asserts"],
                        "route": Route.ISOLATED.value,
                        "preference": False,
                    }
                )
        if block.origin != "user":
            mentions = [m for m in mentions if not m["preference"]]
        return mentions


def _clean_components(raw: str) -> str:
    text = _STRIP_RE.sub(" ", raw)
    text = _NON_WORD_RE.sub(" ", text)
    return " ".join(text.split())


def _route_for(tier: CognitiveTier, predicate: str) -> str:
    if tier is CognitiveTier.TIER_3:
        return Route.ISOLATED.value if predicate == "asserts" else Route.SALVAGE.value
    return Route.CORE.value


# ---------------------------------------------------------------- orchestrator


class ReflectOrchestrator:
    """Runs the reflection pipeline over one adopted snapshot and reports
    completion through the ``on_done`` seam (wired to trigger.on_reflect_complete).

    Async-friendly in shape: it is O(chunk text) per call and never performs
    blocking I/O beyond the injectable LLM seam, so the daemon runs it on a
    background task — nothing touches the /ingest hot path.
    """

    def __init__(
        self,
        *,
        llm: ReflectLLM,
        directory: Path | None = None,
        on_done: Callable[[str], None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_retries: int = 3,
        backoff_base: float = 1.0,
    ) -> None:
        self._llm = llm
        self._directory = directory if directory is not None else CONFIG_DIR / "dreams"
        self._on_done = on_done
        self._sleep = sleep
        self._max_retries = max_retries
        self._backoff_base = backoff_base

    def reflect(self, snapshot: Snapshot) -> ReflectOutcome:
        """Run one reflect pass. The marker gate makes a completed reflect a
        no-op: a recovered snapshot that already wrote back never re-runs."""
        if SnapshotPhase.REFLECT_DONE.value in snapshot.phases:
            return ReflectOutcome(ok=True, result=None, skipped=True)

        prompt = build_reflect_prompt(snapshot)
        result: ReflectionResult | None = None
        last_error = ""
        for attempt in range(self._max_retries + 1):
            try:
                text = self._llm.chat(system=prompt.system, user=prompt.user)
                payload = json.loads(text)
                if not isinstance(payload, list):
                    raise ValueError("reflect output is not a JSON array")
                result = self._assemble(snapshot, prompt.version, payload)
                break
            except Exception as exc:  # noqa: BLE001 - degrade, never raise into the caller
                last_error = str(exc)
                if attempt >= self._max_retries:
                    logger.warning(
                        "reflect failed for %s after %d retries: %s",
                        snapshot.profile_id,
                        self._max_retries,
                        last_error,
                    )
                    return ReflectOutcome(ok=False, result=None, error=last_error)
                self._sleep(self._backoff(attempt))

        try:
            self._finalize(snapshot)
        except Exception as exc:  # noqa: BLE001 - marker before progress
            logger.warning(
                "reflect done but REFLECT_DONE persist failed for %s: %s", snapshot.profile_id, exc
            )
            return ReflectOutcome(ok=False, result=result, error=f"persist failed: {exc}")
        assert result is not None
        return ReflectOutcome(ok=True, result=result)

    def _backoff(self, attempt: int) -> float:
        """Exponential schedule: base, 2*base, 4*base across retries 1..3."""
        return self._backoff_base * (1 << attempt)

    def _finalize(self, snapshot: Snapshot) -> None:
        marked = snapshot.with_phase(SnapshotPhase.REFLECT_DONE.value)
        write_snapshot_file(self._directory, marked)
        if self._on_done is not None:
            self._on_done(snapshot.profile_id)

    # ------------------------------------------------------------ contract assembly

    def _assemble(self, snapshot: Snapshot, version: str, payload: list[dict[str, Any]]) -> ReflectionResult:
        origin_by_chunk = {c.chunk_id: origin_of(c) for c in snapshot.chunks}
        mentions: list[ReflectedTriple] = []
        for item in payload:
            triple = _parse_triple(snapshot, item, origin_by_chunk)
            if triple is not None:
                mentions.append(triple)
        return _fold_triples(snapshot, version, mentions)


def _parse_triple(
    snapshot: Snapshot,
    item: dict[str, Any],
    origin_by_chunk: dict[str, str],
) -> ReflectedTriple | None:
    try:
        subject = str(item["subject"]).strip()
        predicate = str(item["predicate"]).strip()
        obj = str(item["object"]).strip()
        tiers = tuple(sorted({CognitiveTier(int(t)) for t in item["tiers"]}, key=int))
        chunk_ids = tuple(sorted({str(c) for c in item["chunk_ids"]}))
        confidence = max(0.0, min(0.95, float(item["confidence"])))
        route = Route(str(item["route"]))
        preference = bool(item.get("preference", False))
    except (KeyError, TypeError, ValueError):
        return None  # malformed mention: skip, keep the pipeline alive
    if not subject or not predicate or not obj:
        return None
    # FR-2.12 engine invariant: preference-type evidence must be user-originated
    if preference and not all(origin_by_chunk.get(cid) == "user" for cid in chunk_ids):
        return None
    # anti-backflow engine invariant: tier-3 evidence never routes to the main graph
    if any(t is CognitiveTier.TIER_3 for t in tiers) and route is Route.CORE:
        route = Route.ISOLATED if predicate == "asserts" else Route.SALVAGE
    return ReflectedTriple(
        subject=subject,
        predicate=predicate,
        object=obj,
        tiers=tiers,
        chunk_ids=chunk_ids,
        turn_range=snapshot.turn_range,
        confidence=confidence,
        route=route,
        preference=preference,
    )


def _fold_triples(snapshot: Snapshot, version: str, mentions: list[ReflectedTriple]) -> ReflectionResult:
    """AC-3 dedup fold: repeated mentions of the same canonical triple collapse
    into one entry with reinforced confidence, merged provenance, and the most
    restrictive route (tier-3 evidence always dominates)."""
    groups: dict[tuple[str, str, str], list[ReflectedTriple]] = {}
    for mention in mentions:
        key = (
            mention.subject.casefold().strip(),
            mention.predicate.casefold().strip(),
            mention.object.casefold().strip(),
        )
        groups.setdefault(key, []).append(mention)

    folded: list[ReflectedTriple] = []
    for group in groups.values():
        tiers = tuple(sorted({tier for m in group for tier in m.tiers}, key=int))
        chunk_ids = tuple(sorted({cid for m in group for cid in m.chunk_ids}))
        subject = min((m.subject for m in group), key=str.casefold)
        predicate = min((m.predicate for m in group), key=str.casefold)
        obj = min((m.object for m in group), key=str.casefold)
        confidence = min(0.95, max(m.confidence for m in group) + 0.05 * (len(group) - 1))
        route = max((m.route for m in group), key=lambda r: _ROUTE_ORDER[r])
        if any(t is CognitiveTier.TIER_3 for t in tiers) and route is Route.CORE:
            route = Route.ISOLATED if predicate == "asserts" else Route.SALVAGE
        folded.append(
            ReflectedTriple(
                subject=subject,
                predicate=predicate,
                object=obj,
                tiers=tiers,
                chunk_ids=chunk_ids,
                turn_range=snapshot.turn_range,
                confidence=confidence,
                route=route,
                preference=any(m.preference for m in group),
            )
        )

    folded.sort(
        key=lambda t: (t.route.value, t.subject.casefold(), t.predicate.casefold(), t.object.casefold())
    )
    return ReflectionResult(
        snapshot_id=snapshot.snapshot_id,
        profile_id=snapshot.profile_id,
        turn_range=snapshot.turn_range,
        prompt_version=version,
        triples=tuple(folded),
    )
