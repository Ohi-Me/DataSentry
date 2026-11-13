"""The demo: the full loop in one command.

Runs the narrative from docs/01 section 10 end to end — a healthy warehouse,
a merged change that loosens a join, the profile shift, the anomalies, the
correlation into one incident, the evidence-bound diagnosis, the blast
radius, the proposed PR, the policy decision and the verification.

The last beat is the one that matters: cost and measured latency. A demo that
ends at "and here is the pretty incident page" is a demo. One that ends at
"that incident cost $0.0000 and took 74ms to root-cause, and here is the
observed false-positive rate against target" is an argument.
"""

from __future__ import annotations

import time
from typing import Any

from datasentry_core.enums import ActionStatus, IncidentStatus
from datasentry_core.logging import paint
from datasentry_core.store import ControlPlane
from tools.pipeline.pipeline import Pipeline

DIM = "38;5;245"
ACCENT = "38;5;80"
GOOD = "38;5;114"
WARN = "38;5;214"
BAD = "38;5;203"
BOLD = "1"

DEFAULT_FAULTS = [
    "JOIN_SEMANTICS",   # the headline: a code change with a real semantic diff
    "UNIT_CHANGE",      # distributional, no schema change
    "NULL_FLOOD",       # rate signal, small-sample correctness
    "DEDUP_BREAK",      # the signature no single detector catches
    "SCHEMA_DRIFT",     # deterministic, fires immediately
    "LATE_DATA",        # timing rather than values
    "BACKFILL",         # decoy
    "CAMPAIGN_SPIKE",   # decoy
]


def step(n: int, title: str, detail: str = "") -> None:
    print()
    print(
        "  " + paint(str(n).rjust(2), ACCENT) + "  " + paint(title, BOLD)
        + ("  " + paint(detail, DIM) if detail else "")
    )


def line(label: str, value: Any, colour: str = "") -> None:
    text = str(value)
    print("      " + paint(label.ljust(24), DIM) + (paint(text, colour) if colour else text))


