"""The end-to-end pipeline.

Wires every stage into one runnable loop:

    seed -> profile -> detect -> correlate -> RCA -> policy -> execute -> verify

In production these are separate deployables communicating over Kafka. Here
they are called in order against the same control plane, with every hand-off
also published to the event log — so the topics, keys and payloads are
exercised even though nothing is consuming them across a network. That means
splitting a stage back out into its own service is a deployment change, not a
rewrite.

Reproducibility is the design constraint throughout. The world is a function
of its seed, mutations are stored as data on the fault record, and the cycle
cursor lives in the control plane. ``ds seed`` then ``ds inject`` then
``ds detect`` in three separate processes produces exactly what one process
would.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from services.action_executor.executor import ActionExecutor
from services.correlator.correlator import Correlator, compression_report
from services.policy_gate.policy import PolicyGate, PolicyInput
from services.profiler.profiler import Profiler, as_event, cycle_due
from services.verification.worker import VerificationWorker

from datasentry_agent.providers.registry import build_reasoner
from datasentry_agent.runner import Orchestrator
from datasentry_core import bus
from datasentry_core.clock import FrozenClock, iso, utc
from datasentry_core.config import Settings
from datasentry_core.config import settings as get_settings
from datasentry_core.enums import ActionStatus, IncidentStatus, Layer
from datasentry_core.ids import action_id as make_action_id
from datasentry_core.logging import get_logger
from datasentry_core.models import Action, Asset, AssetColumn, FaultRecord
from datasentry_core.store import ControlPlane
from datasentry_detectors import DetectorEnsemble, SeriesContext
from datasentry_lineage import (
    Catalog,
    LineageIndex,
    compute_blast_radius,
    resolve_model,
)
from tools.faultgen import faults as faultlib
from tools.faultgen.generators import Mutation, Simulator
from tools.faultgen.world import World

log = get_logger("pipeline")

AUTHORS = ["arun.k", "dana.m", "priya.s", "marco.f", "lena.b", "tom.w"]


@dataclass
class PipelineState:
    world_seed: int
    tables: int
    origin: datetime
    cycle_hours: float
    cursor: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "world_seed": self.world_seed,
            "tables": self.tables,
            "origin": iso(self.origin),
            "cycle_hours": self.cycle_hours,
            "cursor": self.cursor,
        }

    @classmethod
    def from_dict(cls, doc: dict[str, Any]) -> PipelineState:
        return cls(
            world_seed=int(doc["world_seed"]),
            tables=int(doc["tables"]),
            origin=utc(doc["origin"]),
            cycle_hours=float(doc["cycle_hours"]),
            cursor=int(doc.get("cursor", 0)),
        )


@dataclass
class StageReport:
    name: str
    detail: dict[str, Any] = field(default_factory=dict)

    def line(self) -> str:
        bits = "  ".join(str(k) + "=" + str(v) for k, v in self.detail.items())
        return self.name.ljust(14) + bits


class Pipeline:
    def __init__(self, store: ControlPlane | None = None,
                 config: Settings | None = None) -> None:
        self.settings = config or get_settings()
        self.store = store or ControlPlane()
        self.state: PipelineState | None = None
        self.world: World | None = None
        self.simulator: Simulator | None = None
        self.profiler = Profiler(self.settings)
        self.ensemble = DetectorEnsemble(config=self.settings)
        self._assets: dict[str, Asset] = {}
        self._index: LineageIndex | None = None
        self._radius_cache: dict[tuple[str, str | None], list] = {}

    # -- world lifecycle ----------------------------------------------------

    def bootstrap(self, *, tables: int = 200, days: int = 21, seed: int | None = None,
                  cycle_hours: float = 3.0,
                  origin: datetime | None = None) -> StageReport:
        """Create the world, its lineage and its history. Idempotent per seed."""
        seed = seed if seed is not None else self.settings.seed
        origin = origin or datetime(2026, 8, 1, tzinfo=UTC)
        self.state = PipelineState(seed, tables, origin, cycle_hours, cursor=0)
        self.world = World(tables=tables, seed=seed)
        self.simulator = Simulator(self.world, seed=seed, origin=origin, cycle_hours=cycle_hours)

        self._load_assets()
        coverage = self._build_lineage()
        self._seed_repo_history()

        faultlib.release()
        self.store.set_meta("pipeline", self.state.as_dict())
        cycles = self.advance(days=days)

        return StageReport("seed", {
            "tables": len(self.world.tables),
            "columns": sum(len(t.columns) for t in self.world.tables.values()),
            "edges": coverage["edges"],
            "parse_coverage": coverage["coverage"],
            "cycles": cycles.detail.get("cycles", 0),
            "profiles": cycles.detail.get("profiles", 0),
        })

    def resume(self) -> None:
        """Rebuild the world and replay stored mutations from the control plane."""
        doc = self.store.get_meta("pipeline")
        if not doc:
            raise RuntimeError("no seeded world found — run `ds seed` first")
        self.state = PipelineState.from_dict(doc)
        self.world = World(tables=self.state.tables, seed=self.state.world_seed)
        self.simulator = Simulator(
            self.world, seed=self.state.world_seed, origin=self.state.origin,
            cycle_hours=self.state.cycle_hours,
        )
        for row in self.store.query("SELECT mutations FROM faults"):
            for entry in json.loads(row["mutations"] or "[]"):
                self.simulator.add(_mutation_from_dict(entry))
        self._load_assets()
        self._index = LineageIndex(self.store.all_edges())
        faultlib.release()
        for row in self.store.query("SELECT root_asset_id FROM faults"):
            faultlib.reserve(self.world, row["root_asset_id"])

    def _load_assets(self) -> None:
        assert self.world is not None
        now = self.moment(0)
        for spec in self.world.tables.values():
            asset = Asset(
                asset_id=spec.asset_id,
                warehouse="snowflake",
                database_name=spec.asset_id.split(".")[1],
                schema_name=str(spec.layer) if spec.layer != Layer.BRONZE else "raw",
                table_name=spec.name,
                layer=spec.layer,
                kind=spec.kind,
                tier=spec.tier,
                owner_team=spec.owner_team,
                dbt_model_path=spec.model_ref,
                repo="analytics" if spec.model_ref else None,
                query_count_30d=spec.query_count_30d,
                distinct_consumers_30d=spec.distinct_consumers_30d,
                columns=[
                    AssetColumn(
                        column_name=c.name, data_type=c.dtype, is_nullable=c.null_rate > 0,
                        ordinal=i, contains_pii=c.contains_pii,
                    )
                    for i, c in enumerate(spec.columns)
                ],
            )
            self.store.upsert_asset(asset, now)
            self._assets[asset.asset_id] = asset

    def _build_lineage(self) -> dict[str, Any]:
        """Parse every model to column-level edges and report the coverage."""
        assert self.world is not None
        catalog = Catalog(self.world.catalog_columns())
        now = self.moment(0)
        total_edges = 0
        parsed = 0
        failed: list[str] = []
        coverages: list[float] = []

        for spec in self.world.models():
            outcome = resolve_model(catalog, spec.model_ref or spec.name, spec.asset_id, spec.sql)
            if not outcome.parsed:
                failed.append(spec.model_ref or spec.asset_id)
                # Unparsed SQL falls back to observed table-level edges marked
                # inferred, so the blast radius degrades in accuracy rather
                # than developing a silent hole.
                from datasentry_core.models import LineageEdge

                fallback = [
                    LineageEdge(
                        src_asset_id=parent, src_column=None,
                        dst_asset_id=spec.asset_id, dst_column=None,
                        source="observed", confidence=0.5,
                        model_ref=spec.model_ref,
                    )
                    for parent in spec.upstream
                ]
                total_edges += self.store.upsert_edges(fallback, now)
                continue
            parsed += 1
            coverages.append(outcome.coverage)
            total_edges += self.store.upsert_edges(outcome.edges, now)
            self.store.execute(
                "INSERT OR REPLACE INTO model_versions (model_ref, sha, sql, created_at) "
                "VALUES (?,?,?,?)",
                (spec.model_ref, _sha(spec.sql), spec.sql, iso(now)),
            )

        self._index = LineageIndex(self.store.all_edges())
        models = len(self.world.models())
        return {
            "edges": total_edges,
            "models": models,
            "parsed": parsed,
            "failed": failed[:5],
            "coverage": round(sum(coverages) / len(coverages), 4) if coverages else 0.0,
            "parse_rate": round(parsed / models, 4) if models else 0.0,
        }

    def _seed_repo_history(self) -> None:
        """A handful of benign commits so "recent change" is not always the cause.

        Without unrelated commits in the window, any commit in the window is
        the commit, and the verifier's rule about touching the lineage path
        would never actually be exercised.
        """
        assert self.world is not None and self.state is not None
        rng = random.Random(self.state.world_seed + 991)
        models = list(self.world.models())
        for i in range(14):
            spec = rng.choice(models)
            at = self.state.origin + timedelta(hours=rng.uniform(0, 24 * 18))
            sha = _sha(spec.asset_id + str(i) + str(at))
            self.store.execute(
                "INSERT OR REPLACE INTO git_commits "
                "(sha, repo, author, message, pr_number, files, diff, committed_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    sha, "analytics", rng.choice(AUTHORS),
                    rng.choice([
                        "chore: add column description to " + spec.name,
                        "docs: document grain of " + spec.name,
                        "test: add not_null test on " + spec.name,
                        "refactor: rename CTE in " + spec.name,
                        "chore: bump dbt-snowflake",
                    ]),
                    None, json.dumps([spec.model_ref]), "", iso(at),
                ),
            )

    # -- simulation ---------------------------------------------------------

    def moment(self, cycle_index: int) -> datetime:
        assert self.state is not None
        return self.state.origin + timedelta(hours=self.state.cycle_hours * cycle_index)

    def advance(self, *, days: float | None = None, cycles: int | None = None) -> StageReport:
        """Run the profiler forward, honouring the cadence tiers."""
        assert self.state is not None and self.world is not None and self.simulator is not None
        if cycles is None:
            span_days = days if days is not None else 1
            cycles = max(1, int(span_days * 24 / self.state.cycle_hours))

        start = self.state.cursor
        written = 0
        batches = 0
        specs = list(self.world.tables.values())

        for offset in range(cycles):
            index = start + offset
            moment = self.moment(index)
            produced = []
            for spec in specs:
                if not cycle_due(spec.cadence_stride(), index):
                    continue
                batch = self.simulator.batch(spec, moment, index)
                produced.extend(self.profiler.profile_batch(batch))
                batches += 1
            if produced:
                written += self.store.insert_profiles(produced)
                for profile in produced:
                    self.store.publish(
                        bus.PROFILE_COMPUTED, profile.asset_id, as_event(profile), moment
                    )
            self._emit_telemetry(index, moment)

        self.state.cursor = start + cycles
        self.store.set_meta("pipeline", self.state.as_dict())
        return StageReport("advance", {
            "cycles": cycles, "batches": batches, "profiles": written,
            "through": iso(self.moment(self.state.cursor)),
        })

    def _emit_telemetry(self, index: int, moment: datetime) -> None:
        """dbt runs and query history, so the evidence tools have something real."""
        assert self.world is not None and self.state is not None
        if index % 4:
            return
        rng = random.Random(self.state.world_seed + index)
        for spec in rng.sample(self.world.models(), k=min(12, len(self.world.models()))):
            run_id = "run-" + _sha(spec.asset_id + str(index))[:10]
            self.store.execute(
                "INSERT OR REPLACE INTO dbt_runs (run_id, model_ref, status, rows_affected, "
                "duration_ms, tests_passed, tests_failed, started_at) VALUES (?,?,?,?,?,?,?,?)",
                (run_id, spec.model_ref, "success", rng.randint(500, 60000),
                 rng.randint(4000, 90000), rng.randint(3, 12), 0, iso(moment)),
            )
            self.store.execute(
                "INSERT OR REPLACE INTO query_log (query_id, asset_id, operation, user_name, "
                "rows_produced, query_text, executed_at) VALUES (?,?,?,?,?,?,?)",
                ("q-" + _sha(spec.asset_id + str(index) + "w")[:12], spec.asset_id, "MERGE",
                 "dbt_cloud", rng.randint(500, 60000), "", iso(moment)),
            )

    # -- fault injection ----------------------------------------------------

    def inject(self, fault_type: str, *, at_cycle: int | None = None,
               rng_seed: int | None = None) -> tuple[FaultRecord, StageReport]:
        assert self.world is not None and self.simulator is not None and self.state is not None
        index = at_cycle if at_cycle is not None else self.state.cursor
        moment = self.moment(index)
        rng = random.Random(rng_seed if rng_seed is not None else (self.state.world_seed + index))

        injection = faultlib.build(fault_type, self.world, rng, moment)
        if injection is None:
            raise RuntimeError("could not build a " + fault_type + " fault in this world")

        record = injection.record
        # Reserve the target subtree so the next fault lands somewhere else.
        # Two faults in one lineage subtree correlate into a single incident,
        # which is correct behaviour and useless as a measurement: recall and
        # precision both become untestable.
        faultlib.reserve(self.world, record.root_asset_id)
        for mutation in injection.mutations:
            self.simulator.add(mutation)

        if injection.touches_code and injection.model_ref:
            sha = _sha(injection.model_ref + str(moment))
            pr_number = 4400 + (index % 90)
            self.store.execute(
                "INSERT OR REPLACE INTO git_commits "
                "(sha, repo, author, message, pr_number, files, diff, committed_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (sha, "analytics", "arun.k", injection.commit_message, pr_number,
                 json.dumps(injection.commit_files), "", iso(moment)),
            )
            self.store.execute(
                "INSERT OR REPLACE INTO model_versions (model_ref, sha, sql, created_at) "
                "VALUES (?,?,?,?)",
                (injection.model_ref, sha, injection.sql_after, iso(moment)),
            )
            spec = self.world.tables.get(record.root_asset_id)
            if spec is not None and injection.sql_after:
                spec.sql = injection.sql_after
            record = record.model_copy(update={"commit_sha": sha, "pr_number": pr_number})
            self.store.publish(bus.GIT_EVENT, "analytics", {
                "sha": sha, "pr_number": pr_number, "files": injection.commit_files,
            }, moment)

        if fault_type.upper() in {"SCHEMA_DRIFT", "PLANNED_COLUMN_ADD"}:
            self.store.execute(
                "INSERT OR REPLACE INTO schema_changes (change_id, asset_id, column_name, "
                "change_kind, before_value, after_value, detected_at) VALUES (?,?,?,?,?,?,?)",
                (_sha(record.fault_id)[:16], record.root_asset_id, record.root_column,
                 "RETYPE" if fault_type.upper() == "SCHEMA_DRIFT" else "ADD_COLUMN",
                 None, None, iso(moment)),
            )
            self.store.publish(bus.SCHEMA_CHANGED, record.root_asset_id,
                               {"asset_id": record.root_asset_id}, moment)

        self.store.execute(
            "INSERT OR REPLACE INTO faults (fault_id, fault_type, injected_at, root_asset_id, "
            "root_column, mechanism, cause_class, expected_signals, expected_downstream, "
            "commit_sha, pr_number, is_decoy, mutations) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                record.fault_id, record.fault_type, iso(record.injected_at),
                record.root_asset_id, record.root_column, record.mechanism,
                str(record.cause_class) if record.cause_class else None,
                json.dumps([str(s) for s in record.expected_signals]),
                json.dumps(record.expected_downstream),
                record.commit_sha, record.pr_number, int(record.is_decoy),
                json.dumps([_mutation_to_dict(m) for m in injection.mutations]),
            ),
        )

        return record, StageReport("inject", {
            "type": record.fault_type,
            "asset": record.root_asset_id.split(".", 2)[-1],
            "column": record.root_column or "-",
            "decoy": record.is_decoy,
            "at": iso(moment),
        })

    # -- detection ----------------------------------------------------------

    def detect(self, *, cycles_back: int = 3, warmup: int = 48) -> StageReport:
        """Score the most recent cycles and persist any anomalies.

        The walk is forward on purpose. Conformal thresholds come from a
        calibration set, so the detector has to see the series in order to
        build one — scoring only the newest point would leave every threshold
        at infinity and every column silent.

        ``warmup`` bounds how far back that replay goes. In production the
        calibration set is persisted and only new cycles are scored; here it
        is rebuilt each run, and 48 cycles is comfortably past the 30-cycle
        cold-start floor while keeping the pass quick.
        """
        assert self.state is not None
        muted = self._active_mutes()
        found = []
        horizon = self.moment(max(0, self.state.cursor - cycles_back))

        for asset_id, column in self.store.profiled_series_keys():
            asset = self._assets.get(asset_id)
            if asset is None:
                continue
            series = self.store.profile_series(asset_id, column, limit=warmup + cycles_back + 12)
            if len(series) < 12:
                continue
            data_type = "TABLE"
            if column:
                match = next((c for c in asset.columns if c.column_name == column), None)
                data_type = match.data_type if match else "UNKNOWN"

            is_muted = (asset_id, column) in muted or (asset_id, None) in muted
            for i in range(11, len(series)):
                ctx = SeriesContext(
                    asset=asset, column=column, current=series[i], history=series[:i],
                    data_type=data_type,
                )
                if series[i].window_end < horizon:
                    # Warm the calibrator on older cycles without emitting.
                    self.ensemble.evaluate(ctx, muted=True)
                    continue
                found.extend(self.ensemble.evaluate(ctx, muted=is_muted))

        if found:
            self.store.insert_anomalies(found)
            for anomaly in found:
                self.store.publish(bus.ANOMALY_DETECTED, anomaly.asset_id, {
                    "anomaly_id": anomaly.anomaly_id, "signal": str(anomaly.signal),
                    "score": anomaly.score,
                }, anomaly.detected_at)

        # Record how many cycles were scored per cohort. The recalibration job
        # needs that denominator to turn dispositions into a false-positive
        # *rate*; only detection knows how many times the detector looked.
        from services.detector.recalibration import RecalibrationJob

        RecalibrationJob.record_scored_cycles(
            self.store, self.ensemble.calibration_reports(),
            self.moment(self.state.cursor),
        )
        return StageReport("detect", dict(self.ensemble.stats.as_dict(),
                                          persisted=len(found)))

    def _active_mutes(self) -> set[tuple[str, str | None]]:
        now = self.moment(self.state.cursor if self.state else 0)
        out: set[tuple[str, str | None]] = set()
        for row in self.store.query("SELECT asset_id, column_name, until FROM mutes"):
            if utc(row["until"]) > now:
                out.add((row["asset_id"], row["column_name"] or None))
        return out

    # -- correlation --------------------------------------------------------

    def correlate(self) -> StageReport:
        assert self.state is not None and self._index is not None
        anomalies = self.store.unassigned_anomalies()
        if not anomalies:
            return StageReport("correlate", {"anomalies": 0, "incidents": 0})

        correlator = Correlator(self._index, self._assets, self.settings)
        now = self.moment(self.state.cursor)

        downstream_crit: dict[str, float] = {}
        for asset_id in {a.asset_id for a in anomalies}:
            entries, _ = compute_blast_radius(self._index, self._assets, asset_id, None)
            downstream_crit[asset_id] = max((e.criticality for e in entries), default=0.0)

        result = correlator.correlate(
            anomalies, now=now, downstream_criticality=downstream_crit,
            open_incidents=self.store.list_incidents(limit=200),
        )

        for incident in result.incidents:
            self.store.upsert_incident(incident)
            self.store.publish(bus.INCIDENT_CREATED, incident.incident_id, {
                "incident_id": incident.incident_id, "severity": incident.severity,
                "root_asset_id": incident.root_asset_id,
            }, now)
        for incident_id, anomaly_ids in result.assignments.items():
            self.store.assign_anomalies(incident_id, anomaly_ids)
            # Union over every (asset, column) the incident actually touches,
            # not just the elected root. An incident is a set of anomalies, so
            # taking the radius of one of them under-reports who is affected --
            # and under-reporting blast radius is the one error that makes a
            # human close a ticket they should have escalated.
            self.store.replace_blast_radius(
                incident_id, self._merged_blast_radius(incident_id)
            )

        self._link_faults()
        return StageReport("correlate", compression_report(anomalies, result))

    # A traversal from one column is cheap; a traversal from every column of a
    # 130-anomaly incident is not, and the same columns recur across incidents.
    MAX_RADIUS_TARGETS = 20

    def _incident_targets(self, incident_id: str) -> list[tuple[str, str | None]]:
        """The (asset, column) pairs whose downstream reach defines this incident.

        Capped, and ordered so the cap keeps the ones that matter: the elected
        root first, then the highest-scoring anomalies. An incident with 130
        anomalies has perhaps six distinct upstream sources; the rest are the
        same corruption arriving downstream, and their radius is a subset of
        what the root already covers.
        """
        incident = self.store.get_incident(incident_id)
        ordered: list[tuple[str, str | None]] = []
        seen: set[tuple[str, str | None]] = set()

        def push(pair: tuple[str, str | None]) -> None:
            if pair not in seen:
                seen.add(pair)
                ordered.append(pair)

        if incident:
            push((incident.root_asset_id, incident.root_column))
        anomalies = sorted(
            self.store.anomalies_for_incident(incident_id), key=lambda a: -a.score
        )
        for anomaly in anomalies:
            if len(ordered) >= self.MAX_RADIUS_TARGETS:
                break
            push((anomaly.asset_id, anomaly.column_name))
        return ordered

    def _merged_blast_radius(self, incident_id: str) -> list:
        merged: dict[tuple[str, str | None], Any] = {}
        for asset_id, column in self._incident_targets(incident_id):
            for entry in self._radius(asset_id, column):
                key = (entry.asset_id, entry.column_name)
                current = merged.get(key)
                if current is None or entry.depth < current.depth:
                    merged[key] = entry
        return list(merged.values())

    def _radius(self, asset_id: str, column: str | None) -> list:
        """Memoised downstream reach. Mirrors the ``lineage_closure`` cache."""
        key = (asset_id, column)
        cached = self._radius_cache.get(key)
        if cached is None:
            cached, _ = compute_blast_radius(self._index, self._assets, asset_id, column)
            if len(self._radius_cache) > 4000:
                self._radius_cache.clear()
            self._radius_cache[key] = cached
        return cached

    def _link_faults(self) -> None:
        """Attribute incidents to injected faults, for scoring."""
        for row in self.store.query("SELECT * FROM faults ORDER BY injected_at"):
            if row["detected_incident_id"]:
                continue
            # Prefer an incident rooted at the fault's own asset; fall back to
            # one that merely contains an anomaly on it. A cold table profiled
            # every eighth cycle can be corrupted for a day before it is
            # scored twice, by which time a downstream table has already been
            # elected root — the fault was still detected, just first seen
            # from downstream, and scoring it as a miss would be wrong.
            match = self.store.one(
                "SELECT incident_id FROM incidents WHERE root_asset_id = ? "
                "AND first_detected_at >= ? ORDER BY first_detected_at LIMIT 1",
                (row["root_asset_id"], row["injected_at"]),
            )
            if match is None:
                match = self.store.one(
                    "SELECT a.incident_id FROM anomalies a JOIN incidents i "
                    "ON i.incident_id = a.incident_id "
                    "WHERE a.asset_id = ? AND a.detected_at >= ? AND a.incident_id IS NOT NULL "
                    "ORDER BY a.detected_at LIMIT 1",
                    (row["root_asset_id"], row["injected_at"]),
                )
            if match:
                self.store.execute(
                    "UPDATE faults SET detected_incident_id = ? WHERE fault_id = ?",
                    (match["incident_id"], row["fault_id"]),
                )

    # -- reasoning ----------------------------------------------------------

    def investigate(self, *, limit: int = 10, on_step=None,
                    provider: str | None = None) -> StageReport:
        assert self.state is not None and self._index is not None
        now = self.moment(self.state.cursor)
        # A named provider is how the same gold set gets scored against a
        # different model without editing the environment -- which is what
        # makes "the 8B model is good enough here" a measurement rather than
        # an opinion.
        reasoner = build_reasoner(self.settings, provider=provider) if provider else None
        orchestrator = Orchestrator(
            store=self.store, index=self._index, assets=self._assets,
            clock=FrozenClock(now), config=self.settings, reasoner=reasoner,
        )
        gate = PolicyGate(self.settings)

        pending = [
            i for i in self.store.list_incidents(limit=200)
            if i.status == IncidentStatus.DETECTED
        ]
        pending.sort(key=lambda i: (i.severity, i.first_detected_at))
        pending = pending[:limit]

        investigated = 0
        actions_created = 0
        auto = 0
        dismissed = 0
        costs: list[float] = []
        latencies: list[float] = []

        for incident in pending:
            result = orchestrator.run(incident, on_step=on_step)
            investigated += 1
            costs.append(result.budget.usd)
            latencies.append(result.duration_ms)

            top = result.state.top()
            if top is not None and str(top.cause_class) == "SEASONAL_LEGITIMATE":
                # The investigation concluded nothing is wrong. Dismissing is
                # the product behaviour: an incident inbox that keeps items it
                # has already explained away is an inbox nobody reads. The
                # dismissal and its reasoning stay on the record.
                self.store.set_incident_status(
                    incident.incident_id, IncidentStatus.DISMISSED
                )
                self.store.audit(
                    actor="agent:orchestrator", actor_kind="agent",
                    action="incident.dismissed", target=incident.incident_id,
                    detail={"cause_class": str(top.cause_class),
                            "confidence": top.confidence,
                            "statement": top.statement},
                    at=now,
                )
                dismissed += 1
                continue

            if not result.state.actions:
                continue

            blast = self.store.blast_radius_for(incident.incident_id)
            for proposed in result.state.actions:
                decision = gate.evaluate(
                    PolicyInput(
                        action_type=proposed.action_type,
                        root_tier=self._assets[incident.root_asset_id].tier
                        if incident.root_asset_id in self._assets else 3,
                        blast_radius_size=len(blast),
                        max_downstream_criticality=max((b.criticality for b in blast), default=0.0),
                        confidence=top.confidence if top else 0.0,
                        severity=incident.severity,
                    )
                )
                action = Action(
                    action_id=make_action_id(
                        incident.incident_id, str(proposed.action_type),
                        json.dumps(proposed.params, sort_keys=True, default=str),
                    ),
                    incident_id=incident.incident_id,
                    action_type=proposed.action_type,
                    risk_tier=decision.risk_tier,
                    policy_version=decision.policy_version,
                    params=proposed.params,
                    rationale=proposed.rationale,
                    status=ActionStatus.APPROVED if decision.auto_execute else ActionStatus.PROPOSED,
                    approved_by="policy:auto" if decision.auto_execute else None,
                    approved_at=now if decision.auto_execute else None,
                    created_at=now,
                )
                self.store.upsert_action(action)
                self.store.audit(
                    actor="service:datasentry-policy", actor_kind="service",
                    action="policy.evaluated", target=action.action_id,
                    detail=decision.as_dict(), at=now,
                )
                actions_created += 1
                if decision.auto_execute:
                    auto += 1
                self.store.publish(bus.ACTION_REQUESTED, incident.incident_id, {
                    "action_id": action.action_id, "risk_tier": str(decision.risk_tier),
                }, now)

            incident.status = (
                IncidentStatus.AWAITING_APPROVAL
                if any(a.status == ActionStatus.PROPOSED
                       for a in self.store.actions_for(incident.incident_id))
                else IncidentStatus.RCA_COMPLETE
            )
            self.store.upsert_incident(incident)

        return StageReport("investigate", {
            "incidents": investigated,
            "dismissed_benign": dismissed,
            "actions": actions_created,
            "auto_executed": auto,
            "median_cost_usd": round(_median(costs), 4),
            "p50_latency_ms": int(_median(latencies)),
            "cheap_ratio": round(_cheap_ratio(self.store), 3),
            "provider": getattr(orchestrator.reasoner, "name", "unknown"),
        })

    # -- execution and verification ----------------------------------------

    def execute_approved(self) -> StageReport:
        assert self.state is not None
        now = self.moment(self.state.cursor)
        executor = ActionExecutor(self.store, clock=FrozenClock(now))
        approved = list(
            self.store.list_actions(status=str(ActionStatus.APPROVED), limit=100)
        )
        succeeded = 0
        reverted = 0
        for action in approved:
            result = executor.execute(action)
            if result.status == ActionStatus.SUCCEEDED:
                succeeded += 1
                if str(action.action_type) == "OPEN_PR":
                    reverted += self._apply_revert(action, now)
                self.store.publish(bus.ACTION_COMPLETED, action.incident_id, {
                    "action_id": action.action_id, "external_ref": result.external_ref,
                }, now)
                incident = self.store.get_incident(action.incident_id)
                if incident and incident.status in {
                    IncidentStatus.RCA_COMPLETE, IncidentStatus.AWAITING_APPROVAL,
                    IncidentStatus.REMEDIATING,
                }:
                    self.store.set_incident_status(
                        action.incident_id, IncidentStatus.VERIFYING
                    )
        return StageReport("execute", {
            "approved": len(approved), "succeeded": succeeded, "faults_reverted": reverted,
        })

    def _apply_revert(self, action, now) -> int:
        """A merged revert has to actually fix the data.

        Without this the loop is theatre: the PR opens, CI passes, the
        incident moves to verifying, and the next profiling cycle is still
        corrupted because the generative fault is still running. Verification
        would then escalate every remediation it ever approved -- correct
        behaviour reporting on a broken world.

        Only OPEN_PR clears the fault. Quarantine and feature-view pinning
        contain the damage; they do not repair the source, and the
        verification loop should keep escalating until something does.
        """
        assert self.simulator is not None
        incident = self.store.get_incident(action.incident_id)
        if incident is None:
            return 0

        cleared = 0
        for row in self.store.query("SELECT * FROM faults WHERE is_decoy = 0"):
            if row["root_asset_id"] != incident.root_asset_id:
                continue
            fault_id = row["fault_id"]
            for mutation in self.simulator.mutations:
                if mutation.fault_id == fault_id and mutation.active_until is None:
                    mutation.active_until = now
                    cleared += 1
            stored = json.loads(row["mutations"] or "[]")
            for entry in stored:
                entry.setdefault("active_until", None)
                if entry["active_until"] is None:
                    entry["active_until"] = iso(now)
            self.store.execute(
                "UPDATE faults SET mutations = ? WHERE fault_id = ?",
                (json.dumps(stored), fault_id),
            )
        return cleared

    def verify(self) -> StageReport:
        assert self.state is not None
        now = self.moment(self.state.cursor)
        worker = VerificationWorker(self.store, clock=FrozenClock(now))
        resolved = 0
        escalated = 0
        for incident in self.store.list_incidents(status=str(IncidentStatus.VERIFYING), limit=100):
            outcome = worker.verify(incident)
            if outcome.detail.get("sufficient_evidence"):
                if outcome.cleared:
                    resolved += 1
                else:
                    escalated += 1
        return StageReport("verify", {"resolved": resolved, "escalated": escalated})

    # -- reporting ----------------------------------------------------------

    def health(self) -> dict[str, Any]:
        return {
            "calibration": self.ensemble.calibration_reports(),
            "detector_stats": self.ensemble.stats.as_dict(),
            "profiler": self.profiler.stats.as_dict(),
            "topics": self.store.topic_counts(),
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sha(text: str) -> str:
    import hashlib

    return hashlib.sha1(text.encode()).hexdigest()


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    return ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2


def _cheap_ratio(store: ControlPlane) -> float:
    rows = store.query("SELECT model, COUNT(*) AS n FROM agent_steps WHERE model IS NOT NULL GROUP BY model")
    counts = {r["model"]: r["n"] for r in rows}
    cheap = counts.get("cheap", 0)
    total = cheap + counts.get("frontier", 0)
    return (cheap / total) if total else 0.0


def _mutation_to_dict(m: Mutation) -> dict[str, Any]:
    return {
        "fault_id": m.fault_id,
        "kind": m.kind,
        "asset_id": m.asset_id,
        "column": m.column,
        "active_from": iso(m.active_from) if m.active_from else None,
        "active_until": iso(m.active_until) if m.active_until else None,
        "params": m.params,
    }


def _mutation_from_dict(doc: dict[str, Any]) -> Mutation:
    return Mutation(
        fault_id=doc["fault_id"],
        kind=doc["kind"],
        asset_id=doc["asset_id"],
        column=doc.get("column"),
        active_from=utc(doc["active_from"]) if doc.get("active_from") else None,
        active_until=utc(doc["active_until"]) if doc.get("active_until") else None,
        params=doc.get("params", {}),
    )
