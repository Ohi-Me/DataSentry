"""Sweep the practical-significance floor and measure what it costs.

The floor exists to stop a series with almost no natural variation from
alerting on a movement too small to matter. There are several defensible ways
to set it and they are not distinguishable by argument, so this measures them
against ground truth instead.

    py tools/design/floor_sweep.py

Configurations:

    constant    the hand-set per-signal table in config.py
    q995        the cohort's 99.5th percentile
    q900        the cohort's 90th percentile
    median      the cohort's median, used to floor the threshold rather than
                as a second bar

The sweep deliberately stops after investigate: no approval, no remediation,
no second detection pass. Those steps change what the later cycles look like,
which is the right behaviour for a demo and the wrong behaviour for a
controlled comparison — the point here is to vary one knob and hold the rest
still. Numbers from this tool are therefore not comparable with numbers from
ds demo; they are only comparable with each other.

The last one is the only formulation that asks a *different* question from
the conformal threshold. The others are drawn from the same distribution the
threshold comes from, which makes them a second threshold at a different
quantile rather than an independent check -- and a second threshold on the
same evidence only ever costs recall.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
for path in (REPO_ROOT, REPO_ROOT / "packages"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from tools.pipeline.demo import DEFAULT_FAULTS, score_run  # noqa: E402
from tools.pipeline.pipeline import Pipeline  # noqa: E402

from datasentry_core.config import REPO_ROOT as CFG_ROOT  # noqa: E402
from datasentry_core.config import settings  # noqa: E402
from datasentry_core.store import ControlPlane  # noqa: E402

MODES = ["constant", "q995", "q900", "median"]
METRICS = ["recall", "precision", "decoy_fpr", "root_accuracy",
           "cause_accuracy", "blast_recall"]


def run(mode: str, seed: int, tables: int = 60, days: int = 12) -> dict:
    db = CFG_ROOT / "var" / ("sweep-" + mode + "-" + str(seed) + ".db")
    if db.exists():
        db.unlink()
    store = ControlPlane(db)
    pipeline = Pipeline(store)
    pipeline.ensemble.floor_mode = mode

    pipeline.bootstrap(tables=tables, days=days, seed=seed)
    pipeline.detect(cycles_back=4)
    for index, fault in enumerate(DEFAULT_FAULTS):
        try:
            pipeline.inject(fault, at_cycle=pipeline.state.cursor - 2 + index % 2)
        except RuntimeError:
            continue
    pipeline.advance(cycles=18)
    pipeline.detect(cycles_back=18)
    pipeline.correlate()
    pipeline.investigate(limit=20)
    return score_run(store)


def main() -> int:
    seed = settings().seed
    results: dict[str, dict] = {}
    for mode in MODES:
        started = time.perf_counter()
        results[mode] = run(mode, seed)
        print(f"  {mode:<9} {time.perf_counter() - started:5.0f}s  "
              + "  ".join(k + " " + format(results[mode][k], ".2f") for k in METRICS))

    print()
    print("  " + "metric".ljust(18) + "".join(m.ljust(11) for m in MODES))
    for metric in METRICS:
        row = "  " + metric.ljust(18)
        for mode in MODES:
            row += format(results[mode][metric], ".2f").ljust(11)
        print(row)

    out = REPO_ROOT / "evals" / "results" / "floor-sweep.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    print()
    print("  written to " + str(out.relative_to(REPO_ROOT)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
