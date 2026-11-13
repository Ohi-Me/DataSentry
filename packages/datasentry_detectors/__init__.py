"""datasentry_detectors — statistics, calibrated.

Detection is a statistics problem, not a language-model problem (ADR-004).
Finding anomalies is trivial; finding real regressions at a *calibrated*
false-positive rate across 200k columns with zero per-column configuration is
the hard part, and everything in this package exists to make that specific
claim defensible.

Entry point is DetectorEnsemble.evaluate(SeriesContext) -> list[Anomaly].
"""

from datasentry_detectors.base import BaseDetector, Finding, SeriesContext
from datasentry_detectors.conformal import (
    CalibrationReport,
    ConformalCalibrator,
    ConformalVerdict,
)
from datasentry_detectors.ensemble import DetectorEnsemble, build_contexts
from datasentry_detectors.families import ALL_DETECTORS
from datasentry_detectors.forest import IsolationForest

__all__ = [
    "ALL_DETECTORS",
    "BaseDetector",
    "CalibrationReport",
    "ConformalCalibrator",
    "ConformalVerdict",
    "DetectorEnsemble",
    "Finding",
    "IsolationForest",
    "SeriesContext",
    "build_contexts",
]
