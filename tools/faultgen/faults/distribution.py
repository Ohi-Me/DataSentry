"""Faults that change what the values look like without changing the schema.

These are the expensive ones in real life. The pipeline is green, the row
count is normal, the dashboard is wrong, and nobody finds out for a week.
"""

from __future__ import annotations

import random
from datetime import datetime

from datasentry_core.enums import CauseClass, Signal
from tools.faultgen.faults import (
    Injection,
    category_columns,
    fault,
    make_record,
    numeric_columns,
    pick_table,
    text_columns,
)
from tools.faultgen.generators import Mutation
from tools.faultgen.world import World

FX_RATES = {"INR": 83.2, "JPY": 149.5, "EUR": 0.92, "GBP": 0.79, "MXN": 17.1}


@fault("UNIT_CHANGE")
def unit_change(world: World, rng: random.Random, at: datetime) -> Injection | None:
    """An upstream team starts sending local currency instead of USD.

    The canonical silent regression. Nothing errors, every downstream revenue
    number is wrong by a constant factor, and only a distributional detector
    that is scale-aware will notice. W1 normalised by the baseline IQR moves
    by roughly 80 IQRs here — a z-score on the mean would report "revenue is
    up", which is also what a good quarter looks like.
    """
    spec = pick_table(world, rng, predicate=lambda t: bool(
        [c for c in numeric_columns(t) if c.unit == "USD"]
    ))
    if spec is None:
        return None
    column = rng.choice([c for c in numeric_columns(spec) if c.unit == "USD"])
    currency = rng.choice(list(FX_RATES))
    factor = FX_RATES[currency]

    record = make_record(
        fault_type="UNIT_CHANGE", at=at, asset_id=spec.asset_id, column=column.name,
        mechanism=(
            "upstream producer switched " + column.name + " from USD to " + currency
            + " without a schema change; values are inflated " + format(factor, ".1f") + "x"
        ),
        signals=[Signal.NUMERIC_DRIFT], cause_class=CauseClass.UNIT_CHANGE, world=world,
    )
    return Injection(
        record=record,
        mutations=[
            Mutation(
                fault_id=record.fault_id, kind="scale_numeric", asset_id=spec.asset_id,
                column=column.name, active_from=at,
                params={"factor": factor, "unit": currency},
            )
        ],
    )


@fault("SILENT_TRUNCATION")
def silent_truncation(world: World, rng: random.Random, at: datetime) -> Injection | None:
    """A varchar column starts arriving truncated to 20 characters.

    Caught by the string-length digest, which is why profiling ``len(value)``
    as its own distribution earns its keep. Nothing about the null rate,
    row count or cardinality moves enough to notice.
    """
    spec = pick_table(world, rng, predicate=lambda t: bool(text_columns(t)))
    if spec is None:
        return None
    column = rng.choice(text_columns(spec))
    cap = 20

    record = make_record(
        fault_type="SILENT_TRUNCATION", at=at, asset_id=spec.asset_id, column=column.name,
        mechanism=(
            "a widened source column is being written into a VARCHAR(" + str(cap)
            + ") target; " + column.name + " values are silently truncated"
        ),
        signals=[Signal.STRING_LENGTH, Signal.CARDINALITY],
        cause_class=CauseClass.SOURCE_SCHEMA_DRIFT, world=world,
    )
    return Injection(
        record=record,
        mutations=[
            Mutation(
                fault_id=record.fault_id, kind="truncate_text", asset_id=spec.asset_id,
                column=column.name, active_from=at, params={"max_length": cap},
            )
        ],
    )


@fault("CATEGORY_SHIFT")
def category_shift(world: World, rng: random.Random, at: datetime) -> Injection | None:
    """A new enum value replaces part of an existing one.

    Typically an upstream service renaming a status. Downstream filters that
    match on the old value quietly start dropping rows.
    """
    spec = pick_table(world, rng, predicate=lambda t: bool(category_columns(t)))
    if spec is None:
        return None
    column = rng.choice(category_columns(spec))
    old = rng.choice(column.categories)
    new = old.upper() + "_V2" if old.isupper() else old + "_v2"
    share = round(rng.uniform(0.28, 0.42), 2)

    record = make_record(
        fault_type="CATEGORY_SHIFT", at=at, asset_id=spec.asset_id, column=column.name,
        mechanism=(
            "upstream service renamed " + old + " to " + new + " for "
            + str(int(share * 100)) + "% of traffic in " + column.name
        ),
        signals=[Signal.CATEGORICAL_DRIFT, Signal.NEW_CATEGORY],
        cause_class=CauseClass.SOURCE_SCHEMA_DRIFT, world=world,
    )
    return Injection(
        record=record,
        mutations=[
            Mutation(
                fault_id=record.fault_id, kind="category_replace", asset_id=spec.asset_id,
                column=column.name, active_from=at,
                params={"old": old, "new": new, "share": share},
            )
        ],
    )
