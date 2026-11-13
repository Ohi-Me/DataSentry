"""The synthetic warehouse.

A 200-table lakehouse with a dbt-shaped DAG: bronze landing tables, silver
staging and intermediate models, gold facts, feature views and dashboards.
Not a star schema — multi-hop, with joins, aggregations and fan-in, because a
star would make lineage look easy and prove nothing.

Two design commitments worth naming:

* **Tables are generative models, not stored rows.** A table knows the
  distribution of each of its columns; a cycle draws a batch from it. The
  profiler consumes that batch and keeps only sketches, so raw values exist
  for the duration of one function call and are never written anywhere. That
  is the production semantic exactly, and it is what makes the PII claim
  demonstrable rather than aspirational.
* **Every table carries the SQL that would have built it.** The lineage
  resolver parses that SQL for real. Faults that change a model rewrite it
  and commit the change, so the semantic diff has two genuine versions to
  compare.
"""

from __future__ import annotations

import hashlib
import math
import random
from dataclasses import dataclass, field
from datetime import datetime

from datasentry_core.enums import AssetKind, Layer

# ---------------------------------------------------------------------------
# Column specifications
# ---------------------------------------------------------------------------


@dataclass
class ColumnSpec:
    name: str
    dtype: str
    kind: str  # id | measure | category | timestamp | text | flag
    null_rate: float = 0.0
    # numeric
    mean: float = 0.0
    sigma: float = 1.0
    lognormal: bool = False
    # categorical
    categories: list[str] = field(default_factory=list)
    weights: list[float] = field(default_factory=list)
    # text
    length_mean: float = 24.0
    length_sigma: float = 6.0
    # id
    distinct_ratio: float = 1.0
    contains_pii: bool = False
    unit: str | None = None

    def clone(self) -> ColumnSpec:
        return ColumnSpec(
            name=self.name, dtype=self.dtype, kind=self.kind, null_rate=self.null_rate,
            mean=self.mean, sigma=self.sigma, lognormal=self.lognormal,
            categories=list(self.categories), weights=list(self.weights),
            length_mean=self.length_mean, length_sigma=self.length_sigma,
            distinct_ratio=self.distinct_ratio, contains_pii=self.contains_pii,
            unit=self.unit,
        )


@dataclass
class TableSpec:
    asset_id: str
    layer: Layer
    kind: AssetKind
    tier: int
    owner_team: str
    columns: list[ColumnSpec]
    upstream: list[str] = field(default_factory=list)
    model_ref: str | None = None
    sql: str = ""
    base_rows: int = 5000
    growth_per_day: float = 0.002
    weekly_dip: float = 0.35  # weekend volume multiplier reduction
    daily_amplitude: float = 0.30
    noise: float = 0.05
    query_count_30d: int = 200
    distinct_consumers_30d: int = 4

    @property
    def name(self) -> str:
        return self.asset_id.split(".")[-1]

    def column(self, name: str) -> ColumnSpec | None:
        return next((c for c in self.columns if c.name == name), None)

    def schema_hash(self) -> str:
        payload = "|".join(c.name + ":" + c.dtype for c in self.columns)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    def monitored_columns(self) -> list[ColumnSpec]:
        """Columns worth profiling. IDs, measures, categories and text.

        Timestamps are covered by the table-level freshness signal, so
        profiling them per column would double the cost for no new signal.
        """
        return [c for c in self.columns if c.kind != "timestamp"]

    def cadence_stride(self) -> int:
        """Cadence tiering from docs/04 section 2.2, in cycles.

        This is what makes 200k columns affordable: compute scales with
        cadence, not with table count.
        """
        if self.tier == 1:
            return 1  # hot
        if self.tier == 2:
            return 3  # warm
        return 8  # cold


TEAMS = ["revenue-eng", "growth-data", "risk-analytics", "ml-platform", "core-ingest", "finance-sys"]

REGIONS = ["EMEA", "AMER", "APAC", "LATAM"]
ORDER_STATUS = ["COMPLETE", "PENDING", "CANCELLED", "REFUNDED"]
CHANNELS = ["web", "ios", "android", "partner", "callcenter"]
CURRENCIES = ["USD", "EUR", "GBP", "JPY"]
SEGMENTS = ["enterprise", "midmarket", "smb", "self_serve"]


