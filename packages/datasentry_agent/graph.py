"""A small state graph with conditional edges and checkpointing.

The RCA loop is a graph rather than a chain for one reason: the
``Verify -> Hypothesize`` back edge. Real root-cause analysis is iterative —
you form a theory, the evidence kills it, you form a better one — and a chain
cannot express that. Capping the loop at two revisions is what stops
iteration from becoming an unbounded bill.

    Triage -> Dismiss                     known mute, duplicate, expected
    Triage -> Contextualize -> Hypothesize -> GatherEvidence -> Verify
    Verify -> Hypothesize                 all rejected, budget remains
    Verify -> Narrate                     at least one verified
    Verify -> Inconclusive                all rejected, budget exhausted
    Narrate -> PlanActions -> END

Checkpoints are written at every node boundary to the control plane, not to
memory, so an orchestrator restart resumes an in-flight incident instead of
losing it. This is a fifty-line runtime rather than a framework dependency
because the graph is seven nodes and the semantics we need are exactly the
ones written here.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from datasentry_agent.state import Budget, RCAState, StepRecord
from datasentry_core.clock import SYSTEM_CLOCK, Clock
from datasentry_core.errors import BudgetExceeded
from datasentry_core.logging import get_logger

END = "__end__"

NodeFn = Callable[[RCAState], None]
Router = Callable[[RCAState], str]

log = get_logger("orchestrator")


@dataclass
class Node:
    name: str
    fn: NodeFn
    model_tier: str | None = None


class Checkpointer:
    """Persists run state at node boundaries."""

    def save(self, incident_id: str, node: str, state: dict[str, Any]) -> None:  # pragma: no cover
        raise NotImplementedError

    def load(self, incident_id: str) -> dict[str, Any] | None:  # pragma: no cover
        raise NotImplementedError


class NullCheckpointer(Checkpointer):
    def save(self, incident_id: str, node: str, state: dict[str, Any]) -> None:
        return None

    def load(self, incident_id: str) -> dict[str, Any] | None:
        return None


class StoreCheckpointer(Checkpointer):
    def __init__(self, store, clock: Clock | None = None) -> None:
        self.store = store
        self.clock = clock or SYSTEM_CLOCK

    def save(self, incident_id: str, node: str, state: dict[str, Any]) -> None:
        self.store.save_checkpoint(incident_id, node, state, self.clock.now())

    def load(self, incident_id: str) -> dict[str, Any] | None:
        return self.store.load_checkpoint(incident_id)


class Graph:
    def __init__(self, *, entry: str) -> None:
        self.entry = entry
        self.nodes: dict[str, Node] = {}
        self.routes: dict[str, Router] = {}

    def add_node(self, name: str, fn: NodeFn, *, model_tier: str | None = None) -> None:
        self.nodes[name] = Node(name=name, fn=fn, model_tier=model_tier)

    def add_edge(self, source: str, target: str) -> None:
        self.routes[source] = lambda _state, _t=target: _t

    def add_conditional_edge(self, source: str, router: Router) -> None:
        self.routes[source] = router

    # -- execution ----------------------------------------------------------

    def run(self, state: RCAState, budget: Budget, *,
            checkpointer: Checkpointer | None = None,
            clock: Clock | None = None,
            on_step: Callable[[StepRecord], None] | None = None) -> RCAState:
        checkpointer = checkpointer or NullCheckpointer()
        clock = clock or SYSTEM_CLOCK
        current = self.entry
        seq = 0

        while current != END:
            node = self.nodes.get(current)
            if node is None:
                raise KeyError("no such node: " + current)

            budget.transitions += 1
            started = time.perf_counter()
            before = (budget.tokens_in, budget.tokens_out, budget.usd)
            status = "ok"
            try:
                budget.check()
                node.fn(state)
            except BudgetExceeded as exc:
                status = "budget_exceeded"
                state.terminal = "budget_exceeded"
                log.event("run terminated on budget", incident=state.incident_id,
                          resource=exc.resource, limit=exc.limit, used=round(exc.used, 3))
                self._record(state, seq, node, status, started, before, budget, clock, on_step)
                return state
            except Exception as exc:  # noqa: BLE001 - node failures must not lose the run
                status = "error"
                log.event("node failed", incident=state.incident_id, node=node.name,
                          error=type(exc).__name__)
                state.terminal = state.terminal or "inconclusive"
                self._record(state, seq, node, status, started, before, budget, clock, on_step,
                             summary=type(exc).__name__ + ": " + str(exc)[:200])
                return state

            self._record(state, seq, node, status, started, before, budget, clock, on_step)
            checkpointer.save(state.incident_id, node.name, {
                "node": node.name,
                "round": state.round_index,
                "hypotheses": len(state.hypotheses),
                "evidence": len(state.evidence),
                "budget": budget.as_dict(),
            })
            seq += 1

            router = self.routes.get(current)
            if router is None:
                break
            current = router(state)

        return state

    def _record(self, state: RCAState, seq: int, node: Node, status: str, started: float,
                before: tuple[int, int, float], budget: Budget, clock: Clock,
                on_step: Callable[[StepRecord], None] | None,
                summary: str | None = None) -> None:
        record = StepRecord(
            seq=seq,
            node=node.name,
            model=node.model_tier,
            status=status,
            duration_ms=int((time.perf_counter() - started) * 1000),
            tokens_in=budget.tokens_in - before[0],
            tokens_out=budget.tokens_out - before[1],
            usd=round(budget.usd - before[2], 6),
            summary=summary or _summarise(node.name, state),
            payload=_payload(node.name, state),
            at=clock.now(),
        )
        state.steps.append(record)
        if on_step is not None:
            on_step(record)


def _summarise(node: str, state: RCAState) -> str:
    if node == "Triage":
        return "dismissed: " + (state.dismiss_reason or "") if state.dismissed \
            else "investigate at depth " + state.depth
    if node == "Contextualize":
        d = state.dossier
        if d is None:
            return "no dossier"
        return (
            str(len(d.anomalies)) + " anomalies, " + str(len(d.downstream))
            + " downstream assets, " + str(len(d.recent_commits)) + " recent commits"
        )
    if node == "Hypothesize":
        return str(len(state.hypotheses)) + " hypotheses proposed (round " + str(state.round_index) + ")"
    if node == "GatherEvidence":
        return str(len(state.evidence)) + " evidence artefacts collected"
    if node == "Verify":
        return str(len(state.verified())) + " verified, " + str(len(state.rejected())) + " rejected"
    if node == "Narrate":
        return "narrative written" if state.narrative else "no verified hypothesis to narrate"
    if node == "PlanActions":
        return str(len(state.actions)) + " actions proposed"
    if node == "Dismiss":
        return state.dismiss_reason or "dismissed"
    if node == "Inconclusive":
        return "no hypothesis survived verification"
    return ""


def _payload(node: str, state: RCAState) -> dict[str, Any]:
    if node == "Hypothesize":
        return {
            "hypotheses": [
                {
                    "cause_class": str(h.cause_class),
                    "statement": h.statement,
                    "confidence": h.confidence,
                    "evidence_requests": h.evidence_requests,
                }
                for h in state.hypotheses
                if h.round_index == state.round_index
            ]
        }
    if node == "GatherEvidence":
        return {
            "evidence": [
                {"id": e.evidence_id, "kind": str(e.kind), "summary": e.summary,
                 "tool": e.collected_by}
                for e in state.evidence
            ]
        }
    if node == "Verify":
        return {
            "results": [
                {
                    "cause_class": str(h.cause_class),
                    "status": h.status,
                    "confidence": round(h.confidence, 3),
                    "rejection_reason": h.rejection_reason,
                    "cited": h.evidence_ids,
                }
                for h in state.hypotheses
            ]
        }
    if node == "PlanActions":
        return {
            "actions": [
                {"action_type": str(a.action_type), "rationale": a.rationale, "params": a.params}
                for a in state.actions
            ]
        }
    if node == "Narrate":
        return {"narrative": state.narrative or ""}
    return {}
