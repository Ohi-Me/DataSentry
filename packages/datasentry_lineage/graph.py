"""Graph operations over the lineage edges.

Postgres with recursive CTEs, not a graph database (ADR-002). At two million
edges an indexed recursion meets the sub-300ms traversal budget, and adding a
second datastore to the operational surface to buy nothing measurable is a
cost with no benefit. The revisit trigger is written down: p95 traversal above
500ms, or five million edges.

What lives here is the traversal itself plus the two things built on top of
it — blast radius and criticality — held in one place so the hot path is
readable end to end.

Table-level edges (``src_column IS NULL``) mean "affects the whole asset" and
are how a join-key change propagates to every downstream column without
storing an edge per column. The traversal honours that: reaching an asset
through a table-level edge marks all of its outputs impacted.
"""

from __future__ import annotations

import math
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field

from datasentry_core.enums import AssetKind, Layer
from datasentry_core.models import Asset, BlastRadiusEntry, LineageEdge, LineageGraph, LineageNode

MAX_DEPTH = 8

# docs/04 section 6: a transparent heuristic in v1, and we say so. A learned
# model fitted on 40 labels is theatre; this becomes LightGBM at 300+
# dispositions and not before.
KIND_WEIGHT = {
    AssetKind.FEATURE_VIEW: 1.00,
    AssetKind.REPORT: 0.90,
    AssetKind.DASHBOARD: 0.80,
    AssetKind.ML_MODEL: 0.95,
    AssetKind.TABLE: 0.40,
}

TIER_WEIGHT = {1: 1.00, 2: 0.70, 3: 0.40, 4: 0.20}


def criticality(asset: Asset, *, max_queries: int = 10000, max_consumers: int = 100) -> float:
    """Score in [0, 1]. Weights are the ones in docs/04 section 6, unchanged."""
    tier = TIER_WEIGHT.get(asset.tier, 0.2)
    queries = math.log1p(max(asset.query_count_30d, 0)) / math.log1p(max_queries)
    kind = KIND_WEIGHT.get(asset.kind, 0.4)
    consumers = min(asset.distinct_consumers_30d / max_consumers, 1.0)
    return round(
        0.40 * tier + 0.25 * min(queries, 1.0) + 0.20 * kind + 0.15 * consumers, 4
    )


@dataclass
class Reach:
    asset_id: str
    column: str | None
    depth: int
    confidence: float
    via: tuple[str, str | None] | None = None


@dataclass
class TraversalResult:
    reached: list[Reach] = field(default_factory=list)
    truncated: bool = False
    elapsed_ms: float = 0.0
    visited_edges: int = 0


