"""Detector families, each on the fault it exists to catch.

Every test here injects the signature of one fault class into a synthetic
profile history and asserts that the family responsible sees it — and, just as
importantly, that a clean history produces nothing.
"""

from __future__ import annotations

import math
import random
from datetime import UTC, datetime, timedelta

from datasentry_core.enums import AssetKind, Layer, Signal
from datasentry_core.models import Asset, AssetColumn, ColumnProfile
from datasentry_detectors import DetectorEnsemble, SeriesContext
from datasentry_detectors.families import (
    CardinalityDetector,
    NullRateDetector,
    NumericDriftDetector,
    SchemaDetector,
    VolumeDetector,
)
from datasentry_sketches import TDigest

START = datetime(2026, 8, 1, tzinfo=UTC)
SAMPLE = 900


def _asset(tier: int = 1) -> Asset:
    return Asset(
        asset_id="wh.db.silver.orders", database_name="db", schema_name="silver",
        table_name="orders", layer=Layer.SILVER, kind=AssetKind.TABLE, tier=tier,
        columns=[AssetColumn(column_name="amount", data_type="NUMBER(18,2)")],
    )


def _profile(i: int, **fields) -> ColumnProfile:
    at = START + timedelta(hours=3 * i)
    defaults = {
        "profile_id": "p" + str(i), "asset_id": "wh.db.silver.orders",
        "window_start": at - timedelta(hours=3), "window_end": at, "computed_at": at,
        "row_count": 10000, "sampled_rows": SAMPLE, "freshness_seconds": 5400,
    }
    defaults.update(fields)
    return ColumnProfile(**defaults)


def _context(profiles, column="amount", tier=1, data_type="NUMBER(18,2)"):
    return SeriesContext(asset=_asset(tier), column=column, current=profiles[-1],
                         history=profiles[:-1], data_type=data_type)


# --------------------------------------------------------------------- D1


def test_volume_detector_ignores_seasonality_it_can_explain():
    """A daily cycle is not an incident, and the residual must know that."""
    rng = random.Random(2)
    profiles = [
        _profile(i, column_name=None,
                 row_count=int(10000 * (1 + 0.30 * math.sin(i / 8 * 2 * math.pi))
                               * (1 + rng.gauss(0, 0.03))))
        for i in range(60)
    ]
    finding = VolumeDetector().evaluate(_context(profiles, column=None, data_type="TABLE"))
    assert finding is not None
    assert finding.score < 4.0, "a clean seasonal series must score low"


def test_volume_detector_sees_a_step_change():
    rng = random.Random(2)
    profiles = [
        _profile(i, column_name=None,
                 row_count=int(10000 * (1 + 0.30 * math.sin(i / 8 * 2 * math.pi))
                               * (1 + rng.gauss(0, 0.03))
                               * (1.4 if i >= 58 else 1.0)))
        for i in range(60)
    ]
    finding = VolumeDetector().evaluate(_context(profiles, column=None, data_type="TABLE"))
    assert finding.score > 4.0


# --------------------------------------------------------------------- D2


def test_null_rate_ignores_a_tiny_sample():
    """One null in three rows is not an incident, whatever the ratio says."""
    profiles = [_profile(i, column_name="amount", row_count=3, sampled_rows=3,
                         null_count=0) for i in range(30)]
    profiles.append(_profile(30, column_name="amount", row_count=3, sampled_rows=3,
                             null_count=1))
    finding = NullRateDetector().evaluate(_context(profiles))
    assert finding is not None
    assert finding.score < 3.0, "the posterior knows how little evidence three rows is"


def test_null_rate_screams_at_a_flood_on_a_large_table():
    profiles = [_profile(i, column_name="amount", null_count=20) for i in range(30)]
    profiles.append(_profile(30, column_name="amount", null_count=1500))
    finding = NullRateDetector().evaluate(_context(profiles))
    assert finding.score > 8.0


