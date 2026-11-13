"""The conformal guarantee, checked empirically.

This is the load-bearing claim of the whole detection design: a calibrated
false-positive rate per column, distribution-free. A unit test that only
checked the arithmetic would miss the thing that matters, so these run the
calibrator over thousands of samples and count how often it actually fires.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

import pytest

from datasentry_detectors.conformal import CalibrationReport, ConformalCalibrator


def _run(distribution, *, alpha: float, n: int = 4000, seed: int = 3) -> float:
    rng = random.Random(seed)
    calibrator = ConformalCalibrator(window=200, half_life_days=0, min_history=30)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    fired = 0
    scored = 0
    for i in range(n):
        score = distribution(rng)
        at = start + timedelta(hours=i)
        verdict = calibrator.score("series", score, alpha, at)
        calibrator.observe("series", score, at)
        if i > 400:  # let the calibration set fill first
            scored += 1
            fired += 1 if verdict.breached else 0
    return fired / scored


@pytest.mark.parametrize("alpha", [0.01, 0.005])
def test_observed_fpr_respects_the_target_on_gaussian_noise(alpha):
    observed = _run(lambda rng: abs(rng.gauss(0, 1)), alpha=alpha)
    assert observed <= alpha * 2.0, "observed " + str(observed) + " against target " + str(alpha)


def test_guarantee_holds_on_a_distribution_that_is_nothing_like_normal():
    """Distribution-free is the whole reason for choosing conformal."""
    observed = _run(lambda rng: rng.lognormvariate(0, 2.5), alpha=0.01)
    assert observed <= 0.03


def test_a_young_column_cannot_breach():
    calibrator = ConformalCalibrator(window=200, half_life_days=14, min_history=30)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    for i in range(10):
        calibrator.observe("fresh", float(i), now + timedelta(hours=i))
    verdict = calibrator.score("fresh", 9999.0, 0.01, now + timedelta(hours=11))
    assert not verdict.breached
    assert verdict.provisional


def test_cohort_threshold_covers_cold_start():
    calibrator = ConformalCalibrator(window=200, half_life_days=0, min_history=30)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    rng = random.Random(5)
    for i in range(200):
        calibrator.observe("other_column", abs(rng.gauss(0, 1)),
                           now + timedelta(hours=i), cohort="NUMBER/t1")

    verdict = calibrator.score("brand_new", 12.0, 0.01, now, cohort="NUMBER/t1")
    assert verdict.breached
    assert verdict.provisional, "a borrowed threshold is a weaker claim and must say so"


def test_labelled_incidents_stay_out_of_the_calibration_set():
    """Otherwise a regression teaches the detector that it is normal."""
    calibrator = ConformalCalibrator(window=200, half_life_days=0, min_history=10)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    for i in range(60):
        calibrator.observe("series", 1.0, now + timedelta(hours=i))
    for i in range(20):
        calibrator.observe("series", 500.0, now + timedelta(hours=60 + i),
                           labelled_incident=True)

    threshold = calibrator.threshold("series", 0.01, now + timedelta(hours=90))
    assert threshold.threshold < 10, "the excluded outliers must not move the threshold"


def test_p_values_are_calibrated():
    calibrator = ConformalCalibrator(window=200, half_life_days=0, min_history=30)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    for i in range(200):
        calibrator.observe("series", float(i), now + timedelta(hours=i))

    assert calibrator.p_value("series", 1000.0) < 0.01
    assert calibrator.p_value("series", -1.0) > 0.99


def test_calibration_report_flags_drift():
    report = CalibrationReport(cohort="NUMBER/t1", signal="NULL_RATE", target_fpr=0.01)
    report.observations = 1000
    report.false_positives = 50
    assert report.observed_fpr == 0.05
    assert report.drifted, "5% observed against a 1% target must trigger recalibration"

    healthy = CalibrationReport(cohort="NUMBER/t1", signal="NULL_RATE", target_fpr=0.01)
    healthy.observations = 1000
    healthy.false_positives = 8
    assert not healthy.drifted
