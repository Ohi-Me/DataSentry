"""The reasoning interface.

Deliberately task-level rather than a raw ``complete(prompt)`` call. Each node
asks for the decision it needs — triage, hypothesise, verify, narrate, plan —
and gets back a typed object. Two consequences follow, and both are the point:

* **The nodes contain no prompt strings.** Prompts are versioned files loaded
  by the provider, so a prompt change is a reviewable diff with a version
  number that lands in ``incidents.prompt_version``.
* **A non-LLM implementation is a peer, not a mock.** ``HeuristicReasoner``
  satisfies this same interface from the evidence alone. It is the baseline
  the language model has to beat, it makes the eval suite deterministic, and
  it means the system degrades to a working — if less articulate — RCA when
  the model provider is down, rather than to nothing.

Model routing (ADR-007): triage and planning are classification and run on
the cheap tier; hypothesising, verifying and narrating are genuine multi-step
reasoning over heterogeneous evidence and run on the frontier tier. The
target is at least 60% of calls on the cheap tier, and ``Budget.cheap_ratio``
reports the number actually achieved.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from datasentry_core.enums import ActionType, CauseClass

PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"

# USD per million tokens, (input, output). One table to update rather than a
# number sprinkled through the codebase.
#
# The Groq and Gemini rows are their *paid* rates even though the default
# configuration runs on their free tiers, where real spend is zero. Pricing
# the free tier at zero would make `AgentBudget.max_usd` a dead cap and the
# spend figure on the Health page a decoration, and the first thing that
# happens when a project outgrows a free tier is that neither of those was
# ever exercised. The number shown is therefore what this run *would* cost,
# which is the number worth watching.
PRICING: dict[str, tuple[float, float]] = {
    # Groq
    "llama-3.1-8b-instant": (0.05, 0.08),
    "llama-3.3-70b-versatile": (0.59, 0.79),
    "openai/gpt-oss-20b": (0.10, 0.50),
    "openai/gpt-oss-120b": (0.15, 0.75),
    "qwen/qwen3-32b": (0.29, 0.59),
    "moonshotai/kimi-k2-instruct": (1.00, 3.00),
    # Gemini
    "gemini-2.0-flash": (0.10, 0.40),
    "gemini-2.0-flash-lite": (0.075, 0.30),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-2.5-pro": (1.25, 10.00),
    # Deterministic
    "heuristic": (0.0, 0.0),
}

# An unknown model is priced at the most expensive tier in the table rather
# than at an average. A budget cap that under-reads on a model nobody has
# added yet fails in the direction of spending money you did not agree to.
UNKNOWN_MODEL_RATE = (15.00, 75.00)


def price(model: str, tokens_in: int, tokens_out: int) -> float:
    rate_in, rate_out = PRICING.get(model, UNKNOWN_MODEL_RATE)
    return (tokens_in * rate_in + tokens_out * rate_out) / 1_000_000.0


@dataclass
class Usage:
    model: str = "heuristic"
    tier: str = "cheap"
    tokens_in: int = 0
    tokens_out: int = 0

    @property
    def usd(self) -> float:
        return price(self.model, self.tokens_in, self.tokens_out)


@dataclass
class TriageDecision:
    investigate: bool
    depth: str = "standard"  # shallow | standard | deep
    reason: str = ""
    usage: Usage = field(default_factory=Usage)


@dataclass
class ClaimDraft:
    text: str
    evidence_ids: list[str] = field(default_factory=list)


@dataclass
class HypothesisDraft:
    cause_class: CauseClass
    statement: str
    claims: list[ClaimDraft] = field(default_factory=list)
    confidence: float = 0.5
    evidence_requests: list[str] = field(default_factory=list)


@dataclass
class HypothesisBatch:
    hypotheses: list[HypothesisDraft] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)


@dataclass
class VerificationDraft:
    hypothesis_index: int
    status: str  # verified | rejected
    rejection_reason: str | None = None
    unsupported_claims: list[int] = field(default_factory=list)
    adjusted_confidence: float = 0.0


@dataclass
class VerificationBatch:
    results: list[VerificationDraft] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)


@dataclass
class NarrativeResult:
    text: str
    usage: Usage = field(default_factory=Usage)


@dataclass
class ActionDraft:
    action_type: ActionType
    params: dict[str, Any] = field(default_factory=dict)
    rationale: str = ""
    reversible: bool = True


@dataclass
class ActionPlanResult:
    actions: list[ActionDraft] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)


class Reasoner(Protocol):
    name: str
    prompt_version: str

    def triage(self, context: dict[str, Any]) -> TriageDecision: ...

    def hypothesize(self, context: dict[str, Any]) -> HypothesisBatch: ...

    def verify(self, context: dict[str, Any]) -> VerificationBatch: ...

    def narrate(self, context: dict[str, Any]) -> NarrativeResult: ...

    def plan(self, context: dict[str, Any]) -> ActionPlanResult: ...


def load_prompt(name: str) -> str:
    """Prompts are versioned files, never inline strings (docs/05 section 7)."""
    path = PROMPT_DIR / name
    if not path.exists():
        raise FileNotFoundError("missing prompt file: " + str(path))
    return path.read_text(encoding="utf-8")


def prompt_version() -> str:
    """A digest over every prompt file, so a run records what it actually ran."""
    import hashlib

    digest = hashlib.sha256()
    for path in sorted(PROMPT_DIR.glob("*.md")):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return "prompts/" + digest.hexdigest()[:10]
