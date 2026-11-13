"""Shaping domain objects into what a screen needs.

Presenters live here rather than in the handlers so the endpoints stay a list
of routes and the shaping stays testable. The rule they follow: one screen,
one round trip. The incident workspace needs anomalies, hypotheses, evidence,
blast radius, actions and a timeline; fetching those as six requests would
make the most-used screen in the product the slowest.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from datasentry_core.clock import iso
from datasentry_core.models import Action, Asset, Incident, TimelineEvent
from datasentry_core.store import ControlPlane
from datasentry_lineage import LineageIndex, criticality
from datasentry_sketches import TDigest


def incident_summary(cp: ControlPlane, incident: Incident) -> dict[str, Any]:
    blast = cp.blast_radius_for(incident.incident_id)
    hypotheses = cp.hypotheses_for(incident.incident_id)
    verified = [h for h in hypotheses if h.status == "verified"]
    top = max(verified, key=lambda h: h.confidence) if verified else None
    actions = cp.actions_for(incident.incident_id)
    return {
        "incident_id": incident.incident_id,
        "short_id": incident.incident_id[:8],
        "status": str(incident.status),
        "severity": incident.severity,
        "title": incident.title,
        "root_asset_id": incident.root_asset_id,
        "root_asset": _short(incident.root_asset_id),
        "root_column": incident.root_column,
        "anomaly_count": incident.anomaly_count,
        "occurrence_count": incident.occurrence_count,
        "blast_radius": len(blast),
        "max_criticality": round(max((b.criticality for b in blast), default=0.0), 3),
        "cause_class": str(top.cause_class) if top else None,
        "confidence": round(top.confidence, 3) if top else None,
        "calibrated_confidence": top.calibrated_confidence if top else None,
        "first_detected_at": iso(incident.first_detected_at),
        # Measured from the agent's own step durations, not from the wall clock.
        # The pipeline drives the graph on a simulated clock so the run is
        # reproducible; deriving latency from that clock would report zero for
        # every incident, which is not a fast system, it is an unmeasured one.
        "rca_seconds": _rca_seconds(cp, incident),
        "llm_cost_usd": round(incident.llm_cost_usd, 6),
        "awaiting_approval": sum(1 for a in actions if str(a.status) == "proposed"),
        "actions": len(actions),
    }


def incident_detail(cp: ControlPlane, incident: Incident,
                    assets: dict[str, Asset]) -> dict[str, Any]:
    anomalies = cp.anomalies_for_incident(incident.incident_id)
    hypotheses = cp.hypotheses_for(incident.incident_id)
    evidence = cp.evidence_for(incident.incident_id)
    blast = cp.blast_radius_for(incident.incident_id)
    actions = cp.actions_for(incident.incident_id)
    disposition = cp.disposition_for(incident.incident_id)
    steps = cp.steps_for(incident.incident_id)
    root = assets.get(incident.root_asset_id) or cp.get_asset(incident.root_asset_id)

    return {
        "incident": dict(
            incident_summary(cp, incident),
            narrative=incident.narrative,
            trace_id=incident.trace_id,
            prompt_version=incident.prompt_version,
            token_count=incident.token_count,
            resolved_at=iso(incident.resolved_at) if incident.resolved_at else None,
        ),
        "root_asset": asset_view(cp, root) if root else None,
        "anomalies": [
            {
                "anomaly_id": a.anomaly_id,
                "asset_id": a.asset_id,
                "asset": _short(a.asset_id),
                "column": a.column_name,
                "signal": str(a.signal),
                "detector": a.detector_id,
                "score": round(a.score, 3),
                "p_value": a.p_value,
                "threshold": a.ci_high,
                "observed": a.observed_value,
                "baseline": a.baseline_value,
                "consecutive_breaches": a.consecutive_breaches,
                "provisional": a.provisional,
                "explanation": a.explanation,
                "at": iso(a.detected_at),
            }
            for a in anomalies
        ],
        "hypotheses": [
            {
                "hypothesis_id": h.hypothesis_id,
                "rank": h.rank,
                "cause_class": str(h.cause_class),
                "statement": h.statement,
                "confidence": round(h.confidence, 3),
                "calibrated_confidence": h.calibrated_confidence,
                "status": h.status,
                "rejection_reason": h.rejection_reason,
                "round": h.round_index,
                "claims": [
                    {
                        "text": c.text,
                        "evidence_ids": c.evidence_ids,
                        "supported": c.supported,
                        "support_note": c.support_note,
                    }
                    for c in h.claims
                ],
            }
            for h in sorted(hypotheses, key=lambda h: (h.status != "verified", h.rank))
        ],
        "evidence": [
            {
                "evidence_id": e.evidence_id,
                "kind": str(e.kind),
                "ref": e.ref,
                "summary": e.summary,
                "collected_by": e.collected_by,
                "collected_at": iso(e.collected_at),
                "payload": e.payload,
            }
            for e in evidence
        ],
        "blast_radius": [
            {
                "asset_id": b.asset_id,
                "asset": _short(b.asset_id),
                "column": b.column_name,
                "depth": b.depth,
                "kind": str(b.asset_kind),
                "criticality": round(b.criticality, 3),
                "confidence": round(b.confidence, 3),
                "tier": b.tier,
                "layer": str(b.layer) if b.layer else None,
            }
            for b in blast
        ],
        "actions": [action_view(cp, a) for a in actions],
        "timeline": [t.model_dump(mode="json") for t in timeline_for(cp, incident)],
        "steps": steps,
        "disposition": disposition.model_dump(mode="json") if disposition else None,
        "cost": {
            "usd": round(incident.llm_cost_usd, 6),
            "tokens": incident.token_count,
            "cheap_calls": sum(1 for s in steps if s["model"] == "cheap"),
            "frontier_calls": sum(1 for s in steps if s["model"] == "frontier"),
            "tool_calls": sum(1 for s in steps if s["node"] in {"Contextualize", "GatherEvidence"}),
            "duration_ms": sum(s["duration_ms"] for s in steps),
        },
    }


def timeline_for(cp: ControlPlane, incident: Incident) -> list[TimelineEvent]:
    """One axis for everything, so the commit marker lands on the inflection.

    Seeing the change and the break on the same timeline is the moment the
    incident explains itself. Splitting them across two cards is the single
    easiest way to lose that.
    """
    events: list[TimelineEvent] = []

    # One entry per (series, signal), at its onset, with a recurrence count.
    # A sustained regression re-fires every cycle on every affected column, so
    # a literal listing is a hundred near-identical rows -- which is not a
    # timeline, it is the raw table with worse ergonomics.
    anomalies = cp.anomalies_for_incident(incident.incident_id)
    grouped: dict[tuple[str, str | None, str], list] = {}
    for anomaly in anomalies:
        grouped.setdefault(
            (anomaly.asset_id, anomaly.column_name, str(anomaly.signal)), []
        ).append(anomaly)

    for (asset_id, column, signal), group in grouped.items():
        first = min(group, key=lambda a: a.detected_at)
        last = max(group, key=lambda a: a.detected_at)
        recurrence = ""
        if len(group) > 1:
            recurrence = (
                " · held for " + str(len(group)) + " cycles through "
                + last.detected_at.strftime("%H:%M")
            )
        events.append(
            TimelineEvent(
                at=first.detected_at,
                kind="anomaly",
                label=signal + " on " + _short(asset_id) + ("." + column if column else ""),
                detail=first.explanation + recurrence,
                ref=first.anomaly_id,
            )
        )

    for item in cp.evidence_for(incident.incident_id):
        if str(item.kind) == "GIT_COMMIT":
            for commit in item.payload.get("commits", [])[:6]:
                if not commit.get("touches_lineage"):
                    continue
                events.append(
                    TimelineEvent(
                        at=_dt(commit.get("at")),
                        kind="commit",
                        label="commit " + str(commit.get("sha", ""))[:7]
                              + (" (PR #" + str(commit["pr_number"]) + ")"
                                 if commit.get("pr_number") else ""),
                        detail=str(commit.get("message", "")).splitlines()[0][:160],
                        ref=commit.get("sha"),
                    )
                )
        elif str(item.kind) == "SQL_DIFF":
            events.append(
                TimelineEvent(at=item.collected_at, kind="sql_diff",
                              label="semantic diff", detail=item.summary, ref=item.evidence_id)
            )
        elif str(item.kind) == "DBT_RUN":
            for run in item.payload.get("runs", [])[:4]:
                events.append(
                    TimelineEvent(
                        at=_dt(run.get("started_at")), kind="dbt_run",
                        label="dbt run " + str(run.get("model_ref", "")).split("/")[-1],
                        detail=str(run.get("status")) + ", "
                               + format(run.get("rows_affected", 0), ",") + " rows",
                        ref=run.get("run_id"),
                    )
                )

    events.append(TimelineEvent(at=incident.first_detected_at, kind="status",
                                label="incident raised", detail=incident.title))
    if incident.rca_completed_at:
        events.append(TimelineEvent(at=incident.rca_completed_at, kind="status",
                                    label="root cause established",
                                    detail="cost $" + format(incident.llm_cost_usd, ".4f")))
    for action in cp.actions_for(incident.incident_id):
        events.append(TimelineEvent(at=action.created_at, kind="action",
                                    label=str(action.action_type),
                                    detail=str(action.risk_tier) + " risk — " + action.rationale,
                                    ref=action.action_id))
    if incident.resolved_at:
        events.append(TimelineEvent(at=incident.resolved_at, kind="verification",
                                    label="verified and closed",
                                    detail="anomaly cleared after remediation"))

    events.sort(key=lambda e: e.at)
    return events


def action_view(cp: ControlPlane, action: Action, *, full: bool = False) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "action_id": action.action_id,
        "incident_id": action.incident_id,
        "action_type": str(action.action_type),
        "risk_tier": str(action.risk_tier),
        "policy_version": action.policy_version,
        "status": str(action.status),
        "rationale": action.rationale,
        "approved_by": action.approved_by,
        "external_ref": action.external_ref,
        "created_at": iso(action.created_at),
        "params": action.params,
        "reversible": True,
    }
    incident = cp.get_incident(action.incident_id)
    if incident is not None:
        doc["incident_title"] = incident.title
        doc["severity"] = incident.severity
        doc["root_asset"] = _short(incident.root_asset_id)
        doc["blast_radius"] = len(cp.blast_radius_for(action.incident_id))
    if full:
        doc["result"] = action.result
        doc["diff"] = action.result.get("diff")
    elif action.result.get("diff"):
        doc["diff"] = action.result["diff"]
    return doc


def asset_view(cp: ControlPlane, asset: Asset, *, full: bool = False) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "asset_id": asset.asset_id,
        "name": asset.short_name,
        "table": asset.table_name,
        "layer": str(asset.layer),
        "kind": str(asset.kind),
        "tier": asset.tier,
        "owner_team": asset.owner_team,
        "dbt_model_path": asset.dbt_model_path,
        "criticality": criticality(asset),
        "is_quarantined": asset.is_quarantined,
        "query_count_30d": asset.query_count_30d,
        "distinct_consumers_30d": asset.distinct_consumers_30d,
    }
    if full:
        doc["columns"] = [
            {"name": c.column_name, "type": c.data_type, "nullable": c.is_nullable,
             "contains_pii": c.contains_pii}
            for c in asset.columns
        ]
        doc["recent_anomalies"] = [
            {"signal": str(a.signal), "column": a.column_name, "score": round(a.score, 3),
             "at": iso(a.detected_at), "incident_id": a.incident_id}
            for a in cp.anomalies_for_asset(asset.asset_id, limit=20)
        ]
        doc["incidents"] = [
            incident_summary(cp, i)
            for i in cp.list_incidents(asset_id=asset.asset_id, limit=10)
        ]
    return doc


def profile_series_view(cp: ControlPlane, asset_id: str, column: str | None,
                        cycles: int) -> dict[str, Any]:
    """Series plus the conformal band, so a chart can draw what the detector saw."""
    series = cp.profile_series(asset_id, column, limit=cycles)
    points = []
    for p in series:
        digest = TDigest.decode(p.numeric_sketch)
        points.append(
            {
                "at": iso(p.window_end),
                "rows": p.row_count,
                "sampled_rows": p.sampled_rows,
                "null_rate": round(p.null_rate, 6) if p.null_rate is not None else None,
                "distinct_ratio": round(p.distinct_ratio, 6) if p.distinct_ratio is not None else None,
                "freshness_s": p.freshness_seconds,
                "new_categories": p.new_category_count,
                "degraded": p.degraded,
                "p50": round(digest.quantile(0.5), 6) if digest else None,
                "p95": round(digest.quantile(0.95), 6) if digest else None,
            }
        )

    anomalies = [
        {"at": iso(a.detected_at), "signal": str(a.signal), "score": round(a.score, 3),
         "incident_id": a.incident_id, "explanation": a.explanation}
        for a in cp.anomalies_for_asset(asset_id, limit=60)
        if a.column_name == column
    ]

    return {
        "asset_id": asset_id,
        "column": column,
        "points": points,
        "anomalies": anomalies,
        "columns": [
            c["column_name"]
            for c in [dict(r) for r in cp.query(
                "SELECT column_name FROM asset_columns WHERE asset_id = ? ORDER BY ordinal",
                (asset_id,),
            )]
        ],
    }


def lineage_view(index: LineageIndex, assets: dict[str, Asset], asset_id: str,
                 column: str | None, depth: int) -> dict[str, Any]:
    graph = index.neighborhood(asset_id, column, up=depth, down=depth, assets=assets)
    return {
        "focus": {"asset_id": asset_id, "column": column},
        "nodes": [
            {
                "id": n.asset_id + "::" + (n.column_name or "*"),
                "asset_id": n.asset_id,
                "asset": _short(n.asset_id),
                "column": n.column_name,
                "layer": str(n.layer) if n.layer else None,
                "tier": n.tier,
                "depth": n.depth,
                "impacted": n.impacted,
                "kind": str(assets[n.asset_id].kind) if n.asset_id in assets else "TABLE",
            }
            for n in graph.nodes
        ],
        "edges": [
            {
                "source": e.src_asset_id + "::" + (e.src_column or "*"),
                "target": e.dst_asset_id + "::" + (e.dst_column or "*"),
                "src_asset": e.src_asset_id,
                "dst_asset": e.dst_asset_id,
                "transform": str(e.transform_type),
                "origin": e.source,
                "confidence": e.confidence,
                "model_ref": e.model_ref,
            }
            for e in graph.edges
        ],
        "truncated": graph.truncated,
    }


def detector_health(cp: ControlPlane) -> dict[str, Any]:
    """Observed versus target FPR — the system reporting on itself.

    Reconstructed from what actually happened rather than from a counter the
    detector keeps about itself: emitted anomalies against dispositions,
    grouped by signal. A monitoring system marking its own homework should at
    least use a different pen.
    """
    per_signal = cp.query(
        """
        SELECT a.signal,
               COUNT(*) AS emitted,
               SUM(CASE WHEN d.verdict IN ('false_positive','expected') THEN 1 ELSE 0 END) AS fp,
               SUM(CASE WHEN d.verdict = 'correct_cause' THEN 1 ELSE 0 END) AS tp,
               AVG(a.score) AS avg_score,
               AVG(a.p_value) AS avg_p
        FROM anomalies a
        LEFT JOIN dispositions d ON d.incident_id = a.incident_id
        GROUP BY a.signal ORDER BY emitted DESC
        """
    )
    scored = cp.scalar("SELECT COUNT(*) FROM profiles")
    emitted = cp.scalar("SELECT COUNT(*) FROM anomalies")

    tiers = cp.query(
        """
        SELECT s.tier, COUNT(*) AS emitted FROM anomalies a
        JOIN assets s ON s.asset_id = a.asset_id GROUP BY s.tier ORDER BY s.tier
        """
    )
    targets = {1: 0.010, 2: 0.005, 3: 0.002, 4: 0.002}

    return {
        "profiles_scored": scored,
        "anomalies_emitted": emitted,
        "overall_rate": round(emitted / scored, 6) if scored else 0.0,
        "by_signal": [
            {
                "signal": r["signal"],
                "emitted": r["emitted"],
                "true_positives": r["tp"] or 0,
                "false_positives": r["fp"] or 0,
                "precision": round((r["tp"] or 0) / max((r["tp"] or 0) + (r["fp"] or 0), 1), 3),
                "avg_score": round(r["avg_score"] or 0, 3),
                "avg_p_value": round(r["avg_p"] or 0, 6),
            }
            for r in per_signal
        ],
        "by_tier": [
            {
                "tier": r["tier"],
                "emitted": r["emitted"],
                "target_fpr": targets.get(r["tier"], 0.002),
                "observed_rate": round(
                    r["emitted"] / max(cp.scalar(
                        "SELECT COUNT(*) FROM profiles p JOIN assets s "
                        "ON s.asset_id = p.asset_id WHERE s.tier = ?", (r["tier"],)
                    ), 1), 6
                ),
            }
            for r in tiers
        ],
        "detectors": [
            {"detector_id": r["detector_id"], "signal": r["signal"], "emitted": r["n"],
             "version": r["detector_version"]}
            for r in cp.query(
                "SELECT detector_id, signal, detector_version, COUNT(*) AS n FROM anomalies "
                "GROUP BY detector_id, signal, detector_version ORDER BY n DESC"
            )
        ],
    }


def _rca_seconds(cp: ControlPlane, incident: Incident) -> float | None:
    total = sum(step["duration_ms"] for step in cp.steps_for(incident.incident_id))
    if total:
        return round(total / 1000.0, 3)
    return incident.time_to_root_cause_seconds


def _short(asset_id: str) -> str:
    parts = asset_id.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else asset_id


def _dt(value: Any) -> datetime:
    from datasentry_core.clock import utc

    if isinstance(value, datetime):
        return value
    try:
        return utc(str(value))
    except Exception:  # noqa: BLE001
        return datetime.now().astimezone()
