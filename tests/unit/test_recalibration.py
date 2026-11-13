"""The disposition -> label -> tuning loop.

F10 is only closed if a human's verdict actually changes something. These
tests check the part that is easy to get wrong and impossible to notice: what
each verdict *means to a detector*, which is not what it means to a person.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from services.detector.recalibration import (
    CohortOutcome,
    RecalibrationJob,
    suggested_alpha,
)

from datasentry_core.enums import IncidentStatus, Signal, Verdict
from datasentry_core.models import Anomaly, Asset, AssetColumn, Disposition, Incident

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


def _seed_asset(store, tier: int = 1) -> Asset:
    asset = Asset(
        asset_id="sf.p.silver.orders", database_name="p", schema_name="silver",
        table_name="orders", layer="silver", tier=tier,
        columns=[AssetColumn(column_name="amount", data_type="NUMBER(18,2)", ordinal=0)],
    )
    store.upsert_asset(asset, NOW)
    return asset


def _incident(store, suffix: str, asset_id: str) -> Incident:
    incident = Incident(
        incident_id="inc-" + suffix, status=IncidentStatus.RCA_COMPLETE, severity=2,
        title="null rate on amount", root_asset_id=asset_id, root_column="amount",
        first_detected_at=NOW, created_at=NOW,
    )
    store.upsert_incident(incident)
    return incident


def _anomaly(store, incident_id: str, suffix: str, asset_id: str,
             signal: Signal = Signal.NULL_RATE) -> None:
    store.insert_anomalies([Anomaly(
        anomaly_id="an-" + suffix, profile_id="pf-" + suffix, asset_id=asset_id,
        column_name="amount", signal=signal, detector_id="D2",
        detector_version="1.4.0", score=9.0, observed_value=0.1, baseline_value=0.002,
        incident_id=incident_id, detected_at=NOW,
    )])


def _scored(store, cycles: int, cohort: str = "NUMBER/t1",
            signal: str = "NULL_RATE", target: float = 0.01) -> None:
    """Stand in for a detection pass having scored this many cycles.

    The FPR denominator comes from detection, never from disposition. Without
    it there is no rate, only a count.
    """
    RecalibrationJob.record_scored_cycles(
        store,
        [{"cohort": cohort, "signal": signal, "observations": cycles,
          "target_fpr": target, "observed_fpr": 0.0}],
        NOW,
    )


def _dispose(store, incident_id: str, verdict: Verdict) -> None:
    store.insert_disposition(Disposition(
        disposition_id="d-" + incident_id, incident_id=incident_id,
        verdict=verdict, user_id="dana.m", created_at=NOW,
    ))


def test_a_false_positive_verdict_reaches_the_detector(store):
    """The whole point: a human saying "wrong" has to change something."""
    asset = _seed_asset(store)
    _scored(store, 1000)
    for i in range(6):
        incident_id = "inc-fp" + str(i)
        _incident(store, "fp" + str(i), asset.asset_id)
        _anomaly(store, incident_id, "fp" + str(i), asset.asset_id)
        _dispose(store, incident_id, Verdict.FALSE_POSITIVE)

    report = RecalibrationJob(store).run(since=NOW - timedelta(days=1))

    assert report.dispositions == 6
    assert len(report.cohorts) == 1
    cohort = report.cohorts[0]
    assert cohort.cohort == "NUMBER/t1", "the precision is stripped so like columns pool"
    assert cohort.signal == "NULL_RATE"
    assert cohort.false_positives == 6
    assert cohort.precision == 0.0
    # Six false alarms over a thousand scored cycles is 0.006 -- under a 0.01
    # target. Precision is dreadful and the FPR is fine, which is exactly why
    # the two numbers are kept apart.
    assert cohort.observed_fpr == 0.006
    assert not cohort.drifted


def test_a_correct_cause_verdict_counts_as_a_true_positive(store):
    asset = _seed_asset(store)
    _scored(store, 1000)
    for i in range(4):
        incident_id = "inc-tp" + str(i)
        _incident(store, "tp" + str(i), asset.asset_id)
        _anomaly(store, incident_id, "tp" + str(i), asset.asset_id)
        _dispose(store, incident_id, Verdict.CORRECT_CAUSE)

    cohort = RecalibrationJob(store).run(since=NOW - timedelta(days=1)).cohorts[0]
    assert cohort.true_positives == 4
    assert cohort.false_positives == 0
    assert cohort.precision == 1.0


def test_wrong_cause_is_a_detector_win_and_a_reasoning_loss(store):
    """Detection was right; the reasoning was wrong.

    Counting it against the detector would push a threshold up over a mistake
    the detector did not make, and the column would go quiet for the wrong
    reason. It belongs in the agent eval set, not in this one.
    """
    asset = _seed_asset(store)
    _scored(store, 1000)
    _incident(store, "wc", asset.asset_id)
    _anomaly(store, "inc-wc", "wc", asset.asset_id)
    _dispose(store, "inc-wc", Verdict.WRONG_CAUSE)

    cohort = RecalibrationJob(store).run(since=NOW - timedelta(days=1)).cohorts[0]
    assert cohort.true_positives == 1
    assert cohort.false_positives == 0


def test_a_duplicate_verdict_says_nothing_and_is_not_counted(store):
    """It is neither a hit nor a miss, so counting it either way dilutes both."""
    asset = _seed_asset(store)
    _scored(store, 1000)
    _incident(store, "dup", asset.asset_id)
    _anomaly(store, "inc-dup", "dup", asset.asset_id)
    _dispose(store, "inc-dup", Verdict.DUPLICATE)

    report = RecalibrationJob(store).run(since=NOW - timedelta(days=1))
    assert report.cohorts[0].disposed == 0
    assert report.cohorts[0].observed_fpr == 0.0


def test_the_two_rates_are_not_the_same_number(store):
    """Precision and false-positive rate have different denominators.

    Conflating them is the easy mistake: false positives over *disposed
    incidents* is 1 - precision, and comparing that against a per-cycle alpha
    declares drift on a detector that is behaving exactly as specified.
    """
    entry = CohortOutcome(cohort="NUMBER/t1", signal="NULL_RATE", target_fpr=0.01,
                          true_positives=7, false_positives=3,
                          disposed=10, scored_cycles=5000)
    assert entry.precision == 0.7
    assert entry.observed_fpr == 0.0006
    assert not entry.drifted, "a 30% false-alarm share can still be a 0.06% rate"


def test_drift_needs_a_high_rate_and_enough_of_both_denominators(store):
    thin = CohortOutcome(cohort="NUMBER/t1", signal="NULL_RATE", target_fpr=0.01,
                         false_positives=9, disposed=9, scored_cycles=200)
    assert not thin.drifted, "nine dispositions is not evidence of anything"

    unscored = CohortOutcome(cohort="NUMBER/t1", signal="NULL_RATE", target_fpr=0.01,
                             false_positives=40, disposed=40, scored_cycles=50)
    assert not unscored.drifted, "fifty scored cycles cannot establish a 1% rate"

    real = CohortOutcome(cohort="NUMBER/t1", signal="NULL_RATE", target_fpr=0.01,
                         false_positives=40, disposed=40, scored_cycles=1000)
    assert real.observed_fpr == 0.04
    assert real.drifted


def test_drift_raises_a_health_incident_through_the_normal_machinery(store):
    """A platform problem routed to its own channel is one nobody reads."""
    asset = _seed_asset(store)
    _scored(store, 400)          # 60 false alarms in 400 cycles is 0.15, way past 0.01
    for i in range(60):
        incident_id = "inc-d" + str(i)
        _incident(store, "d" + str(i), asset.asset_id)
        _anomaly(store, incident_id, "d" + str(i), asset.asset_id)
        _dispose(store, incident_id, Verdict.FALSE_POSITIVE)

    report = RecalibrationJob(store).run(since=NOW - timedelta(days=1))
    assert report.drifted == ["NUMBER/t1/NULL_RATE"]
    assert report.health_incident

    raised = store.get_incident(report.health_incident)
    assert raised is not None
    assert "calibration drifted" in raised.title.lower()
    assert raised.severity == 2
    assert raised.narrative and "target false-positive rate" in raised.narrative


def test_running_twice_does_not_raise_a_second_health_incident(store):
    asset = _seed_asset(store)
    _scored(store, 400)
    for i in range(60):
        incident_id = "inc-t" + str(i)
        _incident(store, "t" + str(i), asset.asset_id)
        _anomaly(store, incident_id, "t" + str(i), asset.asset_id)
        _dispose(store, incident_id, Verdict.FALSE_POSITIVE)

    job = RecalibrationJob(store)
    first = job.run(since=NOW - timedelta(days=1))
    second = job.run(since=NOW - timedelta(days=1))
    assert first.health_incident == second.health_incident
    rows = store.query("SELECT COUNT(*) AS n FROM incidents WHERE title LIKE '%calibration drifted%'")
    assert rows[0]["n"] == 1


def test_the_result_is_persisted_for_the_health_page(store):
    asset = _seed_asset(store)
    _scored(store, 1000)
    _incident(store, "p1", asset.asset_id)
    _anomaly(store, "inc-p1", "p1", asset.asset_id)
    _dispose(store, "inc-p1", Verdict.CORRECT_CAUSE)

    job = RecalibrationJob(store)
    job.run(since=NOW - timedelta(days=1))

    latest = job.latest()
    assert latest, "the health page reads this table, so it has to be written"
    assert latest[0]["cohort"] == "NUMBER/t1"
    assert latest[0]["true_positives"] == 1


def test_the_suggested_target_halves_rather_than_solving_to_the_observation(store):
    """Solving to a rate measured on a handful of labels overshoots on noise.

    Trading a precision problem for a recall problem is a bad trade: an extra
    look costs minutes, a missed regression costs a quarter of bad reporting.
    """
    drifted = CohortOutcome(cohort="NUMBER/t1", signal="NULL_RATE", target_fpr=0.01,
                            false_positives=40, disposed=40, scored_cycles=1000)
    assert drifted.drifted
    assert suggested_alpha(drifted) == 0.005

    steady = CohortOutcome(cohort="NUMBER/t1", signal="NULL_RATE", target_fpr=0.01,
                           true_positives=48, false_positives=2,
                           disposed=50, scored_cycles=1000)
    assert not steady.drifted
    assert suggested_alpha(steady) == 0.01


def test_dispositions_split_by_signal_not_just_by_cohort(store):
    """Two detectors on the same column are two different calibrations."""
    asset = _seed_asset(store)
    _scored(store, 1000, signal="NULL_RATE")
    _scored(store, 1000, signal="CARDINALITY")
    _incident(store, "s1", asset.asset_id)
    _anomaly(store, "inc-s1", "s1a", asset.asset_id, Signal.NULL_RATE)
    _anomaly(store, "inc-s1", "s1b", asset.asset_id, Signal.CARDINALITY)
    _dispose(store, "inc-s1", Verdict.FALSE_POSITIVE)

    report = RecalibrationJob(store).run(since=NOW - timedelta(days=1))
    signals = {c.signal for c in report.cohorts}
    assert signals == {"NULL_RATE", "CARDINALITY"}
