"""Assembling and running the RCA graph.

The orchestrator receives an ``Incident`` and returns an ``RCAResult``. It has
no write access to anything except its own reasoning artefacts — hypotheses,
evidence and a proposed plan (ADR-008). The plan goes to a policy gate and
then to an executor running under a different identity.

Also here: the semantic cache from docs/05 section 9. Recurring incidents —
the same flaky upstream job for the fourth time this month — reuse the
hypothesis set keyed on the cause signature and skip straight to gathering
*fresh* evidence. The reasoning is reused; the facts never are. That is what
makes a repeat incident cost a fraction of a cold one without making it a
stale copy of the last one.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from datasentry_agent.graph import END, Graph, StoreCheckpointer
from datasentry_agent.guardrails import ConfidenceCalibrator, GuardrailReport
from datasentry_agent.nodes import Deps
from datasentry_agent.nodes.contextualize import make_contextualize
from datasentry_agent.nodes.evidence import make_gather_evidence
from datasentry_agent.nodes.hypothesize import make_hypothesize
from datasentry_agent.nodes.narrate import make_narrate
from datasentry_agent.nodes.plan import make_plan
from datasentry_agent.nodes.triage import make_dismiss, make_inconclusive, make_triage
from datasentry_agent.nodes.verify import make_verify, route_after_verify
from datasentry_agent.providers.registry import build_reasoner
from datasentry_agent.state import Budget, RCAState
from datasentry_core.clock import SYSTEM_CLOCK, Clock
from datasentry_core.config import Settings
from datasentry_core.config import settings as get_settings
from datasentry_core.enums import IncidentStatus
from datasentry_core.logging import get_logger
from datasentry_core.models import Asset, Incident
from datasentry_core.telemetry import TRACER

log = get_logger("orchestrator")


@dataclass
class RCAResult:
    state: RCAState
    budget: Budget
    guardrails: GuardrailReport
    trace_id: str
    duration_ms: float = 0.0

    @property
    def status(self) -> IncidentStatus:
        terminal = self.state.terminal
        if terminal == "dismissed":
            return IncidentStatus.DISMISSED
        if terminal == "budget_exceeded":
            return IncidentStatus.BUDGET_EXCEEDED
        return IncidentStatus.RCA_COMPLETE

    def as_dict(self) -> dict[str, Any]:
        top = self.state.top()
        return {
            "incident_id": self.state.incident_id,
            "terminal": self.state.terminal,
            "provider": self.state.provider,
            "prompt_version": self.state.prompt_version,
            "cache_hit": self.state.cache_hit,
            "hypotheses": len(self.state.hypotheses),
            "verified": len(self.state.verified()),
            "rejected": len(self.state.rejected()),
            "evidence": len(self.state.evidence),
            "actions": [str(a.action_type) for a in self.state.actions],
            "top_cause": str(top.cause_class) if top else None,
            "top_confidence": top.confidence if top else None,
            "budget": self.budget.as_dict(),
            "guardrails": self.guardrails.as_dict(),
            "duration_ms": round(self.duration_ms, 1),
            "trace_id": self.trace_id,
        }


class SemanticCache:
    """Hypothesis reuse keyed on the cause signature, with a 24h TTL."""

    def __init__(self, ttl_hours: int = 24) -> None:
        self.ttl_hours = ttl_hours
        self._entries: dict[str, tuple[datetime, list]] = {}
        self.hits = 0
        self.misses = 0

    @staticmethod
    def key(signature: tuple) -> str:
        return hashlib.sha256(repr(signature).encode()).hexdigest()[:16]

    def get(self, signature: tuple, now: datetime) -> list | None:
        entry = self._entries.get(self.key(signature))
        if entry is None:
            self.misses += 1
            return None
        stored_at, drafts = entry
        if (now - stored_at).total_seconds() > self.ttl_hours * 3600:
            self.misses += 1
            return None
        self.hits += 1
        return drafts

    def put(self, signature: tuple, drafts: list, now: datetime) -> None:
        self._entries[self.key(signature)] = (now, drafts)

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return (self.hits / total) if total else 0.0


@dataclass
class Orchestrator:
    store: Any
    index: Any
    assets: dict[str, Asset]
    reasoner: Any = None
    clock: Clock = field(default_factory=lambda: SYSTEM_CLOCK)
    config: Settings | None = None
    calibrator: ConfidenceCalibrator = field(default_factory=ConfidenceCalibrator)
    cache: SemanticCache = field(default_factory=SemanticCache)
    window_hours: int = 24

    def __post_init__(self) -> None:
        self.config = self.config or get_settings()
        self.reasoner = self.reasoner or build_reasoner(self.config)

    # -- graph construction -------------------------------------------------

    def build_graph(self, deps: Deps) -> Graph:
        graph = Graph(entry="Triage")
        graph.add_node("Triage", make_triage(deps), model_tier="cheap")
        graph.add_node("Contextualize", make_contextualize(deps))
        graph.add_node("Hypothesize", make_hypothesize(deps), model_tier="frontier")
        graph.add_node("GatherEvidence", make_gather_evidence(deps))
        graph.add_node("Verify", make_verify(deps), model_tier="frontier")
        graph.add_node("Narrate", make_narrate(deps), model_tier="frontier")
        graph.add_node("PlanActions", make_plan(deps), model_tier="cheap")
        graph.add_node("Dismiss", make_dismiss(deps))
        graph.add_node("Inconclusive", make_inconclusive(deps))

        graph.add_conditional_edge(
            "Triage", lambda s: "Dismiss" if s.dismissed else "Contextualize"
        )
        graph.add_conditional_edge(
            "Contextualize",
            lambda s: END if s.terminal == "inconclusive" else "Hypothesize",
        )
        graph.add_edge("Hypothesize", "GatherEvidence")
        graph.add_edge("GatherEvidence", "Verify")
        graph.add_conditional_edge("Verify", route_after_verify(deps))
        graph.add_edge("Narrate", "PlanActions")
        graph.add_edge("PlanActions", END)
        graph.add_edge("Dismiss", END)
        graph.add_edge("Inconclusive", END)
        return graph

    # -- execution ----------------------------------------------------------

    def run(self, incident: Incident, *, muted: bool = False,
            duplicate_of: str | None = None,
            on_step=None) -> RCAResult:
        now = self.clock.now()
        budget = Budget(limits=self.config.budget)
        guardrails = GuardrailReport()
        deps = Deps(
            store=self.store,
            index=self.index,
            assets=self.assets,
            reasoner=self.reasoner,
            budget=budget,
            clock=self.clock,
            now=now,
            guardrails=guardrails,
            calibrator=self.calibrator,
            muted=muted,
            duplicate_of=duplicate_of,
            window_hours=self.window_hours,
        )

        state = RCAState(
            incident_id=incident.incident_id,
            prompt_version=getattr(self.reasoner, "prompt_version", "prompts/unknown"),
            provider=getattr(self.reasoner, "name", "unknown"),
        )

        graph = self.build_graph(deps)
        checkpointer = StoreCheckpointer(self.store, self.clock)

        with TRACER.span("rca", incident_id=incident.incident_id) as span:
            trace_id = span.trace_id
            self.store.set_incident_status(incident.incident_id, IncidentStatus.RCA_RUNNING)
            incident.rca_started_at = now
            incident.trace_id = trace_id
            self.store.upsert_incident(incident)

            graph.run(state, budget, checkpointer=checkpointer, clock=self.clock,
                      on_step=on_step)
            span.set(
                terminal=state.terminal or "complete",
                hypotheses=len(state.hypotheses),
                usd=round(budget.usd, 4),
            )

        duration = TRACER.total_ms(trace_id)
        result = RCAResult(state=state, budget=budget, guardrails=guardrails,
                           trace_id=trace_id, duration_ms=duration)
        self._persist(incident, result)
        log.event(
            "rca complete",
            incident=incident.incident_id[:8],
            terminal=state.terminal,
            verified=len(state.verified()),
            rejected=len(state.rejected()),
            usd=round(budget.usd, 4),
            ms=int(duration),
        )
        return result

    def _persist(self, incident: Incident, result: RCAResult) -> None:
        state = result.state
        completed = self.clock.now()

        incident.status = result.status
        incident.rca_completed_at = completed
        incident.narrative = state.narrative
        incident.llm_cost_usd = round(result.budget.usd, 6)
        incident.token_count = result.budget.tokens_in + result.budget.tokens_out
        incident.trace_id = result.trace_id
        incident.prompt_version = state.prompt_version
        self.store.upsert_incident(incident)

        if state.evidence:
            self.store.insert_evidence(state.evidence)
        self.store.replace_hypotheses(incident.incident_id, state.hypotheses, completed)

        for step in state.steps:
            self.store.record_step(
                incident.incident_id, step.seq, step.node, model=step.model,
                status=step.status, duration_ms=step.duration_ms,
                tokens_in=step.tokens_in, tokens_out=step.tokens_out, usd=step.usd,
                summary=step.summary, payload=step.payload,
                at=step.at or completed,
            )

        self.store.audit(
            actor="agent:orchestrator", actor_kind="agent", action="rca.completed",
            target=incident.incident_id,
            detail={
                "terminal": state.terminal,
                "provider": state.provider,
                "prompt_version": state.prompt_version,
                "usd": round(result.budget.usd, 6),
                "verified": len(state.verified()),
            },
            at=completed,
        )