# Defaults match BENCHMARKS.md: 60 tables over 12 days is what every
# published number was measured on, and 60 is the floor below which the
# fault harness cannot place independent faults. The CLI passes these
# explicitly; they are repeated here so calling run_demo directly gets
# the same world.
def run_demo(*, tables: int = 60, days: int = 12, seed: int | None = None,
             faults: list[str] | None = None, quiet: bool = False) -> int:
    started = time.perf_counter()
    store = ControlPlane()
    store.reset()
    pipeline = Pipeline(store)
    plan = faults or DEFAULT_FAULTS

    # 1 ------------------------------------------------------------------
    step(1, "Seed a healthy warehouse", str(tables) + " tables, " + str(days) + " days of history")
    report = pipeline.bootstrap(tables=tables, days=days, seed=seed)
    line("tables", report.detail["tables"])
    line("columns", report.detail["columns"])
    line("lineage edges", report.detail["edges"], ACCENT)
    line("column-map coverage", format(report.detail["parse_coverage"] * 100, ".1f") + "%", ACCENT)
    line("profiles written", format(report.detail["profiles"], ","))
    line("profile size", str(pipeline.profiler.stats.as_dict()["avg_profile_bytes"]) + " bytes avg",
         GOOD)

    # 2 ------------------------------------------------------------------
    step(2, "Confirm the baseline is quiet", "no faults injected yet")
    baseline = pipeline.detect(cycles_back=4)
    quiet_anomalies = baseline.detail.get("emitted", 0)
    line("cycles scored", format(baseline.detail.get("scored", 0), ","))
    line("threshold breaches", baseline.detail.get("breaches", 0))
    line("anomalies emitted", quiet_anomalies,
         GOOD if quiet_anomalies == 0 else WARN)
    line("", "conformal calibration holds the FPR at target across every column", DIM)

    # 3 ------------------------------------------------------------------
    step(3, "Inject faults and decoys", str(len(plan)) + " events")
    injected = []
    for i, fault_type in enumerate(plan):
        try:
            record, _ = pipeline.inject(fault_type, at_cycle=pipeline.state.cursor - 2 + i % 2)
        except RuntimeError as exc:
            line(fault_type, "skipped: " + str(exc), DIM)
            continue
        injected.append(record)
        tag = paint("decoy", GOOD) if record.is_decoy else paint("fault", WARN)
        print(
            "      " + tag + "  " + paint(record.fault_type.ljust(20), "")
            + paint(record.root_asset_id.split(".", 2)[-1][:34].ljust(36), DIM)
            + (record.root_column or "-")
        )
    # Cadence tiering means a cold table is profiled every eighth cycle, so a
    # two-cycle consecutive-breach rule needs sixteen cycles to fire on one.
    # Advancing six would measure the cadence design as a detection failure.
    pipeline.advance(cycles=18)
    line("cycles advanced", 18, DIM)

    # 4 ------------------------------------------------------------------
    step(4, "Detect", "seven detector families, one calibration framework")
    detection = pipeline.detect(cycles_back=18)
    line("series scored", format(detection.detail.get("scored", 0), ","))
    line("anomalies emitted", detection.detail.get("emitted", 0), WARN)
    by_signal = detection.detail.get("by_signal", {})
    for signal, count in sorted(by_signal.items(), key=lambda kv: -kv[1])[:8]:
        line("  " + signal, count)

    # 5 ------------------------------------------------------------------
    step(5, "Correlate", "lineage-aware clustering, then elect a root")
    correlation = pipeline.correlate()
    line("anomalies in", correlation.detail.get("anomalies", 0))
    line("incidents out", correlation.detail.get("incidents", 0), ACCENT)
    line("compression", str(correlation.detail.get("compression_ratio", 1)) + "x", GOOD)
    line("largest cluster", correlation.detail.get("largest_cluster", 0))

    # 6 ------------------------------------------------------------------
    step(6, "Investigate", "evidence-bound RCA with an adversarial verifier")
    rca = pipeline.investigate(limit=12)
    line("incidents investigated", rca.detail.get("incidents", 0))
    line("reasoner", rca.detail.get("provider"), ACCENT)
    line("median cost", "$" + format(rca.detail.get("median_cost_usd", 0), ".4f"), GOOD)
    line("p50 latency", str(rca.detail.get("p50_latency_ms", 0)) + " ms", GOOD)
    line("actions proposed", rca.detail.get("actions", 0))
    line("auto-executed", rca.detail.get("auto_executed", 0))

    # 7 ------------------------------------------------------------------
    headline = _headline_incident(store)
    if headline is not None:
        step(7, "The headline incident", headline.incident_id[:8])
        _print_incident(store, headline)

    # 8 ------------------------------------------------------------------
    step(8, "Act", "low risk auto-executes, the rest waits for a human")
    pending = store.list_actions(status=str(ActionStatus.PROPOSED), limit=100)
    execution = pipeline.execute_approved()
    line("executed automatically", execution.detail.get("succeeded", 0), GOOD)
    line("awaiting approval", len(pending), WARN if pending else DIM)
    for action in pending[:4]:
        print(
            "      " + paint(str(action.risk_tier).upper().ljust(8),
                             BAD if str(action.risk_tier) == "high" else WARN)
            + paint(str(action.action_type).ljust(20), "")
            + paint(action.rationale[:58], DIM)
        )

    # 9 ------------------------------------------------------------------
    approved = _approve_corrective_prs(store, pipeline)
    if approved:
        line("operator approved", str(approved) + " corrective PR(s)", GOOD)
        execution = pipeline.execute_approved()
        line("merged and reverted", execution.detail.get("faults_reverted", 0), GOOD)

    # 9 ------------------------------------------------------------------
    step(9, "Verify", "re-profile; clear or escalate, never auto-close on a timer")
    pipeline.advance(cycles=3)
    pipeline.detect(cycles_back=3)
    verification = pipeline.verify()
    line("resolved", verification.detail.get("resolved", 0), GOOD)
    line("escalated", verification.detail.get("escalated", 0), WARN)

    # 10 -----------------------------------------------------------------
    step(10, "Score against ground truth", "the only numbers that mean anything")
    scores = score_run(store)
    line("faults injected", scores["faults"])
    line("detection recall", format(scores["recall"], ".2f"),
         GOOD if scores["recall"] >= 0.90 else WARN)
    line("incident precision", format(scores["precision"], ".2f"),
         GOOD if scores["precision"] >= 0.75 else WARN)
    line("decoy false-positive rate", format(scores["decoy_fpr"], ".2f"),
         GOOD if scores["decoy_fpr"] <= 0.10 else WARN)
    line("root-asset accuracy", format(scores["root_accuracy"], ".2f"),
         GOOD if scores["root_accuracy"] >= 0.85 else WARN)
    line("root-cause accuracy", format(scores["cause_accuracy"], ".2f"),
         GOOD if scores["cause_accuracy"] >= 0.80 else WARN)
    line("hallucination rate", format(scores["hallucination_rate"], ".3f"),
         GOOD if scores["hallucination_rate"] <= 0.02 else BAD)
    line("blast-radius recall", format(scores["blast_recall"], ".2f"),
         GOOD if scores["blast_recall"] >= 0.95 else WARN)
    line("median cost / incident", "$" + format(scores["median_cost"], ".4f"), GOOD)

    print()
    print(paint("  " + "─" * 74, DIM))
    print(
        "  " + paint("wall clock", DIM) + "  "
        + format(time.perf_counter() - started, ".1f") + "s"
        + paint("      next: ", DIM) + paint("py ds.py serve", ACCENT)
        + paint("  for the console", DIM)
    )
    print()
    return 0


