"""Faults that break row-level integrity: nulls, duplicates, join semantics.

``JOIN_SEMANTICS`` is the demo centrepiece and the only fault here that also
rewrites a model and lands a commit. That matters: it gives the semantic diff
two genuine versions of the SQL to compare, so the root cause the agent
reaches is derived from real evidence rather than from a fixture.
"""

from __future__ import annotations

import random
from datetime import datetime

from datasentry_core.enums import CauseClass, Signal
from tools.faultgen.faults import (
    Injection,
    fault,
    id_columns,
    make_record,
    numeric_columns,
    pick_table,
)
from tools.faultgen.generators import Mutation
from tools.faultgen.world import World


@fault("NULL_FLOOD")
def null_flood(world: World, rng: random.Random, at: datetime) -> Injection | None:
    """A partner starts sending NULL where it used to send 0.

    The beta-binomial detector is what makes this honest across table sizes:
    the same 15% null rate is overwhelming evidence on a million-row table and
    barely evidence at all on thirty rows, and the posterior already knows the
    difference.
    """
    spec = pick_table(world, rng, predicate=lambda t: bool(numeric_columns(t)))
    if spec is None:
        return None
    column = rng.choice(numeric_columns(spec))
    rate = round(rng.uniform(0.12, 0.22), 3)

    record = make_record(
        fault_type="NULL_FLOOD", at=at, asset_id=spec.asset_id, column=column.name,
        mechanism=(
            "source system began emitting NULL instead of 0 for " + column.name
            + "; roughly " + str(int(rate * 100)) + "% of rows are now null"
        ),
        signals=[Signal.NULL_RATE], cause_class=CauseClass.SOURCE_SCHEMA_DRIFT, world=world,
    )
    return Injection(
        record=record,
        mutations=[
            Mutation(
                fault_id=record.fault_id, kind="null_flood", asset_id=spec.asset_id,
                column=column.name, active_from=at, params={"null_rate": rate},
            )
        ],
    )


@fault("DEDUP_BREAK")
def dedup_break(world: World, rng: random.Random, at: datetime) -> Injection | None:
    """A dedup key changes and rows fan out.

    The signature is the interesting part: row count rises while distinct
    values hold, so the distinctness ratio falls off a cliff. Volume alone
    reads as growth; cardinality alone reads as nothing. Only the ratio says
    "these are duplicates", which is the case D5 exists for.
    """
    spec = pick_table(world, rng, predicate=lambda t: bool(id_columns(t)))
    if spec is None:
        return None
    column = rng.choice(id_columns(spec))
    factor = rng.choice([2.0, 3.0, 3.0, 4.0])

    record = make_record(
        fault_type="DEDUP_BREAK", at=at, asset_id=spec.asset_id, column=column.name,
        mechanism=(
            "the dedup key on " + spec.name + " no longer includes every component, so "
            + column.name + " rows fan out " + format(factor, ".0f") + "x"
        ),
        signals=[Signal.VOLUME, Signal.CARDINALITY],
        cause_class=CauseClass.DEDUP_BREAK, world=world,
    )
    return Injection(
        record=record,
        mutations=[
            Mutation(
                fault_id=record.fault_id, kind="row_multiplier", asset_id=spec.asset_id,
                active_from=at, params={"factor": factor},
            ),
            Mutation(
                fault_id=record.fault_id, kind="distinct_collapse", asset_id=spec.asset_id,
                column=column.name, active_from=at, params={"factor": 1.0 / factor},
            ),
        ],
    )


@fault("JOIN_SEMANTICS")
def join_semantics(world: World, rng: random.Random, at: datetime) -> Injection | None:
    """INNER JOIN becomes LEFT JOIN in a silver model.

    Every pipeline stays green. Unmatched rows are now admitted with NULL
    measures, so volume rises, null rates on the joined columns rise, and
    every downstream revenue aggregate is understated. This is the fault the
    whole demo narrative is built around, and the one where a structured SQL
    diff turns a plausible guess into a certain answer.
    """
    candidates = [
        t for t in world.tables.values()
        if t.sql and "inner join" in t.sql.lower() and str(t.layer) == "silver"
    ]
    if not candidates:
        return None
    spine = [t for t in candidates if t.asset_id in world.spine]
    spec = rng.choice(spine or candidates)

    sql_before = spec.sql
    sql_after = _replace_first_inner_join(sql_before)
    if sql_after is None:
        return None

    measure = next((c for c in numeric_columns(spec)), None)
    joined = _joined_table(sql_before)

    record = make_record(
        fault_type="JOIN_SEMANTICS", at=at, asset_id=spec.asset_id,
        column=measure.name if measure else None,
        mechanism=(
            spec.name + " changed its join to " + joined + " from INNER to LEFT, admitting "
            "unmatched rows with NULL measures"
        ),
        signals=[Signal.VOLUME, Signal.NULL_RATE],
        cause_class=CauseClass.JOIN_SEMANTICS, world=world,
    )

    mutations = [
        Mutation(
            fault_id=record.fault_id, kind="row_multiplier", asset_id=spec.asset_id,
            # A loosened join admits every previously-unmatched row. In a
            # warehouse where a tenth to a third of orders are guest checkouts
            # that is a 25-45% lift, not a rounding error.
            active_from=at, params={"factor": round(rng.uniform(1.25, 1.45), 3)},
        )
    ]
    for column in numeric_columns(spec)[:2]:
        mutations.append(
            Mutation(
                fault_id=record.fault_id, kind="null_flood", asset_id=spec.asset_id,
                column=column.name, active_from=at,
                params={"null_rate": round(rng.uniform(0.07, 0.13), 3)},
            )
        )

    return Injection(
        record=record,
        mutations=mutations,
        sql_before=sql_before,
        sql_after=sql_after,
        model_ref=spec.model_ref,
        commit_message=(
            "fix(" + spec.name + "): widen customer join to keep guest orders\n\n"
            "Guest checkout orders have no customer row yet, so the inner join was\n"
            "dropping them from the staging model. Switching to a left join keeps\n"
            "them in scope for the funnel dashboard."
        ),
        commit_files=[spec.model_ref or ""],
        pr_title="Keep guest orders in " + spec.name,
    )


def _replace_first_inner_join(sql: str) -> str | None:
    lines = sql.splitlines()
    for i, line in enumerate(lines):
        if "inner join" in line.lower():
            idx = line.lower().index("inner join")
            lines[i] = line[:idx] + "left join" + line[idx + len("inner join"):]
            return "\n".join(lines) + "\n"
    return None


def _joined_table(sql: str) -> str:
    for line in sql.splitlines():
        low = line.lower().strip()
        if low.startswith("inner join"):
            parts = line.split()
            if len(parts) >= 3:
                return parts[2]
    return "the joined table"
