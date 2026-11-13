"""Batch generation.

A cycle draws a batch of rows from a table's generative model. The profiler
consumes the batch and keeps only sketches; the rows are then garbage. That
is the same contract the production profiler has with the warehouse, which is
what makes the local loop a real rehearsal rather than a mock.

Batches are **sampled above a cap**, exactly as the real profiler is
(docs/04: "exact, sampled above 10M rows"). Row count is always the true
count; distributional statistics come from a bounded sample. Holding the
sample size constant across cycles is what keeps the distinctness ratio
comparable, which is the signal a dedup break moves.

Determinism is a hard requirement — the eval harness must reproduce the same
incidents from the same seed — so every draw comes from a generator seeded on
``(global seed, asset, column, cycle index)`` rather than from shared state.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from tools.faultgen.world import ColumnSpec, TableSpec, World, expected_rows

SAMPLE_CAP = 900


@dataclass
class Mutation:
    """A declarative, serialisable change to a table's generative model.

    Faults are expressed as mutations rather than as code that patches the
    generator, so a fault set round-trips through JSON and a run is
    reproducible from its record alone.
    """

    fault_id: str
    kind: str
    asset_id: str
    column: str | None = None
    active_from: datetime | None = None
    active_until: datetime | None = None
    params: dict[str, float | str | bool] = field(default_factory=dict)

    def active_at(self, moment: datetime) -> bool:
        if self.active_from and moment < self.active_from:
            return False
        return not (self.active_until and moment > self.active_until)


@dataclass
class Batch:
    spec: TableSpec
    window_start: datetime
    window_end: datetime
    row_count: int
    sampled: int
    freshness_seconds: int
    columns: dict[str, list]
    schema_hash: str
    degraded: bool = False


class Simulator:
    def __init__(self, world: World, *, seed: int = 20260829,
                 origin: datetime | None = None, cycle_hours: float = 3.0,
                 propagation_depth: int = 3) -> None:
        self.world = world
        self.seed = seed
        self.cycle_hours = cycle_hours
        self.origin = origin or datetime(2026, 8, 1, tzinfo=UTC)
        self.mutations: list[Mutation] = []
        self.propagation_depth = propagation_depth
        self._ancestors = _ancestor_map(world, propagation_depth)

    # -- mutation management ------------------------------------------------

    def add(self, mutation: Mutation) -> None:
        self.mutations.append(mutation)

    def clear(self) -> None:
        self.mutations = []

    def active(self, asset_id: str, moment: datetime) -> list[Mutation]:
        """Mutations affecting this asset, including ones inherited from upstream.

        Corruption propagates. A join loosened in a staging model does not
        stop there -- the extra rows and the NULL measures flow into every
        table built on it, which is precisely why one bad change produces
        thirty anomalies and why correlating them into a single incident is
        worth roughly 20x in both alert volume and reasoning cost.

        A harness where faults stayed on one table would make the correlator
        look effective while testing nothing, because there would be nothing
        to correlate.
        """
        out = [m for m in self.mutations if m.asset_id == asset_id and m.active_at(moment)]
        inherited = self._ancestors.get(asset_id, {})
        for mutation in self.mutations:
            depth = inherited.get(mutation.asset_id)
            if depth is None or not mutation.active_at(moment):
                continue
            propagated = self._propagate(mutation, asset_id, depth)
            if propagated is not None:
                out.append(propagated)
        return out

    def _propagate(self, mutation: Mutation, asset_id: str, depth: int) -> Mutation | None:
        """Map an upstream mutation onto a descendant, attenuated by distance.

        Attenuation is not cosmetic. A downstream aggregate dilutes an
        upstream null flood, so the signal is genuinely weaker further from
        the source -- which is what makes electing the *root* of a cluster a
        real problem rather than a formality.
        """
        spec = self.world.tables.get(asset_id)
        if spec is None:
            return None
        attenuation = 0.75 ** depth

        column = mutation.column
        if column is not None and spec.column(column) is None:
                target = next((c for c in spec.columns if c.kind == "measure"), None)
                if target is None:
                    return None
                column = target.name

        params = dict(mutation.params)
        if mutation.kind == "row_multiplier":
            factor = float(params.get("factor", 1.0))
            params["factor"] = 1.0 + (factor - 1.0) * attenuation
        elif mutation.kind == "null_flood":
            params["null_rate"] = float(params.get("null_rate", 0.15)) * attenuation
        elif mutation.kind == "freshness_delay":
            params["seconds"] = float(params.get("seconds", 21600)) * attenuation
        elif mutation.kind in {"retype_column", "add_column"}:
            # Schema changes do not propagate: a downstream model has its own
            # DDL and keeps its own shape.
            return None

        return Mutation(
            fault_id=mutation.fault_id, kind=mutation.kind, asset_id=asset_id,
            column=column, active_from=mutation.active_from,
            active_until=mutation.active_until, params=params,
        )

    # -- the effective spec at a moment -------------------------------------

    def effective(self, spec: TableSpec, moment: datetime) -> tuple[TableSpec, dict[str, float]]:
        """Apply active mutations to produce the spec that governs this cycle."""
        mods: dict[str, float] = {
            "volume_multiplier": 1.0,
            "freshness_extra": 0.0,
            "distinct_multiplier": 1.0,
        }
        active = self.active(spec.asset_id, moment)
        if not active:
            return spec, mods

        clone = TableSpec(
            asset_id=spec.asset_id, layer=spec.layer, kind=spec.kind, tier=spec.tier,
            owner_team=spec.owner_team, columns=[c.clone() for c in spec.columns],
            upstream=list(spec.upstream), model_ref=spec.model_ref, sql=spec.sql,
            base_rows=spec.base_rows, growth_per_day=spec.growth_per_day,
            weekly_dip=spec.weekly_dip, daily_amplitude=spec.daily_amplitude,
            noise=spec.noise, query_count_30d=spec.query_count_30d,
            distinct_consumers_30d=spec.distinct_consumers_30d,
        )

        for mutation in active:
            column = clone.column(mutation.column) if mutation.column else None
            kind = mutation.kind
            p = mutation.params

            if kind == "scale_numeric" and column is not None:
                factor = float(p.get("factor", 1.0))
                # Lognormal columns hold log-space parameters, so a
                # multiplicative unit change is an additive shift in the mean.
                if column.lognormal:
                    column.mean += math.log(factor)
                else:
                    column.mean *= factor
                    column.sigma *= abs(factor)
                column.unit = str(p.get("unit", column.unit or ""))

            elif kind == "null_flood" and column is not None:
                column.null_rate = float(p.get("null_rate", 0.15))

            elif kind == "row_multiplier":
                mods["volume_multiplier"] *= float(p.get("factor", 1.0))

            elif kind == "distinct_collapse" and column is not None:
                column.distinct_ratio *= float(p.get("factor", 0.33))
                mods["distinct_multiplier"] *= float(p.get("factor", 0.33))

            elif kind == "freshness_delay":
                mods["freshness_extra"] += float(p.get("seconds", 21600))

            elif kind == "truncate_text" and column is not None:
                cap = float(p.get("max_length", 20))
                column.length_mean = min(column.length_mean, cap * 0.85)
                column.length_sigma = min(column.length_sigma, cap * 0.08)

            elif kind == "category_replace" and column is not None:
                old = str(p.get("old", column.categories[0] if column.categories else ""))
                new = str(p.get("new", "UNKNOWN"))
                share = float(p.get("share", 0.30))
                if old in column.categories:
                    idx = column.categories.index(old)
                    total = sum(column.weights) or 1.0
                    moved = column.weights[idx] * share
                    column.weights[idx] -= moved
                    column.categories.append(new)
                    column.weights.append(moved + total * 0.02)

            elif kind == "retype_column" and column is not None:
                column.dtype = str(p.get("dtype", "VARCHAR(64)"))

            elif kind == "add_column":
                clone.columns.append(
                    ColumnSpec(
                        name=str(p.get("name", "new_col")),
                        dtype=str(p.get("dtype", "VARCHAR(32)")),
                        kind="category",
                        categories=["A", "B", "C"],
                        weights=[0.5, 0.3, 0.2],
                    )
                )

            elif kind == "shift_hours":
                mods["freshness_extra"] += 0.0  # volume effect handled below
                mods["hour_shift"] = float(p.get("hours", 5.5))

        return clone, mods

    # -- batch generation ---------------------------------------------------

    def cycle_times(self, days: int, *, end: datetime | None = None) -> list[datetime]:
        end = end or (self.origin + timedelta(days=days))
        step = timedelta(hours=self.cycle_hours)
        out = []
        moment = self.origin
        while moment <= end:
            out.append(moment)
            moment += step
        return out

    def batch(self, spec: TableSpec, window_end: datetime, cycle_index: int) -> Batch:
        effective, mods = self.effective(spec, window_end)
        window_start = window_end - timedelta(hours=self.cycle_hours)

        rng = random.Random(
            hash((self.seed, spec.asset_id, cycle_index)) & 0xFFFFFFFF
        )

        moment = window_end
        if "hour_shift" in mods:
            # A timezone shift moves events across the day boundary: the
            # volume curve is read at a different clock position, so a
            # midnight cycle picks up what should have been the previous day.
            moment = window_end + timedelta(hours=mods["hour_shift"])

        base = expected_rows(effective, moment, self.origin)
        noisy = base * (1.0 + rng.gauss(0.0, effective.noise))
        row_count = max(1, int(noisy * mods["volume_multiplier"]))

        freshness = int(
            self.cycle_hours * 1800 * (1.0 + rng.gauss(0.0, 0.15)) + mods["freshness_extra"]
        )
        freshness = max(freshness, 30)

        sampled = min(row_count, SAMPLE_CAP)
        columns: dict[str, list] = {}
        for column in effective.monitored_columns():
            columns[column.name] = _draw(column, sampled, rng, row_count)

        return Batch(
            spec=effective,
            window_start=window_start,
            window_end=window_end,
            row_count=row_count,
            sampled=sampled,
            freshness_seconds=freshness,
            columns=columns,
            schema_hash=effective.schema_hash(),
        )


def _draw(column: ColumnSpec, n: int, rng: random.Random, row_count: int) -> list:
    """Draw ``n`` values for a column, ``None`` where the value is null."""
    out: list = []
    if column.kind == "id":
        # ``distinct_ratio`` is the whole point: a dedup break collapses it
        # while the row count holds, which is the signature D5 exists for.
        distinct = max(1, int(n * column.distinct_ratio))
        # Fixed width, like every real identifier -- UUIDs, surrogate keys,
        # order numbers. Variable-width ids would make the string-length
        # detector track the row count, which is a generator artefact rather
        # than a signal and would flood the run with false positives.
        for _ in range(n):
            out.append("id_" + format(rng.randrange(distinct), "010d"))
        return out

    if column.kind == "category":
        total = sum(column.weights) or 1.0
        cumulative = []
        running = 0.0
        for w in column.weights:
            running += w / total
            cumulative.append(running)
        for _ in range(n):
            if column.null_rate and rng.random() < column.null_rate:
                out.append(None)
                continue
            r = rng.random()
            idx = next((i for i, c in enumerate(cumulative) if r <= c), len(column.categories) - 1)
            out.append(column.categories[idx])
        return out

    if column.kind == "text":
        for _ in range(n):
            if column.null_rate and rng.random() < column.null_rate:
                out.append(None)
                continue
            length = max(1, int(rng.gauss(column.length_mean, column.length_sigma)))
            out.append("x" * length)
        return out

    if column.kind == "flag":
        for _ in range(n):
            out.append(rng.random() < 0.5)
        return out

    # measure
    for _ in range(n):
        if column.null_rate and rng.random() < column.null_rate:
            out.append(None)
            continue
        value = rng.gauss(column.mean, column.sigma)
        out.append(math.exp(value) if column.lognormal else value)
    return out


def _ancestor_map(world: World, depth: int) -> dict[str, dict[str, int]]:
    """asset -> {ancestor: hops}, bounded at ``depth``."""
    direct = {aid: list(spec.upstream) for aid, spec in world.tables.items()}
    out: dict[str, dict[str, int]] = {}
    for asset_id in world.tables:
        found: dict[str, int] = {}
        frontier = [asset_id]
        for hop in range(1, depth + 1):
            nxt: list[str] = []
            for node in frontier:
                for parent in direct.get(node, ()):
                    if parent not in found and parent != asset_id:
                        found[parent] = hop
                        nxt.append(parent)
            if not nxt:
                break
            frontier = nxt
        out[asset_id] = found
    return out
