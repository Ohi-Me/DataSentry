"""The security model, asserted rather than described.

docs/07 makes three architectural claims: the reasoning identity cannot write,
no action type is irreversible, and quarantine never auto-executes. All three
are properties of the code rather than of a policy document, which means they
can be tested — and a claim that is tested is a different kind of claim.
"""

from __future__ import annotations

import pytest
from services.action_executor.executor import ActionExecutor
from services.policy_gate.policy import REVERSIBILITY, PolicyGate, PolicyInput

from datasentry_agent.tools import catalog
from datasentry_core.enums import ActionType


class ReadOnlyStore:
    """A control plane that refuses to be written to.

    Grepping tool source for the word "write" is not a capability test -- the
    git tool legitimately says "commit" a dozen times. This is: every tool in
    the catalog runs against a store that raises on anything other than a
    SELECT. If a mutating tool is ever added, this fails on the attempt rather
    than on a vocabulary check.
    """

    def __init__(self, real):
        self._real = real
        self.attempts: list[str] = []

    def _guard(self, sql: str) -> None:
        head = sql.strip().split(None, 1)[0].upper() if sql.strip() else ""
        if head not in {"SELECT", "WITH", "PRAGMA"}:
            self.attempts.append(sql)
            raise AssertionError("a read-only tool attempted: " + sql[:80])

    def query(self, sql, params=()):
        self._guard(sql)
        return self._real.query(sql, params)

    def one(self, sql, params=()):
        self._guard(sql)
        return self._real.one(sql, params)

    def scalar(self, sql, params=(), default=0):
        self._guard(sql)
        return self._real.scalar(sql, params, default)

    def execute(self, sql, params=()):
        self._guard(sql)

    def executemany(self, sql, rows):
        self._guard(sql)

    def __getattr__(self, name):
        # Repository helpers (profile_series, anomalies_for_asset, ...) are
        # read-only by construction and pass through.
        forbidden = ("insert", "upsert", "replace", "save", "record", "assign",
                     "publish", "audit", "set_", "delete")
        if name.startswith(forbidden):
            raise AssertionError("a read-only tool reached for " + name)
        return getattr(self._real, name)


def test_no_tool_in_the_reasoning_catalog_can_write(store, now):
    """ADR-008 enforced by absence, not by instruction.

    A successful prompt injection against the reasoning layer buys a wrong
    narrative on a screen, because there is no write tool for it to reach.
    """
    from datasentry_lineage import LineageIndex

    guarded = ReadOnlyStore(store)
    ctx = catalog.ToolContext(store=guarded, index=LineageIndex([]), assets={},
                              incident_id="i1", now=now)
    args = {"asset_id": "wh.db.silver.orders", "column": "amount",
            "model_ref": "models/x.sql", "paths": ["models/x.sql"]}

    for name in catalog.REGISTRY:
        try:
            catalog.run_tool(name, ctx, dict(args))
        except AssertionError:
            raise
        except Exception:  # noqa: BLE001 - see the comment below
            # A tool that cannot answer on an empty store is fine; a tool that
            # tries to write is not.
            pass
    assert guarded.attempts == []


def test_the_action_enum_contains_nothing_irreversible():
    """There is no DROP, DELETE or TRUNCATE, and there never was."""
    forbidden = {"DROP", "DELETE", "TRUNCATE", "PURGE", "OVERWRITE", "REWRITE"}
    for action in ActionType:
        assert not any(word in str(action) for word in forbidden)


def test_every_action_type_has_a_reversibility_score_and_an_executor():
    executor_methods = {
        ActionType.OPEN_PR: "_open_pr",
        ActionType.QUARANTINE_TABLE: "_quarantine",
        ActionType.PIN_FEATURE_VIEW: "_pin_feature_view",
        ActionType.NOTIFY_OWNER: "_notify",
        ActionType.MUTE_ALERT: "_mute",
        ActionType.BACKFILL_REQUEST: "_backfill",
    }
    for action in ActionType:
        assert action in REVERSIBILITY, str(action) + " has no reversibility score"
        assert REVERSIBILITY[action] > 0, "an irreversible action must not be implementable"
        assert hasattr(ActionExecutor, executor_methods[action])


def test_quarantine_never_auto_executes_however_confident_the_diagnosis():
    gate = PolicyGate()
    for confidence in (0.0, 0.5, 0.99, 1.0):
        decision = gate.evaluate(PolicyInput(
            action_type=ActionType.QUARANTINE_TABLE, root_tier=3,
            blast_radius_size=0, max_downstream_criticality=0.0,
            confidence=confidence,
        ))
        assert not decision.auto_execute


def test_high_risk_needs_an_admin_and_cannot_be_bulk_approved():
    from datasentry_core.enums import RiskTier

    gate = PolicyGate()
    assert not gate.can_approve(RiskTier.HIGH, "operator")
    assert gate.can_approve(RiskTier.HIGH, "admin")
    assert not gate.bulk_approve_allowed(RiskTier.HIGH)
    assert gate.bulk_approve_allowed(RiskTier.LOW)


def test_the_executor_refuses_an_unapproved_action():
    from datetime import UTC, datetime

    from datasentry_core.enums import ActionStatus, RiskTier
    from datasentry_core.errors import PolicyViolation
    from datasentry_core.models import Action

    action = Action(
        action_id="a1", incident_id="i1", action_type=ActionType.QUARANTINE_TABLE,
        risk_tier=RiskTier.HIGH, policy_version="v1", status=ActionStatus.REJECTED,
        created_at=datetime(2026, 8, 13, tzinfo=UTC),
    )
    with pytest.raises(PolicyViolation):
        ActionExecutor(store=None).execute(action)


def test_every_policy_decision_records_its_version():
    decision = PolicyGate().evaluate(PolicyInput(
        action_type=ActionType.OPEN_PR, root_tier=1, blast_radius_size=10,
        max_downstream_criticality=0.9, confidence=0.9,
    ))
    assert decision.policy_version
    assert decision.rationale
    assert decision.factors, "the inputs to the decision are part of the record"
