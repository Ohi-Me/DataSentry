"""t-digest: mergeable quantile sketch with high accuracy in the tails.

This is the workhorse for numeric columns and for string-length distributions.
Two things make it the right structure here rather than a fixed histogram:

* It is **mergeable**, so a 60-cycle baseline costs a merge, not a rescan.
* It is **accurate at the extremes**, where data regressions actually show up.
  A truncation bug or a unit change moves p99 long before it moves the mean,
  and an equal-width histogram spends its resolution in the middle where
  nothing interesting happens.

Implementation is the clustering variant: centroids are kept sorted by mean
and merged under a size bound that shrinks toward the tails via the standard
scale function ``q -> 4 * delta * q * (1 - q)``.
"""

from __future__ import annotations

import base64
import math
import struct
import zlib
from collections.abc import Iterable, Sequence


class TDigest:
    __slots__ = ("_centroids", "_count", "_dirty", "_max", "_min", "_unmerged", "delta")

    def __init__(self, delta: float = 100.0) -> None:
        self.delta = delta
        # (mean, weight), kept sorted by mean
        self._centroids: list[list[float]] = []
        self._count: float = 0.0
        self._min: float = math.inf
        self._max: float = -math.inf
        self._unmerged: list[list[float]] = []
        # Every read (quantile, cdf, mean) has to see a compressed digest, so
        # each one calls _compress. Without this flag that meant re-running an
        # O(n log n) sort and merge on an already-compressed digest for every
        # single quantile lookup -- and a distribution comparison makes about
        # thirty of them. It was 78% of the detector's runtime.
        self._dirty: bool = False

    # -- construction -------------------------------------------------------

    def add(self, value: float, weight: float = 1.0) -> None:
        if value != value:  # NaN
            return
        # Weighted adds matter more than they look: a column of five distinct
        # categories repeated a thousand times costs five adds instead of five
        # thousand, which is most of the profiler's runtime on wide tables.
        self._unmerged.append([value, weight])
        self._dirty = True
        self._count += weight
        if value < self._min:
            self._min = value
        if value > self._max:
            self._max = value
        if len(self._unmerged) > 512:
            self._compress()

    def add_all(self, values: Iterable[float]) -> TDigest:
        for v in values:
            self.add(float(v))
        return self

    @classmethod
    def of(cls, values: Iterable[float], delta: float = 100.0) -> TDigest:
        return cls(delta).add_all(values)

    # -- internals ----------------------------------------------------------

    def _compress(self) -> None:
        if not self._dirty and not self._unmerged:
            return
        points: list[list[float]] = list(self._unmerged)
        points.extend(self._centroids)
        self._unmerged = []
        if not points:
            self._dirty = False
            return
        points.sort(key=lambda c: c[0])

        total = sum(c[1] for c in points)
        if total <= 0:
            self._centroids = []
            self._dirty = False
            return

        merged: list[list[float]] = []
        current = list(points[0])
        weight_so_far = 0.0

        for point in points[1:]:
            proposed = current[1] + point[1]
            q = (weight_so_far + proposed / 2.0) / total
            limit = 4.0 * total * self.delta_scale(q) / self.delta
            if proposed <= max(limit, 1.0):
                # absorb into the current centroid, weighted mean
                current[0] = (current[0] * current[1] + point[0] * point[1]) / proposed
                current[1] = proposed
            else:
                weight_so_far += current[1]
                merged.append(current)
                current = list(point)
        merged.append(current)
        self._centroids = merged
        self._dirty = False

    @staticmethod
    def delta_scale(q: float) -> float:
        return q * (1.0 - q)

    # -- queries ------------------------------------------------------------

    @property
    def count(self) -> float:
        return self._count

    @property
    def minimum(self) -> float:
        return self._min if self._count else 0.0

    @property
    def maximum(self) -> float:
        return self._max if self._count else 0.0

    def quantile(self, q: float) -> float:
        """Interpolated quantile. ``q`` in [0, 1]."""
        self._compress()
        if not self._centroids:
            return 0.0
        if q <= 0:
            return self._min
        if q >= 1:
            return self._max

        # Interpolate between centroid *centres*, not from the previous
        # centroid's mean across the current one's span. The difference is
        # invisible with hundreds of light centroids and badly wrong with a
        # few heavy ones -- which is exactly the shape produced by weighted
        # adds, where one centroid can hold half the mass.
        target = q * self._count
        centres = []
        seen = 0.0
        for mean, weight in self._centroids:
            centres.append((seen + weight / 2.0, mean))
            seen += weight

        if target <= centres[0][0]:
            span = centres[0][0]
            if span <= 0:
                return centres[0][1]
            return self._min + (target / span) * (centres[0][1] - self._min)

        last_pos, last_mean = centres[-1]
        if target >= last_pos:
            span = self._count - last_pos
            if span <= 0:
                return last_mean
            return last_mean + ((target - last_pos) / span) * (self._max - last_mean)

        for i in range(len(centres) - 1):
            left_pos, left_mean = centres[i]
            right_pos, right_mean = centres[i + 1]
            if left_pos <= target <= right_pos:
                span = right_pos - left_pos
                if span <= 0:
                    return left_mean
                frac = (target - left_pos) / span
                return left_mean + frac * (right_mean - left_mean)
        return self._max

    def quantiles(self, qs: Sequence[float] = (0.01, 0.05, 0.25, 0.5, 0.75, 0.95, 0.99)) -> list[float]:
        return [self.quantile(q) for q in qs]

    def cdf(self, value: float) -> float:
        """Fraction of mass at or below ``value``."""
        self._compress()
        if not self._centroids or self._count == 0:
            return 0.0
        if value <= self._min:
            return 0.0
        if value >= self._max:
            return 1.0
        below = 0.0
        for mean, weight in self._centroids:
            if mean < value:
                below += weight
            elif mean == value:
                below += weight / 2.0
            else:
                break
        return below / self._count

    @property
    def mean(self) -> float:
        self._compress()
        if not self._count:
            return 0.0
        return sum(m * w for m, w in self._centroids) / self._count

    @property
    def stdev(self) -> float:
        self._compress()
        if self._count < 2:
            return 0.0
        mu = self.mean
        var = sum(w * (m - mu) ** 2 for m, w in self._centroids) / self._count
        return math.sqrt(max(var, 0.0))

    def iqr(self) -> float:
        return self.quantile(0.75) - self.quantile(0.25)

    # -- merging ------------------------------------------------------------

    def merge(self, other: TDigest) -> TDigest:
        """Fold ``other`` into a new digest. This is the property that matters."""
        out = TDigest(self.delta)
        self._compress()
        other._compress()
        out._centroids = [list(c) for c in self._centroids] + [list(c) for c in other._centroids]
        out._count = self._count + other._count
        out._min = min(self._min, other._min)
        out._max = max(self._max, other._max)
        out._dirty = True
        out._compress()
        return out

    @classmethod
    def merge_all(cls, digests: Iterable[TDigest], delta: float = 100.0) -> TDigest:
        out = cls(delta)
        for d in digests:
            out = out.merge(d)
        return out

    # -- serialisation ------------------------------------------------------

    def to_bytes(self) -> bytes:
        self._compress()
        header = struct.pack("<dddi", self.delta, self._min if self._count else 0.0,
                             self._max if self._count else 0.0, len(self._centroids))
        body = b"".join(struct.pack("<ff", m, w) for m, w in self._centroids)
        return zlib.compress(header + body, 6)

    def encode(self) -> str:
        return base64.b64encode(self.to_bytes()).decode("ascii")

    @classmethod
    def from_bytes(cls, blob: bytes) -> TDigest:
        raw = zlib.decompress(blob)
        delta, lo, hi, n = struct.unpack_from("<dddi", raw, 0)
        out = cls(delta)
        offset = struct.calcsize("<dddi")
        total = 0.0
        for _ in range(n):
            m, w = struct.unpack_from("<ff", raw, offset)
            offset += struct.calcsize("<ff")
            out._centroids.append([float(m), float(w)])
            total += float(w)
        out._count = total
        out._min = lo if total else math.inf
        out._max = hi if total else -math.inf
        return out

    @classmethod
    def decode(cls, text: str | None) -> TDigest | None:
        if not text:
            return None
        try:
            return cls.from_bytes(base64.b64decode(text))
        except Exception:  # noqa: BLE001 - a corrupt stored sketch is a cache miss, not a crash
            return None

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "TDigest(n=" + str(int(self._count)) + ", centroids=" + str(len(self._centroids)) + ")"
