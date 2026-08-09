"""Build and report on the durability label set v1 (NFR-1.3).

Usage:

    python scripts/label_durability.py build     # sample prelabeled rows
    python scripts/label_durability.py report    # score rows and print the harness report
    python scripts/label_durability.py           # build, then report

The label file (``--labels``, default ``.bench/durability_labels.jsonl``) is
hand-editable: fill the ``label`` field with ``durable`` or ``disposable`` to
pin a human verdict. The harness differs against the human ``label`` when
filled, else falls back to ``prelabel`` and SAYS SO in its output — the
auto-labeled precision is a smoke signal, not the NFR-1.3 acceptance number.
The NFR-1.3 ``>= 0.9`` precision gate applies to human-labeled rows only.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from mnemoseed.capture.benchmark import (
    DurabilityLabelRow,
    evaluate_durability,
    load_corpus,
    load_labels,
    sample_labels,
    save_labels,
)
from mnemoseed.capture.scorer import TurnScorer
from mnemoseed.storage.drivers.synthetic_embedder import SyntheticEmbedder

DEFAULT_OUT = Path(".bench/capture_corpus.jsonl")
DEFAULT_LABELS = Path(".bench/durability_labels.jsonl")
DEFAULT_SAMPLE = 100
DEFAULT_SEED = 0


def build(args: argparse.Namespace) -> None:
    corpus = load_corpus(args.corpus)
    existing = load_labels(args.labels) if args.labels.exists() else []
    rows = sample_labels(corpus, args.sample, args.seed, existing=existing)
    save_labels(args.labels, rows)
    human = sum(1 for row in rows if row.label in ("durable", "disposable"))
    print(f"label set {len(rows)} rows -> {args.labels} ({human} human-labeled, preserved)")
    print(
        "durable={} disposable={}".format(
            sum(1 for row in rows if row.prelabel == "durable"),
            sum(1 for row in rows if row.prelabel == "disposable"),
        )
    )


def _print_report(rows: list[DurabilityLabelRow], report) -> None:
    gate = (
        "PASS"
        if report.human_precision is not None and report.human_precision >= 0.9
        else "SKIP/no human labels"
    )
    print("Durability harness report")
    print("=========================")
    print(f"rows:              {report.total}")
    print(f"human labeled:     {report.human_labeled}")
    print(f"prelabel fallback: {report.prelabel_fallback}  (smoke signal only, per NFR-1.3)")
    print(f"confusion (durable): TP={report.tp} FP={report.fp} TN={report.tn} FN={report.fn}")
    print(f"precision:  {report.precision:.3f}")
    print(f"recall:     {report.recall:.3f}")
    print(f"accuracy:   {report.accuracy:.3f}")
    print(f"human precision (NFR-1.3 gate >= 0.9): {report.human_precision}  [{gate}]")
    print(f"used prelabels:    {report.used_prelabels}")
    if report.mismatches:
        print("\nmismatches (id, verdict, reference):")
        for tid, verdict, reference in report.mismatches:
            row = next(r for r in rows if r.id == tid)
            print(f"  {tid}  verdict={verdict} reference={reference}  text={row.text!r}")
    print(f"\nnote: {report.prelabel_fallback} rows fell back to prelabel because no human label")
    print("is filled yet; the >= 0.9 gate applies once a human reviews the label set.")


def report(args: argparse.Namespace) -> None:
    if not args.labels.exists():
        print(f"error: label set not found: {args.labels} (run build first)", file=sys.stderr)
        return
    rows = load_labels(args.labels)
    if not rows:
        print(f"error: label set is empty: {args.labels}", file=sys.stderr)
        return
    scorer = TurnScorer(embedder=SyntheticEmbedder())
    _print_report(rows, evaluate_durability(rows, scorer))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build and report the durability label set.")
    parser.add_argument("--corpus", type=Path, default=DEFAULT_OUT, help="corpus JSONL path")
    parser.add_argument("--labels", type=Path, default=DEFAULT_LABELS, help="label set JSONL path")
    parser.add_argument("--sample", type=int, default=DEFAULT_SAMPLE, help="target labeled-row count")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="sampling seed")
    parser.add_argument("actions", nargs="*", choices=["build", "report"], default=["build", "report"])
    args = parser.parse_args(argv)
    args.corpus = args.corpus.expanduser()
    args.labels = args.labels.expanduser()
    if "build" in args.actions:
        if not args.corpus.exists():
            print(
                f"error: corpus not found: {args.corpus} (run scripts/build_capture_corpus.py first)",
                file=sys.stderr,
            )
            return 2
        build(args)
    if "report" in args.actions:
        report(args)
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    raise SystemExit(main())
