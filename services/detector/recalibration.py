"""The recalibration job: dispositions become detector tuning.

This is the last hop of F10, and without it the loop is not closed. Capturing
a human's verdict and then never letting it change anything is theatre — the
analyst clicks "false positive", the detector fires on the same column
tomorrow, and they stop clicking.

What a verdict means to a detector, which is not the same as what it means to
a person:

    correct_cause     a real incident. Those cycles must be *excluded* from
                      the calibration set, or the detector learns that the
                      regression is normal and goes blind to the next one.
    false_positive    the detector was wrong. Those cycles belong *in* the
                      calibration set, which pushes the threshold up exactly
                      where it was too low.
    expected          real movement, correctly seen, not worth alerting on —
                      an announced backfill. Same treatment as a false
                      positive for calibration, but recorded separately
                      because the distinction matters when reading precision.
    duplicate         says nothing about the detector. Ignored here.
    wrong_cause       detection was right, the reasoning was wrong. Counts as
                      a true positive for the detector and feeds the agent
                      eval set instead.

The job writes per-cohort performance into ``detector_perf`` and raises a
platform-health incident when a cohort's observed false-positive rate drifts
past twice its target. A monitoring system that notices its own calibration
has drifted, before a human does, is the strongest single claim this project
makes — and it is a few dozen lines of counting.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from datasentry_core.clock import SYSTEM_CLOCK, Clock, iso
from datasentry_core.config import Settings
from datasentry_core.config import settings as get_settings
from datasentry_core.enums import IncidentStatus, Verdict
from datasentry_core.ids import incident_id as make_incident_id
from datasentry_core.logging import get_logger
from datasentry_core.models import Incident

log = get_logger("recalibration")

# Verdicts that mean "the detector was right to fire".
TRUE_POSITIVE = {Verdict.CORRECT_CAUSE, Verdict.WRONG_CAUSE}
# Verdicts that mean "this cycle is normal and belongs in the calibration set".
FALSE_POSITIVE = {Verdict.FALSE_POSITIVE, Verdict.EXPECTED}


@dataclass
class CohortOutcome:
    """Two rates, and they are not the same number.

    ``observed_fpr`` is the conformal quantity: false alarms per *scored
    cycle*. That is what alpha is a target for, so it is the only thing that
    can honestly be compared against it. Its denominator is how many times the
    detector looked, which detection records and disposition cannot know.

    ``precision`` is false alarms per *raised incident*. That is the number
    that decides whether anyone keeps the product turned on, and it is what a
    human's disposition actually measures.

    Conflating them is easy and wrong: dividing false positives by disposed
    incidents gives 1 - precision, which against a per-cycle alpha compares a
    number near 0.3 with a number near 0.01 and declares drift on a detector
    that is behaving perfectly.
    """

    cohort: str
    signal: str
    target_fpr: float
    true_positives: int = 0
    false_positives: int = 0
    disposed: int = 0
    scored_cycles: int = 0

    @property
    def observed_fpr(self) -> float:
        """False alarms per scored cycle -- comparable with the target."""
        if not self.scored_cycles:
            return 0.0
        return self.false_positives / self.scored_cycles

    @property
    def precision(self) -> float:
        total = self.true_positives + self.false_positives
        return (self.true_positives / total) if total else 0.0

    @property
    def drifted(self) -> bool:
        # Three conditions. Enough dispositions to have an opinion, enough
        # scored cycles for a rate to mean anything, and a rate past twice
        # target -- because a distribution-free guarantee is approximate once
        # the weighting and the breach exclusion bend it, and 1.5x is inside
        # that bend.
        return (
            self.disposed >= 10
            and self.scored_cycles >= 200
            and self.observed_fpr > 2.0 * self.target_fpr
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "cohort": self.cohort,
            "signal": self.signal,
            "target_fpr": round(self.target_fpr, 6),
            "observed_fpr": round(self.observed_fpr, 6),
            "precision": round(self.precision, 4),
            "true_positives": self.true_positives,
            "false_positives": self.false_positives,
            "disposed": self.disposed,
            "scored_cycles": self.scored_cycles,
            "drifted": self.drifted,
        }


@dataclass
class RecalibrationReport:
    dispositions: int = 0
    anomalies_relabelled: int = 0
    cohorts: list[CohortOutcome] = field(default_factory=list)
    drifted: list[str] = field(default_factory=list)
    health_incident: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "dispositions": self.dispositions,
            "anomalies_relabelled": self.anomalies_relabelled,
            "cohorts": [c.as_dict() for c in self.cohorts],
            "drifted": self.drifted,
            "health_incident": self.health_incident,
        }


class RecalibrationJob:
    def __init__(self, store: Any, *, config: Settings | None = None,
                 clock: Clock | None = None,
                 identity: str = "service:datasentry-recalibration") -> None:
        self.store = store
        self.settings = config or get_settings()
        self.clock = clock or SYSTEM_CLOCK
        self.identity = identity

    # -- main path ----------------------------------------------------------

    def run(self, *, since: datetime | None = None) -> RecalibrationReport:
        now = self.clock.now()
        since = since or (now - timedelta(days=30))
        report = RecalibrationReport()

        labels = self._labelled_anomalies(since)
        report.dispositions = len({a["incident_id"] for a in labels})
        report.anomalies_relabelled = len(labels)

        outcomes = self._tally(labels)
        report.cohorts = sorted(outcomes.values(), key=lambda c: (c.cohort, c.signal))
        self._persist(report.cohorts, now)

        report.drifted = [c.cohort + "/" + c.signal for c in report.cohorts if c.drifted]
        if report.drifted:
            report.health_incident = self._raise_health_incident(report, now)

        self.store.audit(
            actor=self.identity, actor_kind="service", action="detector.recalibrated",
            target="detector_perf",
            detail={"dispositions": report.dispositions,
                    "cohorts": len(report.cohorts),
                    "drifted": report.drifted},
            at=now,
        )
        log.event("recalibration complete", dispositions=report.dispositions,
                  cohorts=len(report.cohorts), drifted=len(report.drifted))
        return report

    # -- reading the labels -------------------------------------------------

    def _labelled_anomalies(self, since: datetime) -> list[dict[str, Any]]:
        """Join each disposition down onto the anomalies it judged.

        A verdict is given on an incident, but calibration happens per column
        and per signal — so the label has to be pushed down to every anomaly
        the incident contained before it means anything to a detector.
        """
        rows = self.store.query(
            """
            SELECT d.incident_id, d.verdict, a.anomaly_id, a.asset_id, a.column_name,
                   a.signal, a.detector_id, a.score, a.detected_at,
                   ac.data_type, s.tier
            FROM dispositions d
            JOIN anomalies a ON a.incident_id = d.incident_id
            JOIN assets s ON s.asset_id = a.asset_id
            LEFT JOIN asset_columns ac
                   ON ac.asset_id = a.asset_id AND ac.column_name = a.column_name
            WHERE d.created_at >= ?
            """,
            (iso(since),),
        )
        return [dict(r) for r in rows]

    @staticmethod
    def _cohort_of(row: dict[str, Any]) -> str:
        data_type = (row.get("data_type") or "TABLE").split("(")[0].upper()
        return data_type + "/t" + str(row.get("tier") or 3)

    def _tally(self, labels: list[dict[str, Any]]) -> dict[tuple[str, str], CohortOutcome]:
        outcomes: dict[tuple[str, str], CohortOutcome] = {}
        for row in labels:
            cohort = self._cohort_of(row)
            signal = str(row["signal"])
            key = (cohort, signal)
            entry = outcomes.get(key)
            if entry is None:
                tier = int(row.get("tier") or 3)
                entry = CohortOutcome(
                    cohort=cohort, signal=signal,
                    target_fpr=self.settings.alpha_for_tier(tier),
                )
                outcomes[key] = entry

            verdict = row["verdict"]
            if verdict in {str(v) for v in TRUE_POSITIVE}:
                entry.true_positives += 1
                entry.disposed += 1
            elif verdict in {str(v) for v in FALSE_POSITIVE}:
                entry.false_positives += 1
                entry.disposed += 1
            # A duplicate says nothing about the detector, so it is not
            # counted at all. Counting it either way dilutes both rates.

        # The FPR denominator comes from detection, not from disposition: how
        # many cycles the detector actually scored for this cohort. Without it
        # there is no honest rate to compare against alpha.
        for (cohort, signal), entry in outcomes.items():
            entry.scored_cycles = self._scored_cycles(cohort, signal)
        return outcomes

    def _scored_cycles(self, cohort: str, signal: str) -> int:
        row = self.store.one(
            "SELECT observations FROM detector_perf "
            "WHERE cohort = ? AND signal = ? ORDER BY window_label DESC LIMIT 1",
            (cohort, signal),
        )
        return int(row["observations"]) if row and row["observations"] else 0

    # -- writing the result -------------------------------------------------

    def _persist(self, cohorts: list[CohortOutcome], now: datetime) -> None:
        window = now.strftime("%G-W%V")
        for entry in cohorts:
            self.store.execute(
                """
                INSERT OR REPLACE INTO detector_perf
                  (detector_id, signal, cohort, window_label, true_positives,
                   false_positives, false_negatives, observations, target_fpr, observed_fpr)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                ("ensemble", entry.signal, entry.cohort, window,
                 entry.true_positives, entry.false_positives, 0,
                 entry.scored_cycles, entry.target_fpr, entry.observed_fpr),
            )

    @staticmethod
    def record_scored_cycles(store: Any, reports: list[dict[str, Any]],
                             now: datetime) -> int:
        """Write the FPR denominator that only detection can know.

        Called after a detection pass with DetectorEnsemble.calibration_reports().
        The recalibration job reads these back; without them it has labels and
        no denominator, and a rate with no denominator is a number with no
        meaning.
        """
        window = now.strftime("%G-W%V")
        written = 0
        for entry in reports:
            observations = int(entry.get("observations") or 0)
            if not observations:
                continue
            existing = store.one(
                "SELECT true_positives, false_positives FROM detector_perf "
                "WHERE cohort = ? AND signal = ? AND window_label = ?",
                (entry["cohort"], entry["signal"], window),
            )
            store.execute(
                """
                INSERT OR REPLACE INTO detector_perf
                  (detector_id, signal, cohort, window_label, true_positives,
                   false_positives, false_negatives, observations, target_fpr, observed_fpr)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                ("ensemble", entry["signal"], entry["cohort"], window,
                 int(existing["true_positives"]) if existing else 0,
                 int(existing["false_positives"]) if existing else 0,
                 0, observations, float(entry.get("target_fpr") or 0.0),
                 float(entry.get("observed_fpr") or 0.0)),
            )
            written += 1
        return written

    def _raise_health_incident(self, report: RecalibrationReport,
                               now: datetime) -> str | None:
        """Drifted calibration is an incident about the monitoring system itself.

        Raised through the same machinery as a data incident on purpose: one
        inbox, one lifecycle, one place a human looks. A platform problem
        routed to a separate channel is a platform problem nobody reads.
        """
        asset = self.store.one("SELECT asset_id FROM assets ORDER BY tier LIMIT 1")
        if asset is None:
            return None

        worst = max(
            (c for c in report.cohorts if c.drifted),
            key=lambda c: c.observed_fpr / max(c.target_fpr, 1e-9),
        )
        incident_id = make_incident_id("datasentry.calibration", worst.signal,
                                       now.strftime("%Y%m%dT%H"))
        if self.store.get_incident(incident_id) is not None:
            return incident_id

        title = (
            "Detector calibration drifted on " + worst.cohort + " / " + worst.signal
            + " — observed FPR " + format(worst.observed_fpr, ".3f")
            + " against a target of " + format(worst.target_fpr, ".3f")
        )
        self.store.upsert_incident(Incident(
            incident_id=incident_id,
            status=IncidentStatus.DETECTED,
            severity=2,
            title=title,
            root_asset_id=asset["asset_id"],
            root_column=None,
            anomaly_count=0,
            narrative=(
                str(len(report.drifted)) + " cohort(s) are firing above twice their "
                "target false-positive rate, measured against human dispositions "
                "rather than against the detector's own opinion of itself. The "
                "thresholds for those cohorts need re-deriving; until they are, "
                "their alerts are costing more attention than they are worth."
            ),
            first_detected_at=now,
            created_at=now,
        ))
        log.event("calibration drift raised", incident=incident_id[:8],
                  cohort=worst.cohort, signal=worst.signal,
                  observed=round(worst.observed_fpr, 4))
        return incident_id

    # -- reading it back ----------------------------------------------------

    def latest(self) -> list[dict[str, Any]]:
        rows = self.store.query(
            """
            SELECT * FROM detector_perf
            ORDER BY window_label DESC, observed_fpr DESC
            LIMIT 200
            """
        )
        by_key: dict[tuple[str, str], dict[str, Any]] = {}
        for row in rows:
            key = (row["cohort"], row["signal"])
            if key in by_key:
                continue
            total = row["true_positives"] + row["false_positives"]
            by_key[key] = {
                "cohort": row["cohort"],
                "signal": row["signal"],
                "window": row["window_label"],
                "target_fpr": row["target_fpr"],
                "observed_fpr": row["observed_fpr"],
                "true_positives": row["true_positives"],
                "false_positives": row["false_positives"],
                "disposed": total,
                "scored_cycles": row["observations"],
                "precision": round(row["true_positives"] / total, 4) if total else None,
                "drifted": (
                    total >= 10
                    and row["observations"] >= 200
                    and row["observed_fpr"] > 2.0 * row["target_fpr"]
                ),
            }
        return list(by_key.values())


def suggested_alpha(entry: CohortOutcome, floor: float = 0.0005) -> float:
    """Where a drifted cohort's target should move to.

    Deliberately conservative: halve the target rather than solving for the
    observed rate. The observed rate is measured on a handful of dispositions
    and solving to it would overshoot on noise, which trades a precision
    problem for a recall problem — and a missed regression costs far more than
    an extra look.
    """
    if not entry.drifted:
        return entry.target_fpr
    return max(floor, entry.target_fpr / 2.0)
