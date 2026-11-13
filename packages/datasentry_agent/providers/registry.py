"""Which reasoner answers, and why.

``DS_LLM_PROVIDER=auto`` -- the default -- resolves in this order:

    groq -> gemini -> heuristic

Groq and Gemini come first because both have a free tier, so the cheapest
working configuration is also the default one: set one environment variable
and a fresh clone reasons with a real model. The deterministic reasoner is last and
always available, which is the property that matters most -- ``ds demo`` runs
end to end on a machine with no credentials and no network, and the eval suite
is reproducible because of it.

Naming a provider explicitly (``DS_LLM_PROVIDER=gemini``) is exact: if its key
is missing the run degrades to the deterministic reasoner rather than quietly
billing a different vendor. Silently switching provider is the one behaviour
nobody wants from a component that spends money.
"""

from __future__ import annotations

import os
from typing import Any

from datasentry_agent.providers.gemini import GeminiReasoner
from datasentry_agent.providers.groq import GroqReasoner
from datasentry_agent.providers.heuristic import HeuristicReasoner
from datasentry_core.config import Settings
from datasentry_core.config import settings as get_settings
from datasentry_core.logging import get_logger

log = get_logger("orchestrator")

HOSTED: dict[str, type] = {
    "groq": GroqReasoner,
    "gemini": GeminiReasoner,
}

# Free tiers first. The order is the auto-detection order.
PREFERENCE: tuple[str, ...] = ("groq", "gemini")

OFFLINE_NAMES = {"heuristic", "offline", "none", "deterministic"}


def key_for(name: str, config: Settings | None = None) -> str:
    """The configured key for a provider, falling back to the bare env var."""
    cfg = config or get_settings()
    configured = {
        "groq": cfg.groq_api_key,
        "gemini": cfg.gemini_api_key,
    }.get(name, "")
    if configured:
        return configured
    cls = HOSTED.get(name)
    for env_name in getattr(cls, "api_key_env", ()) if cls else ():
        value = os.environ.get(env_name, "")
        if value:
            return value
    return ""


def available(config: Settings | None = None) -> list[str]:
    """Hosted providers that have a usable key, in preference order."""
    cfg = config or get_settings()
    return [name for name in PREFERENCE if key_for(name, cfg)]


def resolve(config: Settings | None = None) -> str:
    """The provider that would answer right now, as a name."""
    cfg = config or get_settings()
    choice = (cfg.llm_provider or "auto").lower().strip()
    if choice in OFFLINE_NAMES:
        return "heuristic"
    if choice in HOSTED:
        return choice if key_for(choice, cfg) else "heuristic"
    ready = available(cfg)
    return ready[0] if ready else "heuristic"


def build_reasoner(config: Settings | None = None, *, provider: str | None = None):
    """Pick a reasoner from configuration, or from an explicit override."""
    cfg = config or get_settings()
    if provider:
        name = provider.lower().strip()
        name = "heuristic" if name in OFFLINE_NAMES else name
        if name in HOSTED:
            if not key_for(name, cfg):
                log.event("provider named but no key present, using the deterministic reasoner",
                          provider=name)
                return HeuristicReasoner()
            return HOSTED[name](cfg)
        return HeuristicReasoner()

    name = resolve(cfg)
    if name == "heuristic":
        return HeuristicReasoner()
    return HOSTED[name](cfg)


def describe(config: Settings | None = None) -> dict[str, Any]:
    """What the console and ``ds doctor`` show about reasoning configuration."""
    cfg = config or get_settings()
    name = resolve(cfg)
    if name == "heuristic":
        return {
            "provider": "heuristic",
            "mode": "deterministic",
            "cheap_model": "heuristic",
            "frontier_model": "heuristic",
            "configured": (cfg.llm_provider or "auto"),
            "keys_present": available(cfg),
        }
    # Model names without building a reasoner. `/api/v1/health/reasoning` is
    # polled by the console, and constructing one would re-hash every prompt
    # file and build a throwaway fallback reasoner on each request.
    cheap, frontier = HOSTED[name].models()
    return {
        "provider": name,
        "mode": "hosted",
        "cheap_model": cfg.llm_cheap_model or cheap,
        "frontier_model": cfg.llm_frontier_model or frontier,
        "configured": (cfg.llm_provider or "auto"),
        "keys_present": available(cfg),
    }
