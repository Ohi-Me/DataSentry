"""The half of a hosted reasoner that is not vendor-specific.

A provider module should be transport and nothing else. Everything a node
actually depends on -- which prompt file answers which question, which tier a
task routes to, what happens when the JSON comes back malformed, and the rule
that a failed call degrades to the deterministic reasoner rather than failing
the run -- is identical whoever is answering, so it lives here once.

Subclasses implement two methods:

``models`` -- the (cheap, frontier) pair this vendor should use by default.
``complete`` -- one request: system prompt in, text and token counts out.

Free tiers change two things about the transport, and both are handled here
rather than in each vendor module:

* **429 is routine, not exceptional.** Groq and Gemini both publish low
  requests-per-minute limits, and an RCA run makes five calls in a burst. A
  single retry honouring ``Retry-After`` turns the common case from a
  degraded narrative into a slightly slower correct one.
* **Small models prefix JSON with prose.** Every JSON task asks the vendor for
  a JSON response format where one exists, and the parser in ``wire`` copes
  when it is ignored anyway.
"""

from __future__ import annotations

import time
from typing import Any

from datasentry_agent.providers.base import (
    ActionPlanResult,
    HypothesisBatch,
    NarrativeResult,
    TriageDecision,
    Usage,
    VerificationBatch,
    load_prompt,
    prompt_version,
)
from datasentry_agent.providers.heuristic import HeuristicReasoner
from datasentry_agent.providers.wire import (
    SYSTEM_INVARIANT,
    coerce_actions,
    coerce_hypotheses,
    coerce_verifications,
    envelope,
    parse_json,
    triage_payload,
)
from datasentry_core.config import Settings
from datasentry_core.config import settings as get_settings
from datasentry_core.logging import get_logger

log = get_logger("orchestrator")

# One retry, not a loop. A run that spends forty seconds waiting out a rate
# limit has already blown the 180-second wall-clock budget for the whole
# investigation, and the deterministic reasoner answers instantly.
MAX_RETRIES = 1
MAX_BACKOFF_SECONDS = 8.0


