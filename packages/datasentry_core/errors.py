"""Typed failures.

Each of these maps to a behaviour described in the failure-mode table of
docs/02 §7, so catching them by type is how services implement the
documented response rather than a generic retry.
"""

from __future__ import annotations


class DataSentryError(Exception):
    """Base class for everything this system raises on purpose."""


class ConfigError(DataSentryError):
    pass


class BudgetExceeded(DataSentryError):
    """Agent run hit a token, dollar, wall-clock or transition cap."""

    def __init__(self, resource: str, limit: float, used: float) -> None:
        super().__init__(f"budget exceeded: {resource} used {used:g} of {limit:g}")
        self.resource = resource
        self.limit = limit
        self.used = used


class EvidenceNotFound(DataSentryError):
    """A hypothesis cited an evidence id that does not exist for this incident.

    This is the hallucination tripwire. It is an exception rather than a
    warning because the correct response is to reject the hypothesis.
    """


class ParseCoverageError(DataSentryError):
    """SQL the lineage parser could not resolve to column-level edges."""

    def __init__(self, model_ref: str, reason: str) -> None:
        super().__init__(f"{model_ref}: {reason}")
        self.model_ref = model_ref
        self.reason = reason


class PolicyViolation(DataSentryError):
    """The executor was handed an action the policy gate did not approve."""


class ToolError(DataSentryError):
    """A read-only agent tool failed. Recorded as evidence of absence."""
