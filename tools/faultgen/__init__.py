"""tools.faultgen — the synthetic warehouse and its fault injector.

Built first, on purpose. Detection recall, incident precision, root-cause
accuracy and blast-radius recall are all measured against the ground truth
this package emits, so nothing downstream can be evaluated until it exists.

    world.py       200-table dbt-shaped DAG with real SQL per model
    generators.py  seasonal batch generation and declarative mutations
    faults/        nine fault types, each with ground truth
    decoys/        three legitimate events that must NOT raise an incident
    cli.py         seed | inject | list | reset
"""

from tools.faultgen.generators import Batch, Mutation, Simulator
from tools.faultgen.world import ColumnSpec, TableSpec, World

__all__ = ["Batch", "ColumnSpec", "Mutation", "Simulator", "TableSpec", "World"]
