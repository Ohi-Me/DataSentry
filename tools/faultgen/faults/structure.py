"""Schema drift: the one fault class that is a fact rather than a statistic."""

from __future__ import annotations

import random
from datetime import datetime

from datasentry_core.enums import CauseClass, Signal
from tools.faultgen.faults import Injection, fault, make_record, numeric_columns, pick_table
from tools.faultgen.generators import Mutation
from tools.faultgen.world import World


@fault("SCHEMA_DRIFT")
def schema_drift(world: World, rng: random.Random, at: datetime) -> Injection | None:
    """An upstream table retypes a column.

    D7 fires deterministically on the schema hash, with no calibration and no
    consecutive-breach requirement, because there is no uncertainty to
    calibrate. It still flows through correlation and incident handling so
    there is exactly one alerting path in the system rather than two.
    """
    spec = pick_table(world, rng, predicate=lambda t: bool(numeric_columns(t)))
    if spec is None:
        return None
    column = rng.choice(numeric_columns(spec))
    new_type = rng.choice(["VARCHAR(32)", "FLOAT", "NUMBER(38,0)"])

    record = make_record(
        fault_type="SCHEMA_DRIFT", at=at, asset_id=spec.asset_id, column=column.name,
        mechanism=(
            "upstream retyped " + column.name + " from " + column.dtype
            + " to " + new_type + " without notice"
        ),
        signals=[Signal.SCHEMA], cause_class=CauseClass.SOURCE_SCHEMA_DRIFT, world=world,
    )
    return Injection(
        record=record,
        mutations=[
            Mutation(
                fault_id=record.fault_id, kind="retype_column", asset_id=spec.asset_id,
                column=column.name, active_from=at, params={"dtype": new_type},
            )
        ],
    )