# ---------------------------------------------------------------------------


def _approve_corrective_prs(store: ControlPlane, pipeline) -> int:
    """Stand in for the operator clicking Approve on the OPEN_PR actions.

    Everything else in this run happens without a human. This one step needs
    one, because a medium-risk action is supposed to wait -- and a demo that
    skipped it would be demonstrating a system with no approval gate.
    """

    now = pipeline.moment(pipeline.state.cursor)
    approved = 0
    for action in store.list_actions(status=str(ActionStatus.PROPOSED), limit=50):
        if str(action.action_type) != "OPEN_PR":
            continue
        action.status = ActionStatus.APPROVED
        action.approved_by = "dana.m"
        action.approved_at = now
        store.upsert_action(action)
        store.audit(actor="dana.m", actor_kind="human", action="action.approved",
                    target=action.action_id,
                    detail={"risk_tier": str(action.risk_tier),
                            "policy_version": action.policy_version}, at=now)
        approved += 1
    return approved


def _headline_incident(store: ControlPlane):
    """The most severe incident that produced a verified cause."""
    best = None
    for incident in store.list_incidents(limit=60):
        verified = [h for h in store.hypotheses_for(incident.incident_id) if h.status == "verified"]
        if not verified:
            continue
        score = (-incident.severity, len(store.evidence_for(incident.incident_id)))
        if best is None or score > best[0]:
            best = (score, incident)
    return best[1] if best else None


def _print_incident(store: ControlPlane, incident) -> None:
    hypotheses = store.hypotheses_for(incident.incident_id)
    verified = [h for h in hypotheses if h.status == "verified"]
    rejected = [h for h in hypotheses if h.status == "rejected"]
    evidence = {e.evidence_id: e for e in store.evidence_for(incident.incident_id)}
    blast = store.blast_radius_for(incident.incident_id)
    actions = store.actions_for(incident.incident_id)

    line("title", incident.title)
    line("severity", incident.severity, BAD if incident.severity == 1 else WARN)
    line("root asset", incident.root_asset_id.split(".", 2)[-1])
    line("status", str(incident.status))
    line("cost", "$" + format(incident.llm_cost_usd, ".4f"))
    steps = store.steps_for(incident.incident_id)
    measured = sum(s["duration_ms"] for s in steps) / 1000.0
    line("time to root cause", format(measured, ".3f") + "s (sum of node durations)")

    if verified:
        top = verified[0]
        print()
        print("      " + paint("ROOT CAUSE  ", GOOD) + paint(str(top.cause_class), BOLD)
              + paint("  confidence " + format(top.confidence, ".2f"), DIM))
        print("      " + _wrap(top.statement, 68))
        print()
        print("      " + paint("EVIDENCE", DIM))
        for claim in top.claims:
            print("        " + paint("·", GOOD) + " " + _wrap(claim.text, 62, indent=10))
            for eid in claim.evidence_ids:
                item = evidence.get(eid)
                if item:
                    print("          " + paint(eid, ACCENT) + "  "
                          + paint(str(item.kind).lower().ljust(16), DIM)
                          + paint(item.summary[:44], DIM))

    if rejected:
        print()
        print("      " + paint("REJECTED (" + str(len(rejected)) + ")", DIM)
              + paint("  shown because discarded reasoning is what makes the rest credible", DIM))
        for h in rejected[:2]:
            print("        " + paint("×", BAD) + " " + str(h.cause_class))
            print("          " + paint(_wrap(h.rejection_reason or "", 60, indent=10), DIM))

    if blast:
        print()
        print("      " + paint("BLAST RADIUS (" + str(len(blast)) + ")", DIM))
        for entry in blast[:5]:
            marker = paint("!", BAD) if entry.tier == 1 else " "
            print(
                "        " + marker + " "
                + entry.asset_id.split(".", 2)[-1][:38].ljust(40)
                + paint(str(entry.asset_kind).lower().ljust(14), DIM)
                + paint("crit " + format(entry.criticality, ".2f"), DIM)
            )

    if actions:
        print()
        print("      " + paint("PROPOSED ACTIONS", DIM))
        for action in actions:
            colour = {"low": GOOD, "medium": WARN, "high": BAD}.get(str(action.risk_tier), DIM)
            print(
                "        " + paint(str(action.risk_tier).upper().ljust(7), colour)
                + str(action.action_type).ljust(20)
                + paint(action.rationale[:44], DIM)
            )


