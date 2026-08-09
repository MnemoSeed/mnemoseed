"""Benchmark tooling for the capture funnel (NFR-1.2 / NFR-1.3 / AC-1 / AC-2).

The real corpus never enters the repo: ``scripts/build_capture_corpus.py``
writes a deterministic sample of local Claude Code session logs to a gitignored
``.bench/`` directory, and the benchmark scripts/tests read it back through
this module. CI runs only the committed synthetic fixture.

Two measurements are defined here:

- NFR-1.2 noise-class stripping rate: the PRD fixes the gate at ``>= 90%`` on
  real session logs. The metric is a *noise-class* rate — numerator is bytes
  the F1 rules actually removed, denominator is bytes the rules matched as
  strippable noise (matched spans/lines), never the whole corpus. It is exposed
  as ``pipeline.stats.noise_class_rate`` via the strip telemetry, and is 0 when
  nothing matched. The full-byte ratio (``bytes_out / bytes_in``) stays a
  reported observation only, because on prose-heavy sessions it follows the
  input population, not the stripper's quality. The 90% gate applies to the
  real corpus only.
- NFR-1.3 durability precision: computed against HUMAN-filled ``label`` field.
  ``prelabel`` comes from a deterministic, documented heuristic below; its
  match against the scorer is a smoke signal, never the acceptance number,
  because the heuristic and the scorer are intentionally independent.

The harness mirrors the production funnel: the text of every labeled row runs
through F1 (the Stripper) before F2 scores it, so host-injected artifacts
(session-compaction wrappers, task-notification blocks, tool noise) do not leak
decision-like phrasing into the durability verdict. A turn that strips to the
empty string is scored disposable — its raw content carried only host
scaffolding, i.e. no user speech to persist.
"""

from __future__ import annotations

import json
import random
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from mnemoseed.capture.pipeline import ScoringPipeline
from mnemoseed.capture.rulesets_v1 import RULESET_V1
from mnemoseed.capture.scorer import Durability, TurnScorer
from mnemoseed.capture.stripper import ContentTarget, Stripper
from mnemoseed.schema.turn import HostId, Turn, TurnRole, TurnStep
from mnemoseed.storage.drivers.synthetic_embedder import SyntheticEmbedder
from mnemoseed.storage.ports import Embedder

# ------------------------------------------------------------ corpus I/O

CORPUS_SUFFIXES = {".jsonl"}


def load_corpus(path: Path) -> list[Turn]:
    """Read a JSONL corpus of serialized Turns."""
    turns: list[Turn] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped:
            turns.append(Turn.model_validate_json(stripped))
    return turns


