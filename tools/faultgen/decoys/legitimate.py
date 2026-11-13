"""Decoys: things that look like incidents and are not.

These are as important as the faults. A harness that only injects real
problems measures recall and nothing else, and a detector tuned only for
recall is a detector that fires constantly. Decoys are what make the precision
and false-positive-rate numbers mean something.

Each of these deliberately moves a signal the detectors watch. A backfill
triples the row count. A campaign doubles traffic in one channel. A planned
column addition changes the schema hash. The correct outcome for all three is
**no incident** — either because the movement stays inside the conformal
interval, or because it is announced and therefore muted.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta

from datasentry_core.enums import Signal
from tools.faultgen.faults import (
    Injection,
    category_columns,
    fault,
    make_record,
    pick_table,
)
from tools.faultgen.generators import Mutation
from tools.faultgen.world import World


@fault("BACKFILL", decoy=True)
def backfill(world: World, rng: random.Random, at: datetime) -> Injection | None:
    """A planned backfill lands three cycles of history at once.

    Volume spikes hard and then returns. The detector may well breach on the
    first cycle; the consecutive-breach requirement is what stops it becoming
    an incident, which is precisely the behaviour that rule buys.
    """
    spec = pick_table(world, rng, layers=("bronze", "silver"))
    if spec is None:
        return None
    factor = round(rng.uniform(2.4, 3.4), 2)

    record = make_record(
        fault_type="BACKFILL", at=at, asset_id=spec.asset_id, column=None,
        mechanism=(
            "scheduled backfill reloaded three days of history into " + spec.name
            + " in a single window — announced, expected, not a regression"
        ),
        signals=[Signal.VOLUME], world=world, is_decoy=True,
    )
    return Injection(
        record=record,
        mutations=[
            Mutation(
                fault_id=record.fault_id, kind="row_multiplier", asset_id=spec.asset_id,
                active_from=at, active_until=at + timedelta(hours=4),
                params={"factor": factor},
            )
        ],
    )


@fault("CAMPAIGN_SPIKE", decoy=True)
def campaign_spike(world: World, rng: random.Random, at: datetime) -> Injection | None:
    """A marketing push lifts one channel's share for two days.

    Genuinely different data, genuinely not a defect. If the categorical
    detector cannot tell this apart from a renamed enum value, its precision
    number is worthless.
    """
    spec = pick_table(world, rng, predicate=lambda t: bool(category_columns(t)))
    if spec is None:
        return None
    column = rng.choice(category_columns(spec))

    record = make_record(
        fault_type="CAMPAIGN_SPIKE", at=at, asset_id=spec.asset_id, column=column.name,
        mechanism=(
            "a paid campaign lifted traffic through one " + column.name
            + " value for two days — real demand, not a data defect"
        ),
        signals=[Signal.VOLUME, Signal.CATEGORICAL_DRIFT], world=world, is_decoy=True,
    )
    return Injection(
        record=record,
        mutations=[
            Mutation(
                fault_id=record.fault_id, kind="row_multiplier", asset_id=spec.asset_id,
                active_from=at, active_until=at + timedelta(hours=48),
                params={"factor": round(rng.uniform(1.35, 1.7), 2)},
            )
        ],
    )


@fault("PLANNED_COLUMN_ADD", decoy=True)
def planned_column_add(world: World, rng: random.Random, at: datetime) -> Injection | None:
    """An additive, backward-compatible column addition.

    The schema hash changes, so D7 will see it. Whether that becomes an
    incident is a policy question, not a statistical one — and the answer for
    an additive change on a non-critical asset is no.
    """
    spec = pick_table(world, rng, layers=("bronze",))
    if spec is None:
        return None

    record = make_record(
        fault_type="PLANNED_COLUMN_ADD", at=at, asset_id=spec.asset_id, column=None,
        mechanism=(
            "an additive, backward-compatible column was added to " + spec.name
            + " as part of a planned release"
        ),
        signals=[Signal.SCHEMA], world=world, is_decoy=True,
    )
    return Injection(
        record=record,
        mutations=[
            Mutation(
                fault_id=record.fault_id, kind="add_column", asset_id=spec.asset_id,
                active_from=at, params={"name": "experiment_bucket", "dtype": "VARCHAR(16)"},
            )
        ],
    )
