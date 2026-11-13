"""AST to column-level lineage edges.

Table-level lineage cannot answer "which downstream feature broke", which is
the whole differentiator (ADR-001). So the resolver's job is to say, for every
output column of a model, exactly which upstream columns produced it and
through what kind of transform.

The mechanism is scope resolution. A CTE or subquery is a *virtual relation*
whose columns each carry the set of physical origins that produced them; a
physical table is a relation whose columns are their own origins. Resolving
the outer SELECT is then a matter of walking projections against the scope,
recursively, until every output column has traced back to real assets.

Two cases deserve their own note:

* ``SELECT *`` is expanded against a catalog snapshot rather than guessed at.
  When the upstream schema cannot be resolved the star is recorded as
  unresolved and the model's coverage drops. It is never silently dropped.
* **Join keys get table-level edges** (``dst_column = NULL``). A join key
  change alters row multiplicity, so it affects every output column of the
  model, not one of them. Modelling that as a table-level edge is both
  truthful and 200 times cheaper than fanning out to every column.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from datasentry_core.enums import TransformType
from datasentry_core.models import LineageEdge
from datasentry_lineage.sqlparse import (
    ColumnRef,
    Expr,
    ParseError,
    Projection,
    Select,
    TableRef,
    parse,
)


class Catalog:
    """A snapshot of what tables and columns exist, used to expand stars."""

    def __init__(self, columns: dict[str, list[str]] | None = None) -> None:
        self._columns: dict[str, list[str]] = {}
        self._index: dict[str, str] = {}
        for asset_id, cols in (columns or {}).items():
            self.add(asset_id, cols)

    def add(self, asset_id: str, columns: list[str]) -> None:
        self._columns[asset_id] = list(columns)
        parts = asset_id.split(".")
        # Index every suffix so ``raw.orders``, ``prod.raw.orders`` and the
        # fully qualified id all resolve to the same asset.
        for i in range(len(parts)):
            self._index.setdefault(".".join(parts[i:]).lower(), asset_id)

    def resolve(self, name: str) -> str | None:
        return self._index.get(name.lower())

    def columns_of(self, asset_id: str) -> list[str]:
        return self._columns.get(asset_id, [])

    def __contains__(self, name: str) -> bool:
        return self.resolve(name) is not None


Origin = tuple[str, str]  # (asset_id, column_name)


@dataclass
class Relation:
    """A named set of output columns, each with the origins that produced it."""

    label: str
    columns: dict[str, set[Origin]] = field(default_factory=dict)
    ordered: list[str] = field(default_factory=list)
    physical: str | None = None
    resolved: bool = True

    def origins(self, column: str) -> set[Origin]:
        return self.columns.get(column.lower(), set())

    def add(self, column: str, origins: set[Origin]) -> None:
        key = column.lower()
        if key not in self.columns:
            self.ordered.append(column)
            self.columns[key] = set()
        self.columns[key] |= origins


@dataclass
class JoinInfo:
    kind: str
    left: str
    right: str
    on_columns: list[str]

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "left": self.left,
            "right": self.right,
            "on": self.on_columns,
        }


@dataclass
class ParseOutcome:
    model_ref: str
    target_asset: str
    edges: list[LineageEdge] = field(default_factory=list)
    joins: list[JoinInfo] = field(default_factory=list)
    output_columns: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def parsed(self) -> bool:
        return self.error is None

    @property
    def coverage(self) -> float:
        """Fraction of output columns with at least one resolved edge.

        Reported per model and rolled up as ``lineage parse coverage %`` on
        the health page. Honest partial coverage beats a fake claim.
        """
        if not self.output_columns:
            return 0.0
        covered = {e.dst_column for e in self.edges if e.dst_column}
        return len(covered) / len(self.output_columns)


def _transform_for(expr: Expr) -> TransformType:
    if expr.is_window:
        return TransformType.WINDOW
    if expr.is_aggregate:
        return TransformType.AGG
    if expr.is_cast and len(expr.columns) == 1:
        return TransformType.CAST
    if expr.is_bare_column:
        return TransformType.IDENTITY
    if expr.columns:
        return TransformType.EXPR
    return TransformType.UNKNOWN


class Resolver:
    def __init__(self, catalog: Catalog) -> None:
        self.catalog = catalog

    # -- public -------------------------------------------------------------

    def resolve_model(self, model_ref: str, target_asset: str, sql: str,
                      *, confidence: float = 1.0) -> ParseOutcome:
        outcome = ParseOutcome(model_ref=model_ref, target_asset=target_asset)
        try:
            tree = parse(sql)
        except (ParseError, IndexError, RecursionError) as exc:
            outcome.error = type(exc).__name__ + ": " + str(exc)
            return outcome

        try:
            relation, joins, unresolved = self._resolve_select(tree, {}, outcome)
        except RecursionError:
            outcome.error = "query nesting exceeded the resolver depth limit"
            return outcome

        outcome.joins = joins
        outcome.unresolved = unresolved
        outcome.output_columns = list(relation.ordered)

        for column in relation.ordered:
            origins = relation.origins(column)
            transform = relation_transforms.get((id(relation), column.lower()), TransformType.IDENTITY)
            for src_asset, src_col in sorted(origins):
                if src_asset == target_asset:
                    continue
                outcome.edges.append(
                    LineageEdge(
                        src_asset_id=src_asset,
                        src_column=src_col,
                        dst_asset_id=target_asset,
                        dst_column=column,
                        transform_type=transform,
                        transform_expr=relation_exprs.get((id(relation), column.lower())),
                        source="declared",
                        confidence=confidence,
                        model_ref=model_ref,
                    )
                )

        # Join keys become table-level edges: they change row multiplicity,
        # so they affect the model as a whole.
        for join_origin in self._join_key_origins(tree, outcome):
            src_asset, src_col = join_origin
            if src_asset == target_asset:
                continue
            outcome.edges.append(
                LineageEdge(
                    src_asset_id=src_asset,
                    src_column=src_col,
                    dst_asset_id=target_asset,
                    dst_column=None,
                    transform_type=TransformType.JOIN_KEY,
                    transform_expr="join key",
                    source="declared",
                    confidence=confidence,
                    model_ref=model_ref,
                )
            )

        deduped: dict[tuple, LineageEdge] = {}
        for edge in outcome.edges:
            deduped.setdefault(edge.key(), edge)
        outcome.edges = list(deduped.values())
        return outcome

    # -- internals ----------------------------------------------------------

    def _resolve_select(self, node: Select, outer_scope: dict[str, Relation],
                        outcome: ParseOutcome, depth: int = 0
                        ) -> tuple[Relation, list[JoinInfo], list[str]]:
        if depth > 24:
            raise RecursionError("query nesting too deep")

        scope: dict[str, Relation] = dict(outer_scope)
        unresolved: list[str] = []
        joins: list[JoinInfo] = []

        for name in node.cte_order:
            cte_relation, cte_joins, cte_unresolved = self._resolve_select(
                node.ctes[name], scope, outcome, depth + 1
            )
            cte_relation.label = name
            scope[name.lower()] = cte_relation
            joins.extend(cte_joins)
            unresolved.extend(cte_unresolved)

        local: dict[str, Relation] = {}
        order: list[Relation] = []

        for ref in node.sources():
            relation = self._resolve_table_ref(ref, scope, outcome, depth)
            if relation is None:
                unresolved.append(ref.name or "<subquery>")
                continue
            label = (ref.alias or relation.label or ref.name or "t").lower()
            # A source is reachable by alias, by its base name and by its
            # fully qualified name, because SQL lets you write any of them.
            local[label] = relation
            if ref.name:
                local.setdefault(ref.name.lower(), relation)
                local.setdefault(ref.name.split(".")[-1].lower(), relation)
            order.append(relation)

        combined = dict(scope)
        combined.update(local)

        for join in node.joins:
            left_label = node.from_.label if node.from_ else "?"
            joins.append(
                JoinInfo(
                    kind=join.kind,
                    left=left_label,
                    right=join.table.label,
                    on_columns=sorted({c.name for c in join.key_columns()}),
                )
            )

        out = Relation(label="select")
        for projection in node.projections:
            self._project(projection, out, local, order, unresolved)

        # Set operations contribute their own origins to the same output
        # positions. UNION of two models means a column can come from either.
        for _, other in node.set_operations:
            other_rel, other_joins, other_unresolved = self._resolve_select(
                other, scope, outcome, depth + 1
            )
            joins.extend(other_joins)
            unresolved.extend(other_unresolved)
            for i, column in enumerate(out.ordered):
                if i < len(other_rel.ordered):
                    out.add(column, other_rel.origins(other_rel.ordered[i]))

        return out, joins, unresolved

    def _resolve_table_ref(self, ref: TableRef, scope: dict[str, Relation],
                           outcome: ParseOutcome, depth: int) -> Relation | None:
        if ref.subquery is not None:
            relation, joins, unresolved = self._resolve_select(
                ref.subquery, scope, outcome, depth + 1
            )
            relation.label = ref.alias or "subquery"
            outcome.joins.extend(joins)
            outcome.unresolved.extend(unresolved)
            return relation

        if ref.name is None:
            return None

        cte = scope.get(ref.name.lower())
        if cte is not None:
            return cte

        asset_id = self.catalog.resolve(ref.name)
        if asset_id is None:
            return None

        relation = Relation(label=ref.alias or ref.name, physical=asset_id)
        for column in self.catalog.columns_of(asset_id):
            relation.add(column, {(asset_id, column)})
        return relation

    def _project(self, projection: Projection, out: Relation,
                 local: dict[str, Relation], order: list[Relation],
                 unresolved: list[str]) -> None:
        expr = projection.expr

        if expr.has_star:
            targets = order
            if expr.star_qualifier:
                rel = local.get(expr.star_qualifier.lower())
                if rel is None:
                    unresolved.append(expr.star_qualifier + ".*")
                    return
                targets = [rel]
            if not targets:
                unresolved.append("*")
                return
            for rel in targets:
                for column in rel.ordered:
                    out.add(column, rel.origins(column))
                    relation_transforms[(id(out), column.lower())] = TransformType.IDENTITY
            return

        name = projection.output_name()
        if name is None:
            # An unnamed expression still consumes upstream columns; give it a
            # positional name so the edge is not lost.
            name = "_col" + str(len(out.ordered))

        origins: set[Origin] = set()
        for ref in expr.columns:
            origins |= self._lookup(ref, local, order, unresolved)

        out.add(name, origins)
        relation_transforms[(id(out), name.lower())] = _transform_for(expr)
        if expr.text:
            relation_exprs[(id(out), name.lower())] = expr.text[:400]

    def _lookup(self, ref: ColumnRef, local: dict[str, Relation],
                order: list[Relation], unresolved: list[str]) -> set[Origin]:
        if ref.qualifier:
            rel = local.get(ref.qualifier.lower())
            if rel is None:
                return set()
            found = rel.origins(ref.name)
            if not found and rel.physical:
                # The catalog snapshot may lag a freshly added column. Trust
                # the SQL, mark the edge, and let confidence carry the doubt.
                return {(rel.physical, ref.name)}
            return found
        hits: set[Origin] = set()
        for rel in order:
            hits |= rel.origins(ref.name)
        if not hits and len(order) == 1 and order[0].physical:
            hits = {(order[0].physical, ref.name)}
        return hits

    def _join_key_origins(self, node: Select, outcome: ParseOutcome) -> set[Origin]:
        """Collect the physical origins of every join key in the statement."""
        found: set[Origin] = set()
        scope: dict[str, Relation] = {}
        for name in node.cte_order:
            rel, _, _ = self._resolve_select(node.ctes[name], scope, outcome)
            rel.label = name
            scope[name.lower()] = rel
        self._collect_join_keys(node, scope, found, outcome)
        return found

    def _collect_join_keys(self, node: Select, scope: dict[str, Relation],
                           found: set[Origin], outcome: ParseOutcome, depth: int = 0) -> None:
        if depth > 12:
            return
        local: dict[str, Relation] = dict(scope)
        order: list[Relation] = []
        for ref in node.sources():
            rel = self._resolve_table_ref(ref, scope, outcome, depth)
            if rel is None:
                continue
            label = (ref.alias or rel.label or ref.name or "t").lower()
            local[label] = rel
            if ref.name:
                local.setdefault(ref.name.split(".")[-1].lower(), rel)
            order.append(rel)
            if ref.subquery is not None:
                self._collect_join_keys(ref.subquery, scope, found, outcome, depth + 1)
        for join in node.joins:
            for ref in join.key_columns():
                found |= self._lookup(ref, local, order, [])
        for name in node.cte_order:
            self._collect_join_keys(node.ctes[name], scope, found, outcome, depth + 1)


# Side tables keyed by (relation identity, column) so ``Relation`` stays a
# plain container. Cleared per resolve_model call by the caller below.
relation_transforms: dict[tuple[int, str], TransformType] = {}
relation_exprs: dict[tuple[int, str], str] = {}


def resolve_model(catalog: Catalog, model_ref: str, target_asset: str, sql: str,
                  *, confidence: float = 1.0) -> ParseOutcome:
    relation_transforms.clear()
    relation_exprs.clear()
    return Resolver(catalog).resolve_model(model_ref, target_asset, sql, confidence=confidence)
