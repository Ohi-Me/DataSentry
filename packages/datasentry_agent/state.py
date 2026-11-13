"""Run state and the budget that bounds it.

Cost is a first-class constraint (docs/02 principle 5), so the budget is not
a monitoring concern bolted on afterwards — it is a field of the state that
every transition checks. A run that exceeds it terminates with
``budget_exceeded`` and the incident stays open with partial results, which is
an honest outcome. Silently spending $4 on one incident is not.

The state is JSON-serialisable in full, because the Postgres checkpointer
persists it at every node boundary. An orchestrator restart resumes an
in-flight incident rather than losing it, and a finished run can be replayed
against a new prompt version with the evidence frozen.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from datasentry_core.config import AgentBudget
from datasentry_core.errors import BudgetExceeded
from datasentry_core.models import (
    Anomaly,
    Asset,
    BlastRadiusEntry,
    Evidence,
    Hypothesis,
    Incident,
    ProposedAction,
)


@dataclass
class Budget:
    limits: AgentBudget
    transitions: int = 0
    tool_calls: int = 0
    usd: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    cheap_calls: int = 0
    frontier_calls: int = 0
    started_at: float = field(default_factory=time.perf_counter)

    @property
    def elapsed(self) -> float:
        return time.perf_counter() - self.started_at

    def check(self) -> None:
        if self.transitions > self.limits.max_transitions:
            raise BudgetExceeded("transitions", self.limits.max_transitions, self.transitions)
        if self.tool_calls > self.limits.max_tool_calls:
            raise BudgetExceeded("tool_calls", self.limits.max_tool_calls, self.tool_calls)
        if self.usd > self.limits.max_usd:
            raise BudgetExceeded("usd", self.limits.max_usd, self.usd)
        if self.elapsed > self.limits.max_wall_seconds:
            raise BudgetExceeded("wall_seconds", self.limits.max_wall_seconds, self.elapsed)

    def charge(self, *, usd: float, tokens_in: int, tokens_out: int, tier: str) -> None:
        self.usd += usd
        self.tokens_in += tokens_in
        self.tokens_out += tokens_out
        if tier == "cheap":
            self.cheap_calls += 1
        else:
            self.frontier_calls += 1
        self.check()

    @property
    def cheap_ratio(self) -> float:
        total = self.cheap_calls + self.frontier_calls
        return (self.cheap_calls / total) if total else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "transitions": self.transitions,
            "tool_calls": self.tool_calls,
            "usd": round(self.usd, 6),
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "cheap_calls": self.cheap_calls,
            "frontier_calls": self.frontier_calls,
            "cheap_ratio": round(self.cheap_ratio, 3),
            "elapsed_s": round(self.elapsed, 3),
        }


@dataclass
class StepRecord:
    seq: int
    node: str
    model: str | None
    status: str
    duration_ms: int
    tokens_in: int = 0
    tokens_out: int = 0
    usd: float = 0.0
    summary: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    at: datetime | None = None


@dataclass
class Dossier:
    """Everything Contextualize assembled, with no model involved.

    Assembling this is a database problem, not a reasoning problem. Every node
    made deterministic is one that is cheaper, faster and testable, which is
    why Contextualize spends zero LLM calls.
    """

    incident: Incident
    root_asset: Asset | None
    anomalies: list[Anomaly] = field(default_factory=list)
    upstream_assets: list[Asset] = field(default_factory=list)
    downstream: list[BlastRadiusEntry] = field(default_factory=list)
    recent_commits: list[dict[str, Any]] = field(default_factory=list)
    recent_runs: list[dict[str, Any]] = field(default_factory=list)
    schema_changes: list[dict[str, Any]] = field(default_factory=list)
    upstream_anomalies: list[Anomaly] = field(default_factory=list)
    lineage_paths: list[dict[str, Any]] = field(default_factory=list)
    parse_coverage: float = 1.0

    def signals(self) -> list[str]:
        seen: list[str] = []
        for a in self.anomalies:
            s = str(a.signal)
            if s not in seen:
                seen.append(s)
        return seen

    def cause_signature(self) -> tuple:
        """Key for the semantic cache. See docs/05 section 9."""
        return (
            self.incident.root_asset_id,
            tuple(sorted(self.signals())),
            any(str(a.signal) == "SCHEMA" for a in self.anomalies),
            tuple(sorted(c["sha"] for c in self.recent_commits)),
        )


@dataclass
class RCAState:
    incident_id: str
    dossier: Dossier | None = None
    depth: str = "standard"  # shallow | standard | deep
    dismissed: bool = False
    dismiss_reason: str | None = None
    hypotheses: list[Hypothesis] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    narrative: str | None = None
    actions: list[ProposedAction] = field(default_factory=list)
    round_index: int = 0
    steps: list[StepRecord] = field(default_factory=list)
    terminal: str | None = None  # complete | dismissed | inconclusive | budget_exceeded
    prompt_version: str = "prompts/2026.08.1"
    provider: str = ""
    cache_hit: bool = False

    def evidence_index(self) -> dict[str, Evidence]:
        return {e.evidence_id: e for e in self.evidence}

    def verified(self) -> list[Hypothesis]:
        return [h for h in self.hypotheses if h.status == "verified"]

    def rejected(self) -> list[Hypothesis]:
        return [h for h in self.hypotheses if h.status == "rejected"]

    def top(self) -> Hypothesis | None:
        verified = sorted(self.verified(), key=lambda h: -h.confidence)
        return verified[0] if verified else None
