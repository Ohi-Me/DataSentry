"""Evidence collection.

Two passes, and the split matters.

**The baseline pass runs in Contextualize, before any hypothesis exists.**
That ordering is forced by the type system and it is the right ordering
anyway: ``Claim.evidence_ids`` has ``min_length=1``, so a hypothesis
physically cannot be constructed before there is evidence to cite. A model
asked to theorise first and find support later is a model asked to decide
what it believes and then go looking for reasons — which is the failure mode
the whole design is built to prevent.

**The targeted pass runs in GatherEvidence**, executing the tools the
hypotheses actually asked for. This is where an investigation gets to be
curious rather than merely thorough.

The baseline set is collected unconditionally. If collection were driven only
by what the hypotheses requested, an investigation could only ever confirm
what it already suspected: the model would ask for the diff that supports its
theory and never for the lineage path that refutes it. Unconditional baseline
collection is what gives the verifier something to reject with.
"""

from __future__ import annotations

from typing import Any

from datasentry_agent.guardrails import scan_for_injection
from datasentry_agent.nodes import Deps
from datasentry_agent.state import RCAState
from datasentry_agent.tools.catalog import ToolContext, run_tool, to_evidence
from datasentry_core.errors import ToolError
from datasentry_core.logging import get_logger

log = get_logger("orchestrator")

# Collected for every incident, whatever the hypotheses ask for.
BASELINE_TOOLS = [
    "get_profile_history",
    "get_lineage_neighborhood",
    "get_asset_metadata",
    "diff_git_range",
    "get_sql_semantic_diff",
    "get_upstream_anomalies",
    "get_dbt_runs",
    # Schema events are cheap to fetch and the single most diagnostic
    # artefact when they exist. Leaving them to the targeted pass meant a
    # schema-drift hypothesis was formed with nothing to cite and then
    # correctly rejected for citing nothing -- an investigation failing on a
    # technicality it created itself.
    "get_schema_changes",
    "get_query_history",
    "search_past_incidents",
]

# Requested tools are capped so one enthusiastic round cannot exhaust the
# budget for the next one.
MAX_REQUESTED = 6


def base_args(state: RCAState, deps: Deps) -> dict[str, Any]:
    dossier = state.dossier
    if dossier is None:
        return {}
    incident = dossier.incident
    asset = dossier.root_asset
    args: dict[str, Any] = {
        "asset_id": incident.root_asset_id,
        "column": incident.root_column,
    }
    if asset and asset.dbt_model_path:
        args["model_ref"] = asset.dbt_model_path
        args["paths"] = [asset.dbt_model_path]
    return args


def collect(state: RCAState, deps: Deps, tool_names: list[str]) -> int:
    """Run the named tools and fold their results into ``state.evidence``."""
    if state.dossier is None:
        return 0

    # Every lookback window is anchored to when the incident *started*, not to
    # when the investigation happens to run. Anchoring to now means a run
    # kicked off a day late looks for the causing commit in the wrong day and
    # reports, with complete confidence, that no commit touched the lineage
    # path. Wrong and certain is the worst combination this system can produce.
    anchor = state.dossier.incident.first_detected_at if state.dossier else deps.now

    ctx = ToolContext(
        store=deps.store,
        index=deps.index,
        assets=deps.assets,
        incident_id=state.incident_id,
        now=anchor,
        window_hours=deps.window_hours,
    )
    args = base_args(state, deps)
    seen = {e.evidence_id for e in state.evidence}
    added = 0

    for name in tool_names:
        if deps.budget.tool_calls >= deps.budget.limits.max_tool_calls:
            log.event("tool budget reached", incident=state.incident_id,
                      collected=len(state.evidence))
            break
        deps.budget.tool_calls += 1
        try:
            result = run_tool(name, ctx, dict(args))
        except ToolError as exc:
            # A tool that cannot answer is absence of evidence, not an error
            # that ends the run. "No commit touched this path" is a finding.
            log.event("tool unavailable", tool=name, reason=str(exc)[:120])
            continue
        except Exception as exc:  # noqa: BLE001 - one bad tool must not sink the run
            log.event("tool failed", tool=name, error=type(exc).__name__)
            continue
        if result is None:
            continue

        markers = scan_for_injection(result.payload)
        if markers:
            # Values never reach a prompt (ADR-010), so this is defence in
            # depth over the fields that are free text — commit messages, SQL
            # comments. A hit is surfaced rather than dropped: somebody trying
            # it is itself worth knowing about.
            result.payload = dict(result.payload)
            result.payload["_injection_markers"] = markers
            log.event("injection markers in evidence", tool=name, markers=markers[:3])

        # ...but the artefact is stamped with when it was actually collected.
        evidence = to_evidence(state.incident_id, name, result, deps.now)
        if evidence.evidence_id in seen:
            continue
        seen.add(evidence.evidence_id)
        state.evidence.append(evidence)
        added += 1

    if added:
        deps.store.insert_evidence(state.evidence)
    return added


def collect_baseline(state: RCAState, deps: Deps) -> int:
    return collect(state, deps, BASELINE_TOOLS)


def make_gather_evidence(deps: Deps):
    """Run the tools the hypotheses asked for, beyond the baseline set."""

    def gather(state: RCAState) -> None:
        requested: list[str] = []
        for hypothesis in state.hypotheses:
            if hypothesis.status != "proposed":
                continue
            for name in hypothesis.evidence_requests:
                if name not in requested and name not in BASELINE_TOOLS:
                    requested.append(name)
        if requested:
            collect(state, deps, requested[:MAX_REQUESTED])

    return gather
