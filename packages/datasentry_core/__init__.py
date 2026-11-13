"""datasentry_core — shared vocabulary for every DataSentry service.

Nothing in this package talks to a network. It defines the types, the
identifiers, the clock, the event bus and the control-plane store that the
rest of the system agrees on. Keeping it dependency-light is deliberate:
every other package imports it, so it must never be the reason a service
is slow to start or hard to test.
"""

from datasentry_core.enums import (
    ActionStatus,
    ActionType,
    CauseClass,
    EvidenceKind,
    IncidentStatus,
    Layer,
    RiskTier,
    Signal,
    TransformType,
    Verdict,
)
from datasentry_core.ids import anomaly_id, evidence_id, incident_id, profile_id, stable_uuid

__all__ = [
    "ActionStatus",
    "ActionType",
    "CauseClass",
    "EvidenceKind",
    "IncidentStatus",
    "Layer",
    "RiskTier",
    "Signal",
    "TransformType",
    "Verdict",
    "anomaly_id",
    "evidence_id",
    "incident_id",
    "profile_id",
    "stable_uuid",
]

__version__ = "0.1.0"
