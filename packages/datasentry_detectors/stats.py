"""Statistical primitives, written out rather than imported.

There is no numpy or scipy in this package on purpose. Everything the
detectors need is a few dozen lines of well-understood numerics, and keeping
them here means the detection path has no compiled dependency, starts
instantly, and can be read end to end by whoever has to trust its output.

Where an approximation is used (the incomplete beta and gamma functions, the
error function) the method and its accuracy are named in the docstring, so
nothing here is a black box.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

SQRT2 = math.sqrt(2.0)


# ---------------------------------------------------------------------------
# Robust location and scale
# ---------------------------------------------------------------------------


def median(xs: Sequence[float]) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0


def mad(xs: Sequence[float], *, scale: float = 1.4826) -> float:
    """Median absolute deviation, scaled to be a consistent estimator of sigma.

    Used instead of the standard deviation everywhere a baseline is computed:
    a single injected fault in the calibration window would inflate sigma and
    hide the very anomaly we are trying to catch. The MAD has a 50% breakdown
    point and does not.
    """
    if not xs:
        return 0.0
    m = median(xs)
    return scale * median([abs(x - m) for x in xs])


def quantile(xs: Sequence[float], q: float) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    if q <= 0:
        return s[0]
    if q >= 1:
        return s[-1]
    pos = q * (len(s) - 1)
    lo = math.floor(pos)
    hi = min(lo + 1, len(s) - 1)
    frac = pos - lo
    return s[lo] * (1 - frac) + s[hi] * frac


def mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def stdev(xs: Sequence[float]) -> float:
    if len(xs) < 2:
        return 0.0
    mu = mean(xs)
    return math.sqrt(sum((x - mu) ** 2 for x in xs) / (len(xs) - 1))


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def safe_div(num: float, den: float, default: float = 0.0) -> float:
    return num / den if den else default


# ---------------------------------------------------------------------------
# Distribution functions
# ---------------------------------------------------------------------------


def erf(x: float) -> float:
    return math.erf(x)


def normal_cdf(x: float, mu: float = 0.0, sigma: float = 1.0) -> float:
    if sigma <= 0:
        return 0.5
    return 0.5 * (1.0 + math.erf((x - mu) / (sigma * SQRT2)))


def normal_sf(x: float, mu: float = 0.0, sigma: float = 1.0) -> float:
    return 1.0 - normal_cdf(x, mu, sigma)


def log_gamma(x: float) -> float:
    return math.lgamma(x)


def _betacf(a: float, b: float, x: float, iterations: int = 200) -> float:
    """Continued fraction for the incomplete beta (Lentz's method)."""
    tiny = 1e-30
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < tiny:
        d = tiny
    d = 1.0 / d
    h = d
    for m in range(1, iterations + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 3e-12:
            break
    return h


def betainc(a: float, b: float, x: float) -> float:
    """Regularised incomplete beta I_x(a, b). Accurate to ~1e-12."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    front = math.exp(
        log_gamma(a + b) - log_gamma(a) - log_gamma(b) + a * math.log(x) + b * math.log(1.0 - x)
    )
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def beta_binomial_tail(k: int, n: int, alpha: float, beta: float, *, upper: bool) -> float:
    """Posterior predictive tail probability for observing ``k`` of ``n``.

    The exact beta-binomial tail is a sum of ``n`` terms, which is fine for a
    small table and far too slow for a large one. We use the normal
    approximation to the beta-binomial above n=2000 and the exact sum below
    it, because the small-sample case is exactly where the approximation is
    wrong and where the correctness matters: three rows with one null is not
    an incident.
    """
    if n <= 0:
        return 1.0
    if n <= 2000:
        total = 0.0
        rng = range(k, n + 1) if upper else range(0, k + 1)
        log_norm = log_gamma(alpha + beta) - log_gamma(alpha) - log_gamma(beta)
        for i in rng:
            log_p = (
                log_gamma(n + 1) - log_gamma(i + 1) - log_gamma(n - i + 1)
                + log_gamma(i + alpha) + log_gamma(n - i + beta)
                - log_gamma(n + alpha + beta)
                + log_norm
            )
            total += math.exp(log_p)
        return clamp(total, 0.0, 1.0)

    mu = n * alpha / (alpha + beta)
    var = (
        n * alpha * beta * (alpha + beta + n)
        / ((alpha + beta) ** 2 * (alpha + beta + 1.0))
    )
    sd = math.sqrt(max(var, 1e-12))
    z = (k - mu) / sd
    return normal_sf(z) if upper else normal_cdf(z)


def chi2_sf(statistic: float, df: int) -> float:
    """Upper tail of the chi-square distribution via the regularised gamma Q."""
    if df <= 0 or statistic <= 0:
        return 1.0
    return _gammaq(df / 2.0, statistic / 2.0)


def _gammaq(a: float, x: float) -> float:
    if x < a + 1.0:
        return 1.0 - _gamma_series(a, x)
    return _gamma_cf(a, x)


def _gamma_series(a: float, x: float, iterations: int = 300) -> float:
    if x <= 0:
        return 0.0
    ap = a
    total = 1.0 / a
    delta = total
    for _ in range(iterations):
        ap += 1.0
        delta *= x / ap
        total += delta
        if abs(delta) < abs(total) * 1e-14:
            break
    return total * math.exp(-x + a * math.log(x) - log_gamma(a))


def _gamma_cf(a: float, x: float, iterations: int = 300) -> float:
    tiny = 1e-300
    b = x + 1.0 - a
    c = 1.0 / tiny
    d = 1.0 / b
    h = d
    for i in range(1, iterations + 1):
        an = -i * (i - a)
        b += 2.0
        d = an * d + b
        if abs(d) < tiny:
            d = tiny
        c = b + an / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-14:
            break
    return h * math.exp(-x + a * math.log(x) - log_gamma(a))


# ---------------------------------------------------------------------------
# Distances between distributions
# ---------------------------------------------------------------------------


def wasserstein1(quantiles_a: Sequence[float], quantiles_b: Sequence[float]) -> float:
    """W1 between two distributions sampled at matching quantile levels.

    W1 equals the integral of |F_a^-1(q) - F_b^-1(q)| dq, so with quantiles
    taken on a common grid this is a simple mean of absolute differences.
    Chosen as the primary numeric-drift metric because it is expressed in the
    units of the column: normalise by the baseline IQR and a currency switch
    from USD to INR reads as roughly 80 IQRs, which no z-score on the mean
    would ever show that cleanly.
    """
    if not quantiles_a or len(quantiles_a) != len(quantiles_b):
        return 0.0
    return sum(abs(a - b) for a, b in zip(quantiles_a, quantiles_b, strict=True)) / len(
        quantiles_a
    )


def jensen_shannon(p: dict[str, float], q: dict[str, float]) -> float:
    """JS divergence in bits, in [0, 1]. Symmetric, and finite on disjoint support."""
    keys = set(p) | set(q)
    if not keys:
        return 0.0
    total = 0.0
    for key in keys:
        pi = max(p.get(key, 0.0), 0.0)
        qi = max(q.get(key, 0.0), 0.0)
        mi = 0.5 * (pi + qi)
        if pi > 0 and mi > 0:
            total += 0.5 * pi * math.log2(pi / mi)
        if qi > 0 and mi > 0:
            total += 0.5 * qi * math.log2(qi / mi)
    return clamp(total, 0.0, 1.0)


def total_variation(p: dict[str, float], q: dict[str, float]) -> float:
    keys = set(p) | set(q)
    return 0.5 * sum(abs(p.get(k, 0.0) - q.get(k, 0.0)) for k in keys)


def chi_square_test(observed: dict[str, float], expected: dict[str, float],
                    total: int) -> tuple[float, float, int]:
    """Pearson chi-square on category mass. Returns (statistic, p-value, df).

    Categories with an expected count below 5 are pooled into a single
    "other" bucket, which is the standard continuity handling and stops a
    long tail of rare categories from dominating the statistic.
    """
    keys = set(observed) | set(expected)
    stat = 0.0
    df = 0
    pooled_obs = 0.0
    pooled_exp = 0.0
    for key in keys:
        exp_count = expected.get(key, 0.0) * total
        obs_count = observed.get(key, 0.0) * total
        if exp_count < 5.0:
            pooled_obs += obs_count
            pooled_exp += exp_count
            continue
        stat += (obs_count - exp_count) ** 2 / exp_count
        df += 1
    if pooled_exp >= 5.0:
        stat += (pooled_obs - pooled_exp) ** 2 / pooled_exp
        df += 1
    df = max(df - 1, 1)
    return stat, chi2_sf(stat, df), df


# ---------------------------------------------------------------------------
# Seasonal decomposition
# ---------------------------------------------------------------------------


def seasonal_decompose(series: Sequence[float], periods: Sequence[int]) -> tuple[list[float], list[float], list[float]]:
    """Additive multi-seasonal decomposition, MSTL in spirit.

    Backfitting, which is the part that makes multiple seasonalities work:
    estimate each periodic component against the series with the *other*
    components already removed, then re-estimate the trend against the series
    with all seasonality removed, and repeat. Estimating each component
    against the raw detrended series independently -- the obvious
    implementation -- leaves the daily cycle sitting inside the weekly
    estimate and both of them inside the residual, which is precisely the
    signal the volume detector then reports as an anomaly.

    Periodic components use the median rather than the mean, so a single
    injected fault inside the window does not bend the seasonal shape toward
    itself.

    Returns ``(trend, seasonal, residual)``. The residual is what the volume
    detector scores, which is how a Monday morning stops looking like an
    incident.
    """
    n = len(series)
    if n == 0:
        return [], [], []
    values = list(series)
    usable = [p for p in periods if p >= 2 and n >= 2 * p]
    if not usable:
        trend = _moving_median(values, min(n, 7))
        return trend, [0.0] * n, [values[i] - trend[i] for i in range(n)]

    window = max(usable)
    components: dict[int, list[float]] = {p: [0.0] * n for p in usable}
    trend = _moving_median(values, window)

    for _ in range(3):
        for period in usable:
            others = [
                sum(components[q][i] for q in usable if q != period) for i in range(n)
            ]
            detrended = [values[i] - trend[i] - others[i] for i in range(n)]
            buckets: list[list[float]] = [[] for _ in range(period)]
            for i, value in enumerate(detrended):
                buckets[i % period].append(value)
            shape = [median(b) if b else 0.0 for b in buckets]
            offset = mean(shape)
            shape = [v - offset for v in shape]
            components[period] = [shape[i % period] for i in range(n)]

        seasonal_now = [sum(components[p][i] for p in usable) for i in range(n)]
        trend = _moving_median([values[i] - seasonal_now[i] for i in range(n)], window)

    seasonal = [sum(components[p][i] for p in usable) for i in range(n)]
    residual = [values[i] - trend[i] - seasonal[i] for i in range(n)]
    return trend, seasonal, residual


def _moving_median(values: Sequence[float], window: int) -> list[float]:
    n = len(values)
    if n == 0:
        return []
    window = max(3, min(window | 1, n if n % 2 else n - 1))
    half = window // 2
    out = []
    for i in range(n):
        lo = max(0, i - half)
        hi = min(n, i + half + 1)
        out.append(median(values[lo:hi]))
    return out
