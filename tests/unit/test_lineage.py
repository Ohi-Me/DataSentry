"""Column-level lineage: parsing, resolution and the semantic diff.

The fixture DAG here is the ground truth. Column mappings are hand-labelled,
so parse accuracy is measured against something independent of the parser --
which is the only way the coverage number means anything.
"""

from __future__ import annotations

from datasentry_core.enums import TransformType
from datasentry_core.models import LineageEdge
from datasentry_lineage import Catalog, LineageIndex, diff_sql, parse, resolve_model

CATALOG = Catalog({
    "wh.db.raw.orders": ["order_id", "customer_id", "amount_usd", "status", "created_at"],
    "wh.db.raw.customers": ["customer_id", "region_code", "segment"],
    "wh.db.raw.refunds": ["refund_id", "order_id", "refund_amt"],
})

MODEL_SQL = """
with base as (
  select o.order_id, o.customer_id, o.amount_usd, o.status, o.created_at
  from raw.orders o
  where o.created_at >= current_date - 30
),
enriched as (
  select b.order_id,
         b.customer_id,
         cast(b.amount_usd as decimal(18,2)) as amount_usd,
         c.region_code,
         upper(b.status) as status_norm,
         row_number() over (partition by b.order_id order by b.created_at desc) as rn
  from base b
  inner join raw.customers c on b.customer_id = c.customer_id
)
select e.order_id,
       e.region_code,
       sum(e.amount_usd) as revenue_usd,
       max(e.status_norm) as last_status
from enriched e
left join raw.refunds r on e.order_id = r.order_id
group by 1, 2
"""


def test_parser_reads_ctes_joins_and_windows():
    tree = parse(MODEL_SQL)
    assert tree.cte_order == ["base", "enriched"]
    assert tree.joins[0].kind == "LEFT"
    assert tree.joins[0].table.name.endswith("refunds")
    inner = tree.ctes["enriched"]
    assert inner.joins[0].kind == "INNER"
    window = next(p for p in inner.projections if p.output_name() == "rn")
    assert window.expr.is_window


def test_select_star_expands_against_the_catalog():
    tree = parse("select * from raw.orders o")
    assert tree.projections[0].expr.has_star
    outcome = resolve_model(CATALOG, "m.sql", "wh.db.silver.copy",
                            "select * from raw.orders o")
    produced = {e.dst_column for e in outcome.edges if e.dst_column}
    assert produced == set(CATALOG.columns_of("wh.db.raw.orders"))


def test_column_mapping_matches_hand_labelled_ground_truth():
    expected = {
        ("order_id", "wh.db.raw.orders", "order_id", TransformType.IDENTITY),
        ("region_code", "wh.db.raw.customers", "region_code", TransformType.IDENTITY),
        ("revenue_usd", "wh.db.raw.orders", "amount_usd", TransformType.AGG),
        ("last_status", "wh.db.raw.orders", "status", TransformType.AGG),
    }
    outcome = resolve_model(CATALOG, "models/fct.sql", "wh.db.gold.fct_orders", MODEL_SQL)
    assert outcome.parsed
    produced = {
        (e.dst_column, e.src_asset_id, e.src_column, e.transform_type)
        for e in outcome.edges if e.dst_column
    }
    assert expected <= produced, "missing " + str(expected - produced)


def test_join_keys_become_table_level_edges():
    """A join key changes row multiplicity, so it affects the whole model."""
    outcome = resolve_model(CATALOG, "models/fct.sql", "wh.db.gold.fct_orders", MODEL_SQL)
    table_level = {(e.src_asset_id, e.src_column)
                   for e in outcome.edges if e.dst_column is None}
    assert ("wh.db.raw.customers", "customer_id") in table_level
    assert ("wh.db.raw.refunds", "order_id") in table_level


def test_unparseable_sql_is_reported_not_swallowed():
    outcome = resolve_model(CATALOG, "m.sql", "wh.db.gold.x", "MERGE INTO ( ( (")
    assert not outcome.parsed or outcome.coverage < 1.0


def test_coverage_is_reported_honestly():
    """count(*) has no source column; coverage should say so rather than round up."""
    outcome = resolve_model(
        CATALOG, "m.sql", "wh.db.gold.x",
        "select o.order_id, count(*) as n from raw.orders o group by 1")
    assert 0.4 < outcome.coverage < 0.6


# --------------------------------------------------------------- semantic diff


BEFORE = """select o.order_id, o.amount_usd as revenue_usd, c.region_code
from raw.orders o inner join raw.customers c on o.customer_id = c.customer_id
where o.status = 'COMPLETE'"""


def test_semantic_diff_names_the_join_change_structurally():
    after = BEFORE.replace("inner join", "left join")
    diff = diff_sql("stg_orders.sql", BEFORE, after, "aaa", "bbb")
    change = next(c for c in diff.changes if c.kind == "join_type")
    assert change.before["kind"] == "INNER"
    assert change.after["kind"] == "LEFT"
    assert change.severity == "breaking"
    assert "customer_id" in change.after["on"]


def test_semantic_diff_catches_a_removed_filter():
    after = BEFORE.rsplit("where", 1)[0]
    diff = diff_sql("m.sql", BEFORE, after)
    assert any(c.kind == "filter" and c.severity == "breaking" for c in diff.changes)


def test_semantic_diff_ignores_formatting():
    reformatted = BEFORE.replace("\n", "\n   ").replace("select", "SELECT")
    assert diff_sql("m.sql", BEFORE, reformatted).is_empty


# ------------------------------------------------------------------- traversal


def _index():
    edges = []
    for column in ("a", "b"):
        edges.append(LineageEdge(src_asset_id="X", src_column=column,
                                 dst_asset_id="Y", dst_column=column))
        edges.append(LineageEdge(src_asset_id="Y", src_column=column,
                                 dst_asset_id="Z", dst_column=column))
    return LineageIndex(edges)


def test_traversal_from_a_table_seeds_every_column():
    """Starting at (asset, None) must not follow only table-level edges."""
    reached = _index().downstream("X", None).reached
    assets = {r.asset_id for r in reached}
    assert assets == {"Y", "Z"}
    assert len(reached) == 4  # two columns x two downstream assets


def test_traversal_respects_depth():
    assert {r.asset_id for r in _index().downstream("X", "a", max_depth=1).reached} == {"Y"}


def test_upstream_is_the_mirror_image():
    assert {r.asset_id for r in _index().upstream("Z", "a").reached} == {"X", "Y"}
