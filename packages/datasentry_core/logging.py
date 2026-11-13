"""Structured logging with a redaction filter.

docs/07 section 4.3 says no raw warehouse value is ever logged. The filter
below is the enforcement and tests/unit/test_redaction.py is the proof. A
rule nobody tests is a rule nobody keeps.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from contextvars import ContextVar
from typing import Any

ESC = "\033["

_trace_id: ContextVar[str | None] = ContextVar("ds_trace_id", default=None)
_incident_id: ContextVar[str | None] = ContextVar("ds_incident_id", default=None)

# Keys whose values must never reach a log sink, whatever they contain.
FORBIDDEN_KEYS = {
    "value",
    "values",
    "sample",
    "samples",
    "raw",
    "row",
    "rows",
    "password",
    "token",
    "api_key",
    "secret",
    "authorization",
}

_SECRET_PATTERN = re.compile(r"(sk-[A-Za-z0-9\-_]{12,}|ghp_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16})")

REDACTED = "[redacted]"


def redact(payload: Any, _depth: int = 0) -> Any:
    """Recursively strip anything that could carry warehouse data or a secret."""
    if _depth > 8:
        return REDACTED
    if isinstance(payload, dict):
        return {
            k: (REDACTED if str(k).lower() in FORBIDDEN_KEYS else redact(v, _depth + 1))
            for k, v in payload.items()
        }
    if isinstance(payload, (list, tuple)):
        return [redact(v, _depth + 1) for v in payload]
    if isinstance(payload, str):
        return _SECRET_PATTERN.sub(REDACTED, payload)
    return payload


class RedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = _SECRET_PATTERN.sub(REDACTED, record.msg)
        extra = getattr(record, "context", None)
        if extra is not None:
            record.context = redact(extra)
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        doc: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname.lower(),
            "service": getattr(record, "service", record.name),
            "msg": record.getMessage(),
        }
        tid = _trace_id.get()
        if tid is not None:
            doc["trace_id"] = tid
        iid = _incident_id.get()
        if iid is not None:
            doc["incident_id"] = iid
        ctx = getattr(record, "context", None)
        if ctx is not None:
            doc["context"] = ctx
        if record.exc_info:
            doc["error"] = self.formatException(record.exc_info)
        return json.dumps(doc, default=str)


_LEVEL_COLOUR = {
    "DEBUG": "38;5;244",
    "INFO": "38;5;80",
    "WARNING": "38;5;214",
    "ERROR": "38;5;203",
    "CRITICAL": "1;38;5;203",
}


def paint(text: str, colour: str) -> str:
    return ESC + colour + "m" + text + ESC + "0m"


class ConsoleFormatter(logging.Formatter):
    """Readable output for local runs. The demo is watched, not grepped."""

    def format(self, record: logging.LogRecord) -> str:
        level = paint(record.levelname[:4].lower().ljust(5), _LEVEL_COLOUR.get(record.levelname, "0"))
        service = paint(str(getattr(record, "service", record.name)).ljust(16), "38;5;245")
        line = level + " " + service + " " + record.getMessage()
        ctx = getattr(record, "context", None)
        if ctx:
            bits = "  ".join(paint(str(k) + "=", "38;5;245") + str(v) for k, v in ctx.items())
            line = line + "   " + bits
        return line


class ServiceAdapter(logging.LoggerAdapter):
    """Attaches the service name and folds ``**fields`` into ``record.context``."""

    def process(self, msg: Any, kwargs: Any) -> tuple[Any, Any]:
        extra = dict(kwargs.get("extra") or {})
        extra.setdefault("service", self.extra.get("service"))
        kwargs["extra"] = extra
        return msg, kwargs

    def event(self, msg: str, **fields: Any) -> None:
        self.info(msg, extra={"service": self.extra.get("service"), "context": fields})


def configure(service: str, *, json_output: bool | None = None, level: int = logging.INFO) -> ServiceAdapter:
    from datasentry_core.config import settings

    use_json = settings().log_json if json_output is None else json_output
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter() if use_json else ConsoleFormatter())
    handler.addFilter(RedactionFilter())

    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    return ServiceAdapter(logging.getLogger(service), {"service": service})


def get_logger(service: str) -> ServiceAdapter:
    if not logging.getLogger().handlers:
        return configure(service)
    return ServiceAdapter(logging.getLogger(service), {"service": service})


def bind(*, trace_id: str | None = None, incident_id: str | None = None) -> None:
    if trace_id is not None:
        _trace_id.set(trace_id)
    if incident_id is not None:
        _incident_id.set(incident_id)


def current_trace_id() -> str | None:
    return _trace_id.get()
