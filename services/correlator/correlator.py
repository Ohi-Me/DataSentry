"""Anomalies to incidents.

Raw anomalies are not incidents. A schema change on a bronze table fires
thirty-odd anomalies across the DAG, and paging a human thirty times about
one event is how monitoring products get switched off. Correlating first is
what buys the roughly 20x alert compression and, because the agent runs once
per incident rather than once per anomaly, the same 20x off the LLM bill
(ADR-006).

The algorithm, from docs/04 section 5:

1. Take the anomalies in a sliding window.
2. Join two of them if their assets are within lineage distance 3 *and* they
   were detected within ten minutes of each other. Both conditions, not
   either: co-timing without a lineage path is coincidence, and a lineage
   path without co-timing is just the DAG.
3. Connected components become candidate incidents.
4. Elect the root by minimising (topological depth, detection time), breaking
   ties upstream. Getting the root right is what makes the RCA tractable —
   the agent investigates one asset, not thirty.
5. Severity from criticality and confidence, never from raw z-score. A
   six-sigma move on a scratch table is not a severity 1.
6. Deduplicate against open incidents on the same root within 24h, bumping
   an occurrence counter instead of creating a second incident.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from datasentry_core.config import Settings
from datasentry_core.config import settings as get_settings
from datasentry_core.enums import IncidentStatus, Signal
from datasentry_core.ids import incident_id as make_incident_id
from datasentry_core.models import Anomaly, Asset, Incident
from datasentry_lineage.graph import LineageIndex, criticality, lineage_distance, topological_depth

SIGNAL_HEADLINE = {
    Signal.VOLUME: "row volume",
    Signal.FRESHNESS: "freshness",
    Signal.NULL_RATE: "null rate",
    Signal.CARDINALITY: "cardinality",
    Signal.NUMERIC_DRIFT: "value distribution",
    Signal.CATEGORICAL_DRIFT: "category mix",
    Signal.NEW_CATEGORY: "unseen categories",
    Signal.REFERENTIAL: "referential integrity",
    Signal.SCHEMA: "schema",
    Signal.MULTIVARIATE: "joint profile",
    Signal.STRING_LENGTH: "value length",
}


@dataclass
class Cluster:
    anomalies: list[Anomaly] = field(default_factory=list)
    root_asset_id: str = ""
    root_column: str | None = None

    @property
    def first_detected(self) -> datetime:
        return min(a.detected_at for a in self.anomalies)

    @property
    def assets(self) -> set[str]:
        return {a.asset_id for a in self.anomalies}

    @property
    def signals(self) -> set[Signal]:
        return {a.signal for a in self.anomalies}


@dataclass
class CorrelationResult:
    incidents: list[Incident] = field(default_factory=list)
    assignments: dict[str, list[str]] = field(default_factory=dict)  # incident -> anomaly ids
    duplicates: dict[str, str] = field(default_factory=dict)  # anomaly -> existing incident
    compression: float = 1.0

    @property
    def created(self) -> int:
        return len(self.incidents)


class Correlator:
    def __init__(self, index: LineageIndex, assets: dict[str, Asset],
                 config: Settings | None = None) -> None:
        self.index = index
        self.assets = assets
        self.settings = config or get_settings()
        self.depths = topological_depth(index, assets)
        self._distance_cache: dict[tuple[str, str], int | None] = {}

    # -- clustering ---------------------------------------------------------

    def _distance(self, a: str, b: str) -> int | None:
        key = (a, b) if a <= b else (b, a)
        if key not in self._distance_cache:
            self._distance_cache[key] = lineage_distance(
                self.index, key[0], key[1],
                max_depth=self.settings.correlation.max_lineage_distance,
            )
        return self._distance_cache[key]

    def _related(self, a: Anomaly, b: Anomaly) -> bool:
        gap = abs((a.detected_at - b.detected_at).total_seconds())
        if gap > self.settings.correlation.link_minutes * 60:
            return False
        if a.asset_id == b.asset_id:
            return True
        distance = self._distance(a.asset_id, b.asset_id)
        return distance is not None and distance <= self.settings.correlation.max_lineage_distance

    def cluster(self, anomalies: list[Anomaly]) -> list[Cluster]:
        """Union-find over the relation above, windowed by detection time."""
        if not anomalies:
            return []
        ordered = sorted(anomalies, key=lambda a: a.detected_at)
        parent = list(range(len(ordered)))

        def find(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        def union(i: int, j: int) -> None:
            ri, rj = find(i), find(j)
            if ri != rj:
                parent[rj] = ri

        window = timedelta(minutes=self.settings.correlation.window_minutes)
        for i, left in enumerate(ordered):
            for j in range(i + 1, len(ordered)):
                right = ordered[j]
                if right.detected_at - left.detected_at > window:
                    break
                if self._related(left, right):
                    union(i, j)

        buckets: dict[int, Cluster] = {}
        for i, anomaly in enumerate(ordered):
            buckets.setdefault(find(i), Cluster()).anomalies.append(anomaly)

        clusters = list(buckets.values())
        for cluster in clusters:
            self._elect_root(cluster)
        return [c for c in clusters if self._worth_raising(c)]

    def _worth_raising(self, cluster: Cluster) -> bool:
        """Is this cluster worth a human, or is it just a data point?"""
        settings = self.settings.correlation

        # A multivariate isolation score says "this combination is unusual and
        # no single signal is out of range". That is corroboration for a cause
        # somebody else found; it is not, on its own, something to wake a
        # person for -- and left unchecked it becomes the most common root in
        # the inbox while explaining nothing.
        if cluster.signals == {Signal.MULTIVARIATE}:
            return False

        if len(cluster.anomalies) >= settings.min_cluster_size:
            return True
        if any(str(s) in settings.always_raise_signals for s in cluster.signals):
            return True
        asset = self.assets.get(cluster.root_asset_id)
        return bool(asset and asset.tier <= settings.always_raise_tier)

    def _elect_root(self, cluster: Cluster) -> None:
        """Shallowest in the DAG, earliest detected, upstream on a tie."""

        def rank(a: Anomaly) -> tuple:
            return (
                # Depth first: the root of a propagating fault is the
                # shallowest asset that broke, full stop.
                self.depths.get(a.asset_id, 99),
                # Then prefer a specific signal over a joint-profile one. At
                # equal depth, "the null rate moved" points at a mechanism and
                # "the combination is unusual" points at a symptom.
                1 if a.signal == Signal.MULTIVARIATE else 0,
                a.detected_at,
                # A schema change is a cause, not a symptom: prefer it as the
                # root when it is tied with a distributional anomaly.
                0 if a.signal == Signal.SCHEMA else 1,
                -a.score,
            )

        root = min(cluster.anomalies, key=rank)
        cluster.root_asset_id = root.asset_id
        same_asset = [a for a in cluster.anomalies if a.asset_id == root.asset_id]
        with_column = [a for a in same_asset if a.column_name]
        cluster.root_column = with_column[0].column_name if with_column else root.column_name

    def _matching_incident(self, cluster: Cluster,
                           open_by_root: dict[str, Incident]) -> Incident | None:
        """An open incident this cluster is part of, by root or by lineage."""
        direct = open_by_root.get(cluster.root_asset_id)
        if direct is not None:
            return direct
        for root, incident in open_by_root.items():
            distance = self._distance(cluster.root_asset_id, root)
            if distance is not None and distance <= self.settings.correlation.max_lineage_distance:
                return incident
        return None

    # -- severity -----------------------------------------------------------

    def severity(self, cluster: Cluster, downstream_criticality: float) -> int:
        """1 (highest) to 4, from impact and confidence — never from z-score."""
        asset = self.assets.get(cluster.root_asset_id)
        tier = asset.tier if asset else 3
        confidence = min(1.0, max(a.score for a in cluster.anomalies) / 12.0)
        breadth = min(1.0, len(cluster.anomalies) / 12.0)
        own = criticality(asset) if asset else 0.3

        score = (
            0.35 * downstream_criticality
            + 0.25 * own
            + 0.20 * (1.0 - (tier - 1) / 3.0)
            + 0.12 * breadth
            + 0.08 * confidence
        )
        if Signal.SCHEMA in cluster.signals:
            score += 0.08
        if score >= 0.72:
            return 1
        if score >= 0.55:
            return 2
        if score >= 0.38:
            return 3
        return 4

    # -- titles -------------------------------------------------------------

    def title(self, cluster: Cluster) -> str:
        asset = self.assets.get(cluster.root_asset_id)
        name = asset.short_name if asset else cluster.root_asset_id.split(".", 2)[-1]
        target = name + ("." + cluster.root_column if cluster.root_column else "")
        signals = sorted(SIGNAL_HEADLINE.get(s, str(s)) for s in cluster.signals)
        if len(signals) == 1:
            return signals[0].capitalize() + " regression on " + target
        return (
            signals[0].capitalize() + " and " + str(len(signals) - 1)
            + " other signals broke on " + target
        )

    # -- main path ----------------------------------------------------------

    def correlate(self, anomalies: list[Anomaly], *, now: datetime,
                  downstream_criticality: dict[str, float] | None = None,
                  open_incidents: list[Incident] | None = None) -> CorrelationResult:
        result = CorrelationResult()
        clusters = self.cluster(anomalies)
        if not clusters:
            return result

        downstream_criticality = downstream_criticality or {}
        open_by_root: dict[str, Incident] = {}
        cutoff = now - timedelta(hours=self.settings.correlation.dedup_hours)
        for incident in open_incidents or []:
            if incident.first_detected_at >= cutoff and incident.status not in {
                IncidentStatus.RESOLVED, IncidentStatus.DISMISSED
            }:
                open_by_root[incident.root_asset_id] = incident

        for cluster in clusters:
            existing = self._matching_incident(cluster, open_by_root)
            if existing is not None:
                # The same event, seen from a different root as more of the DAG
                # lights up. A sustained regression fires on the root in one
                # cycle and three hops downstream in the next; keying dedup on
                # the elected root alone splits one incident into four, each
                # titled after a symptom. If the new cluster is rooted further
                # upstream, the incident re-roots -- the true cause emerged
                # later, which is normal and not a reason to open a second
                # ticket.
                if self.depths.get(cluster.root_asset_id, 99) < self.depths.get(
                    existing.root_asset_id, 99
                ):
                    existing.root_asset_id = cluster.root_asset_id
                    existing.root_column = cluster.root_column
                    existing.title = self.title(cluster)
                    open_by_root[cluster.root_asset_id] = existing
                existing.occurrence_count += 1
                existing.anomaly_count += len(cluster.anomalies)
                if all(i.incident_id != existing.incident_id for i in result.incidents):
                    result.incidents.append(existing)
                result.assignments.setdefault(existing.incident_id, []).extend(
                    a.anomaly_id for a in cluster.anomalies
                )
                for anomaly in cluster.anomalies:
                    result.duplicates[anomaly.anomaly_id] = existing.incident_id
                continue

            window_key = cluster.first_detected.strftime("%Y%m%dT%H")
            iid = make_incident_id(cluster.root_asset_id, cluster.root_column, window_key)
            crit = downstream_criticality.get(cluster.root_asset_id, 0.0)
            incident = Incident(
                incident_id=iid,
                status=IncidentStatus.DETECTED,
                severity=self.severity(cluster, crit),
                title=self.title(cluster),
                root_asset_id=cluster.root_asset_id,
                root_column=cluster.root_column,
                anomaly_count=len(cluster.anomalies),
                first_detected_at=cluster.first_detected,
                created_at=now,
            )
            result.incidents.append(incident)
            result.assignments[iid] = [a.anomaly_id for a in cluster.anomalies]
            open_by_root[cluster.root_asset_id] = incident

        result.compression = len(anomalies) / max(len(result.incidents), 1)
        return result


def compression_report(anomalies: list[Anomaly], result: CorrelationResult) -> dict[str, object]:
    """The 20x claim, measured rather than asserted."""
    per_asset: dict[str, int] = defaultdict(int)
    for anomaly in anomalies:
        per_asset[anomaly.asset_id] += 1
    return {
        "anomalies": len(anomalies),
        "incidents": result.created,
        "compression_ratio": round(result.compression, 2),
        "assets_touched": len(per_asset),
        "largest_cluster": max(
            (len(v) for v in result.assignments.values()), default=0
        ),
    }
