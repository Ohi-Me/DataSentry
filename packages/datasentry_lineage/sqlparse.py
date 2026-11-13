"""A SQL reader sized for lineage, not for execution.

The delivery plan flags this as the highest-risk week, and the honest scope
cut it recommends is exactly the one taken here: support the dialect subset
the transformation layer actually emits, resolve it precisely, and *report
parse coverage as a metric* rather than claiming universal support. Anything
the reader cannot resolve becomes an ``inferred`` edge from observed access,
never a silent gap.

The subset covered, which is what a dbt-style DAG is made of:

    WITH ctes (nested, chained)          JOIN ... ON / USING, all join kinds
    SELECT projections with aliases      GROUP BY / HAVING / QUALIFY
    SELECT *, SELECT alias.*             UNION / UNION ALL / EXCEPT / INTERSECT
    subqueries in FROM                   CAST, ::, CASE, window functions
    quoted and dotted identifiers        aggregate and scalar functions

Expressions are read to the depth lineage needs and no further: which source
columns feed a projection, and what kind of transform sits between them. A
full expression tree would be more code and would not add an edge.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

KEYWORDS = {
    "SELECT", "FROM", "WHERE", "GROUP", "BY", "HAVING", "ORDER", "LIMIT", "OFFSET",
    "JOIN", "INNER", "LEFT", "RIGHT", "FULL", "OUTER", "CROSS", "LATERAL", "ON",
    "USING", "WITH", "AS", "UNION", "ALL", "EXCEPT", "INTERSECT", "DISTINCT",
    "CASE", "WHEN", "THEN", "ELSE", "END", "AND", "OR", "NOT", "IN", "EXISTS",
    "QUALIFY", "OVER", "PARTITION", "CAST", "NULL", "IS", "BETWEEN", "LIKE",
    "ASC", "DESC", "NULLS", "FIRST", "LAST", "INTO", "VALUES", "INSERT", "CREATE",
    "TABLE", "VIEW", "REPLACE", "IF", "COALESCE",
}

AGGREGATES = {
    "SUM", "COUNT", "AVG", "MIN", "MAX", "MEDIAN", "STDDEV", "VARIANCE",
    "ARRAY_AGG", "STRING_AGG", "LISTAGG", "APPROX_COUNT_DISTINCT", "ANY_VALUE",
    "PERCENTILE_CONT", "PERCENTILE_DISC", "CORR", "COVAR_POP",
}

CLAUSE_STOPS = {
    "FROM", "WHERE", "GROUP", "HAVING", "ORDER", "LIMIT", "QUALIFY", "WINDOW",
    "UNION", "EXCEPT", "INTERSECT", "JOIN", "INNER", "LEFT", "RIGHT", "FULL",
    "CROSS", "ON", "USING", "OFFSET",
}

_TOKEN_RE = re.compile(
    r"""
      (?P<ws>\s+)
    | (?P<comment>--[^\n]*|/\*.*?\*/)
    | (?P<string>'(?:[^']|'')*')
    | (?P<quoted>"(?:[^"]|"")*"|`[^`]*`|\[[^\]]*\])
    | (?P<number>\d+\.\d+|\.\d+|\d+)
    | (?P<op>::|<=|>=|<>|!=|\|\||[-+*/%(),.;=<>])
    | (?P<name>[A-Za-z_][A-Za-z0-9_$]*)
    """,
    re.VERBOSE | re.DOTALL,
)


@dataclass(frozen=True)
class Token:
    kind: str
    text: str
    pos: int

    @property
    def upper(self) -> str:
        return self.text.upper()

    def is_kw(self, *words: str) -> bool:
        return self.kind == "name" and self.upper in words


def tokenize(sql: str) -> list[Token]:
    out: list[Token] = []
    i = 0
    n = len(sql)
    while i < n:
        m = _TOKEN_RE.match(sql, i)
        if not m:
            i += 1
            continue
        i = m.end()
        kind = m.lastgroup or "op"
        if kind in {"ws", "comment"}:
            continue
        out.append(Token(kind, m.group(), m.start()))
    return out


# ---------------------------------------------------------------------------
# AST
# ---------------------------------------------------------------------------


@dataclass
class ColumnRef:
    qualifier: str | None
    name: str

    def __str__(self) -> str:
        return (self.qualifier + "." + self.name) if self.qualifier else self.name


@dataclass
class Expr:
    text: str
    columns: list[ColumnRef] = field(default_factory=list)
    functions: list[str] = field(default_factory=list)
    has_star: bool = False
    star_qualifier: str | None = None
    is_cast: bool = False
    is_window: bool = False

    @property
    def is_aggregate(self) -> bool:
        return any(f in AGGREGATES for f in self.functions)

    @property
    def is_bare_column(self) -> bool:
        return len(self.columns) == 1 and not self.functions and not self.is_cast


@dataclass
class Projection:
    expr: Expr
    alias: str | None = None

    def output_name(self) -> str | None:
        if self.alias:
            return self.alias
        if self.expr.is_bare_column:
            return self.expr.columns[0].name
        if len(self.expr.columns) == 1 and self.expr.is_cast:
            return self.expr.columns[0].name
        return None


@dataclass
class TableRef:
    name: str | None = None
    subquery: Select | None = None
    alias: str | None = None

    @property
    def label(self) -> str:
        return self.alias or (self.name or "subquery")


@dataclass
class Join:
    kind: str  # INNER | LEFT | RIGHT | FULL | CROSS
    table: TableRef
    on: Expr | None = None
    using: list[str] = field(default_factory=list)

    def key_columns(self) -> list[ColumnRef]:
        if self.using:
            return [ColumnRef(None, c) for c in self.using]
        return list(self.on.columns) if self.on else []


@dataclass
class Select:
    projections: list[Projection] = field(default_factory=list)
    from_: TableRef | None = None
    joins: list[Join] = field(default_factory=list)
    ctes: dict[str, Select] = field(default_factory=dict)
    cte_order: list[str] = field(default_factory=list)
    where: Expr | None = None
    group_by: list[Expr] = field(default_factory=list)
    having: Expr | None = None
    qualify: Expr | None = None
    distinct: bool = False
    set_operations: list[tuple[str, Select]] = field(default_factory=list)

    def sources(self) -> list[TableRef]:
        out = [self.from_] if self.from_ else []
        out.extend(j.table for j in self.joins)
        return out


class ParseError(Exception):
    pass


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


class Parser:
    def __init__(self, sql: str) -> None:
        self.tokens = tokenize(sql)
        self.i = 0

    # -- token helpers ------------------------------------------------------

    def peek(self, offset: int = 0) -> Token | None:
        idx = self.i + offset
        return self.tokens[idx] if 0 <= idx < len(self.tokens) else None

    def next(self) -> Token | None:
        tok = self.peek()
        if tok is not None:
            self.i += 1
        return tok

    def at_kw(self, *words: str) -> bool:
        tok = self.peek()
        return tok is not None and tok.is_kw(*words)

    def at_op(self, *ops: str) -> bool:
        tok = self.peek()
        return tok is not None and tok.kind == "op" and tok.text in ops

    def expect_op(self, op: str) -> None:
        if not self.at_op(op):
            tok = self.peek()
            raise ParseError("expected " + op + " near " + (tok.text if tok else "<eof>"))
        self.next()

    def eat_kw(self, *words: str) -> bool:
        if self.at_kw(*words):
            self.next()
            return True
        return False

    # -- entry --------------------------------------------------------------

    def parse(self) -> Select:
        # Tolerate a CREATE ... AS wrapper so a model file parses as-is.
        if self.at_kw("CREATE"):
            while self.peek() and not self.at_kw("SELECT", "WITH"):
                self.next()
        stmt = self.parse_query()
        return stmt

    def parse_query(self) -> Select:
        ctes: dict[str, Select] = {}
        order: list[str] = []
        if self.eat_kw("WITH"):
            self.eat_kw("RECURSIVE")
            while True:
                name_tok = self.next()
                if name_tok is None:
                    raise ParseError("truncated WITH clause")
                name = unquote(name_tok.text)
                # Optional column list: WITH t (a, b) AS (...)
                if self.at_op("("):
                    save = self.i
                    self.skip_parens()
                    if not self.at_kw("AS"):
                        self.i = save
                if not self.eat_kw("AS"):
                    raise ParseError("expected AS in WITH clause for " + name)
                self.eat_kw("MATERIALIZED", "NOT")
                self.expect_op("(")
                ctes[name] = self.parse_query()
                self.expect_op(")")
                order.append(name)
                if self.at_op(","):
                    self.next()
                    continue
                break

        select = self.parse_select_core()
        select.ctes = ctes
        select.cte_order = order

        while self.at_kw("UNION", "EXCEPT", "INTERSECT"):
            op = self.next().upper  # type: ignore[union-attr]
            if self.eat_kw("ALL"):
                op += " ALL"
            self.eat_kw("DISTINCT")
            wrapped = False
            if self.at_op("("):
                self.next()
                wrapped = True
            right = self.parse_select_core()
            if wrapped:
                self.expect_op(")")
            select.set_operations.append((op, right))
        return select

    def parse_select_core(self) -> Select:
        if not self.eat_kw("SELECT"):
            tok = self.peek()
            raise ParseError("expected SELECT near " + (tok.text if tok else "<eof>"))
        node = Select()
        node.distinct = self.eat_kw("DISTINCT")
        self.eat_kw("ALL")
        node.projections = self.parse_projections()

        if self.eat_kw("FROM"):
            node.from_ = self.parse_table_ref()
            node.joins = self.parse_joins()

        if self.eat_kw("WHERE"):
            node.where = self.parse_expr()
        if self.at_kw("GROUP"):
            self.next()
            self.eat_kw("BY")
            node.group_by = self.parse_expr_list()
        if self.eat_kw("HAVING"):
            node.having = self.parse_expr()
        if self.eat_kw("QUALIFY"):
            node.qualify = self.parse_expr()
        if self.at_kw("ORDER"):
            self.next()
            self.eat_kw("BY")
            self.parse_expr_list()
        while self.at_kw("LIMIT", "OFFSET"):
            self.next()
            self.next()
        return node

    # -- projections --------------------------------------------------------

    def parse_projections(self) -> list[Projection]:
        out: list[Projection] = []
        while True:
            expr = self.parse_expr(stop_at_comma=True)
            alias = None
            if self.eat_kw("AS"):
                tok = self.next()
                alias = unquote(tok.text) if tok else None
            elif (
                (tok := self.peek()) is not None
                and tok.kind in {"name", "quoted"}
                and not tok.is_kw(*CLAUSE_STOPS)
                and not tok.is_kw("AS")
            ):
                self.next()
                alias = unquote(tok.text)
            out.append(Projection(expr=expr, alias=alias))
            if self.at_op(","):
                self.next()
                continue
            break
        return out

    # -- FROM and JOIN ------------------------------------------------------

    def parse_table_ref(self) -> TableRef:
        self.eat_kw("LATERAL")
        if self.at_op("("):
            self.next()
            sub = self.parse_query()
            self.expect_op(")")
            ref = TableRef(subquery=sub)
        else:
            parts: list[str] = []
            tok = self.next()
            if tok is None:
                raise ParseError("truncated FROM clause")
            parts.append(unquote(tok.text))
            while self.at_op("."):
                self.next()
                nxt = self.next()
                if nxt is None:
                    break
                parts.append(unquote(nxt.text))
            name = ".".join(parts)
            # Table function call, e.g. TABLE(FLATTEN(...)) — skip the args.
            if self.at_op("("):
                self.skip_parens()
            ref = TableRef(name=name)

        if self.eat_kw("AS"):
            tok = self.next()
            ref.alias = unquote(tok.text) if tok else None
        elif (
            (tok := self.peek()) is not None
            and tok.kind in {"name", "quoted"}
            and not tok.is_kw(*CLAUSE_STOPS)
            and not tok.is_kw("AS", "UNION", "EXCEPT", "INTERSECT")
        ):
            self.next()
            ref.alias = unquote(tok.text)
        return ref

    def parse_joins(self) -> list[Join]:
        joins: list[Join] = []
        while True:
            kind = None
            save = self.i
            if self.at_kw("CROSS"):
                self.next()
                if not self.eat_kw("JOIN"):
                    self.i = save
                    break
                kind = "CROSS"
            elif self.at_kw("INNER", "LEFT", "RIGHT", "FULL"):
                word = self.next().upper  # type: ignore[union-attr]
                self.eat_kw("OUTER")
                if not self.eat_kw("JOIN"):
                    self.i = save
                    break
                kind = word
            elif self.at_kw("JOIN"):
                self.next()
                kind = "INNER"
            else:
                break

            table = self.parse_table_ref()
            join = Join(kind=kind, table=table)
            if self.eat_kw("ON"):
                join.on = self.parse_expr()
            elif self.eat_kw("USING"):
                self.expect_op("(")
                cols = []
                while not self.at_op(")"):
                    tok = self.next()
                    if tok is None:
                        break
                    if tok.kind in {"name", "quoted"}:
                        cols.append(unquote(tok.text))
                self.expect_op(")")
                join.using = cols
            joins.append(join)
        return joins

    # -- expressions --------------------------------------------------------

    def parse_expr_list(self) -> list[Expr]:
        out = [self.parse_expr(stop_at_comma=True)]
        while self.at_op(","):
            self.next()
            out.append(self.parse_expr(stop_at_comma=True))
        return out

    def parse_expr(self, *, stop_at_comma: bool = False) -> Expr:
        """Read a span of tokens and harvest the column references inside it.

        Deliberately not a precedence parser. Lineage cares which columns feed
        an output and what shape of transform sits between them; reconstructing
        operator precedence would add a parser and no edges.
        """
        start = self.i
        depth = 0
        columns: list[ColumnRef] = []
        functions: list[str] = []
        has_star = False
        star_qualifier: str | None = None
        is_cast = False
        is_window = False
        case_depth = 0

        while (tok := self.peek()) is not None:
            if tok.kind == "op":
                if tok.text == "(":
                    depth += 1
                    self.next()
                    continue
                if tok.text == ")":
                    if depth == 0:
                        break
                    depth -= 1
                    self.next()
                    continue
                if tok.text == "," and depth == 0 and stop_at_comma:
                    break
                if tok.text == ";":
                    break
                if tok.text == "::":
                    is_cast = True
                    self.next()
                    self.next()  # the type name
                    continue
                if tok.text == "*" and depth == 0:
                    prev = self.peek(-1)
                    prev2 = self.peek(-2)
                    # A bare ``*`` opening the expression is SELECT *; one
                    # following an identifier and a dot is ``alias.*``.
                    # Anything else is multiplication.
                    if self.i == start or prev is None or (
                        prev.kind == "op" and prev.text in {",", "("}
                    ):
                        has_star = True
                        self.next()
                        continue
                    if prev.kind == "op" and prev.text == "." and prev2 is not None:
                        has_star = True
                        star_qualifier = unquote(prev2.text)
                        if columns and columns[-1].name == unquote(prev2.text):
                            columns.pop()
                        self.next()
                        continue
                self.next()
                continue

            if tok.kind == "name":
                upper = tok.upper
                if depth == 0 and case_depth == 0 and upper in CLAUSE_STOPS:
                    break
                if depth == 0 and case_depth == 0 and upper == "AS":
                    break
                if upper == "CASE":
                    case_depth += 1
                    self.next()
                    continue
                if upper == "END":
                    case_depth = max(0, case_depth - 1)
                    self.next()
                    continue
                if upper == "CAST":
                    is_cast = True
                if upper == "OVER":
                    is_window = True
                nxt = self.peek(1)
                if nxt is not None and nxt.kind == "op" and nxt.text == "(":
                    functions.append(upper)
                    self.next()
                    continue
                if upper in KEYWORDS and upper not in {"NULL"}:
                    self.next()
                    continue
                ref = self.read_column_ref()
                if ref is not None:
                    columns.append(ref)
                continue

            if tok.kind == "quoted":
                ref = self.read_column_ref()
                if ref is not None:
                    columns.append(ref)
                continue

            self.next()

        text = " ".join(t.text for t in self.tokens[start : self.i]).strip()
        deduped: list[ColumnRef] = []
        seen = set()
        for c in columns:
            key = (c.qualifier, c.name.lower())
            if key not in seen:
                seen.add(key)
                deduped.append(c)
        return Expr(
            text=text,
            columns=deduped,
            functions=functions,
            has_star=has_star,
            star_qualifier=star_qualifier,
            is_cast=is_cast,
            is_window=is_window,
        )

    def read_column_ref(self) -> ColumnRef | None:
        tok = self.next()
        if tok is None:
            return None
        parts = [unquote(tok.text)]
        while self.at_op("."):
            nxt = self.peek(1)
            if nxt is None:
                break
            if nxt.kind == "op" and nxt.text == "*":
                break
            self.next()
            part = self.next()
            if part is None:
                break
            parts.append(unquote(part.text))
        if len(parts) == 1:
            return ColumnRef(None, parts[0])
        return ColumnRef(parts[-2], parts[-1])

    def skip_parens(self) -> None:
        if not self.at_op("("):
            return
        depth = 0
        while (tok := self.peek()) is not None:
            if tok.kind == "op" and tok.text == "(":
                depth += 1
            elif tok.kind == "op" and tok.text == ")":
                depth -= 1
                self.next()
                if depth == 0:
                    return
                continue
            self.next()


def unquote(text: str) -> str:
    if not text:
        return text
    if text[0] == '"' and text[-1] == '"':
        return text[1:-1].replace('""', '"')
    if text[0] == "`" and text[-1] == "`":
        return text[1:-1]
    if text[0] == "[" and text[-1] == "]":
        return text[1:-1]
    return text


def parse(sql: str) -> Select:
    return Parser(sql).parse()
