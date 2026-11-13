"""Fault injection with ground truth.

Every evaluation number in this project traces back to a ``FaultRecord``.
Detection recall, incident precision, root-asset accuracy, root-cause
accuracy, blast-radius recall — all of them are a comparison against what was
recorded here at injection time. So this is the first thing built and the
thing to get right.

A fault is a declarative bundle: the mutations that change the generative
model, optionally a rewritten SQL model plus the commit that carried it, and
the ground truth that says what should have been detected and where. Nothing
about a fault reaches into the detectors, which is what stops the harness
from quietly grading its own homework.

**Decoys matter as much as faults.** A legitimate backfill, a marketing
campaign spike and a planned column addition are injected the same way and
recorded with ``is_decoy=True``. None of them should raise an incident. A
harness with no negatives measures nothing.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from datasentry_core.enums import CauseClass, Signal
from datasentry_core.models import FaultRecord
from tools.faultgen.generators import Mutation
from tools.faultgen.world import TableSpec, World


@dataclass
class Injection:
    """Everything one injected fault changes about the world."""

    record: FaultRecord
    mutations: list[Mutation] = field(default_factory=list)
    sql_before: str | None = None
    sql_after: str | None = None
    model_ref: str | None = None
    commit_message: str = ""
    commit_files: list[str] = field(default_factory=list)
    pr_title: str = ""

    @property
    def touches_code(self) -> bool:
        return self.sql_after is not None and self.model_ref is not None


Builder = Callable[[World, random.Random, datetime], "Injection | None"]

REGISTRY: dict[str, Builder] = {}
DECOY_NAMES: set[str] = set()


def fault(name: str, *, decoy: bool = False) -> Callable[[Builder], Builder]:
    def wrap(fn: Builder) -> Builder:
        REGISTRY[name] = fn
        if decoy:
            DECOY_NAMES.add(name)
        return fn

    return wrap


# ---------------------------------------------------------------------------
# Helpers shared by the fault modules
# ---------------------------------------------------------------------------


def downstream_of(world: World, asset_id: str, column: str | None = None,
                  depth: int = 8) -> list[str]:
    """Ground-truth blast radius, computed from the DAG the world declares.

    Deliberately independent of the lineage parser: if both were derived from
    the same code, blast-radius recall would measure nothing.

    **Column-aware when a column is given.** A downstream table is affected by
    a null flood in ``amount`` only if it actually selects a column derived
    from ``amount``. Scoring a column-level blast radius against a table-level
    truth marks the system wrong for being precise -- which is the entire
    point of ADR-001, so grading against the coarser answer would be grading
    against the thing we chose not to build.
    """
    children: dict[str, list[str]] = {}
    for spec in world.tables.values():
        for parent in spec.upstream:
            children.setdefault(parent, []).append(spec.asset_id)

    seen: list[str] = []
    # (asset, the name the affected column carries in that asset)
    frontier: list[tuple[str, str | None]] = [(asset_id, column)]
    for _ in range(depth):
        nxt: list[tuple[str, str | None]] = []
        for node, carried in frontier:
            for child in children.get(node, ()):
                spec = world.tables.get(child)
                if spec is None:
                    continue
                inherited: str | None = None
                if carried is None:
                    inherited = None            # a table-level fault affects everything
                elif spec.column(carried) is not None:
                    inherited = carried         # forwarded under the same name
                elif spec.column("total_" + carried) is not None:
                    inherited = "total_" + carried
                elif spec.column("avg_" + carried) is not None:
                    inherited = "avg_" + carried
                else:
                    continue                    # this model does not read that column
                if child not in seen:
                    seen.append(child)
                    nxt.append((child, inherited))
        if not nxt:
            break
        frontier = nxt
    return seen


# Assets already carrying a fault, plus everything reachable from them.
# Injecting a second fault into a subtree that is already corrupted produces
# one entangled incident and measures nothing about either fault.
EXCLUDED: set[str] = set()


def reserve(world: World, asset_id: str, depth: int = 4) -> None:
    """Reserve an asset's whole connected neighbourhood.

    Undirected, not just downstream. Two faults are entangled the moment their
    subtrees *meet* anywhere, and they meet more often than the DAG picture
    suggests: a fault on a bronze table that feeds a silver model three hops
    below another fault's root produces one incident containing both, and both
    become unmeasurable. Reserving the neighbourhood in both directions is
    what keeps each injected fault independently scoreable.

    Real warehouses of course have simultaneous entangled incidents, and the
    correlator handles that case — but a harness that mixes them by accident
    is measuring nothing rather than measuring that.
    """
    neighbours: dict[str, set[str]] = {}
    for spec in world.tables.values():
        for parent in spec.upstream:
            neighbours.setdefault(parent, set()).add(spec.asset_id)
            neighbours.setdefault(spec.asset_id, set()).add(parent)

    EXCLUDED.add(asset_id)
    frontier = {asset_id}
    seen = {asset_id}
    for _ in range(depth):
        nxt: set[str] = set()
        for node in frontier:
            nxt |= neighbours.get(node, set())
        nxt -= seen
        if not nxt:
            break
        EXCLUDED.update(nxt)
        seen |= nxt
        frontier = nxt


def release() -> None:
    EXCLUDED.clear()


def pick_table(world: World, rng: random.Random, *, prefer_spine: bool = True,
               layers: tuple[str, ...] = ("bronze", "silver"),
               predicate: Callable[[TableSpec], bool] | None = None) -> TableSpec | None:
    candidates = [
        t for t in world.tables.values()
        if str(t.layer) in layers
        and t.asset_id not in EXCLUDED
        and (predicate is None or predicate(t))
    ]
    if not candidates:
        # Every unreserved candidate is taken. Better to reuse an asset than
        # to skip the fault entirely and quietly shrink the eval set.
        candidates = [
            t for t in world.tables.values()
            if str(t.layer) in layers and (predicate is None or predicate(t))
        ]
    if not candidates:
        return None
    if prefer_spine:
        spine = [t for t in candidates if t.asset_id in world.spine]
        if spine and rng.random() < 0.65:
            return rng.choice(spine)
    return rng.choice(candidates)


def make_record(*, fault_type: str, at: datetime, asset_id: str, column: str | None,
                mechanism: str, signals: list[Signal], world: World,
                cause_class: CauseClass | None = None, is_decoy: bool = False,
                commit_sha: str | None = None, pr_number: int | None = None) -> FaultRecord:
    return FaultRecord(
        fault_id=fault_type.lower() + "-" + at.strftime("%Y%m%dT%H%M") + "-" + asset_id.split(".")[-1],
        fault_type=fault_type,
        injected_at=at,
        root_asset_id=asset_id,
        root_column=column,
        mechanism=mechanism,
        cause_class=cause_class,
        expected_signals=signals,
        expected_downstream=downstream_of(world, asset_id, column),
        commit_sha=commit_sha,
        pr_number=pr_number,
        is_decoy=is_decoy,
    )


def numeric_columns(spec: TableSpec) -> list:
    return [c for c in spec.columns if c.kind == "measure"]


def category_columns(spec: TableSpec) -> list:
    return [c for c in spec.columns if c.kind == "category" and len(c.categories) > 1]


def id_columns(spec: TableSpec) -> list:
    return [c for c in spec.columns if c.kind == "id"]


def text_columns(spec: TableSpec) -> list:
    return [c for c in spec.columns if c.kind == "text"]


# Import the modules for their registration side effects. Kept at the bottom
# so the helpers above are defined by the time they run.
from tools.faultgen.decoys import legitimate  # noqa: E402,F401
from tools.faultgen.faults import (  # noqa: E402
    distribution,  # noqa: F401
    integrity,  # noqa: F401
    structure,  # noqa: F401
    timing,  # noqa: F401
)


def available() -> list[str]:
    return sorted(REGISTRY)


def real_faults() -> list[str]:
    return sorted(n for n in REGISTRY if n not in DECOY_NAMES)


def decoys() -> list[str]:
    return sorted(DECOY_NAMES)


def build(name: str, world: World, rng: random.Random, at: datetime) -> Injection | None:
    builder = REGISTRY.get(name.upper())
    if builder is None:
        raise KeyError("unknown fault type: " + name)
    return builder(world, rng, at)