class HostedReasoner:
    """Base for every vendor-backed reasoner. Not usable on its own."""

    name = "hosted"
    api_key_env: tuple[str, ...] = ()

    def __init__(self, config: Settings | None = None, *, client: Any = None) -> None:
        self.settings = config or get_settings()
        self.prompt_version = prompt_version()
        self._fallback = HeuristicReasoner()
        self._client = client  # test seam: takes the request body, returns (text, in, out)
        self.degraded = False
        self.api_key = ""
        cheap, frontier = self.models()
        # An explicit DS_LLM_*_MODEL wins; otherwise the vendor's own default,
        # so switching provider does not require also knowing its model names.
        self.cheap_model = self.settings.llm_cheap_model or cheap
        self.frontier_model = self.settings.llm_frontier_model or frontier

    # -- vendor hooks -------------------------------------------------------

    @classmethod
    def models(cls) -> tuple[str, str]:
        """Default (cheap, frontier) model ids for this vendor.

        A classmethod so the registry can report what a provider *would* use
        without building one -- the console polls that endpoint, and
        constructing a reasoner re-hashes every prompt file.
        """
        raise NotImplementedError

    def complete(self, *, model: str, system: str, user: str, max_tokens: int,
                 json_mode: bool) -> tuple[str, int, int]:
        """One completion. Returns (text, tokens_in, tokens_out)."""
        raise NotImplementedError

    def health(self) -> dict[str, Any]:
        """What ``ds doctor`` needs: is the key usable, does the model exist."""
        raise NotImplementedError

    # -- transport ----------------------------------------------------------

    def model_for(self, tier: str) -> str:
        return self.frontier_model if tier == "frontier" else self.cheap_model

    def _call(self, prompt_file: str, payload: dict[str, Any], *, tier: str,
              max_tokens: int = 2000, json_mode: bool = True) -> tuple[str, Usage]:
        model = self.model_for(tier)
        system = SYSTEM_INVARIANT + "\n\n" + load_prompt(prompt_file)
        user = envelope(payload)

        if self._client is not None:  # test seam
            text, tokens_in, tokens_out = self._client(
                {"model": model, "system": system, "user": user, "json_mode": json_mode}
            )
            return text, Usage(model=model, tier=tier, tokens_in=tokens_in, tokens_out=tokens_out)

        attempt = 0
        while True:
            try:
                text, tokens_in, tokens_out = self.complete(
                    model=model, system=system, user=user,
                    max_tokens=max_tokens, json_mode=json_mode,
                )
                return text, Usage(model=model, tier=tier,
                                   tokens_in=tokens_in, tokens_out=tokens_out)
            except RateLimited as exc:
                attempt += 1
                if attempt > MAX_RETRIES:
                    raise
                delay = min(exc.retry_after, MAX_BACKOFF_SECONDS)
                log.event("rate limited, retrying once", provider=self.name,
                          model=model, seconds=round(delay, 1))
                time.sleep(delay)

    def _json(self, prompt_file: str, payload: dict[str, Any], *, tier: str,
              max_tokens: int = 2000) -> tuple[Any, Usage] | None:
        try:
            text, usage = self._call(prompt_file, payload, tier=tier, max_tokens=max_tokens)
            return parse_json(text), usage
        except Exception as exc:  # noqa: BLE001 - degrade, never fail the run
            self.degraded = True
            log.event("reasoner call failed, falling back", provider=self.name,
                      prompt=prompt_file, error=type(exc).__name__)
            return None

    # -- Reasoner interface -------------------------------------------------

    def triage(self, context: dict[str, Any]) -> TriageDecision:
        result = self._json("triage.v1.md", triage_payload(context), tier="cheap", max_tokens=400)
        if result is None:
            return self._fallback.triage(context)
        doc, usage = result
        if not isinstance(doc, dict):
            return self._fallback.triage(context)
        return TriageDecision(
            investigate=bool(doc.get("investigate", True)),
            depth=str(doc.get("depth", "standard")),
            reason=str(doc.get("reason", "")),
            usage=usage,
        )

    def hypothesize(self, context: dict[str, Any]) -> HypothesisBatch:
        tier = "frontier" if context.get("depth") != "shallow" else "cheap"
        result = self._json("hypothesize.v3.md", context, tier=tier, max_tokens=3000)
        if result is None:
            return self._fallback.hypothesize(context)
        doc, usage = result
        drafts = coerce_hypotheses(doc)
        if not drafts:
            return self._fallback.hypothesize(context)
        return HypothesisBatch(hypotheses=drafts, usage=usage)

    def verify(self, context: dict[str, Any]) -> VerificationBatch:
        result = self._json("verify.v2.md", context, tier="frontier", max_tokens=2500)
        if result is None:
            return self._fallback.verify(context)
        doc, usage = result
        results = coerce_verifications(doc)
        if not results:
            return self._fallback.verify(context)
        return VerificationBatch(results=results, usage=usage)

    def narrate(self, context: dict[str, Any]) -> NarrativeResult:
        try:
            text, usage = self._call("narrate.v1.md", context, tier="frontier",
                                     max_tokens=900, json_mode=False)
        except Exception:  # noqa: BLE001
            self.degraded = True
            return self._fallback.narrate(context)
        cleaned = text.strip()
        if not cleaned:
            return self._fallback.narrate(context)
        return NarrativeResult(text=cleaned, usage=usage)

    def plan(self, context: dict[str, Any]) -> ActionPlanResult:
        result = self._json("plan_actions.v1.md", context, tier="cheap", max_tokens=1200)
        if result is None:
            return self._fallback.plan(context)
        doc, usage = result
        actions = coerce_actions(doc)
        if not actions:
            return self._fallback.plan(context)
        return ActionPlanResult(actions=actions, usage=usage)


class RateLimited(Exception):
    """429 from a provider, carrying whatever wait it asked for."""

    def __init__(self, retry_after: float = 2.0) -> None:
        super().__init__("rate limited")
        self.retry_after = retry_after


def retry_after_seconds(headers: Any, default: float = 2.0) -> float:
    """Read ``Retry-After``; both vendors send it, in seconds, on 429."""
    raw = ""
    try:
        raw = str(headers.get("retry-after", "") or headers.get("Retry-After", ""))
    except Exception:  # noqa: BLE001
        return default
    try:
        return max(0.0, float(raw))
    except ValueError:
        return default
