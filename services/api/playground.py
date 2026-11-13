"""The public playground: the real engines, on the visitor's own input.

Two endpoints, each a thin adapter over code the product already runs:

``POST /playground/detect``
    A series of readings and a candidate. Runs the production conformal
    calibrator (``datasentry_detectors.conformal``) over robust
    nonconformity scores and returns the control limits in the series' own
    units, the calibrated p-value and the verdict.

``POST /playground/trace``
    One or two versions of a SQL model. Runs the production column-level
    resolver and the semantic diff (``datasentry_lineage``) and returns the
    lineage, the classified changes, and which output columns each change
    reaches.

Nothing here writes. The playground is reachable by anyone who can reach the
landing page, so three properties are enforced rather than hoped for:

* **Bounded input.** Every field has a size cap checked before any parsing,
  so the cost of a request is bounded by its validation, not its content.
* **Rate limited per client and globally.** Per client so one visitor cannot
  crowd out the rest; globally because behind a PaaS proxy every visitor can
  arrive from the same address, and the global window is the one that
  actually protects the process.
* **Serialised lineage calls.** The resolver keeps two module-level side
  tables that it clears per call. Two concurrent requests in FastAPI's
  threadpool would clear each other's state mid-resolve, so trace calls take
  a lock. Detection keeps no shared state and does not.
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from datasentry_core.config import settings
from datasentry_detectors.conformal import ConformalCalibrator
from datasentry_detectors.stats import mad, median, stdev
from datasentry_lineage import Catalog, diff_sql, resolve_model

router = APIRouter(prefix="/api/v1/playground", tags=["playground"])

MAX_READINGS = 400
MIN_READINGS = 30  # the calibrator's own min_history; below it no limit exists
MAX_SQL_CHARS = 12_000
CYCLE = timedelta(hours=3)

# Kinds of change that alter which rows a model produces rather than how one
# column is computed. Every aggregate downstream of one of these is computed
# over a different set of rows, which is exactly the silent regression the
# product exists for.
ROWSET_KINDS = {"join_type", "join_key", "join_added", "join_removed",
                "filter", "grouping", "distinct"}

_TRACE_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


class SlidingWindow:
    """Requests per rolling minute, per key, plus one global window."""

    def __init__(self, per_client: int, global_limit: int, span: float = 60.0) -> None:
        self.per_client = per_client
        self.global_limit = global_limit
        self.span = span
        self._hits: dict[str, deque[float]] = {}
        self._global: deque[float] = deque()
        self._lock = threading.Lock()

    def allow(self, key: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        cutoff = now - self.span
        with self._lock:
            while self._global and self._global[0] < cutoff:
                self._global.popleft()
            hits = self._hits.setdefault(key, deque())
            while hits and hits[0] < cutoff:
                hits.popleft()
            if len(hits) >= self.per_client or len(self._global) >= self.global_limit:
                return False
            hits.append(now)
            self._global.append(now)
            # Keep the key table from growing without bound under a scan.
            if len(self._hits) > 5000:
                for stale in [k for k, v in self._hits.items() if not v]:
                    del self._hits[stale]
            return True


# Dragging a point is continuous input; the client coalesces to one request
# in flight, so a generous per-client window still bounds a single tab.
DETECT_LIMIT = SlidingWindow(per_client=900, global_limit=6000)
TRACE_LIMIT = SlidingWindow(per_client=60, global_limit=900)


def client_key(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    return request.client.host if request.client else "unknown"


def _guard(limiter: SlidingWindow, request: Request) -> None:
    if not limiter.allow(client_key(request)):
        raise HTTPException(
            status_code=429,
            detail="Too many requests from this browser. Wait a minute and try again.",
            headers={"retry-after": "30"},
        )


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


class DetectBody(BaseModel):
    history: list[float] = Field(min_length=1, max_length=MAX_READINGS)
    candidate: float
    alpha: float = Field(default=0.01, ge=0.001, le=0.25)

    @field_validator("history")
    @classmethod
    def _finite_history(cls, values: list[float]) -> list[float]:
        if any(not math.isfinite(v) for v in values):
            raise ValueError("every reading must be a finite number")
        return values

    @field_validator("candidate")
    @classmethod
    def _finite_candidate(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("the candidate must be a finite number")
        return value


def _robust_scale(history: list[float], center: float) -> float:
    """MAD, falling back when a series is flat enough to make it zero.

    A constant series has a MAD of zero and would turn every non-identical
    reading into an infinite score. The fallbacks keep the limits finite and
    say so in the response.
    """
    spread = mad(history)
    if spread > 0:
        return spread
    spread = stdev(history)
    if spread > 0:
        return spread
    return abs(center) * 0.01 or 1.0


def run_detect(body: DetectBody) -> dict[str, Any]:
    history = list(body.history)
    n = len(history)
    center = median(history)
    scale = _robust_scale(history, center)

    cfg = settings().detection
    calibrator = ConformalCalibrator(
        window=max(n, 1),
        half_life_days=cfg.calibration_half_life_days,
        min_history=MIN_READINGS,
    )
    now = datetime(2026, 1, 1, tzinfo=UTC)
    for i, value in enumerate(history):
        at = now - CYCLE * (n - i)
        calibrator.observe("series", abs(value - center) / scale, at)

    candidate_score = abs(body.candidate - center) / scale
    verdict = calibrator.score("series", candidate_score, body.alpha, now)

    finite = math.isfinite(verdict.threshold)
    limit = verdict.threshold if finite else None
    return {
        "readings": n,
        "calibrated": finite,
        "center": center,
        "scale": scale,
        "alpha": body.alpha,
        # The finest false-alarm rate n readings can honestly promise. Asking
        # for less than 1/(n+1) silently returns the largest score seen, so
        # the page says so instead of implying a guarantee it cannot make.
        "alpha_floor": 1.0 / (n + 1),
        "threshold_score": limit,
        "upper": center + limit * scale if limit is not None else None,
        "lower": center - limit * scale if limit is not None else None,
        "candidate": body.candidate,
        "candidate_score": candidate_score,
        "p_value": verdict.p_value,
        "breached": bool(verdict.breached),
        "margin": verdict.margin if finite else None,
        "direction": "up" if body.candidate >= center else "down",
        "needs": max(0, MIN_READINGS - n),
        "method": (
            "robust score |x - median| / MAD, calibrated by split conformal "
            "prediction with a 14-day half-life; the same calibrator the "
            "detectors run"
        ),
    }


@router.post("/detect")
def detect(request: Request, body: DetectBody) -> dict[str, Any]:
    _guard(DETECT_LIMIT, request)
    return run_detect(body)


# ---------------------------------------------------------------------------
# Lineage and semantic diff
# ---------------------------------------------------------------------------


class TraceBody(BaseModel):
    before: str = Field(min_length=1, max_length=MAX_SQL_CHARS)
    after: str | None = Field(default=None, max_length=MAX_SQL_CHARS)
    model: str = Field(default="your_model", max_length=80)


def _discover_tables(sql: str) -> list[str]:
    """The physical tables a statement reads, found by resolving it once.

    The resolver trusts SQL for columns on any table the catalog knows. For a
    visitor's SQL there is no catalog, so the first pass lists what it could
    not place and the second pass registers exactly those tables.
    """
    first = resolve_model(Catalog({}), "discover", "playground.discover", sql)
    return sorted({u for u in first.unresolved if "*" not in u and not u.startswith("<")})


def _lineage(sql: str, model: str) -> dict[str, Any]:
    tables = _discover_tables(sql)
    outcome = resolve_model(Catalog({t: [] for t in tables}), model,
                            "playground." + model, sql)
    if outcome.error:
        return {"error": outcome.error, "columns": [], "tables": tables,
                "joins": [], "coverage": 0.0, "unresolved": outcome.unresolved}

    by_column: dict[str, dict[str, Any]] = {}
    join_keys: set[str] = set()
    for edge in outcome.edges:
        source = edge.src_asset_id + "." + (edge.src_column or "*")
        if edge.dst_column is None:
            join_keys.add(source)
            continue
        entry = by_column.setdefault(edge.dst_column.lower(), {
            "name": edge.dst_column,
            "transform": str(getattr(edge.transform_type, "value", edge.transform_type)),
            "expr": " ".join((edge.transform_expr or "").split()),
            "sources": [],
        })
        if source not in entry["sources"]:
            entry["sources"].append(source)

    columns = []
    for name in outcome.output_columns:
        entry = by_column.get(name.lower()) or {
            "name": name, "transform": "UNRESOLVED", "expr": "", "sources": [],
        }
        entry["sources"].sort()
        columns.append(entry)

    return {
        "error": None,
        "columns": columns,
        "tables": tables,
        "joins": [j.as_dict() for j in outcome.joins],
        "join_keys": sorted(join_keys),
        "coverage": round(outcome.coverage, 4),
        "unresolved": [u for u in outcome.unresolved if u not in tables],
    }


def _signature(column: dict[str, Any]) -> tuple:
    expr = column.get("expr", "").lower().replace(" ", "")
    return (tuple(column.get("sources", ())), column.get("transform"), expr)


def _impact(before: dict[str, Any], after: dict[str, Any],
            diff: dict[str, Any]) -> list[dict[str, Any]]:
    """Which output columns each change reaches, and how."""
    old = {c["name"].lower(): c for c in before.get("columns", [])}
    new = {c["name"].lower(): c for c in after.get("columns", [])}
    changes = diff.get("changes", [])
    rowset = [c for c in changes if c["kind"] in ROWSET_KINDS]
    rowset_reason = rowset[0]["detail"] if rowset else ""

    def expression_reason(name: str) -> str | None:
        for change in changes:
            if change["kind"] == "expression" and ("column " + name + ":").lower() in \
                    change["detail"].lower():
                return change["detail"]
        return None

    out: list[dict[str, Any]] = []
    for key, column in new.items():
        name = column["name"]
        if key not in old:
            out.append({"column": name, "status": "added", "reason": "new column"})
        elif _signature(old[key]) != _signature(column):
            out.append({"column": name, "status": "changed",
                        "reason": expression_reason(name) or "computed from different inputs"})
        elif rowset and column.get("transform") == "AGG":
            out.append({"column": name, "status": "rowset",
                        "reason": "aggregated over a different set of rows: " + rowset_reason})
        elif rowset:
            out.append({"column": name, "status": "rows",
                        "reason": "same expression, different rows: " + rowset_reason})
        else:
            out.append({"column": name, "status": "unchanged", "reason": ""})
    for key, column in old.items():
        if key not in new:
            out.append({"column": column["name"], "status": "removed",
                        "reason": "dropped from the model"})
    return out


def run_trace(body: TraceBody) -> dict[str, Any]:
    model = "".join(ch for ch in body.model if ch.isalnum() or ch in "_.") or "your_model"
    with _TRACE_LOCK:
        try:
            before = _lineage(body.before, model)
            if not body.after or body.after.strip() == body.before.strip():
                return {"before": before, "after": None, "diff": None, "impact": []}
            after = _lineage(body.after, model)
            diff = diff_sql(model, body.before, body.after).as_dict()
        except RecursionError:
            raise HTTPException(status_code=422,
                                detail="That SQL nests too deeply to read. "
                                       "Flatten a level of subqueries and try again.") from None
    impact = _impact(before, after, diff) if not (before["error"] or after["error"]) else []
    return {"before": before, "after": after, "diff": diff, "impact": impact}


@router.post("/trace")
def trace(request: Request, body: TraceBody) -> dict[str, Any]:
    _guard(TRACE_LIMIT, request)
    return run_trace(body)


# ---------------------------------------------------------------------------
# Examples
# ---------------------------------------------------------------------------

_BASE = """with orders as (
  select order_id, customer_id, amount_cents, status, created_at
  from raw.orders
  where status = 'complete'
)
select
  o.customer_id,
  c.region,
  sum(o.amount_cents) / 100.0      as revenue,
  count(distinct o.order_id)       as order_count,
  max(o.created_at)                as last_order_at
