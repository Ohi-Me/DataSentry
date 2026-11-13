"""Verify: adversarially test every hypothesis against the gathered evidence.

The single most important node in the graph. Everything else produces
plausible text; this is what decides whether plausible text becomes an answer
an engineer is asked to act on.

Two defences run in sequence, and the order matters:

1. **Structural.** Before any reasoning, every cited evidence id is resolved
   against this incident's evidence table. An id that does not exist is a
   fabricated citation, and the claim carrying it is stripped — no argument
   about whether the evidence "probably" said what was claimed.
2. **Semantic.** What survives goes to the verifier, whose default position
   is rejection and which must find that evidence *entails* a claim rather
   than merely co-occurs with it.

Rejections are kept, with their reasons, and shown in the replay view.
Showing the reasoning the system discarded is what makes people trust the
reasoning it kept — an incident that displays two rejected theories and one
verified one is far more credible than one that displays a single confident
answer with no working.
"""

from __future__ import annotations

from datasentry_agent.guardrails import enforce
from datasentry_agent.nodes import Deps, build_context
from datasentry_agent.state import RCAState
from datasentry_core.logging import get_logger

log = get_logger("orchestrator")


def make_verify(deps: Deps):
    def verify(state: RCAState) -> None:
        index = state.evidence_index()

        # Structural pass. Strip claims citing evidence that does not exist,
        # then drop hypotheses left standing on nothing.
        proposed = [h for h in state.hypotheses if h.status == "proposed"]
        survivors = enforce(proposed, index, deps.guardrails)
        dropped = len(proposed) - len(survivors)
        if dropped:
            log.event("hypotheses dropped for fabricated citations",
                      incident=state.incident_id, dropped=dropped)
        state.hypotheses = [h for h in state.hypotheses if h.status != "proposed"] + survivors

        if not survivors:
            state.round_index += 1
            return

        context = build_context(state, deps)
        batch = deps.reasoner.verify(context)
        usage = batch.usage
        deps.budget.charge(
            usd=usage.usd, tokens_in=usage.tokens_in, tokens_out=usage.tokens_out,
            tier=usage.tier,
        )

        by_index = {r.hypothesis_index: r for r in batch.results}
        for i, hypothesis in enumerate(survivors):
            result = by_index.get(i)
            if result is None:
                # No verdict is not a pass. Silence rejects.
                hypothesis.status = "rejected"
                hypothesis.rejection_reason = "the verifier returned no verdict for this hypothesis"
                continue

            if result.status == "verified":
                hypothesis.status = "verified"
                hypothesis.confidence = result.adjusted_confidence or hypothesis.confidence
                hypothesis.calibrated_confidence = deps.calibrator.calibrate(hypothesis.confidence)
                for ci, claim in enumerate(hypothesis.claims):
                    claim.supported = ci not in result.unsupported_claims
            else:
                hypothesis.status = "rejected"
                hypothesis.rejection_reason = (
                    result.rejection_reason or "rejected without a stated reason"
                )
                hypothesis.confidence = result.adjusted_confidence
                for ci, claim in enumerate(hypothesis.claims):
                    claim.supported = ci not in result.unsupported_claims
                    if not claim.supported:
                        claim.support_note = "cited evidence does not entail this claim"

        verified = state.verified()
        verified.sort(key=lambda h: -h.confidence)
        for rank, hypothesis in enumerate(verified):
            hypothesis.rank = rank

        state.round_index += 1
        deps.store.replace_hypotheses(state.incident_id, state.hypotheses, deps.now)

    return verify


def route_after_verify(deps: Deps):
    """Verified -> Narrate. All rejected -> retry, or give up honestly."""

    def router(state: RCAState) -> str:
        if state.verified():
            return "Narrate"
        if state.round_index <= deps.budget.limits.max_hypothesis_rounds:
            return "Hypothesize"
        return "Inconclusive"

    return router
