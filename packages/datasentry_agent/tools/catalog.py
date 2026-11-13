"""The tool catalog. Every tool is read-only.

The agent identity can read the control plane, the profile history, the
lineage graph and the source repository. It cannot write to any of them
(ADR-008). This is not enforced by asking the model not to; it is enforced by
there being no write tool to call, and by the executor running under a
different identity entirely. A perfectly successful prompt injection against
the reasoning layer produces a wrong narrative on a screen.

``get_sql_semantic_diff`` is the one that earns its keep. Handing a model a
raw text diff gets "the SQL changed". Handing it

    {"joins": [{"model": "stg_orders", "from": "INNER", "to": "LEFT"}]}

gets a correct root cause. Structure the evidence and the reasoning follows.

Every result is recorded as an ``Evidence`` row with a short, quotable id.
Those ids are what claims cite, what the verifier resolves, and what the UI
turns into a clickable chip — one click from a claim to its proof.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from datasentry_core.clock import iso
from datasentry_core.enums import EvidenceKind
from datasentry_core.errors import ToolError
from datasentry_core.ids import evidence_id
from datasentry_core.models import Asset, Evidence
from datasentry_lineage.graph import LineageIndex
from datasentry_lineage.semdiff import diff_sql


@dataclass
class ToolResult:
    kind: EvidenceKind
    ref: str
    summary: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolContext:
    store: Any
    index: LineageIndex
    assets: dict[str, Asset]
    incident_id: str
    now: datetime
    window_hours: int = 6


ToolFn = Callable[[ToolContext, dict[str, Any]], ToolResult | None]

REGISTRY: dict[str, ToolFn] = {}
DESCRIPTIONS: dict[str, str] = {}


def tool(name: str, description: str) -> Callable[[ToolFn], ToolFn]:
    def wrap(fn: ToolFn) -> ToolFn:
        REGISTRY[name] = fn
        DESCRIPTIONS[name] = description
        return fn

    return wrap


# ---------------------------------------------------------------------------
# Lineage and metadata
# ---------------------------------------------------------------------------


@tool("get_lineage_neighborhood", "Column-level subgraph around an asset")
def get_lineage_neighborhood(ctx: ToolContext, args: dict[str, Any]) -> ToolResult | None:
    asset_id = args["asset_id"]
    column = args.get("column")
    graph = ctx.index.neighborhood(
        asset_id, column, up=args.get("up", 3), down=args.get("down", 3), assets=ctx.assets
    )
    if not graph.nodes:
        return None
    upstream = [n for n in graph.nodes if n.depth < 0]
    downstream = [n for n in graph.nodes if n.depth > 0]
    models = sorted({e.model_ref for e in graph.edges if e.model_ref})
    return ToolResult(
        kind=EvidenceKind.LINEAGE_PATH,
        ref=asset_id + "::" + (column or "*"),
        summary=(
            str(len(upstream)) + " upstream and " + str(len(downstream))
            + " downstream columns across " + str(len(models)) + " models"
        ),
        payload={
            "asset_id": asset_id,
            "column": column,
            "upstream": [
                {"asset_id": n.asset_id, "column": n.column_name, "depth": abs(n.depth)}
                for n in upstream[:40]
            ],
            "downstream": [
                {"asset_id": n.asset_id, "column": n.column_name, "depth": n.depth}
                for n in downstream[:40]
            ],
            "models_on_path": models[:20],
            "edge_count": len(graph.edges),
        },
    )


@tool("get_asset_metadata", "Owner, tier, layer and consumption for an asset")
def get_asset_metadata(ctx: ToolContext, args: dict[str, Any]) -> ToolResult | None:
    asset = ctx.assets.get(args["asset_id"])
    if asset is None:
        return None
    return ToolResult(
        kind=EvidenceKind.ASSET_METADATA,
        ref=asset.asset_id,
        summary=(
            asset.short_name + " is tier " + str(asset.tier) + ", owned by "
            + (asset.owner_team or "unassigned")
        ),
        payload={
            "asset_id": asset.asset_id,
            "layer": str(asset.layer),
            "kind": str(asset.kind),
            "tier": asset.tier,
            "owner_team": asset.owner_team,
            "dbt_model_path": asset.dbt_model_path,
            "repo": asset.repo,
            "query_count_30d": asset.query_count_30d,
            "distinct_consumers_30d": asset.distinct_consumers_30d,
            "columns": [c.column_name for c in asset.columns][:40],
            "is_quarantined": asset.is_quarantined,
        },
    )


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------


@tool("get_profile_history", "Time series and baseline band for one signal")
def get_profile_history(ctx: ToolContext, args: dict[str, Any]) -> ToolResult | None:
    asset_id = args["asset_id"]
    column = args.get("column")
    cycles = int(args.get("cycles", 60))
    series = ctx.store.profile_series(asset_id, column, limit=cycles)
    if len(series) < 3:
        return None

    points = [
        {
            "at": iso(p.window_end),
            "rows": p.row_count,
            "null_rate": round(p.null_rate, 6) if p.null_rate is not None else None,
            "distinct_ratio": round(p.distinct_ratio, 6) if p.distinct_ratio is not None else None,
            "freshness_s": p.freshness_seconds,
        }
        for p in series
    ]
    latest = points[-1]
    prior = points[-2]

    # Compare the latest cycle against the *baseline*, not against the cycle
    # before it. During a sustained regression the previous cycle is already
    # corrupted, so a point-to-point delta reports the noise between two wrong
    # numbers -- and reports it with the wrong sign as often as not. The
    # baseline is the median of the cycles preceding the anomaly window.
    baseline_window = points[:-4] if len(points) > 8 else points[:-1]
    baseline = {
        key: _median([p[key] for p in baseline_window if p.get(key) is not None])
        for key in ("rows", "null_rate", "distinct_ratio", "freshness_s")
    }
    moved = []
    for key in ("rows", "null_rate", "distinct_ratio", "freshness_s"):
        base, now_value = baseline.get(key), latest.get(key)
        if base and now_value is not None and base != 0:
            change = (now_value - base) / abs(base)
            if abs(change) > 0.08:
                moved.append(key + " " + format(change * 100, "+.1f") + "% vs baseline")

    return ToolResult(
        kind=EvidenceKind.PROFILE_DELTA,
        ref=asset_id + "::" + (column or "*"),
        summary=(
            str(len(points)) + " cycles of history; latest moved "
            + (", ".join(moved) if moved else "within its usual range")
        ),
        payload={
            "asset_id": asset_id,
            "column": column,
            "cycles": len(points),
            "points": points[-60:],
            "latest": latest,
            "prior": prior,
            "baseline": {k: (round(v, 6) if v is not None else None) for k, v in baseline.items()},
            "changed": moved,
        },
    )


# ---------------------------------------------------------------------------
# Source control
# ---------------------------------------------------------------------------


@tool("diff_git_range", "Commits touching the lineage path in a time window")
def diff_git_range(ctx: ToolContext, args: dict[str, Any]) -> ToolResult | None:
    since = args.get("since") or (ctx.now - timedelta(hours=ctx.window_hours))
    paths = set(args.get("paths") or [])
    rows = ctx.store.query(
        "SELECT * FROM git_commits WHERE committed_at >= ? ORDER BY committed_at DESC LIMIT 40",
        (iso(since),),
    )
    commits = []
    for row in rows:
        files = _loads(row["files"])
        touches = (not paths) or bool(paths & set(files))
        commits.append(
            {
                "sha": row["sha"],
                "repo": row["repo"],
                "author": row["author"],
                "message": row["message"],
                "pr_number": row["pr_number"],
                "files": files,
                "at": row["committed_at"],
                "touches_lineage": touches,
            }
        )
    if not commits:
        return None
    on_path = [c for c in commits if c["touches_lineage"]]
    return ToolResult(
        kind=EvidenceKind.GIT_COMMIT,
        ref=(on_path[0]["sha"] if on_path else commits[0]["sha"]),
        summary=(
            str(len(commits)) + " commits in the window, " + str(len(on_path))
            + " touching the lineage path"
        ),
        payload={
            "window_start": iso(since),
            "window_end": iso(ctx.now),
            "commits": commits[:12],
            "on_lineage_path": [c["sha"] for c in on_path],
        },
    )


@tool("get_sql_semantic_diff", "Structured AST diff between two versions of a model")
def get_sql_semantic_diff(ctx: ToolContext, args: dict[str, Any]) -> ToolResult | None:
    model_ref = args.get("model_ref")
    if not model_ref:
        return None
    rows = ctx.store.query(
        "SELECT * FROM model_versions WHERE model_ref = ? ORDER BY created_at DESC LIMIT 2",
        (model_ref,),
    )
    if len(rows) < 2:
        return None
    after, before = rows[0], rows[1]
    diff = diff_sql(model_ref, before["sql"], after["sql"], before["sha"], after["sha"])
    if diff.is_empty:
        return None
    return ToolResult(
        kind=EvidenceKind.SQL_DIFF,
        ref=after["sha"],
        summary=_short(model_ref) + ": " + diff.headline(),
        payload=dict(diff.as_dict(), sql_before=before["sql"], sql_after=after["sql"]),
    )


# ---------------------------------------------------------------------------
# Orchestration telemetry
# ---------------------------------------------------------------------------


@tool("get_dbt_runs", "Run status, duration, row counts and test results")
def get_dbt_runs(ctx: ToolContext, args: dict[str, Any]) -> ToolResult | None:
    model_ref = args.get("model_ref")
    since = args.get("since") or (ctx.now - timedelta(hours=ctx.window_hours * 4))
    if model_ref:
        rows = ctx.store.query(
            "SELECT * FROM dbt_runs WHERE model_ref = ? AND started_at >= ? "
            "ORDER BY started_at DESC LIMIT 12",
            (model_ref, iso(since)),
        )
    else:
        rows = ctx.store.query(
            "SELECT * FROM dbt_runs WHERE started_at >= ? ORDER BY started_at DESC LIMIT 12",
            (iso(since),),
        )
    if not rows:
        return None
    runs = [
        {
            "run_id": r["run_id"],
            "model_ref": r["model_ref"],
            "status": r["status"],
            "rows_affected": r["rows_affected"],
            "duration_ms": r["duration_ms"],
            "tests_passed": r["tests_passed"],
            "tests_failed": r["tests_failed"],
            "started_at": r["started_at"],
        }
        for r in rows
    ]
    failed = [r for r in runs if r["status"] != "success" or r["tests_failed"]]
    return ToolResult(
        kind=EvidenceKind.DBT_RUN,
        ref=runs[0]["run_id"],
        summary=(
            str(len(runs)) + " recent runs, " + str(len(failed)) + " with failures"
            if failed else str(len(runs)) + " recent runs, all green"
        ),
        payload={"runs": runs, "failed": failed},
    )


@tool("get_query_history", "Queries that read or wrote the asset")
def get_query_history(ctx: ToolContext, args: dict[str, Any]) -> ToolResult | None:
    since = args.get("since") or (ctx.now - timedelta(hours=ctx.window_hours * 4))
    rows = ctx.store.query(
        "SELECT * FROM query_log WHERE asset_id = ? AND executed_at >= ? "
        "ORDER BY executed_at DESC LIMIT ?",
        (args["asset_id"], iso(since), int(args.get("limit", 25))),
    )
    if not rows:
        return None
    queries = [
        {
            "query_id": r["query_id"],
            "operation": r["operation"],
            "user": r["user_name"],
            "rows_produced": r["rows_produced"],
            "at": r["executed_at"],
        }
        for r in rows
    ]
    writes = [q for q in queries if q["operation"] in {"INSERT", "MERGE", "CREATE", "COPY"}]
    return ToolResult(
        kind=EvidenceKind.QUERY_LOG,
        ref=queries[0]["query_id"],
        summary=str(len(queries)) + " queries, " + str(len(writes)) + " writes",
        payload={"queries": queries, "writes": writes},
    )


@tool("get_schema_changes", "DDL events recorded against the asset")
def get_schema_changes(ctx: ToolContext, args: dict[str, Any]) -> ToolResult | None:
    since = args.get("since") or (ctx.now - timedelta(days=7))
    rows = ctx.store.query(
        "SELECT * FROM schema_changes WHERE asset_id = ? AND detected_at >= ? "
        "ORDER BY detected_at DESC LIMIT 20",
        (args["asset_id"], iso(since)),
    )
    if not rows:
        return None
    changes = [
        {
            "column": r["column_name"],
            "kind": r["change_kind"],
            "before": r["before_value"],
            "after": r["after_value"],
            "at": r["detected_at"],
        }
        for r in rows
    ]
    return ToolResult(
        kind=EvidenceKind.SCHEMA_CHANGE,
        ref=str(rows[0]["change_id"]),
        summary=(
            str(len(changes)) + " schema change(s): "
            + ", ".join(c["kind"] + " on " + str(c["column"]) for c in changes[:3])
        ),
        payload={"asset_id": args["asset_id"], "changes": changes},
    )


@tool("get_upstream_anomalies", "Anomalies on ancestors in the same window")
def get_upstream_anomalies(ctx: ToolContext, args: dict[str, Any]) -> ToolResult | None:
    asset_id = args["asset_id"]
    walk = ctx.index.upstream(asset_id, args.get("column"), max_depth=4, limit=200)
    ancestors = {r.asset_id for r in walk.reached}
    if not ancestors:
        return None
    since = args.get("since") or (ctx.now - timedelta(hours=ctx.window_hours))
    found = []
    for ancestor in sorted(ancestors)[:40]:
        for anomaly in ctx.store.anomalies_for_asset(ancestor, limit=6):
            if anomaly.detected_at >= since:
                found.append(
                    {
                        "asset_id": anomaly.asset_id,
                        "column": anomaly.column_name,
                        "signal": str(anomaly.signal),
                        "score": round(anomaly.score, 3),
                        "at": iso(anomaly.detected_at),
                        "explanation": anomaly.explanation,
                    }
                )
    if not found:
        return None
    found.sort(key=lambda f: f["at"])
    return ToolResult(
        kind=EvidenceKind.UPSTREAM_ANOMALY,
        ref=found[0]["asset_id"],
        summary=(
            str(len(found)) + " anomalies on ancestors, earliest on "
            + _short(found[0]["asset_id"])
        ),
        payload={"anomalies": found[:20], "earliest": found[0]},
    )


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------


@tool("search_past_incidents", "Similar historical incidents and their resolutions")
def search_past_incidents(ctx: ToolContext, args: dict[str, Any]) -> ToolResult | None:
    asset_id = args.get("asset_id")
    rows = ctx.store.query(
        """
        SELECT i.incident_id, i.title, i.severity, i.status, i.first_detected_at,
               d.verdict, d.correct_cause_class
        FROM incidents i
        LEFT JOIN dispositions d ON d.incident_id = i.incident_id
        WHERE i.root_asset_id = ? AND i.incident_id <> ?
        ORDER BY i.first_detected_at DESC LIMIT ?
        """,
        (asset_id, ctx.incident_id, int(args.get("k", 5))),
    )
    if not rows:
        return None
    past = [
        {
            "incident_id": r["incident_id"],
            "title": r["title"],
            "severity": r["severity"],
            "status": r["status"],
            "at": r["first_detected_at"],
            "verdict": r["verdict"],
            "cause_class": r["correct_cause_class"],
        }
        for r in rows
    ]
    return ToolResult(
        kind=EvidenceKind.PAST_INCIDENT,
        ref=past[0]["incident_id"],
        summary=str(len(past)) + " prior incidents on this asset",
        payload={"incidents": past},
    )


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def run_tool(name: str, ctx: ToolContext, args: dict[str, Any]) -> ToolResult | None:
    fn = REGISTRY.get(name)
    if fn is None:
        raise ToolError("no such tool: " + name)
    try:
        return fn(ctx, args)
    except KeyError as exc:
        raise ToolError(name + " missing argument " + str(exc)) from exc


def to_evidence(incident_id: str, tool_name: str, result: ToolResult,
                collected_at: datetime) -> Evidence:
    return Evidence(
        evidence_id=evidence_id(incident_id, tool_name, result.ref),
        incident_id=incident_id,
        kind=result.kind,
        ref=result.ref,
        summary=result.summary,
        payload=result.payload,
        collected_by=tool_name,
        collected_at=collected_at,
    )


def catalog() -> list[dict[str, str]]:
    return [{"name": n, "description": DESCRIPTIONS[n]} for n in sorted(REGISTRY)]


def _short(text: str) -> str:
    parts = str(text).split("/")
    return parts[-1] if parts else str(text)


def _median(values: list) -> float | None:
    clean = sorted(v for v in values if v is not None)
    if not clean:
        return None
    mid = len(clean) // 2
    return clean[mid] if len(clean) % 2 else (clean[mid - 1] + clean[mid]) / 2


def _loads(raw: str | None) -> list:
    import json

    if not raw:
        return []
    try:
        value = json.loads(raw)
        return value if isinstance(value, list) else []
    except json.JSONDecodeError:
        return []
