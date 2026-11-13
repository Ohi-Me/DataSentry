"""The evaluation harness and its CI gates.

Runs the whole loop over several seeds and scores it against the ground truth
the fault harness recorded at injection time. Two properties make the numbers
worth anything:

* **The harness never touches the detectors.** Ground truth is written when a
  fault is injected and read only when scoring. Nothing in the detection or
  reasoning path can see it.
* **It runs over several worlds, not one.** A single seed measures one
  arrangement of tables; the variance across seeds is what tells you whether a
  change was an improvement or a coincidence.

``ds eval`` prints a gate table and exits non-zero on a regression, which is
what makes this a gate rather than a report. Baselines live in
BENCHMARKS.md and are appended to, never overwritten — watching precision go
0.31 -> 0.44 -> 0.61 across weeks is a better artefact than one final number.
"""

from __future__ import annotations

import json
import statistics
import time
from dataclasses import dataclass, field
from typing import Any

from tools.pipeline.demo import DEFAULT_FAULTS, score_run
from tools.pipeline.pipeline import Pipeline

from datasentry_core.config import REPO_ROOT, settings
from datasentry_core.logging import paint
from datasentry_core.store import ControlPlane

DIM = "38;5;245"
GOOD = "38;5;114"
WARN = "38;5;214"
BAD = "38;5;203"
ACCENT = "38;5;80"

RESULTS = REPO_ROOT / "evals" / "results"


@dataclass
class Gate:
    key: str
    label: str
    target: float
    lower_is_better: bool = False
    # Gates that must hold for the project to mean what it claims. A regression
    # in one of these fails the build; the rest are reported and tracked.
    blocking: bool = False

    def passes(self, value: float) -> bool:
        return value <= self.target if self.lower_is_better else value >= self.target


GATES = [
    Gate("recall", "Detection recall", 0.90),
    Gate("precision", "Incident precision", 0.75),
    Gate("decoy_fpr", "Decoy false-positive rate", 0.10, lower_is_better=True, blocking=True),
    Gate("root_accuracy", "Root-asset accuracy", 0.85, blocking=True),
    Gate("cause_accuracy", "Root-cause accuracy", 0.80),
    Gate("hallucination_rate", "Hallucination rate", 0.02, lower_is_better=True, blocking=True),
    Gate("blast_recall", "Blast-radius recall", 0.95),
    Gate("median_cost", "Median cost per incident", 0.15, lower_is_better=True, blocking=True),
]


@dataclass
class SeedResult:
    seed: int
    scores: dict[str, Any] = field(default_factory=dict)
    seconds: float = 0.0


# Each fault reserves its connected neighbourhood so faults stay independently
# scoreable. Below roughly sixty tables there is not enough warehouse to hold
# eight of them apart, the reservations collide, and every metric measures the
# collision rather than the system.
MIN_TABLES = 60


def run_seed(seed: int, *, tables: int, days: int, faults: list[str],
             provider: str | None = None) -> SeedResult:
    if tables < MIN_TABLES:
        raise ValueError(
            "a " + str(tables) + "-table world cannot hold " + str(len(faults))
            + " independent faults; use --tables " + str(MIN_TABLES) + " or more"
        )
    started = time.perf_counter()
    db = REPO_ROOT / "var" / ("eval-" + str(seed) + ".db")
    if db.exists():
        db.unlink()
    store = ControlPlane(db)
    pipeline = Pipeline(store)

    pipeline.bootstrap(tables=tables, days=days, seed=seed)
    pipeline.detect(cycles_back=4)  # settle the calibration on a clean warehouse

    for index, fault_type in enumerate(faults):
        try:
            pipeline.inject(fault_type, at_cycle=pipeline.state.cursor - 2 + index % 2)
        except RuntimeError:
            continue

    pipeline.advance(cycles=18)
    pipeline.detect(cycles_back=18)
    pipeline.correlate()
    pipeline.investigate(limit=20, provider=provider)
    pipeline.execute_approved()
    pipeline.advance(cycles=3)
    pipeline.detect(cycles_back=3)
    pipeline.verify()

    return SeedResult(seed=seed, scores=score_run(store),
                      seconds=time.perf_counter() - started)


