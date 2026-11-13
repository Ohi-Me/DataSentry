"""The policy gate.

Declarative, versioned, and evaluated on every action with the policy version
recorded alongside the decision. When someone asks six months later why a
quarantine executed without approval, the answer is a row, not a recollection.

Risk tier is computed from three things: how valuable the asset is, how
reversible the action is, and how large the blast radius is. Low risk
auto-executes; medium and high enter the approval tray. High risk
additionally requires an admin and is excluded from bulk approval, because
the whole point of a tray is throughput and throughput is exactly what you
do not want on the irreversible-adjacent end.

Every action type in this system is reversible — that is enforced in the
model layer, not here — so "reversibility" below is really "how much work is
the undo", which is a real gradient worth encoding.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from datasentry_core.config import Settings
from datasentry_core.config import settings as get_settings
from datasentry_core.enums import ActionType, RiskTier

# How much work the undo is. 1.0 is trivial (delete a notification), 0.0 would
# be irreversible and no action type scores it.
REVERSIBILITY = {
    ActionType.NOTIFY_OWNER: 1.00,
    ActionType.MUTE_ALERT: 0.95,
    ActionType.OPEN_PR: 0.90,  # a PR is a proposal; closing it costs nothing
    ActionType.PIN_FEATURE_VIEW: 0.70,
    ActionType.BACKFILL_REQUEST: 0.55,
    ActionType.QUARANTINE_TABLE: 0.45,  # downstream jobs start skipping it
}

# Roles allowed to approve each tier.
APPROVERS = {
    RiskTier.LOW: {"operator", "admin"},
    RiskTier.MEDIUM: {"operator", "admin"},
    RiskTier.HIGH: {"admin"},
}

BULK_APPROVE_ALLOWED = {RiskTier.LOW, RiskTier.MEDIUM}


@dataclass
class PolicyDecision:
    risk_tier: RiskTier
    auto_execute: bool
    policy_version: str
    rationale: str
    required_role: str
    factors: dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "risk_tier": str(self.risk_tier),
            "auto_execute": self.auto_execute,
            "policy_version": self.policy_version,
            "rationale": self.rationale,
            "required_role": self.required_role,
            "factors": {k: round(v, 4) for k, v in self.factors.items()},
        }


@dataclass
class PolicyInput:
    action_type: ActionType
    root_tier: int
    blast_radius_size: int
    max_downstream_criticality: float
    confidence: float
    target_is_quarantined: bool = False
    severity: int = 3


class PolicyGate:
    def __init__(self, config: Settings | None = None) -> None:
        self.settings = config or get_settings()
        self.version = self.settings.policy_version

    def evaluate(self, request: PolicyInput) -> PolicyDecision:
        reversibility = REVERSIBILITY.get(request.action_type, 0.5)
        tier_weight = {1: 1.0, 2: 0.7, 3: 0.4, 4: 0.2}.get(request.root_tier, 0.4)
        blast = min(request.blast_radius_size / 25.0, 1.0)

        # Confidence *reduces* risk: a well-evidenced diagnosis makes acting
        # on it safer. It is capped so a confident diagnosis can never drag a
        # quarantine down into auto-execute territory on its own.
        confidence_relief = min(max(request.confidence, 0.0), 1.0) * 0.15

        risk = (
            0.35 * (1.0 - reversibility)
            + 0.25 * tier_weight
            + 0.25 * blast
            + 0.15 * request.max_downstream_criticality
            - confidence_relief
        )

        factors = {
            "reversibility": reversibility,
            "root_tier_weight": tier_weight,
            "blast_radius": blast,
            "downstream_criticality": request.max_downstream_criticality,
            "confidence_relief": confidence_relief,
            "risk_score": risk,
        }

        if request.action_type in {ActionType.NOTIFY_OWNER} and request.severity > 1:
            return PolicyDecision(
                RiskTier.LOW, True, self.version,
                "notifying an owning team changes nothing and needs no approval",
                "operator", factors,
            )

        if request.action_type == ActionType.QUARANTINE_TABLE:
            # Quarantine always needs a human. Downstream jobs start skipping
            # the table, which is a production behaviour change, and no
            # confidence score should be able to buy that automatically.
            tier = RiskTier.HIGH if request.root_tier <= 2 else RiskTier.MEDIUM
            return PolicyDecision(
                tier, False, self.version,
                "quarantine changes downstream job behaviour and is never auto-executed",
                "admin" if tier == RiskTier.HIGH else "operator", factors,
            )

        if risk >= 0.55:
            tier = RiskTier.HIGH
        elif risk >= 0.32:
            tier = RiskTier.MEDIUM
        else:
            tier = RiskTier.LOW

        auto = tier == RiskTier.LOW
        rationale = (
            "risk " + format(risk, ".2f") + " from "
            + format((1 - reversibility) * 100, ".0f") + "% undo cost, tier-"
            + str(request.root_tier) + " root and " + str(request.blast_radius_size)
            + " downstream assets"
        )
        return PolicyDecision(
            tier, auto, self.version, rationale,
            "admin" if tier == RiskTier.HIGH else "operator", factors,
        )

    def can_approve(self, tier: RiskTier, role: str) -> bool:
        return role in APPROVERS.get(tier, set())

    def bulk_approve_allowed(self, tier: RiskTier) -> bool:
        return tier in BULK_APPROVE_ALLOWED

    def describe(self) -> dict[str, Any]:
        """The policy as data, for the /policies screen and for the audit log."""
        return {
            "version": self.version,
            "thresholds": {"high": 0.55, "medium": 0.32},
            "reversibility": {str(k): v for k, v in REVERSIBILITY.items()},
            "approvers": {str(k): sorted(v) for k, v in APPROVERS.items()},
            "bulk_approve": [str(t) for t in sorted(BULK_APPROVE_ALLOWED, key=str)],
            "invariants": [
                "no action type is irreversible; the enum contains no DROP or DELETE",
                "QUARANTINE_TABLE never auto-executes, at any confidence",
                "high-risk actions require an admin and are excluded from bulk approve",
                "every evaluation is logged with this policy version",
            ],
        }
