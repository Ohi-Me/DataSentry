"""datasentry_agent -- evidence-bound root-cause analysis.

The contract, in three lines:

    in    an Incident (root asset, correlated anomalies, blast-radius stub)
    out   ranked hypotheses, each verified or rejected, every claim citing
          evidence by id, plus a proposed ActionPlan
    never any write access to anything

Statistics detect, this package reasons, deterministic code acts. The
boundaries are not stylistic -- they are what make the output auditable and
what make a prompt injection through warehouse content a cosmetic problem
rather than a security one.
"""

from datasentry_agent.graph import END, Graph, StoreCheckpointer
from datasentry_agent.guardrails import ConfidenceCalibrator, GuardrailReport, scan_for_injection
from datasentry_agent.providers.gemini import GeminiReasoner
from datasentry_agent.providers.groq import GroqReasoner
from datasentry_agent.providers.heuristic import HeuristicReasoner
from datasentry_agent.providers.registry import build_reasoner
from datasentry_agent.runner import Orchestrator, RCAResult, SemanticCache
from datasentry_agent.state import Budget, Dossier, RCAState
from datasentry_agent.tools.catalog import catalog, run_tool

__all__ = [
    "END",
    "Budget",
    "ConfidenceCalibrator",
    "Dossier",
    "GeminiReasoner",
    "Graph",
    "GroqReasoner",
    "GuardrailReport",
    "HeuristicReasoner",
    "Orchestrator",
    "RCAResult",
    "RCAState",
    "SemanticCache",
    "StoreCheckpointer",
    "build_reasoner",
    "catalog",
    "run_tool",
    "scan_for_injection",
]