def run_evaluation(*, rebuild: bool = False, seeds: int = 3, tables: int = 80,
                   days: int = 12, provider: str | None = None) -> int:
    base = settings().seed
    seed_list = [base + i * 977 for i in range(max(1, seeds))]
    results: list[SeedResult] = []

    from datasentry_agent.providers import registry

    reasoner_name = provider or registry.resolve()
    print(paint("  running " + str(len(seed_list)) + " worlds of " + str(tables)
                + " tables", DIM))
    print(paint("  reasoner: " + reasoner_name, DIM))
    print()

    for seed in seed_list:
        result = run_seed(seed, tables=tables, days=days, faults=DEFAULT_FAULTS,
                          provider=provider)
        results.append(result)
        print(
            "  " + paint("seed " + str(seed), DIM)
            + "  recall " + format(result.scores["recall"], ".2f")
            + "  precision " + format(result.scores["precision"], ".2f")
            + "  cause " + format(result.scores["cause_accuracy"], ".2f")
            + "  " + paint(format(result.seconds, ".0f") + "s", DIM)
        )

    print()
    summary = aggregate(results)
    failures = report(summary)

    RESULTS.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%S")
    (RESULTS / ("eval-" + stamp + ".json")).write_text(
        json.dumps({"reasoner": reasoner_name, "tables": tables,
                    "seeds": [r.__dict__ for r in results], "summary": summary},
                   indent=2, default=str),
        encoding="utf-8",
    )
    append_benchmarks(summary, len(seed_list), tables)

    if failures:
        print()
        print(paint("  " + str(len(failures)) + " blocking gate(s) failed: "
                    + ", ".join(failures), BAD))
        return 1
    print()
    print(paint("  every blocking gate holds", GOOD))
    return 0


def aggregate(results: list[SeedResult]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for gate in GATES:
        values = [float(r.scores.get(gate.key, 0.0)) for r in results]
        summary[gate.key] = {
            "mean": round(statistics.fmean(values), 4),
            "min": round(min(values), 4),
            "max": round(max(values), 4),
            # Spread across seeds is the number that says whether a change is
            # real. A metric that moves 0.2 between worlds cannot be tuned by
            # 0.05 and called an improvement.
            "stdev": round(statistics.pstdev(values), 4) if len(values) > 1 else 0.0,
            "target": gate.target,
            "lower_is_better": gate.lower_is_better,
            "blocking": gate.blocking,
        }
    summary["_seeds"] = [r.seed for r in results]
    summary["_seconds"] = round(sum(r.seconds for r in results), 1)
    return summary


def report(summary: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    width = max(len(g.label) for g in GATES) + 2

    print("  " + paint("GATE".ljust(width) + "MEAN    RANGE          TARGET   STATUS", DIM))
    for gate in GATES:
        entry = summary[gate.key]
        mean = entry["mean"]
        ok = gate.passes(mean)
        if not ok and gate.blocking:
            failures.append(gate.key)
        tone = GOOD if ok else (BAD if gate.blocking else WARN)
        label = "PASS" if ok else ("FAIL" if gate.blocking else "below")
        print(
            "  " + gate.label.ljust(width)
            + format(mean, ".3f").ljust(8)
            + (format(entry["min"], ".2f") + "-" + format(entry["max"], ".2f")).ljust(15)
            + ((">= " if not gate.lower_is_better else "<= ")
               + format(gate.target, ".2f")).ljust(9)
            + paint(label, tone)
            + (paint("  (tracked, not blocking)", DIM) if not gate.blocking and not ok else "")
        )
    print()
    print(paint("  spread across seeds: "
                + ", ".join(g.key + " +/-" + format(summary[g.key]["stdev"], ".2f")
                            for g in GATES if summary[g.key]["stdev"] > 0.01), DIM))
    return failures


MARKER = "<!-- ds-eval-runs -->"


def append_benchmarks(summary: dict[str, Any], seeds: int, tables: int) -> None:
    """Append into the run table, never overwrite. The trend is the artefact.

    Rows go in directly under a marker rather than at the end of the file, so
    the hand-written narrative below keeps its meaning instead of being slowly
    pushed under a wall of generated rows.
    """
    path = REPO_ROOT / "BENCHMARKS.md"
    row = (
        "| " + time.strftime("%Y-%m-%d %H:%M") + " | " + str(seeds) + " | "
        + str(tables) + " | "
        + " | ".join(format(summary[g.key]["mean"], ".3f") for g in GATES)
        + " |"
    )
    newline = chr(10)
    header = (
        MARKER + newline * 2
        + "| run | seeds | tables | "
        + " | ".join(g.label.lower() for g in GATES) + " |" + newline
        + "|---|---|---|" + "---|" * len(GATES) + newline
    )

    if not path.exists():
        path.write_text(header + row + newline, encoding="utf-8")
        return

    text = path.read_text(encoding="utf-8")
    if MARKER not in text:
        text = text.rstrip() + newline * 2 + "## Automated runs" + newline * 2 + header
    lines = text.split(newline)
    index = next(i for i, line in enumerate(lines) if line.strip() == MARKER)
    # Skip the blank line, the header row and the separator rule.
    lines.insert(index + 4, row)
    path.write_text(newline.join(lines), encoding="utf-8")
