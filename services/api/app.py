"""The API and the console it serves.

REST for reads and dispositions, a WebSocket for live incident push, and the
static console mounted at the root so ``ds serve`` gives you the whole product
on one port with no build step.

Two endpoints here are product surfaces rather than internal plumbing:
``/health/detectors`` publishes the observed false-positive rate against
target, and ``/health/agent`` publishes the gold-set scores for the deployed
prompt version. A system that publishes its own accuracy is making a much
stronger claim than one that only publishes its successes — and it is the
thesis of the whole project, so it gets a URL.

Every mutating call writes an ``audit_log`` row through middleware rather than
in each handler, so it cannot be forgotten by whoever adds the next endpoint.
"""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from datasentry_core.clock import iso
from datasentry_core.config import REPO_ROOT, settings
from datasentry_core.enums import ActionStatus, IncidentStatus, RiskTier, Verdict
from datasentry_core.ids import stable_uuid
from datasentry_core.logging import configure
from datasentry_core.models import Disposition
from datasentry_core.store import ControlPlane
from datasentry_lineage import LineageIndex, compute_blast_radius
from services.action_executor.executor import ActionExecutor
from services.api.playground import router as playground_router
from services.api.presenters import (
    action_view,
    asset_view,
    detector_health,
    incident_detail,
    incident_summary,
    lineage_view,
    profile_series_view,
)
from services.policy_gate.policy import PolicyGate

WEB_ROOT = REPO_ROOT / "web"
log = configure("api")