def test_null_rate_baseline_survives_a_contaminated_history():
    """A regression must not teach the detector that it is normal."""
    profiles = [_profile(i, column_name="amount", null_count=20) for i in range(40)]
    for i in range(40, 46):  # six contaminated cycles already in history
        profiles.append(_profile(i, column_name="amount", null_count=1200))
    profiles.append(_profile(46, column_name="amount", null_count=1200))

    finding = NullRateDetector().evaluate(_context(profiles))
    assert finding.score > 8.0, "median and MAD must hold against six bad cycles"


# --------------------------------------------------------------------- D3


def _numeric(i: int, mean: float, seed: int = 1) -> ColumnProfile:
    rng = random.Random(seed * 1000 + i)
    digest = TDigest(60.0)
    for _ in range(400):
        digest.add(rng.gauss(mean, mean * 0.2))
    return _profile(i, column_name="amount", numeric_sketch=digest.encode())


def test_numeric_drift_catches_a_unit_change():
    """USD to INR is a multiplicative shift with no schema change."""
    profiles = [_numeric(i, 100.0) for i in range(40)]
    profiles.append(_numeric(40, 8320.0))
    finding = NumericDriftDetector().evaluate(_context(profiles))
    assert finding is not None
    assert finding.score > 20, "a currency switch should be many baseline spreads"


def test_numeric_drift_stays_quiet_on_a_stable_column():
    profiles = [_numeric(i, 100.0) for i in range(41)]
    finding = NumericDriftDetector().evaluate(_context(profiles))
    assert finding.score < 1.0


def test_numeric_drift_does_not_detonate_on_a_near_constant_column():
    """A tiny IQR must not turn a rounding wobble into eighty spreads."""
    profiles = []
    for i in range(41):
        digest = TDigest(60.0)
        for _ in range(400):
            digest.add(8.0 if i < 40 else 8.5)
        profiles.append(_profile(i, column_name="amount", numeric_sketch=digest.encode()))
    finding = NumericDriftDetector().evaluate(_context(profiles))
    assert finding.score < 5.0


# --------------------------------------------------------------------- D5


def test_cardinality_catches_a_dedup_break():
    """Rows hold or rise while distinct values do not: the ratio collapses."""
    profiles = [_profile(i, column_name="amount", row_count=10000,
                         distinct_estimate=9800) for i in range(30)]
    profiles.append(_profile(30, column_name="amount", row_count=30000,
                             distinct_estimate=9800))
    finding = CardinalityDetector().evaluate(_context(profiles))
    assert finding is not None
    assert finding.score > 5.0
    assert "fan-out" in finding.explanation


# --------------------------------------------------------------------- D7


def test_schema_detector_is_deterministic_and_immediate():
    profiles = [_profile(i, column_name="amount", schema_hash="aaaa") for i in range(5)]
    profiles.append(_profile(5, column_name="amount", schema_hash="bbbb"))
    finding = SchemaDetector().evaluate(_context(profiles))
    assert finding is not None
    assert finding.deterministic
    assert finding.signal == Signal.SCHEMA


def test_schema_detector_silent_when_nothing_changed():
    profiles = [_profile(i, column_name="amount", schema_hash="aaaa") for i in range(6)]
    assert SchemaDetector().evaluate(_context(profiles)) is None


# ---------------------------------------------------------------- ensemble