class LineageIndex:
    """In-memory adjacency built once per traversal batch.

    Mirrors what the materialised ``lineage_closure`` table does in Postgres:
    pay the build cost once, then answer traversals from an index rather than
    re-running the recursion for every incident. The recursive CTE remains the
    builder and the fallback when the closure is stale.
    """

    def __init__(self, edges: list[LineageEdge]) -> None:
        self.down: dict[tuple[str, str | None], list[LineageEdge]] = defaultdict(list)
        self.up: dict[tuple[str, str | None], list[LineageEdge]] = defaultdict(list)
        self.asset_columns: dict[str, set[str]] = defaultdict(set)
        self.edges = edges
        for e in edges:
            self.down[(e.src_asset_id, e.src_column)].append(e)
            self.up[(e.dst_asset_id, e.dst_column)].append(e)
            if e.src_column:
                self.asset_columns[e.src_asset_id].add(e.src_column)
            if e.dst_column:
                self.asset_columns[e.dst_asset_id].add(e.dst_column)

    def outgoing(self, asset_id: str, column: str | None) -> list[LineageEdge]:
        """Column edges plus the asset's table-level edges, which affect all."""
        out = list(self.down.get((asset_id, column), ()))
        if column is not None:
            out.extend(self.down.get((asset_id, None), ()))
        return out

    def incoming(self, asset_id: str, column: str | None) -> list[LineageEdge]:
        out = list(self.up.get((asset_id, column), ()))
        if column is not None:
            out.extend(self.up.get((asset_id, None), ()))
        return out

    # -- traversal ----------------------------------------------------------

    def downstream(self, asset_id: str, column: str | None, *,
                   max_depth: int = MAX_DEPTH, limit: int = 2000) -> TraversalResult:
        return self._walk(asset_id, column, max_depth=max_depth, limit=limit, direction="down")

    def upstream(self, asset_id: str, column: str | None, *,
                 max_depth: int = MAX_DEPTH, limit: int = 2000) -> TraversalResult:
        return self._walk(asset_id, column, max_depth=max_depth, limit=limit, direction="up")

    def _walk(self, asset_id: str, column: str | None, *, max_depth: int,
              limit: int, direction: str) -> TraversalResult:
        started = time.perf_counter()
        result = TraversalResult()
        seen: dict[tuple[str, str | None], Reach] = {}

        # A traversal that starts at a table rather than a column has to seed
        # every column of that table, not just the table-level edges. Starting
        # from (asset, None) alone follows only the handful of edges that carry
        # a NULL source column -- join keys, mostly -- and reports a blast
        # radius of three for a table feeding forty downstream columns. Silent
        # under-reporting of blast radius is the failure that gets a real
        # incident closed early.
        seeds: list[tuple[str, str | None, int, float]] = [(asset_id, column, 0, 1.0)]
        if column is None:
            seeds.extend(
                (asset_id, known, 0, 1.0)
                for known in sorted(self.asset_columns.get(asset_id, ()))
            )
        queue: deque[tuple[str, str | None, int, float]] = deque(seeds)

        while queue:
            current_asset, current_col, depth, confidence = queue.popleft()
            if depth >= max_depth:
                result.truncated = True
                continue
            edges = (
                self.outgoing(current_asset, current_col)
                if direction == "down"
                else self.incoming(current_asset, current_col)
            )
            for edge in edges:
                result.visited_edges += 1
                if direction == "down":
                    next_asset, next_col = edge.dst_asset_id, edge.dst_column
                else:
                    next_asset, next_col = edge.src_asset_id, edge.src_column

                # A table-level edge means the whole asset is affected: fan out
                # to every known column of the target rather than stopping.
                targets: list[str | None]
                if next_col is None:
                    known = sorted(self.asset_columns.get(next_asset, ()))
                    targets = list(known) if known else [None]
                else:
                    targets = [next_col]

                for target_col in targets:
                    key = (next_asset, target_col)
                    conf = min(confidence, edge.confidence)
                    existing = seen.get(key)
                    if existing is not None and existing.depth <= depth + 1:
                        if conf > existing.confidence:
                            existing.confidence = conf
                        continue
                    reach = Reach(
                        asset_id=next_asset,
                        column=target_col,
                        depth=depth + 1,
                        confidence=conf,
                        via=(current_asset, current_col),
                    )
                    seen[key] = reach
                    if len(seen) >= limit:
                        result.truncated = True
                        break
                    queue.append((next_asset, target_col, depth + 1, conf))
                if len(seen) >= limit:
                    break
            if len(seen) >= limit:
                break

        origin = {(asset_id, seed[1]) for seed in seeds}
        result.reached = sorted(
            (reach for key, reach in seen.items() if key not in origin),
            key=lambda r: (r.depth, r.asset_id, r.column or ""),
        )
        result.elapsed_ms = (time.perf_counter() - started) * 1000.0
        return result

    # -- neighbourhood for the agent ---------------------------------------

    def neighborhood(self, asset_id: str, column: str | None, *, up: int = 3, down: int = 3,
                     assets: dict[str, Asset] | None = None,
                     impacted: set[tuple[str, str | None]] | None = None) -> LineageGraph:
        assets = assets or {}
        impacted = impacted or set()
        nodes: dict[tuple[str, str | None], LineageNode] = {}
        edges: list[LineageEdge] = []

        def add_node(a_id: str, col: str | None, depth: int) -> None:
            key = (a_id, col)
            asset = assets.get(a_id)
            if key not in nodes or nodes[key].depth > abs(depth):
                nodes[key] = LineageNode(
                    asset_id=a_id,
                    column_name=col,
                    layer=asset.layer if asset else None,
                    tier=asset.tier if asset else 3,
                    depth=depth,
                    impacted=key in impacted or (a_id, None) in impacted,
                )

        add_node(asset_id, column, 0)
        for direction, budget, sign in (("down", down, 1), ("up", up, -1)):
            walk = self._walk(asset_id, column, max_depth=budget, limit=400, direction=direction)
            for reach in walk.reached:
                add_node(reach.asset_id, reach.column, sign * reach.depth)

        keys = set(nodes)
        for edge in self.edges:
            src = (edge.src_asset_id, edge.src_column)
            dst = (edge.dst_asset_id, edge.dst_column)
            src_in = src in keys or (edge.src_asset_id, None) in keys or any(
                k[0] == edge.src_asset_id for k in keys
            )
            dst_in = dst in keys or any(k[0] == edge.dst_asset_id for k in keys)
            if src_in and dst_in:
                edges.append(edge)

        return LineageGraph(nodes=list(nodes.values()), edges=edges[:1500],
                            truncated=len(edges) > 1500)


