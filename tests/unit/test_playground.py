"""The public playground: real engines on untrusted input.

Grouped by the promise each test holds the playground to. The ones that
matter most are not the happy paths -- they are that a visitor's input is
bounded before it is parsed, that one tab cannot starve the rest, that two
visitors tracing SQL at once do not corrupt each other's lineage, and that
dragging a chart point on the landing page never writes to the audit log.
"""

from __future__ import annotations

import random
import threading

import pytest
from services.api.playground import (
    EXAMPLES,
    MAX_SQL_CHARS,
    DetectBody,
    SlidingWindow,
    TraceBody,
    run_detect,
    run_trace,
)


def _history(n: int = 60, seed: int = 7) -> list[float]:
    rng = random.Random(seed)
    return [1200 + rng.gauss(0, 40) for _ in range(n)]


def _example(example_id: str) -> dict[str, str]:
    return next(e for e in EXAMPLES if e["id"] == example_id)


# ---------------------------------------------------------------------------
# Detection is the production calibrator, and it says what it cannot promise
# ---------------------------------------------------------------------------


def test_an_ordinary_reading_is_in_control():
    result = run_detect(DetectBody(history=_history(), candidate=1215, alpha=0.05))
    assert result["calibrated"] is True
    assert result["breached"] is False
    assert result["lower"] < 1215 < result["upper"]


def test_a_doubled_reading_is_out_of_control_in_either_direction():
    history = _history()
    up = run_detect(DetectBody(history=history, candidate=2400, alpha=0.05))
    down = run_detect(DetectBody(history=history, candidate=600, alpha=0.05))
    assert up["breached"] and up["direction"] == "up"
    assert down["breached"] and down["direction"] == "down"


def test_limits_are_in_the_series_own_units_and_bracket_the_centre():
    result = run_detect(DetectBody(history=_history(), candidate=1200, alpha=0.05))
    assert result["lower"] < result["center"] < result["upper"]
    # Symmetric about the median, because the score is an absolute deviation.
    assert result["upper"] - result["center"] == pytest.approx(
        result["center"] - result["lower"])


def test_a_looser_target_draws_tighter_limits():
    history = _history(200)
    strict = run_detect(DetectBody(history=history, candidate=1200, alpha=0.005))
    loose = run_detect(DetectBody(history=history, candidate=1200, alpha=0.20))
    assert loose["upper"] < strict["upper"]


def test_too_few_readings_calibrate_nothing_rather_than_guessing():
    result = run_detect(DetectBody(history=_history(12), candidate=99999))
    assert result["calibrated"] is False
    assert result["breached"] is False
    assert result["upper"] is None
    assert result["needs"] == 30 - 12


def test_the_finest_honest_rate_is_reported():
    """With n readings no split-conformal bound goes below 1/(n+1)."""
    result = run_detect(DetectBody(history=_history(60), candidate=1200, alpha=0.001))
    assert result["alpha_floor"] == pytest.approx(1 / 61)


def test_a_flat_series_still_has_finite_limits():
    result = run_detect(DetectBody(history=[500.0] * 40, candidate=520))
    assert result["scale"] > 0
    assert result["upper"] is not None


# ---------------------------------------------------------------------------
# Lineage and diff are the production resolver, on SQL with no catalog
# ---------------------------------------------------------------------------


def test_every_output_column_is_traced_through_a_cte_with_no_catalog():
    result = run_trace(TraceBody(before=_example("fanout")["before"]))
    lineage = result["before"]
    assert lineage["error"] is None
    assert lineage["coverage"] == 1.0
    revenue = next(c for c in lineage["columns"] if c["name"] == "revenue")
    assert revenue["sources"] == ["raw.orders.amount_cents"]
    assert revenue["transform"] == "AGG"
    assert set(lineage["tables"]) == {"raw.orders", "raw.customers"}
    assert result["after"] is None and result["diff"] is None


def test_a_loosened_join_reaches_every_aggregate():
    result = run_trace(TraceBody(**{k: _example("fanout")[k] for k in ("before", "after")}))
    assert result["diff"]["breaking_count"] >= 1
    status = {i["column"]: i["status"] for i in result["impact"]}
    assert status["revenue"] == "rowset"
    assert status["order_count"] == "rowset"
    assert status["customer_id"] == "rows"


def test_a_dropped_conversion_reaches_only_the_column_it_changed():
    result = run_trace(TraceBody(**{k: _example("units")[k] for k in ("before", "after")}))
    status = {i["column"]: i["status"] for i in result["impact"]}
    assert status["revenue"] == "changed"
    assert {s for c, s in status.items() if c != "revenue"} == {"unchanged"}


