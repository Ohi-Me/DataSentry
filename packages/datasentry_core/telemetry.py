"""Tracing, shaped like OpenTelemetry without the dependency.

docs/07 section 4.1 asks for one trace per incident spanning ingestion to
verification, with ``trace_id`` propagated across the async boundary. That is
the part people skip. Here the trace id rides in the event payload under
``_trace``, every service continues the trace rather than starting a new one,
and ``incidents.trace_id`` is what the console links to.

Swapping this for the real SDK is a matter of reimplementing ``span`` — the
call sites do not change.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

_current_trace: ContextVar[str | None] = ContextVar("ds_otel_trace", default=None)
_current_span: ContextVar[str | None] = ContextVar("ds_otel_span", default=None)


@dataclass
class Span:
    span_id: str
    trace_id: str
    name: str
    parent_id: str | None
    started_at: float
    ended_at: float | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    status: str = "ok"

    @property
    def duration_ms(self) -> float:
        end = self.ended_at if self.ended_at is not None else time.perf_counter()
        return (end - self.started_at) * 1000.0

    def set(self, **attrs: Any) -> None:
        self.attributes.update(attrs)


class Tracer:
    """Collects spans in memory; the API exposes them per incident."""

    def __init__(self) -> None:
        self._spans: dict[str, list[Span]] = {}

    def new_trace_id(self) -> str:
        return uuid.uuid4().hex

    @contextmanager
    def span(self, name: str, *, trace_id: str | None = None, **attrs: Any) -> Iterator[Span]:
        tid = trace_id or _current_trace.get() or self.new_trace_id()
        parent = _current_span.get()
        sp = Span(
            span_id=uuid.uuid4().hex[:16],
            trace_id=tid,
            name=name,
            parent_id=parent,
            started_at=time.perf_counter(),
            attributes=dict(attrs),
        )
        token_t = _current_trace.set(tid)
        token_s = _current_span.set(sp.span_id)
        try:
            yield sp
        except Exception as exc:
            sp.status = "error"
            sp.set(error=type(exc).__name__)
            raise
        finally:
            sp.ended_at = time.perf_counter()
            self._spans.setdefault(tid, []).append(sp)
            _current_span.reset(token_s)
            _current_trace.reset(token_t)

    def spans(self, trace_id: str) -> list[Span]:
        return list(self._spans.get(trace_id, ()))

    def timeline(self, trace_id: str) -> list[dict[str, Any]]:
        spans = sorted(self.spans(trace_id), key=lambda s: s.started_at)
        if not spans:
            return []
        origin = spans[0].started_at
        return [
            {
                "name": s.name,
                "span_id": s.span_id,
                "parent_id": s.parent_id,
                "offset_ms": round((s.started_at - origin) * 1000.0, 2),
                "duration_ms": round(s.duration_ms, 2),
                "status": s.status,
                "attributes": s.attributes,
            }
            for s in spans
        ]

    def total_ms(self, trace_id: str) -> float:
        spans = self.spans(trace_id)
        if not spans:
            return 0.0
        start = min(s.started_at for s in spans)
        end = max((s.ended_at or s.started_at) for s in spans)
        return (end - start) * 1000.0


TRACER = Tracer()


def current_trace_id() -> str | None:
    return _current_trace.get()


def inject(payload: dict[str, Any]) -> dict[str, Any]:
    """Attach trace context to an outgoing event (the Kafka-header equivalent)."""
    tid = _current_trace.get()
    if tid:
        payload = dict(payload)
        payload["_trace"] = tid
    return payload


def extract(payload: dict[str, Any]) -> str | None:
    """Continue a trace carried across the async boundary by an event."""
    return payload.get("_trace")
