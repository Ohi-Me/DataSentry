"""Reasoners.

Three implementations of one interface. Two are hosted -- Groq and Gemini --
and differ only in transport; everything with a security or
quality property attached lives in ``wire`` and ``hosted`` and is shared, so a
new vendor cannot accidentally ship without the evidence envelope or without
the coercion that drops an uncited claim.

The fourth, ``HeuristicReasoner``, is not a mock. It is the baseline a
language model has to beat on the gold set, the thing that makes the eval
suite reproducible, and the degradation path when a provider is rate-limited
or unreachable. ``registry.resolve`` prefers the free tiers and falls back to
it, which is why a fresh clone with no credentials still runs the whole loop.
"""

from datasentry_agent.providers.base import Reasoner, Usage, load_prompt, prompt_version
from datasentry_agent.providers.gemini import GeminiReasoner
from datasentry_agent.providers.groq import GroqReasoner
from datasentry_agent.providers.heuristic import HeuristicReasoner
from datasentry_agent.providers.hosted import HostedReasoner
from datasentry_agent.providers.registry import (
    HOSTED,
    PREFERENCE,
    available,
    build_reasoner,
    describe,
    resolve,
)

__all__ = [
    "HOSTED",
    "PREFERENCE",
    "GeminiReasoner",
    "GroqReasoner",
    "HeuristicReasoner",
    "HostedReasoner",
    "Reasoner",
    "Usage",
    "available",
    "build_reasoner",
    "describe",
    "load_prompt",
    "prompt_version",
    "resolve",
]
