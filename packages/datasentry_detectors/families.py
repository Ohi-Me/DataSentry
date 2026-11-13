"""The seven detector families, D1 through D7.

Grouped in one module because they share the same short vocabulary and are
read together — each is 30 to 60 lines, and splitting them across seven files
would hide the thing that matters, which is how differently each signal has
to be modelled to be honest about what it can and cannot see.

Coverage map, and the fault each family is the one that catches:

    D1  volume, freshness      seasonal residual        LATE_DATA, TIMEZONE_SHIFT
    D2  null rate, referential beta-binomial tail       NULL_FLOOD, JOIN_SEMANTICS
    D3  numeric, string length W1 / IQR + JS            UNIT_CHANGE, SILENT_TRUNCATION
    D4  categorical mass       chi-square + TV + novel  CATEGORY_SHIFT
    D5  cardinality            distinctness ratio       DEDUP_BREAK
    D6  everything jointly     isolation forest         combinations never seen
    D7  schema                 deterministic            SCHEMA_DRIFT
"""

from __future__ import annotations

import math

from datasentry_core.enums import Signal
from datasentry_core.models import ColumnProfile
from datasentry_detectors.base import BaseDetector, Finding, SeriesContext
from datasentry_detectors.forest import IsolationForest
from datasentry_detectors.stats import (
    beta_binomial_tail,
    chi_square_test,
    clamp,
    jensen_shannon,
    mad,
    median,
    safe_div,
    seasonal_decompose,
    total_variation,
    wasserstein1,
)
from datasentry_sketches import TDigest

QUANTILE_GRID = (0.01, 0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95, 0.99)


def _cadence_periods(history: list[ColumnProfile]) -> tuple[int, int]:
    """Seasonal periods in *cycles*, derived from the actual profiling cadence.

    A hard-coded (24, 168) silently assumes hourly profiling. At a three-hour
    cadence the daily cycle is eight points, not twenty-four, and decomposing
    against the wrong period leaves the entire daily swing in the residual —
    which drowns a real 20% volume shift in seasonal noise the detector was
    supposed to have removed. Cadence tiering is a core design choice, so the
    detector has to read it from the data rather than assume it.
    """
    if len(history) < 3:
        return 8, 56
    gaps = [
        (history[i].window_end - history[i - 1].window_end).total_seconds()
        for i in range(1, min(len(history), 12))
    ]
    spacing = median([g for g in gaps if g > 0] or [3600.0])
    daily = max(2, round(86400.0 / spacing))
    return daily, daily * 7


def _residual_score(series: list[float], current: float,
                    periods: tuple[int, int] = (24, 168)) -> tuple[float, float]:
    """Score ``current`` against a seasonally decomposed history.

    Returns ``(nonconformity, expected)``. Nonconformity is the absolute
    residual over the MAD of historical residuals, so it is unitless and
    comparable with every other family's score.
    """
    if len(series) < 8:
        return 0.0, current
    # Decomposition is O(n x periods); a 72-cycle window is nine days at a
    # three-hour cadence, which is enough to see both cycles that matter.
    full = [*series[-72:], current]
    trend, seasonal, residual = seasonal_decompose(full, periods)
    expected = trend[-1] + seasonal[-1]
    hist_resid = residual[:-1]
    scale = mad(hist_resid)
    if scale <= 1e-9:
        # A perfectly flat history. Fall back to a relative deviation so a
        # constant series that suddenly moves is still scoreable, rather than
        # dividing by zero and reporting infinity.
        base = abs(median(series)) or 1.0
        return abs(current - expected) / base, expected
    return abs(residual[-1]) / scale, expected


# ---------------------------------------------------------------------------
# D1 — volume and freshness
# ---------------------------------------------------------------------------