def _measure(name: str, mean: float, sigma: float, *, unit: str | None = None,
             lognormal: bool = True, null_rate: float = 0.002) -> ColumnSpec:
    return ColumnSpec(name=name, dtype="NUMBER(18,2)", kind="measure", mean=mean,
                      sigma=sigma, lognormal=lognormal, null_rate=null_rate, unit=unit)


def _category(name: str, categories: list[str], weights: list[float] | None = None) -> ColumnSpec:
    return ColumnSpec(name=name, dtype="VARCHAR(32)", kind="category",
                      categories=list(categories),
                      weights=list(weights) if weights else [1.0] * len(categories),
                      null_rate=0.001)


def _identifier(name: str, ratio: float = 1.0) -> ColumnSpec:
    return ColumnSpec(name=name, dtype="VARCHAR(36)", kind="id", distinct_ratio=ratio)


def _text(name: str, mean_len: float = 24.0, pii: bool = False) -> ColumnSpec:
    return ColumnSpec(name=name, dtype="VARCHAR(256)", kind="text", length_mean=mean_len,
                      length_sigma=max(mean_len * 0.25, 2.0), null_rate=0.01, contains_pii=pii)


def _timestamp(name: str) -> ColumnSpec:
    return ColumnSpec(name=name, dtype="TIMESTAMP_NTZ", kind="timestamp")


# ---------------------------------------------------------------------------
# The DAG
# ---------------------------------------------------------------------------


