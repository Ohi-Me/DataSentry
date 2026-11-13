"""Faults where the data is correct but arrives at the wrong time.

Both of these are invisible to any check that looks only at values. They show
up as volume and freshness anomalies, which is why the seasonal residual has
to be good enough to tell a late partition apart from a quiet Sunday.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta

from datasentry_core.enums import CauseClass, Signal
from tools.faultgen.faults import Injection, fault, make_record, pick_table
from tools.faultgen.generators import Mutation
from tools.faultgen.world import World


@fault("LATE_DATA")
def late_data(world: World, rng: random.Random, at: datetime) -> Injection | None:
    """A partition arrives six hours late and 20% short.

    The decoy case this has to be told apart from is a legitimate weekend
    dip, which is exactly why volume is scored as a seasonal residual rather
    than against a flat baseline.
    """
    spec = pick_table(world, rng, layers=("bronze", "silver"))
    if spec is None:
        return None
    delay_hours = rng.choice([4, 6, 6, 9])
    shortfall = round(rng.uniform(0.72, 0.86), 3)

    record = make_record(
        fault_type="LATE_DATA", at=at, asset_id=spec.asset_id, column=None,
        mechanism=(
            "the upstream load for " + spec.name + " is running " + str(delay_hours)
            + "h late and has delivered only "
            + str(int(shortfall * 100)) + "% of the expected partition"
        ),
        signals=[Signal.FRESHNESS, Signal.VOLUME],
        cause_class=CauseClass.LATE_DATA, world=world,
    )
    return Injection(
        record=record,
        mutations=[
            Mutation(
                fault_id=record.fault_id, kind="freshness_delay", asset_id=spec.asset_id,
                active_from=at, active_until=at + timedelta(hours=54),
                params={"seconds": delay_hours * 3600},
            ),
            Mutation(
                fault_id=record.fault_id, kind="row_multiplier", asset_id=spec.asset_id,
                active_from=at, active_until=at + timedelta(hours=54),
                params={"factor": shortfall},
            ),
        ],
    )


@fault("TIMEZONE_SHIFT")
def timezone_shift(world: World, rng: random.Random, at: datetime) -> Injection | None:
    """Timestamps shift by +5:30 and events cross the day boundary.

    Daily totals become wrong in both directions at once: one day gains what
    the next loses. Nothing is missing, which is what makes it so easy to miss.
    """
    spec = pick_table(world, rng, layers=("bronze", "silver"))
    if spec is None:
        return None
    hours = rng.choice([5.5, -8.0, 5.5, 9.0])

    record = make_record(
        fault_type="TIMEZONE_SHIFT", at=at, asset_id=spec.asset_id, column=None,
        mechanism=(
            "event timestamps on " + spec.name + " shifted by "
            + format(hours, "+.1f") + "h, moving records across the day boundary"
        ),
        signals=[Signal.VOLUME, Signal.FRESHNESS],
        cause_class=CauseClass.SOURCE_SCHEMA_DRIFT, world=world,
    )
    return Injection(
        record=record,
        mutations=[
            Mutation(
                fault_id=record.fault_id, kind="shift_hours", asset_id=spec.asset_id,
                active_from=at, params={"hours": hours},
            )
        ],
    )
