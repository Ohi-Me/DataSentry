"""Triage: is this real, new, and worth investigating?

The cheapest node in the graph and the one that saves the most money. An
incident dismissed here costs one classification call instead of a full
investigation, and the two things it dismisses -- duplicates and muted assets
-- are exactly the two things that generate alert fatigue.

It also sets investigation depth, which routes later nodes between the cheap
and frontier model tiers. That makes this a cost decision as much as a
quality one.
"""

from __future__ import annotations

from datasentry_agent.nodes import Deps, build_context
from datasentry_agent.state import RCAState


def make_triage(deps: Deps):
    def triage(state: RCAState) -> None:
        decision = deps.reasoner.triage(build_context(state, deps))
        usage = decision.usage
        deps.budget.charge(
            usd=usage.usd, tokens_in=usage.tokens_in, tokens_out=usage.tokens_out,
            tier=usage.tier,
        )
        state.depth = decision.depth
        if not decision.investigate:
            state.dismissed = True
            state.dismiss_reason = decision.reason

    return triage


def make_dismiss(deps: Deps):
    def dismiss(state: RCAState) -> None:
        state.terminal = "dismissed"

    return dismiss


def make_inconclusive(deps: Deps):
    def inconclusive(state: RCAState) -> None:
        """No hypothesis survived verification.

        Recorded as an outcome rather than papered over with the least-bad
        rejected hypothesis. "We could not establish a cause" is information;
        a confident wrong answer is a liability.
        """
        state.terminal = "inconclusive"

    return inconclusive
