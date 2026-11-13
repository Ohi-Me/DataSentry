"""Contextualize: assemble the incident dossier. No model involved.

Assembling a dossier is a database problem. Anomalies with their baselines,
the lineage neighbourhood, the blast radius, the commits on that path, recent
job runs and schema events — all of it is a set of joins, and paying a
frontier model to perform joins would be slow, expensive and untestable.

This node produces the entire factual basis the later reasoning nodes work
from, which is why it runs first and why keeping it deterministic matters
more than it looks. Everything downstream inherits its correctness.
"""

from __future__ import annotations

from datetime import timedelta

from datasentry_agent.nodes import Deps
from datasentry_agent.nodes.evidence import collect_baseline
from datasentry_agent.state import Dossier, RCAState
from datasentry_core.clock import iso
from datasentry_lineage.graph import compute_blast_radius


def make_contextualize(deps: Deps):
    def contextualize(state: RCAState) -> None:
        store = deps.store
        incident = store.get_incident(state.incident_id)
        if incident is None:
            state.terminal = "inconclusive"
            return

        asset = deps.assets.get(incident.root_asset_id)
        anomalies = store.anomalies_for_incident(state.incident_id)
        since = incident.first_detected_at - timedelta(hours=deps.window_hours)

        downstream, walk = compute_blast_radius(
            deps.index, deps.assets, incident.root_asset_id, incident.root_column
        )

        # Commits are annotated with whether they touch the lineage path, not
        # merely whether they landed in the window. That distinction is what
        # the verifier uses to reject post-hoc-ergo-propter-hoc reasoning, and
        # it has to be computed here rather than inferred by a model later.
        models_on_path = {
            e.model_ref
            for e in deps.index.edges
            if e.model_ref and (
                e.dst_asset_id == incident.root_asset_id
                or e.src_asset_id == incident.root_asset_id
            )
        }
        if asset and asset.dbt_model_path:
            models_on_path.add(asset.dbt_model_path)

        commits = []
        for row in store.query(
            "SELECT * FROM git_commits WHERE committed_at >= ? ORDER BY committed_at DESC LIMIT 30",
            (iso(since),),
        ):
            files = _loads(row["files"])
            touches = bool(models_on_path & set(files))
            model_ref = files[0] if files else None
            commit = {
                "sha": row["sha"],
                "repo": row["repo"],
                "author": row["author"],
                "message": row["message"],
                "pr_number": row["pr_number"],
                "files": files,
                "model_ref": model_ref,
                "at": row["committed_at"],
                "touches_lineage": touches,
            }
            # Attach the structured diff to the commit that carried it. A
            # commit and a diff sitting in separate lists means the reasoning
            # layer has to join them by inference, which is exactly the kind
            # of guess that turns into "a commit landed, therefore the commit
            # did it". Joined here, "this commit changed the join" is a fact
            # the verifier can check rather than a correlation.
            if touches and model_ref:
                commit["semantic_diff"] = _semantic_diff(store, model_ref, row["sha"])
            commits.append(commit)

        runs = [
            {
                "run_id": r["run_id"],
                "model_ref": r["model_ref"],
                "status": r["status"],
                "rows_affected": r["rows_affected"],
                "tests_failed": r["tests_failed"],
                "started_at": r["started_at"],
            }
            for r in store.query(
                "SELECT * FROM dbt_runs WHERE started_at >= ? ORDER BY started_at DESC LIMIT 15",
                (iso(since),),
            )
        ]

        schema_changes = [
            {
                "column": r["column_name"],
                "kind": r["change_kind"],
                "before": r["before_value"],
                "after": r["after_value"],
                "at": r["detected_at"],
            }
            for r in store.query(
                "SELECT * FROM schema_changes WHERE asset_id = ? AND detected_at >= ? "
                "ORDER BY detected_at DESC LIMIT 10",
                (incident.root_asset_id, iso(since)),
            )
        ]

        upstream_walk = deps.index.upstream(
            incident.root_asset_id, incident.root_column, max_depth=4, limit=120
        )
        ancestors = {r.asset_id for r in upstream_walk.reached}
        upstream_anomalies = []
        for ancestor in sorted(ancestors)[:30]:
            for anomaly in store.anomalies_for_asset(ancestor, limit=4):
                if anomaly.detected_at >= since:
                    upstream_anomalies.append(anomaly)
        upstream_anomalies.sort(key=lambda a: a.detected_at)

        state.dossier = Dossier(
            incident=incident,
            root_asset=asset,
            anomalies=anomalies,
            upstream_assets=[deps.assets[a] for a in sorted(ancestors)[:20] if a in deps.assets],
            downstream=downstream,
            recent_commits=commits,
            recent_runs=runs,
            schema_changes=schema_changes,
            upstream_anomalies=upstream_anomalies[:20],
            lineage_paths=[
                {"asset_id": r.asset_id, "column": r.column, "depth": r.depth}
                for r in walk.reached[:40]
            ],
        )

        # Collect the baseline evidence set here, before any hypothesis
        # exists. Claim.evidence_ids has min_length=1, so a hypothesis
        # literally cannot be constructed until there is something to cite --
        # which is the type system enforcing the right order of work.
        collect_baseline(state, deps)

    return contextualize


def _loads(raw: str | None) -> list:
    import json

    if not raw:
        return []
    try:
        value = json.loads(raw)
        return value if isinstance(value, list) else []
    except json.JSONDecodeError:
        return []


def _semantic_diff(store, model_ref: str, sha: str) -> dict:
    """Structured diff between this commit's version of a model and the prior one."""
    from datasentry_lineage.semdiff import diff_sql

    rows = store.query(
        "SELECT * FROM model_versions WHERE model_ref = ? ORDER BY created_at DESC LIMIT 4",
        (model_ref,),
    )
    if len(rows) < 2:
        return {}
    # Only diff the version this commit actually produced. Falling back to the
    # newest version would attribute somebody else's change to this commit,
    # which is exactly the false causal link the verifier exists to catch --
    # and it would be arriving pre-laundered as evidence.
    index = next((i for i, r in enumerate(rows) if r["sha"] == sha), None)
    if index is None or index + 1 >= len(rows):
        return {}
    after = rows[index]
    before = rows[index + 1]
    diff = diff_sql(model_ref, before["sql"], after["sql"], before["sha"], after["sha"])
    return diff.as_dict() if not diff.is_empty else {}