class Hub:
    """WebSocket fan-out.

    The API keeps one broadcast set and patches clients rather than telling
    them to refetch. During an incident burst, "invalidate and refetch" turns
    one event into N round trips per connected browser, which is how a live
    console becomes the thing that falls over during the outage it exists to
    show you.
    """

    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        self._lock = asyncio.Lock()
        self.sent = 0

    async def join(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._clients.add(ws)

    async def leave(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.discard(ws)

    async def broadcast(self, kind: str, payload: dict[str, Any]) -> None:
        message = json.dumps({"type": kind, "payload": payload, "at": iso(datetime.now(UTC))})
        async with self._lock:
            targets = list(self._clients)
        for ws in targets:
            try:
                await ws.send_text(message)
                self.sent += 1
            except Exception:  # noqa: BLE001 - a dead socket is not an error
                await self.leave(ws)


HUB = Hub()
STARTED_AT = time.time()


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.store = ControlPlane()
    app.state.index = LineageIndex(app.state.store.all_edges())
    app.state.assets = {
        a.asset_id: app.state.store.get_asset(a.asset_id)
        for a in app.state.store.list_assets(limit=2000)
    }
    log.event("api ready", assets=len(app.state.assets),
              edges=len(app.state.index.edges),
              incidents=app.state.store.scalar("SELECT COUNT(*) FROM incidents"))
    yield


app = FastAPI(
    title="DataSentry",
    version="0.2.0",
    description="Lakehouse reliability control plane",
    lifespan=lifespan,
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
)

# Same-origin by default, because the deployed shape is one process serving
# both the console and the API -- there is no cross-site call to allow. A
# split deployment names its web origin in DS_CORS_ORIGINS. There is no
# wildcard path: `allow_origins=["*"]` with `allow_credentials=True` is
# rejected by every browser anyway, and the version that works is the one
# that quietly lets any page on the internet drive an approval endpoint.
app.include_router(playground_router)

_origins = settings().cors_origin_list()
if _origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["content-type", "x-ds-user"],
    )


def store(request: Request) -> ControlPlane:
    return request.app.state.store


def index(request: Request) -> LineageIndex:
    return request.app.state.index


def assets(request: Request) -> dict:
    return request.app.state.assets


@app.middleware("http")
async def no_cache_console(request: Request, call_next):
    """Serve console assets uncached.

    There is no build step and no content hash in the filenames, so a browser
    that caches these will happily serve last hour's JavaScript against this
    hour's API and produce a bug that exists only on one machine. Revalidation
    costs nothing on localhost and removes a whole category of phantom.
    """
    response = await call_next(request)
    if request.url.path.startswith(("/assets", "/console")) or request.url.path == "/":
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response


@app.middleware("http")
async def audit_mutations(request: Request, call_next):
    """Every mutating call is audited by middleware, not by each handler."""
    response = await call_next(request)
    # The playground's POSTs are computations on the visitor's input, not
    # mutations. Auditing them would bury real dispositions under every drag
    # of a chart point on the landing page.
    if (request.method in {"POST", "PUT", "PATCH", "DELETE"} and response.status_code < 400
            and not request.url.path.startswith(API + "/playground")):
        try:
            request.app.state.store.audit(
                actor=request.headers.get("x-ds-user", "anonymous"),
                actor_kind="human",
                action=request.method.lower() + " " + request.url.path,
                target=request.url.path,
                detail={"query": dict(request.query_params),
                        "status": response.status_code},
                at=datetime.now(UTC),
            )
        except Exception:  # noqa: BLE001 - auditing must never break a request
            log.event("audit write failed", path=request.url.path)
    return response


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------


class DispositionBody(BaseModel):
    verdict: Verdict
    correct_cause_class: str | None = None
    notes: str | None = Field(default=None, max_length=2000)
    user_id: str = "analyst"


class ApprovalBody(BaseModel):
    note: str | None = Field(default=None, max_length=500)
    user_id: str = "analyst"
    role: str = "admin"


class RejectionBody(BaseModel):
    reason: str = Field(min_length=1, max_length=500)
    user_id: str = "analyst"


class MuteBody(BaseModel):
    hours: int = Field(default=24, ge=1, le=720)
    reason: str = Field(min_length=1, max_length=300)
    column: str | None = None
    user_id: str = "analyst"


# ---------------------------------------------------------------------------
# Incidents
# ---------------------------------------------------------------------------

API = "/api/v1"


@app.get(API + "/incidents")
def list_incidents(
    request: Request,
    status: str | None = None,
    severity: int | None = None,
    asset_id: str | None = None,
    limit: int = Query(default=100, le=500),
) -> dict[str, Any]:
    cp = store(request)
    items = cp.list_incidents(status=status, severity=severity, asset_id=asset_id, limit=limit)
    return {
        "items": [incident_summary(cp, i) for i in items],
        "counts": _status_counts(cp),
    }


@app.get(API + "/incidents/{incident_id}")
def get_incident(request: Request, incident_id: str) -> dict[str, Any]:
    cp = store(request)
    incident = cp.get_incident(incident_id)
    if incident is None:
        raise HTTPException(404, "no such incident")
    return incident_detail(cp, incident, assets(request))


@app.get(API + "/incidents/{incident_id}/trace")
def get_trace(request: Request, incident_id: str) -> dict[str, Any]:
    cp = store(request)
    steps = cp.steps_for(incident_id)
    incident = cp.get_incident(incident_id)
    if incident is None:
        raise HTTPException(404, "no such incident")
    return {
        "incident_id": incident_id,
        "trace_id": incident.trace_id,
        "prompt_version": incident.prompt_version,
        "nodes": steps,
        "totals": {
            "duration_ms": sum(s["duration_ms"] for s in steps),
            "tokens_in": sum(s["tokens_in"] for s in steps),
            "tokens_out": sum(s["tokens_out"] for s in steps),
            "usd": round(sum(s["usd"] for s in steps), 6),
            "cheap_calls": sum(1 for s in steps if s["model"] == "cheap"),
            "frontier_calls": sum(1 for s in steps if s["model"] == "frontier"),
        },
    }


@app.post(API + "/incidents/{incident_id}/disposition", status_code=201)
async def dispose(request: Request, incident_id: str, body: DispositionBody) -> dict[str, Any]:
    """Capture the human verdict.

    This is the feedback loop, not a comment box. Dispositions become labels
    for conformal recalibration and rows in the agent eval set, which is what
    makes the system get better instead of merely older.
    """
    cp = store(request)
    incident = cp.get_incident(incident_id)
    if incident is None:
        raise HTTPException(404, "no such incident")
    now = datetime.now(UTC)
    disposition = Disposition(
        disposition_id=stable_uuid("disposition", incident_id, body.user_id, iso(now)),
        incident_id=incident_id,
        verdict=body.verdict,
        correct_cause_class=body.correct_cause_class,
        notes=body.notes,
        user_id=body.user_id,
        created_at=now,
    )
    cp.insert_disposition(disposition)
    if body.verdict in {Verdict.FALSE_POSITIVE, Verdict.EXPECTED, Verdict.DUPLICATE}:
        cp.set_incident_status(incident_id, IncidentStatus.DISMISSED)
    await HUB.broadcast("incident.updated", {"incident_id": incident_id,
                                             "verdict": str(body.verdict)})
    return {"disposition_id": disposition.disposition_id, "verdict": str(body.verdict)}


@app.post(API + "/incidents/{incident_id}/mute", status_code=201)
def mute(request: Request, incident_id: str, body: MuteBody) -> dict[str, Any]:
    cp = store(request)
    incident = cp.get_incident(incident_id)
    if incident is None:
        raise HTTPException(404, "no such incident")
    from datetime import timedelta

    until = datetime.now(UTC) + timedelta(hours=body.hours)
    cp.execute(
        "INSERT OR REPLACE INTO mutes (asset_id, column_name, reason, until, created_by) "
        "VALUES (?,?,?,?,?)",
        (incident.root_asset_id, body.column or "", body.reason, iso(until), body.user_id),
    )
    # Mutes always expire. There is no open-ended mute in this system, because
    # an open-ended mute is how a monitored asset stops being monitored
    # without anyone deciding to stop monitoring it.
    return {"asset_id": incident.root_asset_id, "until": iso(until)}


@app.post(API + "/incidents/{incident_id}/rerun-rca", status_code=202)
async def rerun_rca(request: Request, incident_id: str,
                    prompt_version: str | None = None) -> dict[str, Any]:
    """Replay the graph against the frozen evidence set.

    This is how prompts get improved without guessing: same incident, same
    evidence, new prompt version, diff the outcome.
    """
    cp = store(request)
    incident = cp.get_incident(incident_id)
    if incident is None:
        raise HTTPException(404, "no such incident")

    from datasentry_agent.runner import Orchestrator

    orchestrator = Orchestrator(store=cp, index=index(request), assets=assets(request))
    incident.status = IncidentStatus.DETECTED
    result = await asyncio.to_thread(orchestrator.run, incident)
    await HUB.broadcast("incident.updated", {"incident_id": incident_id, "rca": "complete"})
    return {"run": result.as_dict()}


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


@app.get(API + "/actions")
def list_actions(request: Request, status: str | None = None,
                 risk_tier: str | None = None,
                 limit: int = Query(default=100, le=500)) -> dict[str, Any]:
    cp = store(request)
    items = cp.list_actions(status=status, risk_tier=risk_tier, limit=limit)
    return {"items": [action_view(cp, a) for a in items]}


@app.get(API + "/actions/{action_id}")
def get_action(request: Request, action_id: str) -> dict[str, Any]:
    cp = store(request)
    action = cp.get_action(action_id)
    if action is None:
        raise HTTPException(404, "no such action")
    return action_view(cp, action, full=True)


@app.post(API + "/actions/{action_id}/approve", status_code=202)
async def approve(request: Request, action_id: str, body: ApprovalBody) -> dict[str, Any]:
    cp = store(request)
    action = cp.get_action(action_id)
    if action is None:
        raise HTTPException(404, "no such action")

    gate = PolicyGate()
    if not gate.can_approve(RiskTier(str(action.risk_tier)), body.role):
        # Role enforcement lives here rather than in the UI, because the UI is
        # not a security boundary.
        raise HTTPException(
            403,
            "role " + body.role + " cannot approve a " + str(action.risk_tier) + "-risk action",
        )

    action.status = ActionStatus.APPROVED
    action.approved_by = body.user_id
    action.approved_at = datetime.now(UTC)
    cp.upsert_action(action)

    executor = ActionExecutor(cp)
    result = await asyncio.to_thread(executor.execute, action)
    cp.set_incident_status(action.incident_id, IncidentStatus.VERIFYING)
    await HUB.broadcast("action.completed", {
        "action_id": action_id, "incident_id": action.incident_id,
        "status": str(result.status), "external_ref": result.external_ref,
    })
    return {"action_id": action_id, "status": str(result.status),
            "external_ref": result.external_ref, "detail": result.detail}


@app.post(API + "/actions/{action_id}/reject")
async def reject(request: Request, action_id: str, body: RejectionBody) -> dict[str, Any]:
    cp = store(request)
    action = cp.get_action(action_id)
    if action is None:
        raise HTTPException(404, "no such action")
    action.status = ActionStatus.REJECTED
    action.result = {"rejected_by": body.user_id, "reason": body.reason}
    cp.upsert_action(action)
    # A rejection is a training signal, not a dead end: it says the plan was
    # wrong for a reason a human can name.
    await HUB.broadcast("action.completed", {"action_id": action_id, "status": "rejected"})
    return {"action_id": action_id, "status": "rejected"}


# ---------------------------------------------------------------------------
# Assets and lineage
# ---------------------------------------------------------------------------


@app.get(API + "/assets")
def list_assets(request: Request, layer: str | None = None, tier: int | None = None,
                search: str | None = None,
                limit: int = Query(default=200, le=1000)) -> dict[str, Any]:
    cp = store(request)
    items = cp.list_assets(layer=layer, tier=tier, search=search, limit=limit)
    return {"items": [asset_view(cp, a) for a in items]}


@app.get(API + "/assets/{asset_id}")
def get_asset(request: Request, asset_id: str) -> dict[str, Any]:
    cp = store(request)
    asset = cp.get_asset(asset_id)
    if asset is None:
        raise HTTPException(404, "no such asset")
    return asset_view(cp, asset, full=True)


@app.get(API + "/assets/{asset_id}/profile")
def get_profile(request: Request, asset_id: str, column: str | None = None,
                cycles: int = Query(default=90, le=400)) -> dict[str, Any]:
    cp = store(request)
    return profile_series_view(cp, asset_id, column, cycles)


@app.get(API + "/assets/{asset_id}/lineage")
def get_lineage(request: Request, asset_id: str, column: str | None = None,
                depth: int = Query(default=3, ge=1, le=8)) -> dict[str, Any]:
    return lineage_view(index(request), assets(request), asset_id, column, depth)


@app.get(API + "/assets/{asset_id}/blast-radius")
def get_blast_radius(request: Request, asset_id: str,
                     column: str | None = None) -> dict[str, Any]:
    entries, walk = compute_blast_radius(index(request), assets(request), asset_id, column)
    return {
        "asset_id": asset_id,
        "column": column,
        "entries": [e.model_dump(mode="json") for e in entries],
        "elapsed_ms": round(walk.elapsed_ms, 3),
        "truncated": walk.truncated,
    }


# ---------------------------------------------------------------------------
# Health — product surfaces, not internal endpoints
# ---------------------------------------------------------------------------


@app.get(API + "/health/detectors")
def health_detectors(request: Request) -> dict[str, Any]:
    return detector_health(store(request))


@app.get(API + "/health/calibration")
def health_calibration(request: Request) -> dict[str, Any]:
    """Observed false-positive rate against target, per cohort.

    Measured against human dispositions rather than against the detector's own
    opinion of itself, which is the only version of this number worth
    publishing. A product surface, not internal plumbing: a system that shows
    you where its own calibration has drifted is making a much stronger claim
    than one that only shows you its catches.
    """
    from services.detector.recalibration import RecalibrationJob

    job = RecalibrationJob(store(request))
    cohorts = job.latest()
    return {
        "cohorts": cohorts,
        "drifted": [c for c in cohorts if c["drifted"]],
        "note": (
            "Empty until incidents have been disposed of. The loop is "
            "disposition -> label -> threshold, and it starts with a human."
        ),
    }


@app.post(API + "/health/recalibrate")
def run_recalibration(request: Request) -> dict[str, Any]:
    from services.detector.recalibration import RecalibrationJob

    report = RecalibrationJob(store(request)).run()
    return report.as_dict()


@app.get(API + "/health/pipeline")
def health_pipeline(request: Request) -> dict[str, Any]:
    cp = store(request)
    topics = cp.topic_counts()
    profiled = cp.scalar("SELECT COUNT(DISTINCT asset_id || '::' || IFNULL(column_name,'*')) FROM profiles")
    total_columns = cp.scalar("SELECT COUNT(*) FROM asset_columns") + cp.scalar("SELECT COUNT(*) FROM assets")
    parsed_models = cp.scalar("SELECT COUNT(DISTINCT model_ref) FROM lineage_edges WHERE source = 'declared'")
    all_models = cp.scalar("SELECT COUNT(*) FROM assets WHERE dbt_model_path IS NOT NULL")
    return {
        "uptime_s": round(time.time() - STARTED_AT, 1),
        "topics": topics,
        "events_total": sum(topics.values()),
        "profiles": cp.scalar("SELECT COUNT(*) FROM profiles"),
        "series_profiled": profiled,
        "profiling_coverage": round(profiled / total_columns, 4) if total_columns else 0.0,
        "lineage_edges": cp.scalar("SELECT COUNT(*) FROM lineage_edges"),
        "lineage_parse_coverage": round(parsed_models / all_models, 4) if all_models else 0.0,
        "observed_edges": cp.scalar("SELECT COUNT(*) FROM lineage_edges WHERE source = 'observed'"),
        "anomalies": cp.scalar("SELECT COUNT(*) FROM anomalies"),
        "incidents": cp.scalar("SELECT COUNT(*) FROM incidents"),
        "websocket_clients": len(HUB._clients),
        "websocket_messages": HUB.sent,
    }


@app.get(API + "/health/agent")
def health_agent(request: Request) -> dict[str, Any]:
    cp = store(request)
    rows = cp.query(
        "SELECT prompt_version, COUNT(*) AS runs, AVG(llm_cost_usd) AS avg_cost, "
        "SUM(token_count) AS tokens FROM incidents WHERE prompt_version IS NOT NULL "
        "GROUP BY prompt_version"
    )
    steps = cp.query(
        "SELECT model, COUNT(*) AS n, AVG(duration_ms) AS ms FROM agent_steps "
        "WHERE model IS NOT NULL GROUP BY model"
    )
    tiers = {r["model"]: r["n"] for r in steps}
    cheap, frontier = tiers.get("cheap", 0), tiers.get("frontier", 0)

    claims = cp.query("SELECT claims FROM hypotheses")
    evidence_ids = {e["evidence_id"] for e in cp.query("SELECT evidence_id FROM evidence")}
    total_claims = 0
    fabricated = 0
    for row in claims:
        for claim in json.loads(row["claims"] or "[]"):
            total_claims += 1
            if any(eid not in evidence_ids for eid in claim.get("evidence_ids", [])):
                fabricated += 1

    verified = cp.scalar("SELECT COUNT(*) FROM hypotheses WHERE status = 'verified'")
    rejected = cp.scalar("SELECT COUNT(*) FROM hypotheses WHERE status = 'rejected'")
    durations = [
        r["ms"] for r in cp.query(
            "SELECT SUM(duration_ms) AS ms FROM agent_steps GROUP BY incident_id"
        )
    ]
    durations.sort()

    return {
        "prompt_versions": [
            {"version": r["prompt_version"], "runs": r["runs"],
             "avg_cost_usd": round(r["avg_cost"] or 0, 6), "tokens": r["tokens"] or 0}
            for r in rows
        ],
        "model_split": {
            "cheap": cheap, "frontier": frontier,
            "cheap_ratio": round(cheap / (cheap + frontier), 3) if (cheap + frontier) else 0.0,
            "target_cheap_ratio": 0.6,
        },
        "hypotheses": {"verified": verified, "rejected": rejected,
                       "rejection_rate": round(rejected / (verified + rejected), 3)
                       if (verified + rejected) else 0.0},
        "hallucination_rate": round(fabricated / total_claims, 5) if total_claims else 0.0,
        "hallucination_target": 0.02,
        "claims_checked": total_claims,
        "latency_ms": {
            "p50": durations[len(durations) // 2] if durations else 0,
            "p95": durations[int(len(durations) * 0.95)] if durations else 0,
        },
        "cost_per_incident_usd": round(
            cp.scalar("SELECT AVG(llm_cost_usd) FROM incidents WHERE rca_completed_at IS NOT NULL",
                      default=0.0) or 0.0, 6
        ),
        "cost_target_usd": 0.15,
    }


@app.get("/healthz")
def liveness() -> dict[str, Any]:
    """Unauthenticated liveness, for a platform health check.

    Deliberately says nothing about the data: a load balancer asking whether
    to keep routing here does not need the incident count, and an endpoint a
    platform polls every ten seconds should not touch the database.
    """
    return {"status": "ok", "service": "datasentry", "version": app.version}


@app.get("/readyz")
def readiness(request: Request) -> dict[str, Any]:
    """Readiness: the control plane is reachable and the lineage index is built.

    Separate from liveness because the answers imply different actions. A
    failed liveness check means restart me; a failed readiness check means
    stop sending traffic while I finish coming up, and restarting would only
    make it take longer.
    """
    try:
        assets_count = int(request.app.state.store.scalar("SELECT COUNT(*) FROM assets") or 0)
    except Exception:  # noqa: BLE001
        return JSONResponse({"status": "degraded", "reason": "control plane unreadable"},
                            status_code=503)
    return {
        "status": "ready",
        "assets": assets_count,
        "lineage_edges": len(request.app.state.index.edges),
        "seeded": assets_count > 0,
    }


@app.get(API + "/health/reasoning")
def health_reasoning() -> dict[str, Any]:
    """Which reasoner is answering, and what it would cost.

    On the Health page next to the accuracy numbers, because the two are read
    together: a recall figure means something different when it was produced
    by the deterministic baseline than when a model produced it, and the page
    should not make you guess which.
    """
    from datasentry_agent.providers import registry
    from datasentry_agent.providers.base import PRICING

    cfg = settings()
    info = registry.describe(cfg)
    rates = {
        tier: {
            "model": info[tier + "_model"],
            "usd_per_m_input": PRICING.get(info[tier + "_model"], (0.0, 0.0))[0],
            "usd_per_m_output": PRICING.get(info[tier + "_model"], (0.0, 0.0))[1],
        }
        for tier in ("cheap", "frontier")
    }
    return {
        **info,
        "rates": rates,
        "free_tier": info["provider"] in {"groq", "gemini", "heuristic"},
        "budget_usd": cfg.budget.max_usd,
        "prompt_version": _prompt_version(),
    }


def _prompt_version() -> str:
    from datasentry_agent.providers.base import prompt_version

    try:
        return prompt_version()
    except Exception:  # noqa: BLE001
        return "unknown"


@app.get(API + "/health/policy")
def health_policy() -> dict[str, Any]:
    return PolicyGate().describe()


@app.get(API + "/health/scores")
def health_scores(request: Request) -> dict[str, Any]:
    """Ground-truth scores for the current run, if a fault harness seeded it."""
    from tools.pipeline.demo import score_run

    cp = store(request)
    if not cp.scalar("SELECT COUNT(*) FROM faults"):
        return {"available": False}
    scores = score_run(cp)
    scores["available"] = True
    scores["targets"] = {
        "recall": 0.90, "precision": 0.75, "decoy_fpr": 0.10,
        "root_accuracy": 0.85, "cause_accuracy": 0.80,
        "hallucination_rate": 0.02, "blast_recall": 0.95, "median_cost": 0.15,
    }
    return scores


@app.get(API + "/audit")
def audit_tail(request: Request, limit: int = Query(default=80, le=500)) -> dict[str, Any]:
    return {"items": store(request).audit_tail(limit)}


@app.get("/metrics", response_class=PlainTextResponse)
def metrics(request: Request) -> str:
    """Prometheus exposition. Text, deliberately, so it needs no client."""
    cp = store(request)
    lines = [
        "# HELP datasentry_incidents_total Incidents by status",
        "# TYPE datasentry_incidents_total gauge",
    ]
    for row in cp.query("SELECT status, COUNT(*) AS n FROM incidents GROUP BY status"):
        lines.append(
            'datasentry_incidents_total{status="' + row["status"] + '"} ' + str(row["n"])
        )
    lines += [
        "# HELP datasentry_anomalies_total Anomalies by signal",
        "# TYPE datasentry_anomalies_total gauge",
    ]
    for row in cp.query("SELECT signal, COUNT(*) AS n FROM anomalies GROUP BY signal"):
        lines.append(
            'datasentry_anomalies_total{signal="' + row["signal"] + '"} ' + str(row["n"])
        )
    for name, value in [
        ("datasentry_profiles_total", cp.scalar("SELECT COUNT(*) FROM profiles")),
        ("datasentry_lineage_edges", cp.scalar("SELECT COUNT(*) FROM lineage_edges")),
        ("datasentry_actions_total", cp.scalar("SELECT COUNT(*) FROM actions")),
        ("datasentry_llm_cost_usd", round(
            cp.scalar("SELECT SUM(llm_cost_usd) FROM incidents", default=0.0) or 0.0, 6)),
        ("datasentry_uptime_seconds", round(time.time() - STARTED_AT, 1)),
    ]:
        lines.append("# TYPE " + name + " gauge")
        lines.append(name + " " + str(value))
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------


@app.websocket("/ws/incidents")
async def incidents_socket(ws: WebSocket) -> None:
    await HUB.join(ws)
    try:
        await ws.send_text(json.dumps({"type": "hello", "payload": {"ok": True}}))
        while True:
            # The client sends heartbeats; we reply so a proxy does not reap
            # an idle connection during a quiet warehouse.
            await ws.receive_text()
            await ws.send_text(json.dumps({"type": "pong", "payload": {}}))
    except WebSocketDisconnect:
        await HUB.leave(ws)
    except Exception:  # noqa: BLE001
        await HUB.leave(ws)


# ---------------------------------------------------------------------------
# Console
# ---------------------------------------------------------------------------


def _status_counts(cp: ControlPlane) -> dict[str, int]:
    rows = cp.query("SELECT status, COUNT(*) AS n FROM incidents GROUP BY status")
    return {r["status"]: r["n"] for r in rows}


if (WEB_ROOT / "assets").exists():
    app.mount("/assets", StaticFiles(directory=str(WEB_ROOT / "assets")), name="assets")


def _page(name: str, *, status_code: int = 200) -> HTMLResponse:
    page = WEB_ROOT / name
    if not page.exists():
        return HTMLResponse(
            "<h1>DataSentry</h1><p>Web assets are missing from " + str(WEB_ROOT) + "</p>",
            status_code=500,
        )
    return HTMLResponse(page.read_text(encoding="utf-8"), status_code=status_code)


@app.get("/", response_class=HTMLResponse)
def landing() -> HTMLResponse:
    """What DataSentry is, how it works, and the playground."""
    return _page("landing.html")


@app.get("/console", response_class=HTMLResponse)
@app.get("/console/", response_class=HTMLResponse, include_in_schema=False)
def console() -> HTMLResponse:
    return _page("index.html")


@app.exception_handler(404)
async def spa_fallback(request: Request, exc: Any) -> Any:
    """Console routes fall back to the console shell; the API stays honest.

    Anything else that does not exist gets the landing page with a real 404
    status, so a mistyped link lands somewhere useful without telling a
    crawler that the path exists.
    """
    path = request.url.path
    if path.startswith(("/api", "/metrics", "/ws", "/assets")):
        return JSONResponse({"detail": "not found"}, status_code=404)
    if path.startswith("/console"):
        return _page("index.html")
    return _page("landing.html", status_code=404)
