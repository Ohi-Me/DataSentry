"""Structured SQL diff.

This is the highest-value tool in the system, and the reason is narrow enough
to state in one sentence: handing a reasoning model a raw text diff gets you
"the SQL changed", while handing it

    {"joins": [{"model": "stg_orders", "from": "INNER", "to": "LEFT",
                "on": ["customer_id"]}]}

gets you a correct root cause. Structure the evidence and the reasoning falls
out. Almost all of the accuracy in the RCA loop is bought here rather than in
the prompt.

The diff compares two parsed statements along the axes that actually change
data semantics — join kinds, join keys, filter predicates, casts, aggregations,
the set of output columns, and DISTINCT — and ignores the axes that do not,
such as whitespace, alias renames and column ordering. A model reading this
never has to infer intent from formatting noise.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from datasentry_lineage.sqlparse import Expr, ParseError, Select, parse


@dataclass
class Change:
    kind: str
    detail: str
    before: Any = None
    after: Any = None
    severity: str = "info"  # info | notable | breaking

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "detail": self.detail,
            "before": self.before,
            "after": self.after,
            "severity": self.severity,
        }


@dataclass
class SemanticDiff:
    model_ref: str
    sha_before: str
    sha_after: str
    changes: list[Change] = field(default_factory=list)
    error: str | None = None

    @property
    def breaking(self) -> list[Change]:
        return [c for c in self.changes if c.severity == "breaking"]

    @property
    def is_empty(self) -> bool:
        return not self.changes and self.error is None

    def headline(self) -> str:
        if self.error:
            return "could not parse: " + self.error
        if not self.changes:
            return "no semantic change"
        ranked = sorted(
            self.changes,
            key=lambda c: {"breaking": 0, "notable": 1, "info": 2}[c.severity],
        )
        return ranked[0].detail

    def as_dict(self) -> dict[str, Any]:
        return {
            "model_ref": self.model_ref,
            "sha_before": self.sha_before,
            "sha_after": self.sha_after,
            "headline": self.headline(),
            "changes": [c.as_dict() for c in self.changes],
            "breaking_count": len(self.breaking),
            "error": self.error,
        }


# Join kinds ordered by how many rows they admit. Loosening a join is the
# classic silent regression: nothing errors, the row count moves, and every
# downstream aggregate is quietly wrong.
_ADMISSIVENESS = {"CROSS": 4, "FULL": 3, "LEFT": 2, "RIGHT": 2, "INNER": 1}


def _collect_joins(node: Select, prefix: str = "") -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for name in node.cte_order:
        out.update(_collect_joins(node.ctes[name], prefix + name + "/"))
    for i, join in enumerate(node.joins):
        target = join.table.name or (join.table.alias or "subquery")
        key = prefix + target + "#" + str(i)
        out[key] = {
            "target": target,
            "kind": join.kind,
            "on": sorted({c.name.lower() for c in join.key_columns()}),
            "scope": prefix.rstrip("/") or "main",
        }
        if join.table.subquery is not None:
            out.update(_collect_joins(join.table.subquery, key + "/"))
    if node.from_ and node.from_.subquery is not None:
        out.update(_collect_joins(node.from_.subquery, prefix + "from/"))
    for _, other in node.set_operations:
        out.update(_collect_joins(other, prefix + "union/"))
    return out


def _collect_projections(node: Select, prefix: str = "") -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for name in node.cte_order:
        out.update(_collect_projections(node.ctes[name], prefix + name + "/"))
    for projection in node.projections:
        name = projection.output_name()
        if not name:
            continue
        expr = projection.expr
        out[prefix + name.lower()] = {
            "name": name,
            "sources": sorted(str(c).lower() for c in expr.columns),
            "functions": sorted(expr.functions),
            "is_cast": expr.is_cast,
            "is_aggregate": expr.is_aggregate,
            "is_window": expr.is_window,
            "text": _normalise(expr.text),
        }
    return out


def _collect_filters(node: Select, prefix: str = "") -> dict[str, str]:
    out: dict[str, str] = {}
    for name in node.cte_order:
        out.update(_collect_filters(node.ctes[name], prefix + name + "/"))
    if node.where is not None:
        out[prefix + "where"] = _normalise(node.where.text)
    if node.having is not None:
        out[prefix + "having"] = _normalise(node.having.text)
    if node.qualify is not None:
        out[prefix + "qualify"] = _normalise(node.qualify.text)
    return out


def _collect_grouping(node: Select, prefix: str = "") -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for name in node.cte_order:
        out.update(_collect_grouping(node.ctes[name], prefix + name + "/"))
    if node.group_by:
        out[prefix + "group_by"] = [_normalise(e.text) for e in node.group_by]
    return out


def _normalise(text: str) -> str:
    return " ".join(text.split()).lower()


def diff_sql(model_ref: str, sql_before: str, sql_after: str,
             sha_before: str = "before", sha_after: str = "after") -> SemanticDiff:
    result = SemanticDiff(model_ref=model_ref, sha_before=sha_before, sha_after=sha_after)
    try:
        before = parse(sql_before)
        after = parse(sql_after)
    except (ParseError, IndexError, RecursionError) as exc:
        result.error = str(exc)
        return result

    _diff_joins(before, after, result)
    _diff_projections(before, after, result)
    _diff_filters(before, after, result)
    _diff_grouping(before, after, result)

    if before.distinct != after.distinct:
        result.changes.append(
            Change(
                kind="distinct",
                detail=("DISTINCT added" if after.distinct else "DISTINCT removed")
                       + " — row multiplicity changes",
                before=before.distinct,
                after=after.distinct,
                severity="breaking",
            )
        )
    return result


def _diff_joins(before: Select, after: Select, result: SemanticDiff) -> None:
    a = _collect_joins(before)
    b = _collect_joins(after)

    for key in sorted(set(a) & set(b)):
        old, new = a[key], b[key]
        if old["kind"] != new["kind"]:
            loosened = _ADMISSIVENESS.get(new["kind"], 1) > _ADMISSIVENESS.get(old["kind"], 1)
            result.changes.append(
                Change(
                    kind="join_type",
                    detail=(
                        "join to " + new["target"] + " changed from " + old["kind"]
                        + " to " + new["kind"]
                        + (" — admits unmatched rows with NULL measures"
                           if loosened else " — drops previously matched rows")
                    ),
                    before={"kind": old["kind"], "on": old["on"], "model": old["target"]},
                    after={"kind": new["kind"], "on": new["on"], "model": new["target"]},
                    severity="breaking",
                )
            )
        if old["on"] != new["on"]:
            result.changes.append(
                Change(
                    kind="join_key",
                    detail=(
                        "join key on " + new["target"] + " changed from "
                        + ", ".join(old["on"] or ["<none>"]) + " to "
                        + ", ".join(new["on"] or ["<none>"])
                        + " — row fan-out will change"
                    ),
                    before=old["on"],
                    after=new["on"],
                    severity="breaking",
                )
            )

    for key in sorted(set(b) - set(a)):
        result.changes.append(
            Change(
                kind="join_added",
                detail=b[key]["kind"] + " join to " + b[key]["target"] + " added",
                after=b[key],
                severity="notable",
            )
        )
    for key in sorted(set(a) - set(b)):
        result.changes.append(
            Change(
                kind="join_removed",
                detail=a[key]["kind"] + " join to " + a[key]["target"] + " removed",
                before=a[key],
                severity="notable",
            )
        )


def _diff_projections(before: Select, after: Select, result: SemanticDiff) -> None:
    a = _collect_projections(before)
    b = _collect_projections(after)

    for key in sorted(set(b) - set(a)):
        result.changes.append(
            Change(kind="column_added", detail="column " + b[key]["name"] + " added",
                   after=b[key]["text"], severity="info")
        )
    for key in sorted(set(a) - set(b)):
        result.changes.append(
            Change(kind="column_removed", detail="column " + a[key]["name"] + " removed",
                   before=a[key]["text"], severity="breaking")
        )

    for key in sorted(set(a) & set(b)):
        old, new = a[key], b[key]
        if old["text"] == new["text"]:
            continue
        severity = "notable"
        reason = "expression changed"
        if old["is_aggregate"] != new["is_aggregate"]:
            severity, reason = "breaking", "aggregation added" if new["is_aggregate"] else "aggregation removed"
        elif old["is_cast"] != new["is_cast"]:
            severity, reason = "breaking", "cast added" if new["is_cast"] else "cast removed"
        elif set(old["functions"]) != set(new["functions"]):
            added = sorted(set(new["functions"]) - set(old["functions"]))
            removed = sorted(set(old["functions"]) - set(new["functions"]))
            reason = "functions changed" + (" +" + ",".join(added) if added else "") \
                     + (" -" + ",".join(removed) if removed else "")
            severity = "breaking"
        elif set(old["sources"]) != set(new["sources"]):
            severity, reason = "breaking", "source columns changed"
        result.changes.append(
            Change(
                kind="expression",
                detail="column " + new["name"] + ": " + reason,
                before=old["text"],
                after=new["text"],
                severity=severity,
            )
        )


def _diff_filters(before: Select, after: Select, result: SemanticDiff) -> None:
    a = _collect_filters(before)
    b = _collect_filters(after)
    for key in sorted(set(a) | set(b)):
        old, new = a.get(key), b.get(key)
        if old == new:
            continue
        if old is None:
            detail = key + " predicate added — rows are now excluded"
        elif new is None:
            detail = key + " predicate removed — previously excluded rows now flow through"
        else:
            detail = key + " predicate changed"
        result.changes.append(
            Change(kind="filter", detail=detail, before=old, after=new, severity="breaking")
        )


def _diff_grouping(before: Select, after: Select, result: SemanticDiff) -> None:
    a = _collect_grouping(before)
    b = _collect_grouping(after)
    for key in sorted(set(a) | set(b)):
        old, new = a.get(key), b.get(key)
        if old == new:
            continue
        result.changes.append(
            Change(
                kind="grouping",
                detail=key + " changed — aggregation grain moved",
                before=old,
                after=new,
                severity="breaking",
            )
        )


def expr_signature(expr: Expr) -> str:  # pragma: no cover - helper for tests
    return _normalise(expr.text)