def write_corpus(path: Path, turns: Sequence[Turn]) -> None:
    """Write Turns as one JSON object per line (a deterministic corpus file)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(turn.model_dump_json() for turn in turns)
    path.write_text(body + ("\n" if body else ""), encoding="utf-8")


def user_text(turn: Turn) -> str:
    """Forward, joined content of the USER steps (the scanner's raw input)."""
    parts = [step.content for step in turn.steps if step.role is TurnRole.USER]
    return " ".join(parts)


def label_id(turn: Turn) -> str:
    """Stable id for a corpus turn, independent of corpus rebuild settings."""
    return f"{turn.session_id}:{turn.turn_index}"


# ------------------------------------------------------------ funnel driver


def drive_funnel(
    corpus: Sequence[Turn],
    *,
    scorer: TurnScorer | None = None,
) -> ScoringPipeline:
    """Run the full scoring funnel over a corpus and return the drained pipeline.

    The pipeline's ``stats`` carry the cumulative F1-F3 telemetry (bytes in/out,
    rule hits, dropped reasons); buffered durable turns are read per session.
    """
    pipeline = ScoringPipeline(
        scorer=scorer if scorer is not None else TurnScorer(embedder=cast(Embedder, SyntheticEmbedder()))
    )
    for turn in corpus:
        pipeline.submit_turn(turn)
    for session_id in pipeline.sessions():
        pipeline.drain(session_id)
    return pipeline


def score_text(
    scorer: TurnScorer,
    text: str,
    *,
    stripper: Stripper | None = None,
) -> Durability:
    """Durability verdict for raw user text, after F1 stripping (the
    label-harness scoring path).

    Mirrors the production funnel: F1 runs before F2. A strip that leaves the
    empty string scores disposable, because the raw content was host
    scaffolding only — no user speech survived to persist.
    """
    resolved = stripper if stripper is not None else Stripper(RULESET_V1)
    stripped, _ = resolved.strip_text(text, ContentTarget.MESSAGE_TEXT)
    if not stripped.strip():
        return Durability.DISPOSABLE
    turn = Turn(
        turn_index=0,
        session_id="label-harness",
        profile_id="prof-benchmark",
        host=HostId.GENERIC,
        started_at=0.0,
        steps=[TurnStep(role=TurnRole.USER, content=stripped)],
    )
    return scorer.score_turn(turn).durability.durability


# ------------------------------------------------------------ durability labels


@dataclass(frozen=True)
class DurabilityLabelRow:
    """One reviewable label-set entry.

    ``prelabel`` is machine-generated; ``label`` is the human verdict, empty
    until reviewed. The harness diffs against ``label`` when filled and falls
    back to ``prelabel`` otherwise, and says so in its report.
    """

    id: str
    text: str
    prelabel: str
    prelabel_reason: str
    label: str = ""


def load_labels(path: Path) -> list[DurabilityLabelRow]:
    rows: list[DurabilityLabelRow] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped:
            rows.append(DurabilityLabelRow(**json.loads(stripped)))
    return rows


def save_labels(path: Path, rows: Sequence[DurabilityLabelRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(_row_json(row) for row in rows)
    path.write_text(body + ("\n" if body else ""), encoding="utf-8")


def _row_json(row: DurabilityLabelRow) -> str:
    return json.dumps(
        {
            "id": row.id,
            "text": row.text,
            "prelabel": row.prelabel,
            "prelabel_reason": row.prelabel_reason,
            "label": row.label,
        },
        ensure_ascii=False,
    )


def sample_labels(
    corpus: Sequence[Turn],
    n: int,
    seed: int,
    *,
    existing: Sequence[DurabilityLabelRow] = (),
) -> list[DurabilityLabelRow]:
    """Deterministically sample n user-text turns for human labeling.

    Rows already present in ``existing`` are preserved verbatim (their human
    ``label`` survives a rebuild); only missing ids get fresh prelabels.
    """
    by_id = {row.id: row for row in existing}
    candidates: list[tuple[str, str]] = []
    seen: set[str] = set()
    for turn in corpus:
        tid = label_id(turn)
        if tid in seen:
            continue
        seen.add(tid)
        text = user_text(turn)
        if not text.strip():
            continue
        candidates.append((tid, text))
    rng = random.Random(seed)
    picked = rng.sample(candidates, min(n, len(candidates)))
    rows = [by_id[tid] if tid in by_id else _fresh_label(tid, text) for tid, text in picked]
    return sorted(rows, key=lambda row: row.id)


def _fresh_label(tid: str, text: str) -> DurabilityLabelRow:
    return DurabilityLabelRow(
        id=tid,
        text=text,
        prelabel=prelabel(text),
        prelabel_reason=prelabel_reason(text),
    )


# ------------------------------------------------------------ prelabel heuristic
#
# A documented, deterministic heuristic independent of the TurnScorer under
# test. Durable signals (preference / decision / habit-rule / stance /
# abstraction) take precedence; disposable signals (venting / interjection /
# time-scoped) reject otherwise-markerless turns; the default is the
# conservative reject — capture is a refusal system first (design/01 stage 1).

_DURABLE_REASONS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "pref-marker",
        re.compile(
            r"我[^。！？\n]{0,12}?(?:喜欢|爱|偏爱|欣赏|倾向于|偏好|认可|爱用|推崇)|"
            r"\bI (?:like|love|prefer|enjoy|value|favour|favor)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "decision-marker",
        re.compile(r"以后|决定|打算|改为|改成|换用|弃用|一律用|统一用|从今往后|下次开始"),
    ),
    (
        "rule-marker",
        re.compile(r"每次|都必须|都要|一定得|绝不能|再也不会|从不|一律"),
    ),
    (
        "stance-marker",
        re.compile(r"我认为|我觉得|相信|坚持|反对|支持|建议|推荐|希望|看重|在意"),
    ),
    (
        "abstraction-noun",
        re.compile(r"原则|方法论|习惯|规则|流程|方案|标准|规范|偏好|模板|套路|机制|准则|风格", re.IGNORECASE),
    ),
)

_DISPOSABLE_REASONS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "venting-marker",
        re.compile(
            r"烦死|气死|累死|受够了|崩溃|无语|服了|太难|太累|好烦|很烦|真烦|心烦|糟心|"
            r"麻了|破防|emo|恶心|annoying|annoyed|terrible|awful|hate|despise|ugh|"
            r"so tired|so done|so (?:stressed|frustrating)",
            re.IGNORECASE,
        ),
    ),
    (
        "interjection",
        re.compile(
            r"(?i)^\s*(?:好的|好|嗯|哦|噢|好吧|行|行吧|收到|知道了|明白|对|没错|可以|"
            r"哈哈|ok|okay|fine|great|thanks|sure|yep|yeah|yes|no|cool|noted|"
            r"了解|没问题)\s*[。！？!?.,…]*\s*$"
        ),
    ),
    (
        "time-scoped",
        re.compile(
            r"今天|明天|昨天|后天|上周|下周|这个月|等下|待会|现在|马上|下午|上午|晚上|\d+[:：]\d+|\d{1,2}\s*(?:号|日|点钟)"
        ),
    ),
)