def test_the_ensemble_needs_consecutive_breaches():
    """A single spike is noise; the rule that says so is the k=2 requirement.

    Fifty cycles, not forty: scoring starts at cycle 12, so forty leaves the
    column with 28 calibration points and still inside cold start. This test
    is about the consecutive-breach rule, so it needs a column that has
    exited cold start — the boundary itself is covered below.
    """
    ensemble = DetectorEnsemble()
    profiles = [_profile(i, column_name="amount", null_count=20) for i in range(50)]

    emitted = []
    for i in range(12, len(profiles)):
        emitted += ensemble.evaluate(
            SeriesContext(asset=_asset(), column="amount", current=profiles[i],
                          history=profiles[:i], data_type="NUMBER(18,2)"))
    assert emitted == [], "a clean series must emit nothing at all"

    spike = _profile(50, column_name="amount", null_count=1500)
    once = ensemble.evaluate(
        SeriesContext(asset=_asset(), column="amount", current=spike,
                      history=profiles, data_type="NUMBER(18,2)"))
    assert once == [], "one breaching cycle is not yet an anomaly"

    again = ensemble.evaluate(
        SeriesContext(asset=_asset(), column="amount",
                      current=_profile(51, column_name="amount", null_count=1500),
                      history=[*profiles, spike], data_type="NUMBER(18,2)"))
    assert any(a.signal == Signal.NULL_RATE for a in again)
    assert all(a.consecutive_breaches >= 2 for a in again)


def test_a_column_inside_cold_start_stays_silent():
    """Under 30 calibration points, a column has no threshold of its own.

    It borrows one from its cohort, and with no cohort either it emits
    nothing at all. Silence is the honest output: a young column that starts
    alerting is worse than a young column that says nothing, and at 200k
    columns there is always a young one.
    """
    ensemble = DetectorEnsemble()
    profiles = [_profile(i, column_name="amount", null_count=20) for i in range(24)]
    for i in range(12, len(profiles)):
        ensemble.evaluate(
            SeriesContext(asset=_asset(), column="amount", current=profiles[i],
                          history=profiles[:i], data_type="NUMBER(18,2)"))

    emitted = []
    for offset in range(3):
        current = _profile(24 + offset, column_name="amount", null_count=1500)
        emitted += ensemble.evaluate(
            SeriesContext(asset=_asset(), column="amount", current=current,
                          history=profiles, data_type="NUMBER(18,2)"))
    assert emitted == []


def test_cohort_pools_are_kept_per_signal():
    """A nonconformity score only means something in its own detector's units.

    D2 reports -log10(tail probability); D3 reports a distance in baseline
    spreads. Pooling those into one cohort list produces a cold-start
    threshold that is meaningless to both, so the pool key carries the signal.
    """
    from datasentry_detectors.conformal import cohort_key

    ensemble = DetectorEnsemble()
    profiles = [_profile(i, column_name="amount", null_count=20) for i in range(50)]
    for i in range(12, len(profiles)):
        ensemble.evaluate(
            SeriesContext(asset=_asset(), column="amount", current=profiles[i],
                          history=profiles[:i], data_type="NUMBER(18,2)"))

    sizes = ensemble.calibrator.cohort_sizes()
    assert sizes, "the cohort pool should have been populated"
    for key in sizes:
        assert "::" in key, "cohort pool key must carry the signal: " + key
    signals = {key.split("::", 1)[1] for key in sizes}
    assert len(signals) > 1, "several detector families should be pooling separately"
    # The cohort strips the parenthesised precision, so NUMBER(18,2) and
    # NUMBER(10,4) pool together -- they are the same kind of column.
    assert cohort_key("NUMBER/t1", "NULL_RATE") in sizes


def test_muted_series_emit_nothing():
    ensemble = DetectorEnsemble()
    profiles = [_profile(i, column_name="amount", null_count=20) for i in range(40)]
    for i in range(12, len(profiles)):
        ensemble.evaluate(SeriesContext(asset=_asset(), column="amount",
                                        current=profiles[i], history=profiles[:i],
                                        data_type="NUMBER(18,2)"), muted=True)
    for value in (1500, 1500):
        profiles.append(_profile(len(profiles), column_name="amount", null_count=value))
        result = ensemble.evaluate(
            SeriesContext(asset=_asset(), column="amount", current=profiles[-1],
                          history=profiles[:-1], data_type="NUMBER(18,2)"), muted=True)
        assert result == []
