"""The domain model.

These are the types every service exchanges and the API returns verbatim.
Two constraints in here are load-bearing rather than decorative:

* ``Claim.evidence_ids`` has ``min_length=1``. A claim without evidence
  cannot be constructed, so "cite your sources" is enforced by parsing and
  not by asking a model nicely (ADR-005).
* ``ProposedAction.action_type`` is a closed enum. There is no path from
  model output to an action the executor did not already implement
  (ADR-008).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from datasentry_core.enums import (
    ActionStatus,
    ActionType,
    AssetKind,
    CauseClass,
    EvidenceKind,
    IncidentStatus,
    Layer,
    RiskTier,
    Signal,
    TransformType,
    Verdict,
)


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=False, use_enum_values=False)


# --------------------------------------------------------------------------
# Assets and lineage
# --------------------------------------------------------------------------


class AssetColumn(Model):
    column_name: str
    data_type: str
    is_nullable: bool = True
    ordinal: int = 0
    contains_pii: bool = False


class Asset(Model):
    asset_id: str  # warehouse.database.schema.table
    warehouse: str = "snowflake"
    database_name: str
    schema_name: str
    table_name: str
    layer: Layer
    kind: AssetKind = AssetKind.TABLE
    tier: int = Field(default=3, ge=1, le=4)  # 1 = business critical
    owner_team: str | None = None
    dbt_model_path: str | None = None
    repo: str | None = None
    is_quarantined: bool = False
    query_count_30d: int = 0
    distinct_consumers_30d: int = 0
    columns: list[AssetColumn] = Field(default_factory=list)

    @property
    def short_name(self) -> str:
        return self.schema_name + "." + self.table_name


class LineageEdge(Model):
    src_asset_id: str
    src_column: str | None
    dst_asset_id: str
    dst_column: str | None
    transform_type: TransformType = TransformType.IDENTITY
    transform_expr: str | None = None
    source: Literal["declared", "observed"] = "declared"
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    model_ref: str | None = None

    def key(self) -> tuple:
        return (
            self.src_asset_id,
            self.src_column,
            self.dst_asset_id,
            self.dst_column,
            self.model_ref,
            self.source,
        )


class LineageNode(Model):
    asset_id: str
    column_name: str | None = None
    layer: Layer | None = None
    tier: int = 3
    depth: int = 0
    impacted: bool = False


class LineageGraph(Model):
    nodes: list[LineageNode] = Field(default_factory=list)
    edges: list[LineageEdge] = Field(default_factory=list)
    truncated: bool = False


# --------------------------------------------------------------------------
# Profiling and detection
# --------------------------------------------------------------------------


class ColumnProfile(Model):
    """One profiling cycle for one column, or for a table when ``column`` is None.

    Note the absence of raw values. ``min_value_hash`` and ``max_value_hash``
    are salted digests used only for change detection; the distribution lives
    in mergeable sketches. This is ADR-010 made concrete and is the answer to
    "how do you handle PII?".
    """

    profile_id: str
    asset_id: str
    column_name: str | None = None
    window_start: datetime
    window_end: datetime
    row_count: int = 0
    # Rows actually examined. Profiling is sampled above a cap, and a rate
    # detector that treats a 900-row sample as 900,000 observations will be
    # wildly overconfident -- so the sample size travels with the estimate.
    sampled_rows: int | None = None
    freshness_seconds: int = 0
    null_count: int | None = None
    distinct_estimate: int | None = None
    numeric_sketch: str | None = None  # serialised t-digest
    string_length_sketch: str | None = None
    categorical_topk: dict[str, int] | None = None
    new_category_count: int | None = None
    min_value_hash: str | None = None
    max_value_hash: str | None = None
    referential_violation_count: int | None = None
    schema_hash: str | None = None
    degraded: bool = False
    profiler_version: str = "profiler/1.2.0"
    computed_at: datetime

    @property
    def null_rate(self) -> float | None:
        if self.null_count is None or self.row_count == 0:
            return None
        return self.null_count / self.row_count

    @property
    def distinct_ratio(self) -> float | None:
        if self.distinct_estimate is None or self.row_count == 0:
            return None
        return self.distinct_estimate / self.row_count


class Anomaly(Model):
    anomaly_id: str
    profile_id: str
    asset_id: str
    column_name: str | None = None
    signal: Signal
    detector_id: str
    detector_version: str
    score: float
    p_value: float | None = None
    ci_low: float | None = None
    ci_high: float | None = None
    observed_value: float
    baseline_value: float
    consecutive_breaches: int = 1
    provisional: bool = False  # cold-start cohort threshold rather than own history
    incident_id: str | None = None
    detected_at: datetime
    explanation: str = ""


# --------------------------------------------------------------------------
# Incidents, evidence and reasoning
# --------------------------------------------------------------------------


class Evidence(Model):
    evidence_id: str
    incident_id: str
    kind: EvidenceKind
    ref: str
    summary: str
    payload: dict[str, Any] = Field(default_factory=dict)
    collected_by: str
    collected_at: datetime


class Claim(Model):
    """An assertion plus the artefacts that back it.

    ``min_length=1`` on evidence_ids is the whole point of this class.
    """

    text: str = Field(max_length=400)
    evidence_ids: list[str] = Field(min_length=1)
    supported: bool | None = None
    support_note: str | None = None


class Hypothesis(Model):
    hypothesis_id: str
    incident_id: str
    rank: int = 0
    cause_class: CauseClass
    statement: str = Field(max_length=800)
    claims: list[Claim] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    calibrated_confidence: float | None = None
    status: Literal["proposed", "verified", "rejected"] = "proposed"
    rejection_reason: str | None = None
    evidence_requests: list[str] = Field(default_factory=list)
    round_index: int = 0

    @model_validator(mode="after")
    def _rejection_needs_reason(self) -> Hypothesis:
        if self.status == "rejected" and not self.rejection_reason:
            raise ValueError("rejected hypotheses require a rejection_reason")
        return self

    @property
    def evidence_ids(self) -> list[str]:
        seen: list[str] = []
        for claim in self.claims:
            for eid in claim.evidence_ids:
                if eid not in seen:
                    seen.append(eid)
        return seen


class BlastRadiusEntry(Model):
    asset_id: str
    column_name: str | None
    depth: int
    asset_kind: AssetKind
    criticality: float
    confidence: float = 1.0
    tier: int = 3
    layer: Layer | None = None
    last_consumed_at: datetime | None = None


class ProposedAction(Model):
    action_type: ActionType
    params: dict[str, Any] = Field(default_factory=dict)
    rationale: str
    reversible: bool = True

    @field_validator("reversible")
    @classmethod
    def _must_be_reversible(cls, v: bool) -> bool:
        # There is no irreversible action type implemented. If this ever
        # trips, someone added one and the review should stop there.
        if not v:
            raise ValueError("DataSentry does not implement irreversible actions")
        return v


class Action(Model):
    action_id: str
    incident_id: str
    action_type: ActionType
    risk_tier: RiskTier
    policy_version: str
    params: dict[str, Any] = Field(default_factory=dict)
    rationale: str = ""
    status: ActionStatus = ActionStatus.PROPOSED
    approved_by: str | None = None
    approved_at: datetime | None = None
    external_ref: str | None = None
    result: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class TimelineEvent(Model):
    at: datetime
    kind: str  # anomaly | commit | dbt_run | status | action | verification
    label: str
    detail: str = ""
    ref: str | None = None


class Disposition(Model):
    disposition_id: str
    incident_id: str
    verdict: Verdict
    correct_cause_class: CauseClass | None = None
    notes: str | None = None
    user_id: str
    created_at: datetime


class AgentCost(Model):
    tokens_in: int = 0
    tokens_out: int = 0
    usd: float = 0.0
    cheap_calls: int = 0
    frontier_calls: int = 0
    tool_calls: int = 0
    cache_hit: bool = False

    @property
    def cheap_ratio(self) -> float:
        total = self.cheap_calls + self.frontier_calls
        return (self.cheap_calls / total) if total else 0.0


class Incident(Model):
    incident_id: str
    status: IncidentStatus = IncidentStatus.DETECTED
    severity: int = Field(default=3, ge=1, le=4)
    title: str
    root_asset_id: str
    root_column: str | None = None
    anomaly_count: int = 0
    occurrence_count: int = 1
    narrative: str | None = None
    first_detected_at: datetime
    rca_started_at: datetime | None = None
    rca_completed_at: datetime | None = None
    resolved_at: datetime | None = None
    llm_cost_usd: float = 0.0
    token_count: int = 0
    trace_id: str | None = None
    prompt_version: str | None = None
    created_at: datetime

    @property
    def time_to_root_cause_seconds(self) -> float | None:
        if self.rca_started_at and self.rca_completed_at:
            return (self.rca_completed_at - self.rca_started_at).total_seconds()
        return None


class IncidentDetail(Model):
    """Everything the incident workspace screen needs, in one round trip."""

    incident: Incident
    anomalies: list[Anomaly] = Field(default_factory=list)
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    blast_radius: list[BlastRadiusEntry] = Field(default_factory=list)
    actions: list[Action] = Field(default_factory=list)
    timeline: list[TimelineEvent] = Field(default_factory=list)
    disposition: Disposition | None = None
    cost: AgentCost = Field(default_factory=AgentCost)
    root_asset: Asset | None = None


# --------------------------------------------------------------------------
# Ground truth (fault harness) and evaluation
# --------------------------------------------------------------------------


class FaultRecord(Model):
    """Ground truth for one injected fault.

    Every evaluation number in the project traces back to this record, which
    is why it is a first-class domain type and not a test fixture.
    """

    fault_id: str
    fault_type: str
    injected_at: datetime
    root_asset_id: str
    root_column: str | None = None
    mechanism: str
    cause_class: CauseClass | None = None
    expected_signals: list[Signal] = Field(default_factory=list)
    expected_downstream: list[str] = Field(default_factory=list)
    commit_sha: str | None = None
    pr_number: int | None = None
    is_decoy: bool = False