class VolumeDetector(BaseDetector):
    """Seasonal residual on row counts.

    MSTL-style decomposition rather than Prophet or ARIMA: warehouse loads
    have a daily and a weekday/weekend cycle, and a robust decomposition
    captures both at a fraction of the per-series fitting cost. The claim to
    defend is that it matches Prophet's recall on the injected fault set at
    roughly 1/40th the compute — which is measurable here, not rhetorical.
    """

    detector_id = "D1"
    signal = Signal.VOLUME
    # Three full daily cycles. Scoring earlier produces a decomposition the
    # data cannot support, and those unreliable early residuals go straight
    # into the calibration set and inflate the threshold for the rest of the
    # column's life. Cold start is a real cost; paying it with silence is
    # cheaper than paying it with a permanently deaf detector.
    min_history = 24

    def applicable(self, ctx: SeriesContext) -> bool:
        return ctx.column is None and ctx.cycles() >= self.min_history

    def evaluate(self, ctx: SeriesContext) -> Finding | None:
        series = ctx.values(lambda p: p.row_count)
        if len(series) < self.min_history:
            return None
        observed = float(ctx.current.row_count)
        score, expected = _residual_score(series, observed, _cadence_periods(ctx.history))
        direction = "above" if observed > expected else "below"
        return self.finding(
            score=score,
            observed=observed,
            baseline=expected,
            explanation=(
                "row count " + format(int(observed), ",") + " is " + direction
                + " the seasonal expectation of " + format(int(expected), ",")
            ),
            deviation_ratio=safe_div(observed, expected or 1.0, 1.0),
        )


