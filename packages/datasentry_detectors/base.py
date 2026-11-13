"""Detector contract.

Every detector family answers the same question in the same units: given a
column's profile history and its newest cycle, how *nonconforming* is that
cycle? Returning a comparable nonconformity score rather than a bespoke
statistic is what lets all seven families share one calibration framework,
one threshold story and one FPR guarantee.

A detector does not decide whether something is an anomaly. It scores. The
ensemble calibrates, applies the consecutive-breach rule and decides. Keeping
that split means adding an eighth family later costs a file, not a redesign.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from datasentry_core.enums import Signal
from datasentry_core.models import Asset, ColumnProfile


@dataclass
class SeriesContext:
    """Everything a detector is allowed to see.

    Note what is absent: no raw rows, no sample values, no connection to the
    warehouse. A detector operates on profiles alone, which is what keeps the
    detection path cheap enough to run over 200k columns and incapable of
    leaking data by construction.
    """

    asset: Asset
    column: str | None
    current: ColumnProfile
    history: list[ColumnProfile]  # oldest first, excluding ``current``
    data_type: str = "UNKNOWN"

    @property
    def tier(self) -> int:
        return self.asset.tier

    @property
    def cohort(self) -> str:
        """Cold-start pool: columns of the same type at the same tier."""
        return self.data_type.split("(")[0].upper() + "/t" + str(self.tier)

    @property
    def key_prefix(self) -> str:
        return self.asset.asset_id + "::" + (self.column or "*")

    def cycles(self) -> int:
        return len(self.history)

    def values(self, extractor) -> list[float]:
        out = []
        for profile in self.history:
            v = extractor(profile)
            if v is not None:
                out.append(float(v))
        return out


@dataclass
class Finding:
    """A detector's opinion, before calibration."""

    signal: Signal
    detector_id: str
    score: float
    observed: float
    baseline: float
    explanation: str
    # Schema changes are facts, not statistics. They bypass the conformal
    # threshold and fire immediately (docs/04 section 3.7).
    deterministic: bool = False
    extra: dict[str, float] = field(default_factory=dict)


class Detector(Protocol):
    detector_id: str
    signal: Signal
    version: str

    def applicable(self, ctx: SeriesContext) -> bool: ...

    def evaluate(self, ctx: SeriesContext) -> Finding | None: ...


class BaseDetector:
    detector_id = "D0"
    signal = Signal.VOLUME
    version = "1.4.0"
    min_history = 8

    def applicable(self, ctx: SeriesContext) -> bool:
        return ctx.cycles() >= self.min_history

    def evaluate(self, ctx: SeriesContext) -> Finding | None:  # pragma: no cover - interface
        raise NotImplementedError

    def finding(self, *, score: float, observed: float, baseline: float,
                explanation: str, **extra: float) -> Finding:
        return Finding(
            signal=self.signal,
            detector_id=self.detector_id,
            score=score,
            observed=observed,
            baseline=baseline,
            explanation=explanation,
            extra={k: round(v, 6) for k, v in extra.items()},
        )