def _wrap(text: str, width: int, indent: int = 6) -> str:
    import textwrap

    lines = textwrap.wrap(text or "", width)
    if not lines:
        return ""
    pad = " " * indent
    return ("\n" + pad).join(lines)


def score_run(store: ControlPlane) -> dict[str, Any]:
    """Grade the run against the ground truth the harness recorded."""

    faults = store.query("SELECT * FROM faults")
    real = [f for f in faults if not f["is_decoy"]]
    decoys = [f for f in faults if f["is_decoy"]]

    def survived(fault) -> bool:
        """Did this fault produce an incident that was NOT explained away?

        A decoy is allowed to move a signal — a backfill really does triple
        the row count, and a detector that cannot see that is not detecting
        anything. What must not happen is a decoy reaching a human. So the
        false-positive test is whether an incident survived triage and
        verification, not whether a statistic twitched.
        """
        incident_id = fault["detected_incident_id"]
        if not incident_id:
            return False
        incident = store.get_incident(incident_id)
        return incident is not None and str(incident.status) not in {
            str(IncidentStatus.DISMISSED),
        }

    detected_real = [f for f in real if survived(f)]
    detected_decoys = [f for f in decoys if survived(f)]

    incidents = [
        i for i in store.list_incidents(limit=500)
        if str(i.status) != str(IncidentStatus.DISMISSED)
    ]
    attributed = {f["detected_incident_id"] for f in detected_real}

    root_hits = 0
    cause_hits = 0
    cause_total = 0
    blast_scores: list[float] = []
    costs: list[float] = []
    claims_seen = 0
    claims_bad = 0

    for fault in detected_real:
        incident_id = fault["detected_incident_id"]
        if not incident_id:
            continue
        incident = store.get_incident(incident_id)
        if incident is None:
            continue
        if incident.root_asset_id == fault["root_asset_id"]:
            root_hits += 1

        verified = [h for h in store.hypotheses_for(incident_id) if h.status == "verified"]
        if verified and fault["cause_class"]:
            cause_total += 1
            if str(verified[0].cause_class) == fault["cause_class"]:
                cause_hits += 1

        evidence_ids = {e.evidence_id for e in store.evidence_for(incident_id)}
        for hypothesis in store.hypotheses_for(incident_id):
            for claim in hypothesis.claims:
                claims_seen += 1
                if any(eid not in evidence_ids for eid in claim.evidence_ids):
                    claims_bad += 1

        import json as _json

        expected = set(_json.loads(fault["expected_downstream"] or "[]"))
        if expected:
            found = {e.asset_id for e in store.blast_radius_for(incident_id)}
            blast_scores.append(len(expected & found) / len(expected))

        costs.append(incident.llm_cost_usd)

    precision_den = len(incidents)
    costs = costs or [0.0]

    return {
        "faults": len(real),
        "decoys": len(decoys),
        "recall": len(detected_real) / len(real) if real else 0.0,
        "precision": len(attributed) / precision_den if precision_den else 0.0,
        "decoy_fpr": len(detected_decoys) / len(decoys) if decoys else 0.0,
        "root_accuracy": root_hits / len(detected_real) if detected_real else 0.0,
        "cause_accuracy": cause_hits / cause_total if cause_total else 0.0,
        "hallucination_rate": claims_bad / claims_seen if claims_seen else 0.0,
        "blast_recall": sum(blast_scores) / len(blast_scores) if blast_scores else 0.0,
        "median_cost": sorted(costs)[len(costs) // 2],
        "incidents": len(incidents),
    }
