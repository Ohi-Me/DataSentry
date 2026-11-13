"""datasentry_lineage — column-level lineage.

Table-level lineage cannot answer "which downstream feature broke", so it
cannot answer the question the product exists to answer. Everything here
works at column granularity (ADR-001).

Three pieces:

    sqlparse   a SQL reader sized for lineage, not for execution
    resolver   AST plus catalog snapshot -> column-level edges, with coverage
    semdiff    structured diff between two versions of a model
    graph      traversal, blast radius, criticality, closure

Coverage is reported, never assumed. SQL the resolver cannot handle becomes
an inferred edge from observed access and shows up in the parse-coverage
metric, rather than becoming an invisible hole in the blast radius.
"""

from datasentry_lineage.graph import (
    LineageIndex,
    build_closure,
    compute_blast_radius,
    criticality,
    lineage_distance,
    topological_depth,
)
from datasentry_lineage.resolver import Catalog, ParseOutcome, resolve_model
from datasentry_lineage.semdiff import SemanticDiff, diff_sql
from datasentry_lineage.sqlparse import ParseError, Select, parse, tokenize

__all__ = [
    "Catalog",
    "LineageIndex",
    "ParseError",
    "ParseOutcome",
    "Select",
    "SemanticDiff",
    "build_closure",
    "compute_blast_radius",
    "criticality",
    "diff_sql",
    "lineage_distance",
    "parse",
    "resolve_model",
    "tokenize",
    "topological_depth",
]