def compute_blast_radius(index: LineageIndex, assets: dict[str, Asset], asset_id: str,
                         column: str | None, *, max_depth: int = MAX_DEPTH
                         ) -> tuple[list[BlastRadiusEntry], TraversalResult]:
    """Downstream reach, ranked by how much anyone should care."""
    walk = index.downstream(asset_id, column, max_depth=max_depth)
    entries: list[BlastRadiusEntry] = []
    for reach in walk.reached:
        asset = assets.get(reach.asset_id)
        if asset is None:
            continue
        entries.append(
            BlastRadiusEntry(
                asset_id=reach.asset_id,
                column_name=reach.column,
                depth=reach.depth,
                asset_kind=asset.kind,
                criticality=criticality(asset),
                confidence=round(reach.confidence, 4),
                tier=asset.tier,
                layer=asset.layer,
            )
        )
    entries.sort(key=lambda e: (-e.criticality, e.depth, e.asset_id))
    return entries, walk


def lineage_distance(index: LineageIndex, a: str, b: str, *, max_depth: int = 6) -> int | None:
    """Directed asset-level hop count: is one of these an ancestor of the other?

    Correlation asks "are these two anomalies the same event", which is a
    causal question, and causality in a DAG has a direction. Two tables three
    undirected hops apart may share no data at all — a sibling of a sibling is
    a stranger — and clustering on undirected distance in a warehouse with
    real fan-in merges every simultaneous anomaly into one giant incident. It
    looks like spectacular alert compression and it is useless: the analyst
    gets one incident titled "forty things broke".

    So the test is directed reachability, either way round. If corruption
    could have flowed from one to the other, they may be the same event. If it
    could not, they are two events that happened to coincide.
    """
    if a == b:
        return 0
    forward = _directed_distance(index, a, b, max_depth)
    if forward is not None:
        return forward
    return _directed_distance(index, b, a, max_depth)


def _directed_distance(index: LineageIndex, src: str, dst: str, max_depth: int) -> int | None:
    frontier = {src}
    seen = {src}
    for depth in range(1, max_depth + 1):
        nxt: set[str] = set()
        for asset in frontier:
            for col in [*list(index.asset_columns.get(asset, ())), None]:
                for edge in index.outgoing(asset, col):
                    nxt.add(edge.dst_asset_id)
        nxt -= seen
        if dst in nxt:
            return depth
        seen |= nxt
        frontier = nxt
        if not frontier:
            break
    return None


def topological_depth(index: LineageIndex, assets: dict[str, Asset]) -> dict[str, int]:
    """Longest-path depth per asset, so the correlator can elect a root.

    Cycles are broken by visit order rather than raising: a real warehouse
    occasionally has one, and refusing to compute blast radius because of it
    would be the wrong trade.
    """
    incoming: dict[str, set[str]] = defaultdict(set)
    outgoing: dict[str, set[str]] = defaultdict(set)
    for edge in index.edges:
        if edge.src_asset_id != edge.dst_asset_id:
            outgoing[edge.src_asset_id].add(edge.dst_asset_id)
            incoming[edge.dst_asset_id].add(edge.src_asset_id)

    depth: dict[str, int] = dict.fromkeys(assets, 0)
    ready = deque(a for a in assets if not incoming.get(a))
    processed = set()
    while ready:
        node = ready.popleft()
        if node in processed:
            continue
        processed.add(node)
        for child in outgoing.get(node, ()):
            depth[child] = max(depth.get(child, 0), depth.get(node, 0) + 1)
            if incoming.get(child, set()) <= processed:
                ready.append(child)
    for asset_id in assets:
        depth.setdefault(asset_id, 0)
    return depth


def build_closure(index: LineageIndex, *, max_depth: int = MAX_DEPTH,
                  limit_per_source: int = 500) -> list[tuple[str, str | None, str, str | None, int, float]]:
    """Materialise the transitive closure so the hot path skips the recursion."""
    rows: list[tuple[str, str | None, str, str | None, int, float]] = []
    sources = {(e.src_asset_id, e.src_column) for e in index.edges}
    for asset_id, column in sorted(sources, key=lambda s: (s[0], s[1] or "")):
        walk = index.downstream(asset_id, column, max_depth=max_depth, limit=limit_per_source)
        for reach in walk.reached:
            rows.append(
                (asset_id, column, reach.asset_id, reach.column, reach.depth, round(reach.confidence, 4))
            )
    return rows


def summarise_layers(entries: list[BlastRadiusEntry]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for entry in entries:
        counts[str(entry.layer or Layer.SILVER)] += 1
    return dict(counts)
