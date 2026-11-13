"""Split conformal calibration.

This is the centre of the detection design, so it is worth stating the problem
precisely. A raw score threshold gives an unknown, per-column-varying
false-positive rate. Across 200k columns at even 0.1% you get 200 alerts a
cycle and the product is dead on arrival.

Split conformal fixes that without assuming anything about the score
distribution. Keep a rolling calibration set of nonconformity scores for a
(column, signal); for a target FPR alpha, the threshold is the
``ceil((n+1)(1-alpha))``-th order statistic. Under exchangeability the
marginal false-positive rate is at most alpha, distribution-free. That
guarantee is the reason this technique is right for 200k heterogeneous
columns, where normality is not merely unproven but obviously false.

Three adaptations to reality, each of which is a place the textbook result
bends:

* **Exchangeability is violated by trends.** Mitigated by scoring residuals
  rather than raw values (see D1) and by weighting calibration points with an
  exponential decay, half-life 14 days. The guarantee becomes approximate; we
  say so rather than pretending otherwise, and we measure the realised FPR.
* **Cold start.** A column with under 30 cycles has no usable calibration set,
  so it borrows a cohort threshold pooled across columns of the same type and
  tier, and its anomalies are marked ``provisional``.
* **Consecutive breaches.** Requiring k=2 consecutive breaching cycles takes
  the effective rate to roughly alpha^2 for independent noise while costing
  one cycle of latency.

``CalibrationReport`` closes the loop: observed FPR against target, per
cohort. A monitoring system that monitors its own calibration is the single
strongest thing in this project, and it is four lines of arithmetic.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from datasentry_detectors.stats import clamp


def cohort_key(cohort: str, signal: str) -> str:
    """Pool key for cold-start thresholds and the practical-significance floor.

    Both mechanisms borrow strength from columns that behave alike, and
    "alike" means the same data type, the same asset tier *and* the same
    detector — because a score is only meaningful in its own detector's units.
    """
    return cohort + "::" + signal


@dataclass(frozen=True)
class CalibrationPoint:
    score: float
    at: datetime
    labelled_incident: bool = False


@dataclass
class ConformalThreshold:
    key: str
    alpha: float
    threshold: float
    n: int
    provisional: bool
    cohort: str | None = None

    def breaches(self, score: float) -> bool:
        return score > self.threshold


@dataclass
class ConformalVerdict:
    score: float
    threshold: float
    p_value: float
    alpha: float
    breached: bool
    provisional: bool
    n_calibration: int

    @property
    def margin(self) -> float:
        """How far past the threshold, in threshold units. Drives severity."""
        if self.threshold <= 0:
            return 0.0
        return (self.score - self.threshold) / self.threshold


class ConformalCalibrator:
    """Per-(column, signal) thresholds with a cohort fallback."""

    def __init__(
        self,
        *,
        window: int = 200,
        half_life_days: float = 14.0,
        min_history: int = 30,
    ) -> None:
        self.window = window
        self.half_life_days = half_life_days
        self.min_history = min_history
        self._series: dict[str, list[CalibrationPoint]] = {}
        self._cohorts: dict[str, list[float]] = {}

    # -- ingestion ----------------------------------------------------------

    def observe(self, key: str, score: float, at: datetime, *,
                cohort: str | None = None, labelled_incident: bool = False) -> None:
        """Record a nonconformity score.

        Cycles a human labelled as a genuine incident are excluded from the
        calibration set. Leaving them in would teach the detector that the
        regression is normal, which is how monitoring systems quietly go
        blind after their first real outage.
        """
        if score != score or math.isinf(score):
            return
        bucket = self._series.setdefault(key, [])
        bucket.append(CalibrationPoint(score, at, labelled_incident))
        if len(bucket) > self.window * 2:
            del bucket[: len(bucket) - self.window]
        if cohort and not labelled_incident:
            # The cohort key must carry the signal. A nonconformity score is
            # only comparable within one detector family: D2 reports
            # -log10(tail probability) and D3 reports a distance in baseline
            # spreads, and pooling those into one list produces a threshold
            # that means nothing to either. Callers build the key with
            # cohort_key() below rather than passing a bare cohort.
            pool = self._cohorts.setdefault(cohort, [])
            pool.append(score)
            if len(pool) > self.window * 8:
                del pool[: len(pool) - self.window * 4]

    def history_size(self, key: str) -> int:
        return len(self._series.get(key, ()))

    # -- thresholds ---------------------------------------------------------

    def _weights(self, points: Sequence[CalibrationPoint], now: datetime) -> list[float]:
        if self.half_life_days <= 0:
            return [1.0] * len(points)
        lam = math.log(2.0) / (self.half_life_days * 86400.0)
        out = []
        for p in points:
            age = max(0.0, (now - p.at).total_seconds())
            out.append(math.exp(-lam * age))
        return out

    @staticmethod
    def _weighted_quantile(scores: Sequence[float], weights: Sequence[float], q: float) -> float:
        pairs = sorted(zip(scores, weights, strict=True), key=lambda sw: sw[0])
        total = sum(w for _, w in pairs)
        if total <= 0:
            return pairs[-1][0] if pairs else 0.0
        target = q * total
        cumulative = 0.0
        for score, weight in pairs:
            cumulative += weight
            if cumulative >= target:
                return score
        return pairs[-1][0]

    def threshold(self, key: str, alpha: float, now: datetime,
                  cohort: str | None = None) -> ConformalThreshold:
        points = [p for p in self._series.get(key, ()) if not p.labelled_incident]
        points = points[-self.window :]

        if len(points) >= self.min_history:
            weights = self._weights(points, now)
            scores = [p.score for p in points]
            n = len(scores)
            # Finite-sample correction: the (n+1)(1-alpha) order statistic is
            # what gives the coverage guarantee, not the plain empirical
            # quantile. At n=200, alpha=0.01 that is the difference between
            # the 198th and the 199th value, and the difference between
            # meeting the stated FPR and quietly missing it.
            level = min(1.0, math.ceil((n + 1) * (1.0 - alpha)) / n)
            thr = self._weighted_quantile(scores, weights, level)
            return ConformalThreshold(key, alpha, thr, n, provisional=False, cohort=cohort)

        pool = self._cohorts.get(cohort or "", [])
        if len(pool) >= self.min_history:
            n = len(pool)
            level = min(1.0, math.ceil((n + 1) * (1.0 - alpha)) / n)
            thr = self._weighted_quantile(pool, [1.0] * n, level)
            return ConformalThreshold(key, alpha, thr, n, provisional=True, cohort=cohort)

        # Nothing to calibrate against yet. Return a threshold that cannot be
        # breached rather than guessing — a young column producing alerts is
        # worse than a young column producing nothing.
        return ConformalThreshold(key, alpha, math.inf, len(points), provisional=True, cohort=cohort)

    def cohort_floor(self, cohort: str | None, quantile: float = 0.995,
                     min_samples: int = 400) -> float | None:
        """A practical-significance floor derived from the cohort's own scores.

        The conformal threshold answers "is this cycle unusual *for this
        column*". On a series with almost no natural variation the answer is
        yes for a movement far too small to matter: the threshold collapses
        toward zero and a rounding-level wobble clears it. Statistical
        significance without practical significance is noise with a p-value
        attached, and 200k columns produce a great deal of it.

        The fix is a second bar, and the honest version of that bar is not a
        hand-set constant per signal — that is per-signal tuning smuggled back
        in, which is the thing conformal calibration exists to remove. It is a
        high quantile of the score distribution *across the cohort*: columns
        of the same type at the same tier, scored by the same detector. A
        movement that would be unremarkable anywhere else in its cohort is not
        worth a human, however unusual it is for one quiet column.

        Returns ``None`` when the cohort has too few observations to say
        anything, and the caller falls back to a configured floor.
        """
        pool = self._cohorts.get(cohort or "", [])
        if len(pool) < min_samples:
            return None
        ordered = sorted(pool)
        index = min(len(ordered) - 1, math.ceil(quantile * len(ordered)) - 1)
        return ordered[max(index, 0)]

    def cohort_sizes(self) -> dict[str, int]:
        return {key: len(pool) for key, pool in self._cohorts.items()}

    def p_value(self, key: str, score: float) -> float:
        """Calibrated p-value: (1 + #{s_i >= s_new}) / (n + 1)."""
        points = [p for p in self._series.get(key, ()) if not p.labelled_incident][-self.window :]
        n = len(points)
        if n == 0:
            return 1.0
        greater = sum(1 for p in points if p.score >= score)
        return clamp((1.0 + greater) / (n + 1.0), 0.0, 1.0)

    def score(self, key: str, score: float, alpha: float, now: datetime,
              cohort: str | None = None) -> ConformalVerdict:
        thr = self.threshold(key, alpha, now, cohort)
        return ConformalVerdict(
            score=score,
            threshold=thr.threshold if math.isfinite(thr.threshold) else float("inf"),
            p_value=self.p_value(key, score),
            alpha=alpha,
            breached=thr.breaches(score),
            provisional=thr.provisional,
            n_calibration=thr.n,
        )

    # -- persistence --------------------------------------------------------

    def snapshot(self) -> dict[str, list[tuple[float, str, bool]]]:
        return {
            key: [(p.score, p.at.isoformat(), p.labelled_incident) for p in points]
            for key, points in self._series.items()
        }

    def restore(self, snapshot: dict[str, list[tuple[float, str, bool]]]) -> None:
        from datasentry_core.clock import utc

        self._series = {
            key: [CalibrationPoint(s, utc(at), flag) for s, at, flag in points]
            for key, points in snapshot.items()
        }


@dataclass
class CalibrationReport:
    """Observed versus target FPR — the self-monitoring loop.

    If ``observed_fpr`` exceeds twice ``target_fpr`` for a cohort, the
    recalibration job runs and a platform-health incident is raised. That is
    the system noticing it has drifted before a human does.
    """

    cohort: str
    signal: str
    target_fpr: float
    observations: int = 0
    breaches: int = 0
    true_positives: int = 0
    false_positives: int = 0
    false_negatives: int = 0
    breach_scores: list[float] = field(default_factory=list)

    @property
    def observed_fpr(self) -> float:
        return (self.false_positives / self.observations) if self.observations else 0.0

    @property
    def precision(self) -> float:
        total = self.true_positives + self.false_positives
        return (self.true_positives / total) if total else 0.0

    @property
    def recall(self) -> float:
        total = self.true_positives + self.false_negatives
        return (self.true_positives / total) if total else 0.0

    @property
    def drifted(self) -> bool:
        return self.observed_fpr > 2.0 * self.target_fpr and self.observations >= 100

    def as_dict(self) -> dict[str, object]:
        return {
            "cohort": self.cohort,
            "signal": self.signal,
            "target_fpr": round(self.target_fpr, 6),
            "observed_fpr": round(self.observed_fpr, 6),
            "observations": self.observations,
            "true_positives": self.true_positives,
            "false_positives": self.false_positives,
            "false_negatives": self.false_negatives,
            "precision": round(self.precision, 4),
            "recall": round(self.recall, 4),
            "drifted": self.drifted,
        }