def test_a_removed_column_is_reported_as_removed():
    result = run_trace(TraceBody(**{k: _example("grain")[k] for k in ("before", "after")}))
    status = {i["column"]: i["status"] for i in result["impact"]}
    assert status["customer_id"] == "removed"


def test_unreadable_sql_is_reported_not_raised():
    result = run_trace(TraceBody(before="this is not sql"))
    assert "expected SELECT" in result["before"]["error"]
    assert result["before"]["columns"] == []


def test_concurrent_traces_do_not_corrupt_each_other():
    """The resolver clears module-level side tables per call.

    Without the lock, one request's clear lands in the middle of another's
    resolve and the transform classification comes back wrong. Every
    thread must see the answer a lone request sees.
    """
    body = TraceBody(**{k: _example("fanout")[k] for k in ("before", "after")})
    expected = run_trace(body)
    results: list[dict] = []
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            for _ in range(4):
                results.append(run_trace(body))
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assert below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert all(r == expected for r in results)


# ---------------------------------------------------------------------------
# Bounded input and rate limits
# ---------------------------------------------------------------------------


def test_the_window_admits_the_limit_then_refuses():
    window = SlidingWindow(per_client=3, global_limit=100)
    assert [window.allow("a", now=0.0) for _ in range(4)] == [True, True, True, False]
    assert window.allow("b", now=0.0) is True  # another client is unaffected


def test_the_window_slides():
    window = SlidingWindow(per_client=1, global_limit=100, span=60)
    assert window.allow("a", now=0.0)
    assert not window.allow("a", now=30.0)
    assert window.allow("a", now=61.0)


def test_the_global_window_protects_the_process_behind_a_shared_proxy():
    window = SlidingWindow(per_client=100, global_limit=2)
    assert window.allow("a", now=0.0)
    assert window.allow("b", now=0.0)
    assert not window.allow("c", now=0.0)


@pytest.fixture
def client(tmp_path, monkeypatch):
    from datasentry_core.config import reset_settings

    monkeypatch.setenv("DS_DB_PATH", str(tmp_path / "control.db"))
    reset_settings()
    from fastapi.testclient import TestClient
    from services.api.app import app

    with TestClient(app) as c:
        yield c
    reset_settings()


def test_oversized_sql_is_refused_before_it_is_parsed(client):
    response = client.post("/api/v1/playground/trace",
                           json={"before": "select 1 " * (MAX_SQL_CHARS // 4)})
    assert response.status_code == 422


def test_oversized_series_is_refused(client):
    response = client.post("/api/v1/playground/detect",
                           json={"history": [1.0] * 5000, "candidate": 1.0})
    assert response.status_code == 422


def test_an_out_of_range_target_rate_is_refused(client):
    response = client.post("/api/v1/playground/detect",
                           json={"history": _history(), "candidate": 1.0, "alpha": 0.9})
    assert response.status_code == 422


def test_playground_calls_never_reach_the_audit_log(client):
    """The audit log is append-only and records human dispositions.

    Every drag of the landing page's chart is a POST. If those were audited
    the log would be unreadable within an hour of the page going live.
    """
    store = client.app.state.store
    before = store.scalar("SELECT COUNT(*) FROM audit_log")
    for _ in range(5):
        assert client.post("/api/v1/playground/detect",
                           json={"history": _history(), "candidate": 1300}).status_code == 200
    assert client.post("/api/v1/playground/trace",
                       json={"before": "select a from t"}).status_code == 200
    assert store.scalar("SELECT COUNT(*) FROM audit_log") == before


def test_examples_are_served_with_their_limits(client):
    body = client.get("/api/v1/playground/examples").json()
    assert {e["id"] for e in body["sql"]} == {"fanout", "units", "filter", "grain"}
    assert body["limits"]["sql_chars"] == MAX_SQL_CHARS


# ---------------------------------------------------------------------------
# Routing: landing at /, console at /console
# ---------------------------------------------------------------------------


def test_the_landing_page_is_served_at_the_root(client):
    response = client.get("/")
    assert response.status_code == 200
    assert 'data-page="landing"' in response.text


def test_the_console_is_served_at_console(client):
    response = client.get("/console")
    assert response.status_code == 200
    assert 'id="app"' in response.text


def test_an_unknown_page_is_a_real_404_that_still_lands_somewhere(client):
    response = client.get("/no-such-page")
    assert response.status_code == 404
    assert 'data-page="landing"' in response.text


def test_an_unknown_api_path_stays_json(client):
    response = client.get("/api/v1/no-such-thing")
    assert response.status_code == 404
    assert response.json() == {"detail": "not found"}
