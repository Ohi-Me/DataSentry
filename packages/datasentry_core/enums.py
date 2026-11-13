"""Closed vocabularies.

Every one of these is closed on purpose. `ActionType` in particular is the
security boundary described in ADR-008: the reasoning layer can only ever
name a member of this enum, so there is no path from model output to
arbitrary execution.
"""

from __future__ import annotations

from enum import StrEnum


class Layer(StrEnum):
    BRONZE = "bronze"
    SILVER = "silver"
    GOLD = "gold"


class Signal(StrEnum):
    """One member per detector family. See docs/04-detection-ml.md."""

    VOLUME = "VOLUME"
    FRESHNESS = "FRESHNESS"
    NULL_RATE = "NULL_RATE"
    CARDINALITY = "CARDINALITY"
    NUMERIC_DRIFT = "NUMERIC_DRIFT"
    CATEGORICAL_DRIFT = "CATEGORICAL_DRIFT"
    NEW_CATEGORY = "NEW_CATEGORY"
    REFERENTIAL = "REFERENTIAL"
    SCHEMA = "SCHEMA"
    MULTIVARIATE = "MULTIVARIATE"
    STRING_LENGTH = "STRING_LENGTH"


class IncidentStatus(StrEnum):
    DETECTED = "detected"
    RCA_RUNNING = "rca_running"
    RCA_COMPLETE = "rca_complete"
    AWAITING_APPROVAL = "awaiting_approval"
    REMEDIATING = "remediating"
    VERIFYING = "verifying"
    RESOLVED = "resolved"
    ESCALATED = "escalated"
    DISMISSED = "dismissed"
    BUDGET_EXCEEDED = "budget_exceeded"


class CauseClass(StrEnum):
    UPSTREAM_CODE_CHANGE = "UPSTREAM_CODE_CHANGE"
    SOURCE_SCHEMA_DRIFT = "SOURCE_SCHEMA_DRIFT"
    LATE_DATA = "LATE_DATA"
    VOLUME_SHIFT = "VOLUME_SHIFT"
    JOIN_SEMANTICS = "JOIN_SEMANTICS"
    UNIT_CHANGE = "UNIT_CHANGE"
    DEDUP_BREAK = "DEDUP_BREAK"
    INFRA_FAILURE = "INFRA_FAILURE"
    SEASONAL_LEGITIMATE = "SEASONAL_LEGITIMATE"


class EvidenceKind(StrEnum):
    GIT_COMMIT = "GIT_COMMIT"
    SQL_DIFF = "SQL_DIFF"
    PROFILE_DELTA = "PROFILE_DELTA"
    QUERY_LOG = "QUERY_LOG"
    DBT_RUN = "DBT_RUN"
    SCHEMA_CHANGE = "SCHEMA_CHANGE"
    LINEAGE_PATH = "LINEAGE_PATH"
    PAST_INCIDENT = "PAST_INCIDENT"
    UPSTREAM_ANOMALY = "UPSTREAM_ANOMALY"
    ASSET_METADATA = "ASSET_METADATA"


class ActionType(StrEnum):
    """The complete set of things DataSentry is able to do to the world.

    Every member is reversible by construction. There is deliberately no
    DROP, DELETE or TRUNCATE — those were never implemented, which is a
    stronger guarantee than a policy forbidding them.
    """

    OPEN_PR = "OPEN_PR"
    QUARANTINE_TABLE = "QUARANTINE_TABLE"
    PIN_FEATURE_VIEW = "PIN_FEATURE_VIEW"
    NOTIFY_OWNER = "NOTIFY_OWNER"
    MUTE_ALERT = "MUTE_ALERT"
    BACKFILL_REQUEST = "BACKFILL_REQUEST"


class ActionStatus(StrEnum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTING = "executing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REVERTED = "reverted"


class RiskTier(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class TransformType(StrEnum):
    IDENTITY = "IDENTITY"
    CAST = "CAST"
    AGG = "AGG"
    JOIN_KEY = "JOIN_KEY"
    EXPR = "EXPR"
    WINDOW = "WINDOW"
    UNKNOWN = "UNKNOWN"


class Verdict(StrEnum):
    CORRECT_CAUSE = "correct_cause"
    WRONG_CAUSE = "wrong_cause"
    FALSE_POSITIVE = "false_positive"
    DUPLICATE = "duplicate"
    EXPECTED = "expected"


class AssetKind(StrEnum):
    TABLE = "TABLE"
    FEATURE_VIEW = "FEATURE_VIEW"
    DASHBOARD = "DASHBOARD"
    ML_MODEL = "ML_MODEL"
    REPORT = "REPORT"
