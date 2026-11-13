"""Graph nodes.

One module per node, plus the shared dependency container and the context
builder they all use. The split matters because these nodes have genuinely
different characters — one is a database join, three are reasoning calls, one
is a fan-out of read-only tools — and a single file would hide that.

Node budgets, from docs/05 section 3:

    Triage          cheap tier    1 call
    Contextualize   no model      0 calls   <- assembling a dossier is SQL
    Hypothesize     frontier      1 call per round
    GatherEvidence  no model      <=12 tool calls
    Verify          frontier      1 call per round
    Narrate         frontier      1 call
    PlanActions     cheap tier    1 call

Two of the seven use no model at all. Every node made deterministic is one
that is cheaper, faster and testable, and it is the reason the cost-per-
incident number is what it is.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from datasentry_agent.guardrails import ConfidenceCalibrator, GuardrailReport
from datasentry_agent.state import Budget, RCAState
from datasentry_core.clock import Clock, iso
from datasentry_core.models import Asset


@dataclass
class Deps:
    """Everything the nodes need, injected rather than imported.

    Nodes take their collaborators as data so the eval harness can run the
    graph against frozen evidence with a different reasoner and diff the
    outcome — which is what makes prompt iteration measurable instead of
    a guess.
    """

    store: Any
    index: Any
    assets: dict[str, Asset]
    reasoner: Any
    budget: Budget
    clock: Clock
    now: datetime
    guardrails: GuardrailReport
    calibrator: ConfidenceCalibrator
    # How far back evidence collection looks. A day, not a few hours: at a
    # three-hour profiling cadence with a two-cycle consecutive-breach rule,
    # detection lags the change by six hours minimum. A six-hour lookback
    # would systematically miss the commit that caused the incident and then
    # correctly report "no commit touched this path" -- confidently wrong.
    window_hours: int = 24
    muted: bool = False
    duplicate_of: str | None = None


def build_context(state: RCAState, deps: Deps) -> dict[str, Any]:
    """The payload handed to the reasoner. Statistics only, never values."""
    dossier = state.dossier
    incident = dossier.incident if dossier else None
    asset = dossier.root_asset if dossier else None

    context: dict[str, Any] = {
        "incident": {
            "incident_id": state.incident_id,
            "title": incident.title if incident else "",
            "severity": incident.severity if incident else 3,
            "root_asset_id": incident.root_asset_id if incident else "",
            "root_column": incident.root_column if incident else None,
            "first_detected_at": iso(incident.first_detected_at) if incident else None,
            "anomaly_count": incident.anomaly_count if incident else 0,
        },
        "depth": state.depth,
        "root_tier": asset.tier if asset else 3,
        "owner_team": asset.owner_team if asset else None,
        "muted": deps.muted,
        "duplicate_of": deps.duplicate_of,
        "signals": dossier.signals() if dossier else [],
        "anomalies": [
            {
                "signal": str(a.signal),
                "column": a.column_name,
                "observed": a.observed_value,
                "baseline": a.baseline_value,
                "score": round(a.score, 3),
                "p_value": a.p_value,
                "consecutive_breaches": a.consecutive_breaches,
                "explanation": a.explanation,
                "at": iso(a.detected_at),
            }
            for a in (dossier.anomalies if dossier else [])
        ],
        "downstream": [
            {
                "asset_id": d.asset_id,
                "column": d.column_name,
                "depth": d.depth,
                "asset_kind": str(d.asset_kind),
                "criticality": d.criticality,
                "tier": d.tier,
            }
            for d in (dossier.downstream if dossier else [])[:25]
        ],
        "commits": dossier.recent_commits if dossier else [],
        "dbt_runs": dossier.recent_runs if dossier else [],
        "schema_changes": dossier.schema_changes if dossier else [],
        "upstream_anomalies": [
            {"asset_id": a.asset_id, "column": a.column_name, "signal": str(a.signal),
             "at": iso(a.detected_at)}
            for a in (dossier.upstream_anomalies if dossier else [])
        ],
        "evidence": [
            {
                "id": e.evidence_id,
                "kind": str(e.kind),
                "ref": e.ref,
                "summary": e.summary,
                "tool": e.collected_by,
                "payload": e.payload,
            }
            for e in state.evidence
        ],
        "hypotheses": [
            {
                "cause_class": str(h.cause_class),
                "statement": h.statement,
                "confidence": h.confidence,
                "claims": [{"text": c.text, "evidence_ids": c.evidence_ids} for c in h.claims],
            }
            for h in state.hypotheses
            if h.status == "proposed"
        ],
        "rejected": [
            {"cause_class": str(h.cause_class), "reason": h.rejection_reason}
            for h in state.rejected()
        ],
        "round": state.round_index,
    }

    top = state.top()
    if top is not None:
        context["top_hypothesis"] = {
            "cause_class": str(top.cause_class),
            "statement": top.statement,
            "confidence": top.confidence,
            "evidence_ids": top.evidence_ids,
            "claims": [{"text": c.text, "evidence_ids": c.evidence_ids} for c in top.claims],
        }
    if state.actions:
        context["actions"] = [
            {"action_type": str(a.action_type), "params": a.params, "rationale": a.rationale}
            for a in state.actions
        ]
    return context