class FreshnessDetector(BaseDetector):
    detector_id = "D1f"
    signal = Signal.FRESHNESS
    min_history = 24

    def applicable(self, ctx: SeriesContext) -> bool:
        return ctx.column is None and ctx.cycles() >= self.min_history

    def evaluate(self, ctx: SeriesContext) -> Finding | None:
        series = ctx.values(lambda p: p.freshness_seconds)
        if len(series) < self.min_history:
            return None
        observed = float(ctx.current.freshness_seconds)
        baseline = median(series)
        scale = mad(series)
        if scale <= 1e-9:
            scale = max(baseline * 0.1, 60.0)
        # One-sided: data arriving early is not an incident.
        score = max(0.0, (observed - baseline) / scale)
        return self.finding(
            score=score,
            observed=observed,
            baseline=baseline,
            explanation=(
                "table is " + str(int(observed // 60)) + " min stale against a typical "
                + str(int(baseline // 60)) + " min"
            ),
        )


# ---------------------------------------------------------------------------
# D2 — rate signals
# ---------------------------------------------------------------------------

# Cap on prior strength. Without it a perfectly flat history produces a prior
# so tight that a single extra null reads as astronomically significant.
MAX_PRIOR_STRENGTH = 20_000.0


def _robust_beta(rates: list[float], observations: float | None = None) -> tuple[float, float, float]:
    """Fit Beta(alpha, beta) to a rate history, robustly.

    Method of moments on the mean and variance is the textbook fit and it is
    wrong here for a specific reason: the history a detector sees while a
    regression is ongoing *contains the regression*. Two contaminated cycles
    drag the mean up and blow the variance out, the prior goes diffuse, and
    the detector concludes that a 12% null rate is unremarkable — three cycles
    after correctly screaming about it. An anomaly that teaches the detector
    it is normal is how a monitoring system goes quietly blind.

    Median and MAD have a 50% breakdown point, so the baseline holds until
    more than half the window is contaminated. By then the change genuinely
    is the new normal, and a human disposition is the right instrument.
    """
    mu = clamp(median(rates), 1e-6, 1 - 1e-6)
    spread = mad(rates)
    var = max(spread * spread, mu * (1.0 - mu) / MAX_PRIOR_STRENGTH, 1e-12)
    common = max(mu * (1 - mu) / var - 1.0, 1e-3)

    # A prior cannot be stronger than the data it was fitted from. Thirty
    # cycles of a three-row table is ninety observations; a Beta carrying
    # twenty thousand pseudo-observations would declare a single null
    # overwhelming evidence, which is the exact small-sample error this
    # detector exists to avoid. Capping alpha+beta at the observations
    # actually seen keeps the posterior as confident as the history earns and
    # no more.
    if observations is not None and observations > 0:
        common = min(common, observations)

    return max(mu * common, 0.5), max((1 - mu) * common, 0.5), mu




class NullRateDetector(BaseDetector):
    """Beta-binomial posterior predictive tail on (nulls, rows).

    A ratio detector on null rate gets the small-sample case wrong in both
    directions: it screams about a three-row table with one null and shrugs at
    a 40-million-row table drifting from 0.1% to 0.4%. Fitting Beta(alpha,
    beta) to the trailing window and scoring the current cycle by its tail
    probability handles both correctly, because the posterior already knows
    how much evidence it has.
    """

    detector_id = "D2"
    signal = Signal.NULL_RATE
    min_history = 10

    def applicable(self, ctx: SeriesContext) -> bool:
        return ctx.column is not None and ctx.current.null_count is not None

    def evaluate(self, ctx: SeriesContext) -> Finding | None:
        history = [p for p in ctx.history if p.null_count is not None and p.row_count > 0]
        if len(history) < self.min_history or ctx.current.row_count <= 0:
            return None

        rates = [p.null_count / p.row_count for p in history]  # type: ignore[operator]
        seen = sum(float(p.sampled_rows or p.row_count) for p in history)
        alpha, beta, mu = _robust_beta(rates, observations=seen)

        # Score against the number of rows actually examined, not the table's
        # row count. Profiling is sampled above a cap, and treating a
        # 900-row sample as 900,000 observations makes every rate look
        # overwhelmingly significant -- a false-positive machine dressed up
        # as rigour.
        observed_rate = (ctx.current.null_count or 0) / ctx.current.row_count
        n = int(ctx.current.sampled_rows or ctx.current.row_count)
        k = round(observed_rate * n)
        upper = observed_rate > mu
        tail = clamp(beta_binomial_tail(k, n, alpha, beta, upper=upper), 1e-12, 1.0)
        score = -math.log10(tail)

        return self.finding(
            score=score,
            observed=observed_rate,
            baseline=mu,
            explanation=(
                "null rate " + format(observed_rate * 100, ".2f") + "% against a baseline of "
                + format(mu * 100, ".2f") + "% (posterior tail p=" + format(tail, ".2e") + ")"
            ),
            alpha=alpha,
            beta=beta,
            tail_p=tail,
        )


class ReferentialDetector(NullRateDetector):
    """Same machinery, applied to foreign-key violations."""

    detector_id = "D2r"
    signal = Signal.REFERENTIAL

    def applicable(self, ctx: SeriesContext) -> bool:
        return ctx.column is not None and ctx.current.referential_violation_count is not None

    def evaluate(self, ctx: SeriesContext) -> Finding | None:
        history = [
            p for p in ctx.history
            if p.referential_violation_count is not None and p.row_count > 0
        ]
        if len(history) < self.min_history or ctx.current.row_count <= 0:
            return None
        rates = [p.referential_violation_count / p.row_count for p in history]  # type: ignore[operator]
        seen = sum(float(p.sampled_rows or p.row_count) for p in history)
        alpha, beta, mu = _robust_beta(rates, observations=seen)
        rate = (ctx.current.referential_violation_count or 0) / ctx.current.row_count
        n = int(ctx.current.sampled_rows or ctx.current.row_count)
        k = round(rate * n)
        tail = clamp(beta_binomial_tail(k, n, alpha, beta, upper=rate > mu), 1e-12, 1.0)
        return self.finding(
            score=-math.log10(tail),
            observed=rate,
            baseline=mu,
            explanation=(
                format(k, ",") + " orphaned keys (" + format(rate * 100, ".2f")
                + "%) against a baseline of " + format(mu * 100, ".2f") + "%"
            ),
            tail_p=tail,
        )


# ---------------------------------------------------------------------------
# D3 — numeric drift
# ---------------------------------------------------------------------------


# A 30-cycle baseline is the merge of 30 small objects rather than a rescan of
# 30 cycles of rows -- the payoff for choosing mergeable sketches. It is still
# not free, so the result is cached and refreshed every ``BASELINE_STRIDE``
# cycles: a 30-cycle window barely moves in six, and recomputing it on every
# scored point was most of the detector's runtime.
BASELINE_CYCLES = 30
BASELINE_STRIDE = 6
_BASELINE_CACHE: dict[tuple, TDigest | None] = {}


def _merged_baseline(history: list[ColumnProfile], attr: str,
                     cache_key: tuple | None = None) -> TDigest | None:
    if cache_key is not None:
        key = (*cache_key, attr, len(history) // BASELINE_STRIDE)
        if key in _BASELINE_CACHE:
            return _BASELINE_CACHE[key]
    else:
        key = None

    digests = []
    for profile in history[-BASELINE_CYCLES:]:
        blob = getattr(profile, attr, None)
        digest = TDigest.decode(blob)
        if digest is not None and digest.count > 0:
            digests.append(digest)
    merged = TDigest.merge_all(digests) if digests else None

    if key is not None:
        if len(_BASELINE_CACHE) > 4000:
            _BASELINE_CACHE.clear()
        _BASELINE_CACHE[key] = merged
    return merged


class NumericDriftDetector(BaseDetector):
    """Wasserstein-1 normalised by baseline IQR, with JS divergence as backup.

    W1 is scale-aware and expressed in the column's own units, which is what
    makes a currency switch legible: USD to INR moves the distribution by
    roughly 80 baseline IQRs. A z-score on the mean would report "the mean
    went up", which is true of a good quarter as well.

    JS divergence on quantile-binned mass is the secondary signal because it
    catches shape changes that leave the mean intact — a bimodal split, for
    instance, where W1 can stay small.
    """

    detector_id = "D3"
    signal = Signal.NUMERIC_DRIFT
    min_history = 10
    sketch_attr = "numeric_sketch"

    def applicable(self, ctx: SeriesContext) -> bool:
        return ctx.column is not None and getattr(ctx.current, self.sketch_attr, None) is not None

    def evaluate(self, ctx: SeriesContext) -> Finding | None:
        current = TDigest.decode(getattr(ctx.current, self.sketch_attr))
        if current is None or current.count < 20:
            return None
        baseline = _merged_baseline(
            ctx.history, self.sketch_attr,
            cache_key=(ctx.asset.asset_id, ctx.column, self.detector_id),
        )
        if baseline is None or baseline.count < 100:
            return None

        cur_q = [current.quantile(q) for q in QUANTILE_GRID]
        base_q = [baseline.quantile(q) for q in QUANTILE_GRID]

        # Normalising by the IQR is what makes W1 comparable across columns —
        # and it detonates on a near-degenerate distribution. A status column
        # whose values are all eight or nine characters long has an IQR near
        # zero, so a half-character shift reads as eighty IQRs. Flooring the
        # scale at a fraction of the distribution's own magnitude keeps the
        # metric meaningful where the spread is genuinely tiny.
        iqr = baseline.iqr()
        centre = abs(baseline.quantile(0.5))
        scale = max(iqr, centre * 0.05, 1e-6)

        w1 = wasserstein1(cur_q, base_q) / scale
        js = jensen_shannon(_quantile_mass(current, base_q), _quantile_mass(baseline, base_q))
        score = max(w1, js * 4.0)

        return self.finding(
            score=score,
            observed=current.quantile(0.5),
            baseline=baseline.quantile(0.5),
            explanation=(
                "distribution shifted by " + format(w1, ".1f")
                + " baseline spreads (median " + format(baseline.quantile(0.5), ".4g")
                + " to " + format(current.quantile(0.5), ".4g") + ")"
            ),
            w1_over_iqr=w1,
            js_divergence=js,
            baseline_iqr=iqr,
            scale_used=scale,
        )


class StringLengthDetector(NumericDriftDetector):
    """The same comparison on len(value). Catches truncation and format changes."""

    detector_id = "D3s"
    signal = Signal.STRING_LENGTH
    sketch_attr = "string_length_sketch"

    def evaluate(self, ctx: SeriesContext) -> Finding | None:
        finding = super().evaluate(ctx)
        if finding is None:
            return None
        finding.explanation = (
            "value length shifted: median " + format(finding.baseline, ".1f")
            + " to " + format(finding.observed, ".1f") + " characters"
        )
        return finding


def _quantile_mass(digest: TDigest, edges: list[float]) -> dict[str, float]:
    """Bin a digest against the baseline's quantile edges to get a mass vector."""
    out: dict[str, float] = {}
    previous = 0.0
    for i, edge in enumerate(edges):
        cdf = digest.cdf(edge)
        out["b" + str(i)] = max(cdf - previous, 0.0)
        previous = cdf
    out["b" + str(len(edges))] = max(1.0 - previous, 0.0)
    return out


# ---------------------------------------------------------------------------
# D4 — categorical drift
# ---------------------------------------------------------------------------


class CategoricalDriftDetector(BaseDetector):
    detector_id = "D4"
    signal = Signal.CATEGORICAL_DRIFT
    min_history = 10

    def applicable(self, ctx: SeriesContext) -> bool:
        return ctx.column is not None and bool(ctx.current.categorical_topk)

    def evaluate(self, ctx: SeriesContext) -> Finding | None:
        current = ctx.current.categorical_topk or {}
        if not current:
            return None
        history = [p.categorical_topk for p in ctx.history[-30:] if p.categorical_topk]
        if len(history) < self.min_history:
            return None

        baseline_counts: dict[str, float] = {}
        for snapshot in history:
            for label, count in snapshot.items():
                baseline_counts[label] = baseline_counts.get(label, 0.0) + count
        base_total = sum(baseline_counts.values()) or 1.0
        baseline_mass = {k: v / base_total for k, v in baseline_counts.items()}

        cur_total = sum(current.values()) or 1
        current_mass = {k: v / cur_total for k, v in current.items()}

        # Chi-square scales with N, so the N handed to it has to be the number
        # of rows actually examined. The stored counts are scaled up to the
        # table's row count for readability; feeding that figure in would
        # claim fifty thousand observations from a nine-hundred-row sample and
        # make every sampling wobble look overwhelming.
        observed_n = int(ctx.current.sampled_rows or cur_total)
        stat, p_value, df = chi_square_test(current_mass, baseline_mass, observed_n)
        tv = total_variation(current_mass, baseline_mass)
        js = jensen_shannon(current_mass, baseline_mass)

        # Chi-square grows with sample size, so a huge table trips it on a
        # trivial shift. Normalising by df keeps the score comparable across
        # columns of wildly different width, which is what calibration needs.
        score = max(stat / max(df, 1) / 10.0, tv * 10.0)

        moved = sorted(
            ((abs(current_mass.get(k, 0.0) - baseline_mass.get(k, 0.0)), k) for k in set(current_mass) | set(baseline_mass)),
            reverse=True,
        )[:3]
        detail = ", ".join(
            k[:8] + " " + format(baseline_mass.get(k, 0.0) * 100, ".1f") + "% to "
            + format(current_mass.get(k, 0.0) * 100, ".1f") + "%"
            for _, k in moved
        )

        return self.finding(
            score=score,
            observed=tv,
            baseline=0.0,
            explanation="category mass moved (TV=" + format(tv, ".3f") + "): " + detail,
            chi2=stat,
            chi2_p=p_value,
            total_variation=tv,
            js_divergence=js,
        )


class NewCategoryDetector(BaseDetector):
    detector_id = "D4n"
    signal = Signal.NEW_CATEGORY
    min_history = 6

    def applicable(self, ctx: SeriesContext) -> bool:
        return ctx.column is not None and ctx.current.new_category_count is not None

    def evaluate(self, ctx: SeriesContext) -> Finding | None:
        history = [p.new_category_count for p in ctx.history if p.new_category_count is not None]
        if len(history) < self.min_history:
            return None
        observed = float(ctx.current.new_category_count or 0)
        if observed <= 0:
            return None
        baseline = median([float(h) for h in history])
        scale = max(mad([float(h) for h in history]), 0.5)
        score = max(0.0, (observed - baseline) / scale)
        return self.finding(
            score=score,
            observed=observed,
            baseline=baseline,
            explanation=(
                str(int(observed)) + " values outside the baseline vocabulary "
                "(typically " + format(baseline, ".1f") + ")"
            ),
        )


# ---------------------------------------------------------------------------
# D5 — cardinality
# ---------------------------------------------------------------------------


class CardinalityDetector(BaseDetector):
    """Distinctness ratio, not raw distinct count.

    A dedup break has a signature no single-signal detector catches: row count
    holds steady, or rises, while distinct values stay flat, so the ratio of
    distinct to rows falls sharply. Watching the raw distinct count misses it
    entirely because nothing about the distinct count changed.
    """

    detector_id = "D5"
    signal = Signal.CARDINALITY
    min_history = 10

    def applicable(self, ctx: SeriesContext) -> bool:
        return (
            ctx.column is not None
            and ctx.current.distinct_estimate is not None
            and ctx.current.row_count > 0
        )

    def evaluate(self, ctx: SeriesContext) -> Finding | None:
        history = [
            p.distinct_estimate / p.row_count  # type: ignore[operator]
            for p in ctx.history
            if p.distinct_estimate is not None and p.row_count > 0
        ]
        if len(history) < self.min_history:
            return None
        observed = (ctx.current.distinct_estimate or 0) / ctx.current.row_count
        baseline = median(history)
        scale = max(mad(history), baseline * 0.02, 1e-4)
        score = abs(observed - baseline) / scale
        direction = "fan-out" if observed < baseline else "fan-in"
        return self.finding(
            score=score,
            observed=observed,
            baseline=baseline,
            explanation=(
                "distinctness ratio " + format(observed, ".3f") + " against "
                + format(baseline, ".3f") + " — consistent with a join " + direction
            ),
            ratio_change=safe_div(observed, baseline or 1.0, 1.0),
        )


# ---------------------------------------------------------------------------
# D6 — multivariate
# ---------------------------------------------------------------------------


class MultivariateDetector(BaseDetector):
    """Isolation Forest over the standardised joint profile vector.

    Catches the case where every individual signal sits inside its interval
    but the combination has never occurred — row count up 5%, nulls up 5%,
    cardinality down 5%, each unremarkable alone.

    Trained per asset-tier cohort rather than per column. 200k models would be
    200k things to retrain, version and explain; one per cohort is a handful.

    **Standardisation is what makes the cohort real.** Feeding raw magnitudes
    to a shared model does not pool anything: a forest fitted on a table doing
    fifty thousand rows a cycle marks a table doing four hundred as bizarre,
    and the detector spends its life reporting that tables differ in size.
    Each series is first mapped into its own robust z-space — median and MAD
    from its own history — so a point means "how unusual is this *for this
    series*", and those are genuinely comparable across a cohort.

    The model is fitted once, from a pooled buffer, and then frozen. Refitting
    mid-stream would move the score distribution underneath the calibration
    set and quietly void the conformal guarantee.
    """

    detector_id = "D6"
    signal = Signal.MULTIVARIATE
    min_history = 24
    # Standardised rows pooled across a cohort before its model is fitted.
    # Below this the forest is describing one table's habits, not a cohort's.
    fit_min_rows = 240
    buffer_cap = 3000

    def __init__(self) -> None:
        self._forests: dict[str, tuple[IsolationForest, float, float]] = {}
        self._buffers: dict[str, list[list[float]]] = {}

    def applicable(self, ctx: SeriesContext) -> bool:
        return ctx.column is not None and ctx.cycles() >= self.min_history

    @staticmethod
    def _raw(profile: ColumnProfile) -> list[float]:
        return [
            math.log1p(max(profile.row_count, 0)),
            profile.null_rate if profile.null_rate is not None else 0.0,
            profile.distinct_ratio if profile.distinct_ratio is not None else 0.0,
            math.log1p(max(profile.freshness_seconds, 0)),
            math.log1p(max(profile.new_category_count or 0, 0)),
        ]

    @staticmethod
    def _reference(history: list[ColumnProfile]) -> list[tuple[float, float]]:
        """Per-feature median and robust scale, from this series' own history."""
        columns = list(zip(*[MultivariateDetector._raw(p) for p in history], strict=True))
        out = []
        for values in columns:
            centre = median(list(values))
            spread = mad(list(values))
            out.append((centre, spread if spread > 1e-9 else 1.0))
        return out

    @staticmethod
    def _standardise(profile: ColumnProfile,
                     reference: list[tuple[float, float]]) -> list[float]:
        return [
            (value - centre) / scale
            for value, (centre, scale) in zip(MultivariateDetector._raw(profile), reference,
                                              strict=True)
        ]

    def evaluate(self, ctx: SeriesContext) -> Finding | None:
        if len(ctx.history) < self.min_history:
            return None
        reference = self._reference(ctx.history)
        rows = [self._standardise(p, reference) for p in ctx.history]

        entry = self._forests.get(ctx.cohort)
        if entry is None:
            buffer = self._buffers.setdefault(ctx.cohort, [])
            if len(buffer) < self.buffer_cap:
                buffer.extend(rows[-40:])
            if len(buffer) < self.fit_min_rows:
                # Cold start for the cohort. Silence is the honest output.
                return None
            forest = IsolationForest(n_trees=48, sample_size=96, seed=17)
            forest.fit(buffer)
            sample = buffer[-200:]
            scores = [forest.anomaly_score(r) for r in sample]
            entry = (forest, median(scores), max(mad(scores), 0.01))
            self._forests[ctx.cohort] = entry

        forest, base, scale = entry
        raw = forest.anomaly_score(self._standardise(ctx.current, reference))
        score = max(0.0, (raw - base) / scale)
        return self.finding(
            score=score,
            observed=raw,
            baseline=base,
            explanation=(
                "the standardised joint profile sits at isolation score "
                + format(raw, ".3f") + " against a cohort typical of "
                + format(base, ".3f") + " — no single signal is out of range"
            ),
            isolation_score=raw,
        )


# ---------------------------------------------------------------------------
# D7 — schema
# ---------------------------------------------------------------------------


class SchemaDetector(BaseDetector):
    """Deterministic. A type change is not a probability, it is a fact.

    Included in the ensemble anyway so that it flows through the same
    correlation, severity, incident and remediation machinery as everything
    else, rather than becoming a second alerting path with its own habits.
    """

    detector_id = "D7"
    signal = Signal.SCHEMA
    min_history = 1

    def applicable(self, ctx: SeriesContext) -> bool:
        return ctx.current.schema_hash is not None and bool(ctx.history)

    def evaluate(self, ctx: SeriesContext) -> Finding | None:
        previous = None
        for profile in reversed(ctx.history):
            if profile.schema_hash:
                previous = profile.schema_hash
                break
        if previous is None or previous == ctx.current.schema_hash:
            return None
        return Finding(
            signal=self.signal,
            detector_id=self.detector_id,
            score=1.0,
            observed=1.0,
            baseline=0.0,
            explanation="schema hash changed from " + previous[:10] + " to "
                        + str(ctx.current.schema_hash)[:10],
            deterministic=True,
            extra={},
        )


ALL_DETECTORS: list[BaseDetector] = [
    VolumeDetector(),
    FreshnessDetector(),
    NullRateDetector(),
    ReferentialDetector(),
    NumericDriftDetector(),
    StringLengthDetector(),
    CategoricalDriftDetector(),
    NewCategoryDetector(),
    CardinalityDetector(),
    MultivariateDetector(),
    SchemaDetector(),
]
