"""PlanActions: map a verified cause to remediation.

Back on the cheap tier, because this is a lookup dressed as a decision: cause
class plus blast radius shape determines the action set. The node emits a
plan and stops. It has no capability to execute one -- a separate process
with a separate identity evaluates the plan against policy and acts (ADR-008).

Whatever the reasoner returns is filtered through the ActionType enum before
it lands in state, so an invented action type is dropped rather than passed
along to be rejected later.
"""

from __future__ import annotations

from datasentry_agent.nodes import Deps, build_context
from datasentry_agent.state import RCAState
from datasentry_core.models import ProposedAction


def make_plan(deps: Deps):
    def plan(state: RCAState) -> None:
        if not state.verified():
            state.terminal = state.terminal or "inconclusive"
            return
        result = deps.reasoner.plan(build_context(state, deps))
        usage = result.usage
        deps.budget.charge(
            usd=usage.usd, tokens_in=usage.tokens_in, tokens_out=usage.tokens_out,
            tier=usage.tier,
        )
        actions = []
        for draft in result.actions:
            try:
                actions.append(
                    ProposedAction(
                        action_type=draft.action_type,
                        params=draft.params,
                        rationale=draft.rationale,
                        reversible=True,
                    )
                )
            except ValueError:
                continue
        state.actions = actions
        state.terminal = "complete"

    return plan
