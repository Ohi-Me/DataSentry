"""The profiler.

Production runs this as a Spark Structured Streaming job with
``foreachBatch``: read the new rows, compute per-partition sketches, merge
into the in-flight window state, emit on window close. The class below is the
same computation with the same contract, driven by a batch iterator instead
of a streaming source, so the local loop exercises the real code path rather
than a stub.

The contract that matters, and the one worth checking in review:

    a batch goes in, sketches come out, and no value survives the call.

``profile_batch`` never retains, logs or returns a raw value. The only
value-derived strings that leave are salted 16-hex digests used to detect
that the minimum or maximum moved. That is ADR-010, and it is enforced by the
shape of the function rather than by a policy document.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field

from tools.faultgen.generators import Batch

from datasentry_core.config import Settings
from datasentry_core.config import settings as get_settings
from datasentry_core.ids import profile_id, salted_hash
from datasentry_core.models import ColumnProfile
from datasentry_sketches import BloomFilter, CountMinTopK, HyperLogLog, TDigest
from datasentry_sketches.countmin import label_of

# Digest resolution. Lower compression means fewer centroids and a smaller
# serialised profile; 60 keeps the tail accuracy the numeric detector needs
# while holding a profile row near the 3 KB budget.
NUMERIC_DELTA = 60.0
LENGTH_DELTA = 25.0
TOPK = 50

# Above this many values in a batch, switch from an exact distinct set to
# HyperLogLog. Below it, holding the set is cheaper and exact.
HLL_THRESHOLD = 50_000


@dataclass
class ProfilerStats:
    profiles: int = 0
    columns: int = 0
    bytes_written: int = 0
    degraded: int = 0
    by_asset: dict[str, int] = field(default_factory=lambda: defaultdict(int))

    @property
    def avg_bytes(self) -> float:
        return self.bytes_written / self.profiles if self.profiles else 0.0

    def as_dict(self) -> dict[str, object]:
        return {
            "profiles": self.profiles,
            "columns": self.columns,
            "degraded": self.degraded,
            "avg_profile_bytes": round(self.avg_bytes, 1),
        }


class Profiler:
    def __init__(self, config: Settings | None = None) -> None:
        self.settings = config or get_settings()
        self.stats = ProfilerStats()
        # Baseline vocabulary per (asset, column). A Bloom filter rather than a
        # value set: it answers "have we seen this before" in a few kilobytes
        # and cannot be read back into the values it was built from.
        self._vocab: dict[tuple[str, str], BloomFilter] = {}

    # -- main path ----------------------------------------------------------

    def profile_batch(self, batch: Batch, *, degraded: bool = False) -> list[ColumnProfile]:
        version = self.settings.profiler_version
        salt = self.settings.profile_salt
        out: list[ColumnProfile] = []

        table_profile = ColumnProfile(
            profile_id=profile_id(batch.spec.asset_id, None, batch.window_end, version),
            asset_id=batch.spec.asset_id,
            column_name=None,
            window_start=batch.window_start,
            window_end=batch.window_end,
            row_count=batch.row_count,
            freshness_seconds=batch.freshness_seconds,
            schema_hash=batch.schema_hash,
            degraded=degraded,
            profiler_version=version,
            computed_at=batch.window_end,
        )
        out.append(table_profile)

        scale = batch.row_count / batch.sampled if batch.sampled else 1.0

        for column in batch.spec.monitored_columns():
            values = batch.columns.get(column.name)
            if values is None:
                continue
            profile = self._profile_column(
                batch, column.name, column.kind, values, scale, salt, version, degraded
            )
            out.append(profile)

        self.stats.profiles += len(out)
        self.stats.columns += len(out) - 1
        self.stats.by_asset[batch.spec.asset_id] += 1
        if degraded:
            self.stats.degraded += len(out)
        self.stats.bytes_written += sum(_approx_size(p) for p in out)
        return out

    def _profile_column(self, batch: Batch, name: str, kind: str, values: list,
                        scale: float, salt: str, version: str,
                        degraded: bool) -> ColumnProfile:
        present = [v for v in values if v is not None]
        null_sample = len(values) - len(present)

        numeric_sketch: str | None = None
        length_sketch: str | None = None
        topk: dict[str, int] | None = None
        new_categories: int | None = None
        distinct_estimate: int | None = None
        min_hash: str | None = None
        max_hash: str | None = None

        if present:
            # Collapse repeats before touching a sketch. A status column with
            # four values repeated nine hundred times costs four sketch
            # updates instead of nine hundred, and this one line is most of
            # the profiler's throughput on wide tables.
            counts = Counter(present)

            distinct_estimate = self._distinct(counts, present, scale)

            if kind == "measure":
                digest = TDigest(NUMERIC_DELTA)
                for value, weight in counts.items():
                    digest.add(float(value), float(weight))
                numeric_sketch = digest.encode()
                min_hash = salted_hash(round(digest.minimum, 6), salt)
                max_hash = salted_hash(round(digest.maximum, 6), salt)

            elif kind in {"text", "category", "id"}:
                lengths = TDigest(LENGTH_DELTA)
                for value, weight in counts.items():
                    lengths.add(float(len(str(value))), float(weight))
                length_sketch = lengths.encode()

                if kind == "category":
                    sketch = CountMinTopK(k=TOPK, salt=salt)
                    for value, weight in counts.items():
                        sketch.add(value, weight)
                    topk = {k: int(v * scale) for k, v in sketch.top_k().items()}
                    new_categories = self._novel_count(
                        batch.spec.asset_id, name, list(counts), salt
                    )

                ordered = sorted(str(v) for v in counts)
                min_hash = salted_hash(ordered[0], salt)
                max_hash = salted_hash(ordered[-1], salt)

        return ColumnProfile(
            profile_id=profile_id(batch.spec.asset_id, name, batch.window_end, version),
            asset_id=batch.spec.asset_id,
            column_name=name,
            window_start=batch.window_start,
            window_end=batch.window_end,
            row_count=batch.row_count,
            sampled_rows=len(values),
            freshness_seconds=batch.freshness_seconds,
            null_count=round(null_sample * scale),
            distinct_estimate=distinct_estimate,
            numeric_sketch=numeric_sketch,
            string_length_sketch=length_sketch,
            categorical_topk=topk,
            new_category_count=new_categories,
            min_value_hash=min_hash,
            max_value_hash=max_hash,
            # Referential integrity needs the parent key domain, which the
            # local harness does not feed. D2r is implemented and stays inert
            # rather than reporting a number it cannot compute honestly.
            referential_violation_count=None,
            schema_hash=batch.schema_hash,
            degraded=degraded,
            profiler_version=version,
            computed_at=batch.window_end,
        )

    def _distinct(self, counts: Counter, present: list, scale: float) -> int:
        """Distinct estimate, exact on a bounded batch and HyperLogLog above it.

        HyperLogLog exists for the case you cannot hold a set: a full scan of
        a billion-row table. On a bounded micro-batch an exact set is both
        cheaper and exact, so using it there is not a shortcut — using HLL
        there would be. The threshold is where holding the set stops being
        free.

        Either way what lands in the profile is one integer, scaled to the
        true row count so the figure is interpretable; the *ratio* is what
        the cardinality detector reads, and a constant sample size keeps that
        ratio comparable cycle to cycle.
        """
        if len(present) > HLL_THRESHOLD:
            hll = HyperLogLog(p=12)
            for value in counts:
                hll.add(value)
            raw = hll.count()
        else:
            raw = len(counts)
        return max(1, int(raw * scale))

    def _novel_count(self, asset_id: str, column: str, values: list, salt: str) -> int:
        """How many values fell outside the baseline vocabulary."""
        key = (asset_id, column)
        bloom = self._vocab.get(key)
        labels = {label_of(v, salt) for v in values}
        if bloom is None:
            self._vocab[key] = BloomFilter(capacity=4096).add_all(labels)
            return 0
        novel = sum(1 for label in labels if label not in bloom)
        for label in labels:
            bloom.add(label)
        return novel


def _approx_size(profile: ColumnProfile) -> int:
    """Serialised size, so the 3 KB per column per cycle claim is measured."""
    total = 220  # fixed fields
    for blob in (profile.numeric_sketch, profile.string_length_sketch):
        if blob:
            total += len(blob)
    if profile.categorical_topk:
        total += sum(len(k) + 8 for k in profile.categorical_topk)
    return total


def cycle_due(stride: int, cycle_index: int) -> bool:
    """Cadence tiering: hot every cycle, warm every third, cold every eighth."""
    return cycle_index % stride == 0


def profile_window(profiler: Profiler, batches: list[Batch]) -> list[ColumnProfile]:
    out: list[ColumnProfile] = []
    for batch in batches:
        out.extend(profiler.profile_batch(batch))
    return out


def summarise(profiles: list[ColumnProfile]) -> dict[str, object]:
    assets = {p.asset_id for p in profiles}
    series = {(p.asset_id, p.column_name) for p in profiles}
    windows = sorted({p.window_end for p in profiles})
    return {
        "profiles": len(profiles),
        "assets": len(assets),
        "series": len(series),
        "first_window": windows[0].isoformat() if windows else None,
        "last_window": windows[-1].isoformat() if windows else None,
        "avg_bytes": round(sum(_approx_size(p) for p in profiles) / len(profiles), 1) if profiles else 0,
    }


def as_event(profile: ColumnProfile) -> dict[str, object]:
    """The ``ds.profile.computed.v1`` payload."""
    return {
        "profile_id": profile.profile_id,
        "table_id": profile.asset_id,
        "column_name": profile.column_name,
        "window_start": profile.window_start.isoformat(),
        "window_end": profile.window_end.isoformat(),
        "row_count": profile.row_count,
        "freshness_seconds": profile.freshness_seconds,
        "null_count": profile.null_count,
        "distinct_estimate": profile.distinct_estimate,
        "degraded": profile.degraded,
        "profiler_version": profile.profiler_version,
    }
