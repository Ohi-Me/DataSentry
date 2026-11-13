"""Hypothesize: propose 3-5 candidate causes from the dossier.

The frontier model earns its cost here. This is genuine multi-step reasoning
over heterogeneous evidence -- profile deltas, a lineage subgraph, a
structured SQL diff, job telemetry -- which is the one cognitive task in the
loop that statistics cannot do and a human does badly at 2am.

On a second round the node also receives the rejections from the first, so
the model reasons against what has already been ruled out instead of
proposing it again. That back edge is the reason this is a graph.
"""

from __future__ import annotations

from datasentry_agent.nodes import Deps, build_context
from datasentry_agent.state import RCAState
from datasentry_core.ids import stable_uuid
from datasentry_core.models import Claim, Hypothesis


def make_hypothesize(deps: Deps):
    def hypothesize(state: RCAState) -> None:
        batch = deps.reasoner.hypothesize(build_context(state, deps))
        usage = batch.usage
        deps.budget.charge(
            usd=usage.usd, tokens_in=usage.tokens_in, tokens_out=usage.tokens_out,
            tier=usage.tier,
        )

        # Keep prior rounds' rejections; they are shown in the replay view and
        # are a feature, not garbage. Only proposed ones are replaced.
        kept = [h for h in state.hypotheses if h.status == "rejected"]
        fresh = []
        for rank, draft in enumerate(batch.hypotheses):
            fresh.append(
                Hypothesis(
                    hypothesis_id=stable_uuid(
                        "hypothesis", state.incident_id, str(draft.cause_class),
                        str(state.round_index),
                    ),
                    incident_id=state.incident_id,
                    rank=rank,
                    cause_class=draft.cause_class,
                    statement=draft.statement,
                    claims=[
                        Claim(text=c.text, evidence_ids=list(c.evidence_ids))
                        for c in draft.claims
                        if c.evidence_ids
                    ],
                    confidence=draft.confidence,
                    status="proposed",
                    evidence_requests=list(draft.evidence_requests),
                    round_index=state.round_index,
                )
            )
        state.hypotheses = kept + [h for h in fresh if h.claims]

    return hypothesize
