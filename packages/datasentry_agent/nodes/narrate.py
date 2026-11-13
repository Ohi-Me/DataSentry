"""Narrate: write the incident story from verified claims only.

The constraint is the whole node. The narrative may assert nothing that is
not in a verified claim, and every mechanism it names carries the evidence id
that supports it. That is what makes the prose in the UI checkable in
seconds rather than something an analyst has to take on faith.

If nothing was verified, this node writes nothing. An incident that reads
"cause not established" is honest; one that quietly narrates the least-bad
rejected hypothesis is how a monitoring product loses its users.
"""

from __future__ import annotations

from datasentry_agent.nodes import Deps, build_context
from datasentry_agent.state import RCAState


def make_narrate(deps: Deps):
    def narrate(state: RCAState) -> None:
        if not state.verified():
            state.narrative = None
            return
        result = deps.reasoner.narrate(build_context(state, deps))
        usage = result.usage
        deps.budget.charge(
            usd=usage.usd, tokens_in=usage.tokens_in, tokens_out=usage.tokens_out,
            tier=usage.tier,
        )
        state.narrative = result.text

    return narrate