from orders o
join raw.customers c on c.customer_id = o.customer_id
group by o.customer_id, c.region"""

EXAMPLES: list[dict[str, str]] = [
    {
        "id": "fanout",
        "label": "Add a join that fans out",
        "story": "Someone joins addresses to get a postcode. Customers with two "
                 "addresses now appear twice, and revenue doubles for them. "
                 "Every test passes.",
        "before": _BASE,
        "after": _BASE.replace(
            "join raw.customers c on c.customer_id = o.customer_id",
            "left join raw.customers c on c.customer_id = o.customer_id\n"
            "left join raw.addresses a on a.customer_id = o.customer_id",
        ),
    },
    {
        "id": "units",
        "label": "Drop a unit conversion",
        "story": "A refactor moves the cents-to-dollars conversion upstream and "
                 "forgets to remove it here. Revenue is now a hundred times too "
                 "large, and still a valid number.",
        "before": _BASE,
        "after": _BASE.replace("sum(o.amount_cents) / 100.0      as revenue",
                               "sum(o.amount_cents)              as revenue"),
    },
    {
        "id": "filter",
        "label": "Widen a filter",
        "story": "Refunds start counting as completed orders. Order counts rise "
                 "a few percent — plausible enough that nobody looks.",
        "before": _BASE,
        "after": _BASE.replace("where status = 'complete'",
                               "where status in ('complete', 'refunded')"),
    },
    {
        "id": "grain",
        "label": "Change the grain",
        "story": "The model is regrouped by region only. Every downstream join "
                 "on customer_id now matches the wrong rows.",
        "before": _BASE,
        "after": _BASE.replace("  o.customer_id,\n  c.region,", "  c.region,")
                      .replace("group by o.customer_id, c.region", "group by c.region"),
    },
]


@router.get("/examples")
def examples() -> dict[str, Any]:
    return {"sql": EXAMPLES, "limits": {"sql_chars": MAX_SQL_CHARS,
                                        "readings": MAX_READINGS,
                                        "min_readings": MIN_READINGS}}
