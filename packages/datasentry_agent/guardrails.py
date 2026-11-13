"""Guardrails.

Hallucinated root causes are the failure mode that kills trust in a system
like this, so the defences are layered and none of them is a prompt asking
for good behaviour:

* **The type system.** ``Claim.evidence_ids`` has ``min_length=1``, so a
  claim without evidence cannot be constructed at all.
* **Post-parse validation.** Every cited id must exist in this incident's
  evidence table. An id that does not resolve is a fabricated citation, and
  the claim carrying it is stripped before the verifier ever sees it.
* **The adversarial verifier.** Its default is rejection; verification has to
  be earned against evidence that entails the claim rather than merely
  co-occurs with it.
* **The closed action enum.** Whatever survives all of the above still cannot
  name an action the executor does not implement.

``hallucination_rate`` is measured and published rather than assumed. A
system that reports its own failure rate is making a much stronger claim than
one that reports only successes.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from datasentry_core.models import Claim, Evidence, Hypothesis


@dataclass
class GuardrailReport:
    claims_seen: int = 0
    claims_stripped: int = 0
    fabricated_ids: list[str] = field(default_factory=list)
    hypotheses_dropped: int = 0

    @property
    def hallucination_rate(self) -> float:
        """Fraction of claims citing evidence that does not exist."""
        return (self.claims_stripped / self.claims_seen) if self.claims_seen else 0.0

    def as_dict(self) -> dict[str, object]:
        return {
            "claims_seen": self.claims_seen,
            "claims_stripped": self.claims_stripped,
            "fabricated_ids": self.fabricated_ids[:20],
            "hypotheses_dropped": self.hypotheses_dropped,
            "hallucination_rate": round(self.hallucination_rate, 5),
        }


def validate_claims(claims: list[Claim], evidence: dict[str, Evidence],
                    report: GuardrailReport) -> list[Claim]:
    """Drop claims citing ids that do not exist for this incident."""
    kept: list[Claim] = []
    for claim in claims:
        report.claims_seen += 1
        missing = [eid for eid in claim.evidence_ids if eid not in evidence]
        if missing:
            report.claims_stripped += 1
            report.fabricated_ids.extend(missing)
            continue
        kept.append(claim)
    return kept


def enforce(hypotheses: list[Hypothesis], evidence: dict[str, Evidence],
            report: GuardrailReport) -> list[Hypothesis]:
    """Strip unsupported claims, then drop hypotheses left with nothing to stand on."""
    survivors: list[Hypothesis] = []
    for hypothesis in hypotheses:
        hypothesis.claims = validate_claims(hypothesis.claims, evidence, report)
        if not hypothesis.claims:
            report.hypotheses_dropped += 1
            continue
        survivors.append(hypothesis)
    return survivors


# ---------------------------------------------------------------------------
# Confidence calibration
# ---------------------------------------------------------------------------


class ConfidenceCalibrator:
    """Maps raw confidence to observed correctness, from human dispositions.

    A model's self-reported 0.9 is not a 90% chance of being right, and
    showing an analyst an uncalibrated number teaches them to distrust all of
    them. This bins historical dispositions and reports the empirical hit rate
    for the bin — isotonic regression in its simplest honest form.

    With too few dispositions to fit anything, it returns the raw value and
    says so via ``ready``. Fitting a calibration curve to a dozen labels would
    be worse than not calibrating at all.
    """

    def __init__(self, bins: int = 5, min_samples: int = 40) -> None:
        self.bins = bins
        self.min_samples = min_samples
        self._hits: list[int] = [0] * bins
        self._total: list[int] = [0] * bins

    def observe(self, raw_confidence: float, correct: bool) -> None:
        index = self._bin(raw_confidence)
        self._total[index] += 1
        if correct:
            self._hits[index] += 1

    @property
    def ready(self) -> bool:
        return sum(self._total) >= self.min_samples

    def _bin(self, value: float) -> int:
        return min(self.bins - 1, max(0, int(value * self.bins)))

    def calibrate(self, raw_confidence: float) -> float:
        if not self.ready:
            return raw_confidence
        index = self._bin(raw_confidence)
        if self._total[index] == 0:
            return raw_confidence
        return round(self._hits[index] / self._total[index], 3)

    def curve(self) -> list[dict[str, float]]:
        out = []
        for i in range(self.bins):
            if not self._total[i]:
                continue
            out.append(
                {
                    "bin_low": round(i / self.bins, 2),
                    "bin_high": round((i + 1) / self.bins, 2),
                    "observed": round(self._hits[i] / self._total[i], 3),
                    "n": self._total[i],
                }
            )
        return out


# ---------------------------------------------------------------------------
# Prompt-injection containment
# ---------------------------------------------------------------------------

INJECTION_MARKERS = (
    "ignore previous", "ignore all previous", "disregard the above",
    "system:", "you are now", "new instructions", "override your",
    "do not follow", "act as", "reveal your prompt",
)


def scan_for_injection(payload: object, _depth: int = 0) -> list[str]:
    """Flag evidence content that appears to address the model.

    Values never reach a prompt in the first place (ADR-010) — only
    statistics do — so this is defence in depth over the fields that *are*
    free text: commit messages, SQL comments, model names. A hit is surfaced
    on the incident as a finding rather than silently dropped, because
    somebody trying it is itself worth knowing about.
    """
    if _depth > 6:
        return []
    found: list[str] = []
    if isinstance(payload, dict):
        for value in payload.values():
            found.extend(scan_for_injection(value, _depth + 1))
    elif isinstance(payload, (list, tuple)):
        for value in payload:
            found.extend(scan_for_injection(value, _depth + 1))
    elif isinstance(payload, str):
        lowered = payload.lower()
        for marker in INJECTION_MARKERS:
            if marker in lowered:
                found.append(marker)
    return found
