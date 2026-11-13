"""The closed loop, end to end, on a small world.

Not a mock in sight: a real synthetic warehouse, real profiling with real
sketches, real conformal calibration, real lineage parsing, the real
correlator, the real reasoning graph and the real policy gate. If this passes,
the loop closes.

Kept small (18 tables, 6 days) so it runs in CI in well under a minute. The
full-scale numbers come from `ds eval`.
"""

from __future__ import annotations

import pytest
from tools.pipeline.pipeline import Pipeline

from datasentry_core.enums import ActionStatus
from datasentry_core.store import ControlPlane


@pytest.fixture(scope="module")
def loop(tmp_path_factory):
    store = ControlPlane(tmp_path_factory.mktemp("e2e") / "control.db")
    pipeline = Pipeline(store)
    pipeline.bootstrap(tables=18, days=6, seed=4242)
    pipeline.detect(cycles_back=3)
    pipeline.inject("JOIN_SEMANTICS", at_cycle=pipeline.state.cursor - 2)
    pipeline.advance(cycles=12)
    pipeline.detect(cycles_back=12)
    pipeline.correlate()
    pipeline.investigate(limit=10)
    return pipeline


def test_the_warehouse_gets_built_and_profiled(loop):
    assert loop.store.scalar("SELECT COUNT(*) FROM assets") == 18
    assert loop.store.scalar("SELECT COUNT(*) FROM profiles") > 500
    assert loop.store.scalar("SELECT COUNT(*) FROM lineage_edges") > 0


def test_profiles_contain_no_raw_values(loop):
    """The PII claim, checked against what is actually on disk."""
    row = loop.store.one(
        "SELECT * FROM profiles WHERE numeric_sketch IS NOT NULL LIMIT 1")
    assert row is not None
    assert set(row.keys()) & {"sample", "values", "raw"} == set()
    # Value-derived strings that do survive are salted digests, not values.
    assert row["min_value_hash"] is None or len(row["min_value_hash"]) == 16


def test_the_injected_fault_produces_an_incident(loop):
    fault = loop.store.one("SELECT * FROM faults WHERE fault_type = 'JOIN_SEMANTICS'")
    assert fault is not None
    assert fault["detected_incident_id"], "the join change was not detected at all"


def test_the_incident_names_the_join_change_with_evidence(loop):
    fault = loop.store.one("SELECT * FROM faults WHERE fault_type = 'JOIN_SEMANTICS'")
    incident_id = fault["detected_incident_id"]

    verified = [h for h in loop.store.hypotheses_for(incident_id) if h.status == "verified"]
    assert verified, "no hypothesis survived verification"
    assert str(verified[0].cause_class) == "JOIN_SEMANTICS"

    evidence = {e.evidence_id: e for e in loop.store.evidence_for(incident_id)}
    assert any(str(e.kind) == "SQL_DIFF" for e in evidence.values()), \
        "the structured diff is the evidence that makes this diagnosis certain"

    for claim in verified[0].claims:
        assert claim.evidence_ids
        for eid in claim.evidence_ids:
            assert eid in evidence, "a claim cites evidence that does not exist"


def test_rejected_hypotheses_are_kept_with_reasons(loop):
    fault = loop.store.one("SELECT * FROM faults WHERE fault_type = 'JOIN_SEMANTICS'")
    rejected = [h for h in loop.store.hypotheses_for(fault["detected_incident_id"])
                if h.status == "rejected"]
    assert rejected, "an adversarial verifier that never rejects is decoration"
    for hypothesis in rejected:
        assert hypothesis.rejection_reason
        assert len(hypothesis.rejection_reason) > 20, "a reason a human can act on"


def test_the_blast_radius_reaches_downstream(loop):
    fault = loop.store.one("SELECT * FROM faults WHERE fault_type = 'JOIN_SEMANTICS'")
    entries = loop.store.blast_radius_for(fault["detected_incident_id"])
    assert entries
    assert max(e.criticality for e in entries) > 0


def test_the_policy_gate_holds_the_expensive_actions(loop):
    fault = loop.store.one("SELECT * FROM faults WHERE fault_type = 'JOIN_SEMANTICS'")
    actions = loop.store.actions_for(fault["detected_incident_id"])
    assert actions, "a verified code-change cause should propose remediation"

    for action in actions:
        assert action.policy_version
        if str(action.action_type) == "QUARANTINE_TABLE":
            assert action.status == ActionStatus.PROPOSED, \
                "quarantine must never auto-execute, at any confidence"


def test_correlation_compresses_the_alert_volume(loop):
    anomalies = loop.store.scalar("SELECT COUNT(*) FROM anomalies WHERE incident_id IS NOT NULL")
    incidents = loop.store.scalar("SELECT COUNT(*) FROM incidents")
    assert incidents >= 1
    assert anomalies / incidents > 1.5, "correlation is doing no work"


def test_every_agent_run_is_audited(loop):
    entries = loop.store.audit_tail(200)
    assert any(e["action"] == "rca.completed" and e["actor_kind"] == "agent" for e in entries)
    assert any(e["action"] == "policy.evaluated" for e in entries)


def test_the_run_is_reproducible(tmp_path):
    """Same seed, same world. Without this the eval numbers mean nothing."""
    def build():
        store = ControlPlane(tmp_path / ("r" + str(id(object())) + ".db"))
        pipeline = Pipeline(store)
        pipeline.bootstrap(tables=12, days=3, seed=777)
        return [
            (r["asset_id"], r["column_name"], r["row_count"], r["null_count"])
            for r in store.query(
                "SELECT asset_id, column_name, row_count, null_count FROM profiles "
                "ORDER BY profile_id LIMIT 200")
        ]

    assert build() == build()