class World:
    """Catalog, DAG and SQL for the synthetic warehouse."""

    def __init__(self, *, tables: int = 200, seed: int = 20260829,
                 database: str = "ANALYTICS", warehouse: str = "snowflake") -> None:
        self.seed = seed
        self.database = database
        self.warehouse = warehouse
        self.rng = random.Random(seed)
        self.tables: dict[str, TableSpec] = {}
        self._build(tables)

    # -- construction -------------------------------------------------------

    def _aid(self, layer: str, name: str) -> str:
        return self.warehouse[:2] + "." + self.database + "." + layer + "." + name

    def _build(self, target: int) -> None:
        self._build_spine()
        self._build_filler(target)

    def _build_spine(self) -> None:
        """The hand-written core the demo narrative runs through.

        Everything else is generated, but this path — raw orders through
        staging into a revenue fact, a feature view and a dashboard — is
        authored so the story it tells is a real one.
        """
        db = self.database

        raw_orders = TableSpec(
            asset_id=self._aid("raw", "orders"),
            layer=Layer.BRONZE, kind=AssetKind.TABLE, tier=2, owner_team="core-ingest",
            base_rows=42000, query_count_30d=1400, distinct_consumers_30d=11,
            columns=[
                _identifier("order_id"),
                _identifier("customer_id", ratio=0.22),
                _measure("amount_usd", mean=4.05, sigma=0.85, unit="USD"),
                _category("status", ORDER_STATUS, [0.72, 0.14, 0.09, 0.05]),
                _category("channel", CHANNELS, [0.41, 0.23, 0.19, 0.11, 0.06]),
                _category("currency", CURRENCIES, [0.78, 0.12, 0.07, 0.03]),
                _text("ship_note", 34.0),
                _timestamp("created_at"),
            ],
        )
        raw_customers = TableSpec(
            asset_id=self._aid("raw", "customers"),
            layer=Layer.BRONZE, kind=AssetKind.TABLE, tier=2, owner_team="core-ingest",
            base_rows=9200, growth_per_day=0.004, query_count_30d=900, distinct_consumers_30d=8,
            columns=[
                _identifier("customer_id"),
                _category("region_code", REGIONS, [0.34, 0.38, 0.19, 0.09]),
                _category("segment", SEGMENTS, [0.11, 0.24, 0.42, 0.23]),
                _text("company_name", 22.0, pii=True),
                _measure("credit_limit", mean=9.1, sigma=0.6, unit="USD"),
                _timestamp("created_at"),
            ],
        )
        raw_refunds = TableSpec(
            asset_id=self._aid("raw", "refunds"),
            layer=Layer.BRONZE, kind=AssetKind.TABLE, tier=3, owner_team="finance-sys",
            base_rows=2600, query_count_30d=300, distinct_consumers_30d=4,
            columns=[
                _identifier("refund_id"),
                _identifier("order_id", ratio=0.95),
                _measure("refund_amt", mean=3.4, sigma=0.7, unit="USD"),
                _category("reason", ["damaged", "late", "duplicate", "fraud"], [0.4, 0.3, 0.2, 0.1]),
                _timestamp("refunded_at"),
            ],
        )

        stg_orders = TableSpec(
            asset_id=self._aid("silver", "stg_orders"),
            layer=Layer.SILVER, kind=AssetKind.TABLE, tier=1, owner_team="revenue-eng",
            base_rows=41000, query_count_30d=3800, distinct_consumers_30d=19,
            upstream=[raw_orders.asset_id, raw_customers.asset_id],
            model_ref="models/silver/stg_orders.sql",
            columns=[
                _identifier("order_id"),
                _identifier("customer_id", ratio=0.22),
                _measure("amount_usd", mean=4.05, sigma=0.85, unit="USD"),
                _category("status_norm", ORDER_STATUS, [0.72, 0.14, 0.09, 0.05]),
                _category("region_code", REGIONS, [0.34, 0.38, 0.19, 0.09]),
                _category("channel", CHANNELS, [0.41, 0.23, 0.19, 0.11, 0.06]),
                _timestamp("created_at"),
            ],
        )
        stg_orders.sql = (
            "select\n"
            "    o.order_id,\n"
            "    o.customer_id,\n"
            "    cast(o.amount_usd as decimal(18,2)) as amount_usd,\n"
            "    upper(o.status) as status_norm,\n"
            "    c.region_code,\n"
            "    o.channel,\n"
            "    o.created_at\n"
            "from " + db + ".raw.orders o\n"
            "inner join " + db + ".raw.customers c\n"
            "    on o.customer_id = c.customer_id\n"
            "where o.status is not null\n"
        )

        int_order_econ = TableSpec(
            asset_id=self._aid("silver", "int_order_economics"),
            layer=Layer.SILVER, kind=AssetKind.TABLE, tier=1, owner_team="revenue-eng",
            base_rows=41000, query_count_30d=2100, distinct_consumers_30d=13,
            upstream=[stg_orders.asset_id, raw_refunds.asset_id],
            model_ref="models/silver/int_order_economics.sql",
            columns=[
                _identifier("order_id"),
                _identifier("customer_id", ratio=0.22),
                _measure("gross_revenue_usd", mean=4.05, sigma=0.85, unit="USD"),
                _measure("refund_usd", mean=2.1, sigma=0.9, unit="USD", null_rate=0.62),
                _measure("net_revenue_usd", mean=4.0, sigma=0.86, unit="USD"),
                _category("region_code", REGIONS, [0.34, 0.38, 0.19, 0.09]),
                _category("status_norm", ORDER_STATUS, [0.72, 0.14, 0.09, 0.05]),
            ],
        )
        int_order_econ.sql = (
            "select\n"
            "    s.order_id,\n"
            "    s.customer_id,\n"
            "    s.amount_usd as gross_revenue_usd,\n"
            "    r.refund_amt as refund_usd,\n"
            "    s.amount_usd - coalesce(r.refund_amt, 0) as net_revenue_usd,\n"
            "    s.region_code,\n"
            "    s.status_norm\n"
            "from " + db + ".silver.stg_orders s\n"
            "left join " + db + ".raw.refunds r\n"
            "    on s.order_id = r.order_id\n"
        )

        fct_orders = TableSpec(
            asset_id=self._aid("gold", "fct_orders"),
            layer=Layer.GOLD, kind=AssetKind.TABLE, tier=1, owner_team="revenue-eng",
            base_rows=38000, query_count_30d=8800, distinct_consumers_30d=41,
            upstream=[int_order_econ.asset_id],
            model_ref="models/gold/fct_orders.sql",
            columns=[
                _identifier("order_id"),
                _identifier("customer_id", ratio=0.22),
                _measure("revenue_usd", mean=4.0, sigma=0.86, unit="USD"),
                _measure("refund_usd", mean=2.1, sigma=0.9, unit="USD", null_rate=0.62),
                _category("region_code", REGIONS, [0.34, 0.38, 0.19, 0.09]),
                _category("status_norm", ORDER_STATUS, [0.72, 0.14, 0.09, 0.05]),
            ],
        )
        fct_orders.sql = (
            "select\n"
            "    e.order_id,\n"
            "    e.customer_id,\n"
            "    e.net_revenue_usd as revenue_usd,\n"
            "    e.refund_usd,\n"
            "    e.region_code,\n"
            "    e.status_norm\n"
            "from " + db + ".silver.int_order_economics e\n"
            "where e.status_norm <> 'CANCELLED'\n"
        )

        agg_revenue = TableSpec(
            asset_id=self._aid("gold", "fct_orders_daily"),
            layer=Layer.GOLD, kind=AssetKind.TABLE, tier=1, owner_team="revenue-eng",
            base_rows=1200, query_count_30d=6400, distinct_consumers_30d=33,
            upstream=[fct_orders.asset_id],
            model_ref="models/gold/fct_orders_daily.sql",
            columns=[
                _category("region_code", REGIONS, [0.34, 0.38, 0.19, 0.09]),
                _measure("revenue_usd", mean=8.6, sigma=0.5, unit="USD"),
                _measure("order_count", mean=6.2, sigma=0.4, lognormal=True),
            ],
        )
        agg_revenue.sql = (
            "select\n"
            "    f.region_code,\n"
            "    sum(f.revenue_usd) as revenue_usd,\n"
            "    count(*) as order_count\n"
            "from " + db + ".gold.fct_orders f\n"
            "group by f.region_code\n"
        )

        feature_view = TableSpec(
            asset_id=self._aid("gold", "ml_features_customer_rev_30d"),
            layer=Layer.GOLD, kind=AssetKind.FEATURE_VIEW, tier=1, owner_team="ml-platform",
            base_rows=9200, query_count_30d=9600, distinct_consumers_30d=52,
            upstream=[fct_orders.asset_id],
            model_ref="models/gold/ml_features_customer_rev_30d.sql",
            columns=[
                _identifier("customer_id"),
                _measure("rev_30d", mean=5.4, sigma=0.9, unit="USD"),
                _measure("order_count_30d", mean=1.6, sigma=0.5),
                _measure("avg_order_value", mean=4.0, sigma=0.7, unit="USD"),
            ],
        )
        feature_view.sql = (
            "select\n"
            "    f.customer_id,\n"
            "    sum(f.revenue_usd) as rev_30d,\n"
            "    count(f.order_id) as order_count_30d,\n"
            "    avg(f.revenue_usd) as avg_order_value\n"
            "from " + db + ".gold.fct_orders f\n"
            "group by f.customer_id\n"
        )

        dashboard = TableSpec(
            asset_id=self._aid("gold", "dash_emea_revenue"),
            layer=Layer.GOLD, kind=AssetKind.DASHBOARD, tier=1, owner_team="finance-sys",
            base_rows=400, query_count_30d=5200, distinct_consumers_30d=88,
            upstream=[agg_revenue.asset_id],
            model_ref="models/gold/dash_emea_revenue.sql",
            columns=[
                _category("region_code", ["EMEA"], [1.0]),
                _measure("revenue_usd", mean=9.0, sigma=0.4, unit="USD"),
            ],
        )
        dashboard.sql = (
            "select d.region_code, d.revenue_usd\n"
            "from " + db + ".gold.fct_orders_daily d\n"
            "where d.region_code = 'EMEA'\n"
        )

        churn_model = TableSpec(
            asset_id=self._aid("gold", "ml_model_churn_scores"),
            layer=Layer.GOLD, kind=AssetKind.ML_MODEL, tier=1, owner_team="ml-platform",
            base_rows=9200, query_count_30d=3100, distinct_consumers_30d=17,
            upstream=[feature_view.asset_id],
            model_ref="models/gold/ml_model_churn_scores.sql",
            columns=[
                _identifier("customer_id"),
                _measure("churn_score", mean=-1.2, sigma=0.4, lognormal=False),
            ],
        )
        churn_model.sql = (
            "select m.customer_id, m.rev_30d / nullif(m.order_count_30d, 0) as churn_score\n"
            "from " + db + ".gold.ml_features_customer_rev_30d m\n"
        )

        for spec in [raw_orders, raw_customers, raw_refunds, stg_orders, int_order_econ,
                     fct_orders, agg_revenue, feature_view, dashboard, churn_model]:
            self.tables[spec.asset_id] = spec

        self.spine = [s.asset_id for s in [raw_orders, stg_orders, int_order_econ, fct_orders,
                                           agg_revenue, feature_view, dashboard, churn_model]]

    def _build_filler(self, target: int) -> None:
        """Generate the rest of the warehouse around the spine.

        Proportions follow the delivery plan: roughly 60 bronze, 90 silver,
        50 gold, with each silver model drawing from one or two bronze tables
        and each gold model from one or two silver ones. The result has real
        fan-in, which is what makes correlation non-trivial.
        """
        rng = self.rng
        db = self.database
        remaining = max(0, target - len(self.tables))
        n_bronze = int(remaining * 0.30)
        n_silver = int(remaining * 0.45)
        n_gold = remaining - n_bronze - n_silver

        domains = ["billing", "usage", "support", "inventory", "marketing", "shipping",
                   "identity", "catalog", "pricing", "telemetry"]

        bronze_ids: list[str] = []
        for i in range(n_bronze):
            domain = domains[i % len(domains)]
            name = "src_" + domain + "_" + str(i // len(domains) + 1).zfill(2)
            spec = TableSpec(
                asset_id=self._aid("raw", name),
                layer=Layer.BRONZE, kind=AssetKind.TABLE, tier=3,
                owner_team=rng.choice(TEAMS),
                base_rows=rng.randint(1200, 60000),
                growth_per_day=rng.uniform(0.0, 0.004),
                daily_amplitude=rng.uniform(0.15, 0.45),
                weekly_dip=rng.uniform(0.15, 0.45),
                noise=rng.uniform(0.03, 0.09),
                query_count_30d=rng.randint(20, 600),
                distinct_consumers_30d=rng.randint(1, 7),
                columns=self._random_columns(rng, domain, entity=True),
            )
            self.tables[spec.asset_id] = spec
            bronze_ids.append(spec.asset_id)

        silver_ids: list[str] = []
        for i in range(n_silver):
            domain = domains[i % len(domains)]
            name = "stg_" + domain + "_" + str(i // len(domains) + 1).zfill(2)
            parents = rng.sample(bronze_ids, k=min(len(bronze_ids), rng.choice([1, 1, 2])))
            tier = 2 if rng.random() < 0.35 else 3
            spec = TableSpec(
                asset_id=self._aid("silver", name),
                layer=Layer.SILVER, kind=AssetKind.TABLE, tier=tier,
                owner_team=rng.choice(TEAMS),
                base_rows=rng.randint(900, 45000),
                growth_per_day=rng.uniform(0.0, 0.003),
                noise=rng.uniform(0.03, 0.08),
                query_count_30d=rng.randint(60, 2200),
                distinct_consumers_30d=rng.randint(2, 15),
                upstream=parents,
                model_ref="models/silver/" + name + ".sql",
                columns=self._derived_columns(rng, parents, gold=False),
            )
            spec.sql = self._render_sql(spec, parents, db, rng)
            self.tables[spec.asset_id] = spec
            silver_ids.append(spec.asset_id)

        for i in range(n_gold):
            domain = domains[i % len(domains)]
            name = "fct_" + domain + "_" + str(i // len(domains) + 1).zfill(2)
            parents = rng.sample(silver_ids, k=min(len(silver_ids), rng.choice([1, 1, 2]))) if silver_ids else []
            kind = AssetKind.TABLE
            roll = rng.random()
            if roll > 0.86:
                kind, name = AssetKind.FEATURE_VIEW, "ml_features_" + domain + "_" + str(i).zfill(2)
            elif roll > 0.72:
                kind, name = AssetKind.DASHBOARD, "dash_" + domain + "_" + str(i).zfill(2)
            elif roll > 0.66:
                kind, name = AssetKind.REPORT, "rpt_" + domain + "_" + str(i).zfill(2)
            spec = TableSpec(
                asset_id=self._aid("gold", name),
                layer=Layer.GOLD, kind=kind, tier=1 if rng.random() < 0.4 else 2,
                owner_team=rng.choice(TEAMS),
                base_rows=rng.randint(400, 20000),
                noise=rng.uniform(0.02, 0.07),
                query_count_30d=rng.randint(300, 9000),
                distinct_consumers_30d=rng.randint(5, 70),
                upstream=parents,
                model_ref="models/gold/" + name + ".sql",
                columns=self._derived_columns(rng, parents, gold=True),
            )
            spec.sql = self._render_sql(spec, parents, db, rng)
            self.tables[spec.asset_id] = spec

    def _derived_columns(self, rng: random.Random, parents: list[str],
                         *, gold: bool) -> list[ColumnSpec]:
        """Build a model column set out of the columns its parents provide.

        Real transformation models mostly select, rename and aggregate what
        their sources give them. Generating a child column set independently
        and projecting "null as x" for everything the parent lacks produces a
        DAG that is structurally connected but carries almost no column-level
        lineage -- which would make blast radius look broken when the fault is
        in the fixture rather than in the resolver.
        """
        available: list[ColumnSpec] = []
        for parent in parents:
            parent_spec = self.tables.get(parent)
            if parent_spec is None:
                continue
            for column in parent_spec.columns:
                if column.kind == "timestamp":
                    continue
                if not any(c.name == column.name for c in available):
                    available.append(column)
        if not available:
            return self._random_columns(rng, "generic", entity=False, gold=gold)

        keep = rng.sample(available, k=min(len(available), rng.randint(3, 6)))
        columns = [c.clone() for c in keep]

        # One derived measure, so a model does something rather than only
        # forwarding. Aggregates are the transform type worth exercising.
        measures = [c for c in available if c.kind == "measure"]
        if measures and gold:
            source = rng.choice(measures)
            derived = source.clone()
            derived.name = ("total_" if rng.random() < 0.5 else "avg_") + source.name
            columns.append(derived)

        if not gold:
            columns.append(_timestamp("updated_at"))
        return columns

    def _random_columns(self, rng: random.Random, domain: str, *, entity: bool,
                        gold: bool = False) -> list[ColumnSpec]:
        cols: list[ColumnSpec] = [_identifier(domain + "_id")]
        cols.append(_identifier("customer_id", ratio=rng.uniform(0.12, 0.4)))
        for i in range(rng.randint(1, 3)):
            cols.append(
                _measure(
                    ["amount", "quantity", "duration_sec", "score", "cost_usd"][i % 5],
                    mean=rng.uniform(2.0, 7.0), sigma=rng.uniform(0.4, 1.1),
                    unit="USD" if i % 5 in (0, 4) else None,
                    null_rate=rng.uniform(0.0, 0.03),
                )
            )
        cat_pool = [
            ("status", ORDER_STATUS), ("region_code", REGIONS), ("channel", CHANNELS),
            ("segment", SEGMENTS), ("currency", CURRENCIES),
        ]
        for name, values in rng.sample(cat_pool, k=rng.randint(1, 2)):
            weights = [rng.uniform(0.5, 3.0) for _ in values]
            cols.append(_category(name, values, weights))
        if entity and rng.random() < 0.5:
            cols.append(_text("description", rng.uniform(18.0, 60.0)))
        if not gold:
            cols.append(_timestamp("updated_at"))
        return cols

    def _render_sql(self, spec: TableSpec, parents: list[str], db: str,
                    rng: random.Random) -> str:
        if not parents:
            return "select 1 as placeholder\n"
        primary = parents[0]
        p_spec = self.tables[primary]
        alias = "a"
        lines = ["select"]
        projections = []
        secondary = self.tables.get(parents[1]) if len(parents) > 1 else None
        for col in spec.columns:
            source = p_spec.column(col.name)
            if source is not None:
                projections.append("    " + alias + "." + col.name)
            elif secondary is not None and secondary.column(col.name) is not None:
                projections.append("    b." + col.name)
            elif col.name.startswith(("total_", "avg_")):
                base = col.name.split("_", 1)[1]
                fn = "sum" if col.name.startswith("total_") else "avg"
                if p_spec.column(base) is not None:
                    projections.append("    " + fn + "(" + alias + "." + base + ") as " + col.name)
                elif secondary is not None and secondary.column(base) is not None:
                    projections.append("    " + fn + "(b." + base + ") as " + col.name)
                else:
                    projections.append("    null as " + col.name)
            elif col.kind == "measure" and p_spec.columns:
                candidate = next((c for c in p_spec.columns if c.kind == "measure"), None)
                if candidate:
                    projections.append(
                        "    sum(" + alias + "." + candidate.name + ") as " + col.name
                    )
                else:
                    projections.append("    null as " + col.name)
            else:
                projections.append("    null as " + col.name)
        lines.append(",\n".join(projections))
        lines.append("from " + self._short(primary) + " " + alias)
        if len(parents) > 1:
            secondary = parents[1]
            s_spec = self.tables[secondary]
            shared = next(
                (c.name for c in p_spec.columns
                 if s_spec.column(c.name) is not None and c.kind == "id"),
                None,
            )
            if shared:
                join_kind = "inner join" if rng.random() < 0.7 else "left join"
                lines.append(
                    join_kind + " " + self._short(secondary) + " b on "
                    + alias + "." + shared + " = b." + shared
                )
        if any("sum(" in p for p in projections):
            keys = [
                "    " + alias + "." + c.name
                for c in spec.columns
                if c.kind in {"id", "category"} and p_spec.column(c.name) is not None
            ]
            if keys:
                lines.append("group by\n" + ",\n".join(keys))
        return "\n".join(lines) + "\n"

    def _short(self, asset_id: str) -> str:
        parts = asset_id.split(".")
        return ".".join(parts[-3:])

    # -- accessors ----------------------------------------------------------

    def catalog_columns(self) -> dict[str, list[str]]:
        return {aid: [c.name for c in spec.columns] for aid, spec in self.tables.items()}

    def models(self) -> list[TableSpec]:
        return [t for t in self.tables.values() if t.model_ref and t.sql]

    def by_layer(self, layer: Layer) -> list[TableSpec]:
        return [t for t in self.tables.values() if t.layer == layer]

    def stats(self) -> dict[str, int]:
        return {
            "tables": len(self.tables),
            "columns": sum(len(t.columns) for t in self.tables.values()),
            "models": len(self.models()),
            "bronze": len(self.by_layer(Layer.BRONZE)),
            "silver": len(self.by_layer(Layer.SILVER)),
            "gold": len(self.by_layer(Layer.GOLD)),
        }


# ---------------------------------------------------------------------------
# Volume model
# ---------------------------------------------------------------------------


def expected_rows(spec: TableSpec, moment: datetime, origin: datetime) -> float:
    """Seasonal volume: a daily cycle, a weekday/weekend cycle and slow growth.

    The detectors have to learn this shape and then not fire on it. A harness
    whose volume is flat would make D1 look far better than it is.
    """
    days = (moment - origin).total_seconds() / 86400.0
    trend = spec.base_rows * (1.0 + spec.growth_per_day) ** days
    hour = moment.hour + moment.minute / 60.0
    daily = 1.0 + spec.daily_amplitude * math.sin((hour - 9.0) / 24.0 * 2 * math.pi)
    weekend = 1.0 - (spec.weekly_dip if moment.weekday() >= 5 else 0.0)
    return max(trend * daily * weekend, 1.0)