def prelabel(text: str) -> str:
    """Durable/disposable verdict from the documented heuristic."""
    return _prelabel_match(text)[0]


def prelabel_reason(text: str) -> str:
    """Which heuristic family fired (or ``no-signal`` for the default reject)."""
    return _prelabel_match(text)[1]


def _prelabel_match(text: str) -> tuple[str, str]:
    for reason, pattern in _DURABLE_REASONS:
        if pattern.search(text):
            return "durable", reason
    for reason, pattern in _DISPOSABLE_REASONS:
        if pattern.search(text):
            return "disposable", reason
    return "disposable", "no-signal"


# ------------------------------------------------------------ evaluation


@dataclass(frozen=True)
class PrecisionReport:
    """Durability-class metrics over a labeled set.

    Confusion is counted against the per-row reference (human ``label`` when
    filled, else ``prelabel``); ``used_prelabels`` flags that any row fell back.
    ``human_precision`` is precision over human-labeled rows only — the NFR-1.3
    number — or None when no row has a human label.
    """

    total: int
    human_labeled: int
    prelabel_fallback: int
    tp: int
    fp: int
    tn: int
    fn: int
    precision: float
    recall: float
    accuracy: float
    human_precision: float | None
    used_prelabels: bool
    mismatches: tuple[tuple[str, str, str], ...]  # (id, verdict, reference)


def evaluate_durability(
    rows: Sequence[DurabilityLabelRow],
    scorer: TurnScorer,
    *,
    stripper: Stripper | None = None,
) -> PrecisionReport:
    """Score every labeled text and compute durable-class precision/recall.

    Each row's text is stripped with the ruleset first (default RULESET_V1), so
    host-injected artifacts do not leak into the F2 verdict; a fully-stripped
    row scores disposable (no user speech survived).
    """
    resolved = stripper if stripper is not None else Stripper(RULESET_V1)
    tp = fp = tn = fn = 0
    human_labeled = 0
    hp_tp = 0
    hp_total_positive = 0
    mismatches: list[tuple[str, str, str]] = []
    for row in rows:
        reference = row.label if row.label in ("durable", "disposable") else row.prelabel
        verdict = score_text(scorer, row.text, stripper=resolved).value
        if row.label in ("durable", "disposable"):
            human_labeled += 1
            if reference == "durable":
                hp_total_positive += 1
                if verdict == "durable":
                    hp_tp += 1
        if verdict == reference:
            if verdict == "durable":
                tp += 1
            else:
                tn += 1
        elif verdict == "durable":
            fp += 1
            mismatches.append((row.id, verdict, reference))
        else:
            fn += 1
            mismatches.append((row.id, verdict, reference))
    positive = tp + fp
    relevant = tp + fn
    precision = (tp / positive) if positive else 0.0
    recall = (tp / relevant) if relevant else 0.0
    total = len(rows)
    human_precision = (hp_tp / hp_total_positive) if hp_total_positive else None
    return PrecisionReport(
        total=total,
        human_labeled=human_labeled,
        prelabel_fallback=total - human_labeled,
        tp=tp,
        fp=fp,
        tn=tn,
        fn=fn,
        precision=precision,
        recall=recall,
        accuracy=(tp + tn) / total if total else 0.0,
        human_precision=human_precision,
        used_prelabels=human_labeled < total,
        mismatches=tuple(mismatches),
    )
