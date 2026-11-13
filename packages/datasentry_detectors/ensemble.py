"""The ensemble: score, calibrate, decide.

Ordering here is load-bearing. For each detector we *score against the
existing calibration set first, and only then add the new point to it*.
Reversed, the point being judged would help set its own threshold, the
guarantee would quietly stop holding, and nothing would look wrong — the
worst class of bug in a statistical system.

The decision rule is deliberately boring: breach the conformal threshold on
``k`` consecutive cycles and you are an anomaly. No score fusion, no tuned
weights, no per-column configuration. Everything interesting happens in the
calibration, which is the point of ADR-003.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import ClassVar

from datasentry_core.config import Settings
from datasentry_core.config import settings as get_settings
from datasentry_core.enums import Signal
from datasentry_core.ids import anomaly_id
from datasentry_core.models import Anomaly
from datasentry_detectors.base import BaseDetector, Finding, SeriesContext
from datasentry_detectors.conformal import CalibrationReport, ConformalCalibrator, cohort_key
from datasentry_detectors.families import ALL_DETECTORS


@dataclass
class EnsembleStats:
    scored: int = 0
    breaches: int = 0
    emitted: int = 0
    suppressed_provisional: int = 0
    derived_floors: int = 0
    by_signal: dict[str, int] = field(default_factory=lambda: defaultdict(int))

    def as_dict(self) -> dict[str, object]:
        return {
            "scored": self.scored,
            "breaches": self.breaches,
            "emitted": self.emitted,
            "suppressed_provisional": self.suppressed_provisional,
            "derived_floors": self.derived_floors,
            "by_signal": dict(self.by_signal),
        }


class DetectorEnsemble:
    def __init__(
        self,
        *,
        config: Settings | None = None,
        calibrator: ConformalCalibrator | None = None,
        detectors: list[BaseDetector] | None = None,
    ) -> None:
        self.settings = config or get_settings()
        det = self.settings.detection
        self.calibrator = calibrator or ConformalCalibrator(
            window=det.calibration_window,
            half_life_days=det.calibration_half_life_days,
            min_history=det.min_history_cycles,
        )
        self.detectors = detectors if detectors is not None else ALL_DETECTORS
        self._streaks: dict[str, int] = defaultdict(int)
        self._floors: dict[str, float] = {}
        # How the practical-significance floor is set.
        #
        # "constant" -- the per-signal table in config.py -- is the default
        # because it measured better, not because it is more elegant. It is
        # not: a hand-set number per signal is the last piece of per-signal
        # tuning in a system whose central claim is that conformal calibration
        # removes the need for it, and replacing it was the top item on the
        # roadmap. Three derived formulations were tried and all three were
        # worse (precision 0.40 -> 0.33, root-asset 1.00 -> 0.75, root-cause
        # 0.67 -> 0.33). They are kept so the result stays reproducible.
        #
        # Why they lose: a quantile of the cohort's score distribution is not
        # independent of the conformal threshold, which is a quantile of the
        # same distribution. It is a second threshold wearing a different hat,
        # and a second threshold on the same evidence only ever costs recall.
        # A floor that genuinely asks a different question would have to come
        # from somewhere else -- the size of the movement in the column's own
        # units, not its rank among the cohort's scores.
        #
        # tools/design/floor_sweep.py is the measurement.
        self.floor_mode = "constant"
        self.stats = EnsembleStats()
        self.reports: dict[tuple[str, str], CalibrationReport] = {}

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _key(ctx: SeriesContext, detector: BaseDetector) -> str:
        return ctx.key_prefix + "::" + detector.detector_id

    # Quantile of the cohort score distribution used by each floor mode.
    # See tools/design/floor_sweep.py for what each one costs.
    _FLOOR_QUANTILE: ClassVar[dict[str, float]] = {
        "q995": 0.995, "q900": 0.90, "median": 0.50,
    }

    def _effect_floor(self, pool: str, signal: str) -> float:
        """The practical-significance bar for one cohort and signal.

        A movement has to clear the conformal threshold *and* this. The two
        answer different questions: the threshold asks "is this unusual for
        this column", the floor asks "is it big enough to be worth a person".

        Getting the floor from the same distribution the threshold comes from
        is the trap. A high quantile of the cohort's scores is not an
        independent check — it is a second conformal threshold at a stricter
        quantile, and a second threshold on the same evidence only ever costs
        recall. Measured: ``q995`` dropped recall 0.67 -> 0.50 and precision
        0.50 -> 0.25 against the constants.

        ``median`` is the formulation that asks something different. It floors
        the *threshold* at the cohort's typical score rather than adding a bar
        above it: a movement smaller than what this kind of column does on an
        ordinary day is not interesting anywhere, however unusual it is for
        one quiet series. A healthy column's own threshold sits well above the
        cohort median, so it is untouched; only a degenerate one gets lifted.

        Memoised per pass — the pool grows monotonically within a run and a
        quantile over a few thousand floats is not free at 200k columns.
        """
        configured = self.settings.min_effect_for(signal)
        if self.floor_mode == "constant":
            return configured

        cached = self._floors.get(pool)
        if cached is not None:
            return cached

        quantile = self._FLOOR_QUANTILE.get(self.floor_mode, 0.50)
        derived = self.calibrator.cohort_floor(pool, quantile=quantile)
        if derived is None:
            # Cold cohort: fall back to the configured constant without
            # caching, because the pool is still filling.
            return configured

        self._floors[pool] = derived
        self.stats.derived_floors += 1
        return derived

    def _report(self, cohort: str, signal: str, alpha: float) -> CalibrationReport:
        entry = self.reports.get((cohort, signal))
        if entry is None:
            entry = CalibrationReport(cohort=cohort, signal=signal, target_fpr=alpha)
            self.reports[(cohort, signal)] = entry
        return entry

    # -- main path ----------------------------------------------------------

    def evaluate(self, ctx: SeriesContext, *, muted: bool = False) -> list[Anomaly]:
        alpha = self.settings.alpha_for_tier(ctx.tier)
        now = ctx.current.window_end
        out: list[Anomaly] = []

        for detector in self.detectors:
            if not detector.applicable(ctx):
                continue
            finding = detector.evaluate(ctx)
            if finding is None:
                continue

            key = self._key(ctx, detector)
            required = self.settings.breaches_for(str(finding.signal))
            self.stats.scored += 1
            report = self._report(ctx.cohort, str(finding.signal), alpha)
            report.observations += 1

            if finding.deterministic:
                self._streaks[key] = required  # fires immediately, by design
                anomaly = self._emit(ctx, detector, finding, None, required, muted)
                if anomaly is not None:
                    out.append(anomaly)
                continue

            pool = cohort_key(ctx.cohort, str(finding.signal))
            verdict = self.calibrator.score(key, finding.score, alpha, now, pool)
            # Score first, learn second. See the module docstring.
            #
            # A breaching point is excluded from the calibration set. Without
            # that, a sustained regression walks its own threshold upward:
            # cycle one breaches, its extreme score joins the calibration set,
            # and cycle two — identical, equally wrong — no longer clears the
            # bar it just raised. The consecutive-breach rule would then never
            # fire on exactly the faults it exists to catch.
            #
            # The cost is real and worth stating: excluding flagged points
            # biases the set low, so the realised FPR drifts above target if a
            # column breaches often. That is what the calibration report
            # measures and what the recalibration job responds to.
            self.calibrator.observe(
                key, finding.score, now, cohort=pool,
                labelled_incident=verdict.breached,
            )


            # Two bars, and they answer different questions. The conformal
            # threshold asks "is this unusual for this column"; the effect
            # floor asks "is it big enough to be worth a person". A quiet
            # column clears the first on a movement that fails the second.
            margin = self.settings.detection.breach_margin
            floor = self._effect_floor(pool, str(finding.signal))
            clears_threshold = (
                verdict.breached
                and finding.score >= verdict.threshold * margin
                and finding.score >= floor
            )
            if not clears_threshold:
                self._streaks[key] = 0
                continue

            self.stats.breaches += 1
            report.breaches += 1
            report.breach_scores.append(finding.score)
            self._streaks[key] += 1
            if self._streaks[key] < required:
                continue

            anomaly = self._emit(ctx, detector, finding, verdict, self._streaks[key], muted)
            if anomaly is not None:
                out.append(anomaly)

        return out

    def _emit(self, ctx: SeriesContext, detector: BaseDetector, finding: Finding,
              verdict, streak: int, muted: bool) -> Anomaly | None:
        if muted:
            return None
        provisional = bool(verdict and verdict.provisional)
        if provisional and finding.score < (verdict.threshold * 1.5 if verdict else 0):
            # A cohort-borrowed threshold is a weaker claim than the column's
            # own. Requiring a clear margin before acting on one is how a new
            # column avoids generating noise in its first days.
            self.stats.suppressed_provisional += 1
            return None

        self.stats.emitted += 1
        self.stats.by_signal[str(finding.signal)] += 1
        return Anomaly(
            anomaly_id=anomaly_id(ctx.current.profile_id, detector.detector_id),
            profile_id=ctx.current.profile_id,
            asset_id=ctx.asset.asset_id,
            column_name=ctx.column,
            signal=finding.signal,
            detector_id=detector.detector_id,
            detector_version=detector.version,
            score=round(finding.score, 6),
            p_value=round(verdict.p_value, 8) if verdict else None,
            ci_low=None,
            ci_high=round(verdict.threshold, 6) if verdict and verdict.threshold != float("inf") else None,
            observed_value=round(finding.observed, 6),
            baseline_value=round(finding.baseline, 6),
            consecutive_breaches=streak,
            provisional=provisional,
            detected_at=ctx.current.window_end,
            explanation=finding.explanation,
        )

    # -- calibration reporting ---------------------------------------------

    def calibration_reports(self) -> list[dict[str, object]]:
        return [r.as_dict() for r in sorted(self.reports.values(), key=lambda r: (r.cohort, r.signal))]

    def label_fpr(self, cohort: str, signal: Signal | str, *, true_positive: bool) -> None:
        """Attribute an emitted anomaly to TP or FP once ground truth is known."""
        entry = self.reports.get((cohort, str(signal)))
        if entry is None:
            return
        if true_positive:
            entry.true_positives += 1
        else:
            entry.false_positives += 1

    def label_miss(self, cohort: str, signal: Signal | str) -> None:
        entry = self.reports.get((cohort, str(signal)))
        if entry is not None:
            entry.false_negatives += 1

    def drifted_cohorts(self) -> list[dict[str, object]]:
        return [r.as_dict() for r in self.reports.values() if r.drifted]


def build_contexts(store, asset_map: dict, *, at: datetime | None = None) -> list[SeriesContext]:
    """Assemble one context per profiled series, newest cycle as ``current``."""
    contexts: list[SeriesContext] = []
    for asset_id, column in store.profiled_series_keys():
        asset = asset_map.get(asset_id)
        if asset is None:
            continue
        series = store.profile_series(asset_id, column, limit=200, before=at)
        if len(series) < 2:
            continue
        data_type = "TABLE"
        if column:
            match = next((c for c in asset.columns if c.column_name == column), None)
            data_type = match.data_type if match else "UNKNOWN"
        contexts.append(
            SeriesContext(
                asset=asset,
                column=column,
                current=series[-1],
                history=series[:-1],
                data_type=data_type,
            )
        )
    return contexts
