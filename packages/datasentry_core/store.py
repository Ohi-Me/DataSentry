"""The control plane store.

Production targets Postgres 16 (the DDL lives in infra/sql/001_control_plane.sql
and is the authority on types and constraints). Locally we run the identical
logical schema on SQLite so the whole system boots with no container runtime.
The repository layer below is the only place that knows the difference, and it
is deliberately thin: raw SQL, no ORM, so the queries in docs/03 are the
queries that actually run.

Two properties matter more than convenience here:

* Every write is idempotent on a natural key. Re-running the pipeline over the
  same events converges rather than duplicating (docs/02 section 8).
* ``audit_log`` is append-only. There is no update or delete path in this
  module, which is what makes the security story real rather than aspirational.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from datasentry_core.clock import iso, utc
from datasentry_core.config import settings
from datasentry_core.enums import IncidentStatus
from datasentry_core.models import (
    Action,
    Anomaly,
    Asset,
    AssetColumn,
    BlastRadiusEntry,
    ColumnProfile,
    Disposition,
    Evidence,
    Hypothesis,
    Incident,
    LineageEdge,
)

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS assets (
  asset_id       TEXT PRIMARY KEY,
  warehouse      TEXT NOT NULL,
  database_name  TEXT NOT NULL,
  schema_name    TEXT NOT NULL,
  table_name     TEXT NOT NULL,
  layer          TEXT NOT NULL CHECK (layer IN ('bronze','silver','gold')),
  kind           TEXT NOT NULL DEFAULT 'TABLE',
  tier           INTEGER NOT NULL DEFAULT 3,
  owner_team     TEXT,
  dbt_model_path TEXT,
  repo           TEXT,
  is_quarantined INTEGER NOT NULL DEFAULT 0,
  query_count_30d INTEGER NOT NULL DEFAULT 0,
  distinct_consumers_30d INTEGER NOT NULL DEFAULT 0,
  created_at     TEXT NOT NULL,
  updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS asset_columns (
  asset_id     TEXT NOT NULL REFERENCES assets(asset_id) ON DELETE CASCADE,
  column_name  TEXT NOT NULL,
  data_type    TEXT NOT NULL,
  is_nullable  INTEGER NOT NULL DEFAULT 1,
  ordinal      INTEGER NOT NULL DEFAULT 0,
  contains_pii INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (asset_id, column_name)
);

CREATE TABLE IF NOT EXISTS lineage_edges (
  edge_id        INTEGER PRIMARY KEY AUTOINCREMENT,
  src_asset_id   TEXT NOT NULL,
  src_column     TEXT,
  dst_asset_id   TEXT NOT NULL,
  dst_column     TEXT,
  transform_type TEXT NOT NULL DEFAULT 'IDENTITY',
  transform_expr TEXT,
  source         TEXT NOT NULL CHECK (source IN ('declared','observed')),
  confidence     REAL NOT NULL DEFAULT 1.0,
  model_ref      TEXT,
  first_seen_at  TEXT NOT NULL,
  last_seen_at   TEXT NOT NULL,
  UNIQUE (src_asset_id, src_column, dst_asset_id, dst_column, model_ref, source)
);
CREATE INDEX IF NOT EXISTS idx_lineage_src ON lineage_edges (src_asset_id, src_column);
CREATE INDEX IF NOT EXISTS idx_lineage_dst ON lineage_edges (dst_asset_id, dst_column);

CREATE TABLE IF NOT EXISTS lineage_closure (
  src_asset_id   TEXT NOT NULL,
  src_column     TEXT,
  dst_asset_id   TEXT NOT NULL,
  dst_column     TEXT,
  depth          INTEGER NOT NULL,
  min_confidence REAL NOT NULL,
  PRIMARY KEY (src_asset_id, src_column, dst_asset_id, dst_column)
);
CREATE INDEX IF NOT EXISTS idx_closure_src ON lineage_closure (src_asset_id, src_column, depth);

CREATE TABLE IF NOT EXISTS profiles (
  profile_id     TEXT PRIMARY KEY,
  asset_id       TEXT NOT NULL,
  column_name    TEXT,
  window_start   TEXT NOT NULL,
  window_end     TEXT NOT NULL,
  row_count      INTEGER NOT NULL DEFAULT 0,
  sampled_rows   INTEGER,
  freshness_seconds INTEGER NOT NULL DEFAULT 0,
  null_count     INTEGER,
  distinct_estimate INTEGER,
  numeric_sketch TEXT,
  string_length_sketch TEXT,
  categorical_topk TEXT,
  new_category_count INTEGER,
  min_value_hash TEXT,
  max_value_hash TEXT,
  referential_violation_count INTEGER,
  schema_hash    TEXT,
  degraded       INTEGER NOT NULL DEFAULT 0,
  profiler_version TEXT NOT NULL,
  computed_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_profiles_series ON profiles (asset_id, column_name, window_end);

CREATE TABLE IF NOT EXISTS anomalies (
  anomaly_id     TEXT PRIMARY KEY,
  profile_id     TEXT NOT NULL,
  asset_id       TEXT NOT NULL,
  column_name    TEXT,
  signal         TEXT NOT NULL,
  detector_id    TEXT NOT NULL,
  detector_version TEXT NOT NULL,
  score          REAL NOT NULL,
  p_value        REAL,
  ci_low         REAL,
  ci_high        REAL,
  observed_value REAL NOT NULL,
  baseline_value REAL NOT NULL,
  consecutive_breaches INTEGER NOT NULL DEFAULT 1,
  provisional    INTEGER NOT NULL DEFAULT 0,
  incident_id    TEXT,
  explanation    TEXT NOT NULL DEFAULT '',
  detected_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_anomalies_incident ON anomalies (incident_id);
CREATE INDEX IF NOT EXISTS idx_anomalies_asset_time ON anomalies (asset_id, detected_at DESC);

CREATE TABLE IF NOT EXISTS incidents (
  incident_id    TEXT PRIMARY KEY,
  status         TEXT NOT NULL DEFAULT 'detected',
  severity       INTEGER NOT NULL DEFAULT 3,
  title          TEXT NOT NULL,
  root_asset_id  TEXT NOT NULL,
  root_column    TEXT,
  anomaly_count  INTEGER NOT NULL DEFAULT 0,
  occurrence_count INTEGER NOT NULL DEFAULT 1,
  narrative      TEXT,
  first_detected_at TEXT NOT NULL,
  rca_started_at TEXT,
  rca_completed_at TEXT,
  resolved_at    TEXT,
  llm_cost_usd   REAL NOT NULL DEFAULT 0,
  token_count    INTEGER NOT NULL DEFAULT 0,
  trace_id       TEXT,
  prompt_version TEXT,
  created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_incidents_status ON incidents (status, severity, first_detected_at DESC);

CREATE TABLE IF NOT EXISTS evidence (
  evidence_id  TEXT PRIMARY KEY,
  incident_id  TEXT NOT NULL REFERENCES incidents(incident_id) ON DELETE CASCADE,
  kind         TEXT NOT NULL,
  ref          TEXT NOT NULL,
  summary      TEXT NOT NULL,
  payload      TEXT NOT NULL,
  collected_by TEXT NOT NULL,
  collected_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_evidence_incident ON evidence (incident_id, kind);

CREATE TABLE IF NOT EXISTS hypotheses (
  hypothesis_id TEXT PRIMARY KEY,
  incident_id   TEXT NOT NULL REFERENCES incidents(incident_id) ON DELETE CASCADE,
  rank          INTEGER NOT NULL DEFAULT 0,
  cause_class   TEXT NOT NULL,
  statement     TEXT NOT NULL,
  claims        TEXT NOT NULL,
  confidence    REAL NOT NULL,
  calibrated_confidence REAL,
  status        TEXT NOT NULL,
  rejection_reason TEXT,
  evidence_requests TEXT NOT NULL DEFAULT '[]',
  round_index   INTEGER NOT NULL DEFAULT 0,
  created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_hypotheses_incident ON hypotheses (incident_id, rank);

CREATE TABLE IF NOT EXISTS blast_radius (
  incident_id  TEXT NOT NULL REFERENCES incidents(incident_id) ON DELETE CASCADE,
  asset_id     TEXT NOT NULL,
  column_name  TEXT NOT NULL DEFAULT '',
  depth        INTEGER NOT NULL,
  asset_kind   TEXT NOT NULL,
  criticality  REAL NOT NULL,
  confidence   REAL NOT NULL DEFAULT 1.0,
  tier         INTEGER NOT NULL DEFAULT 3,
  layer        TEXT,
  last_consumed_at TEXT,
  PRIMARY KEY (incident_id, asset_id, column_name)
);

CREATE TABLE IF NOT EXISTS actions (
  action_id     TEXT PRIMARY KEY,
  incident_id   TEXT NOT NULL REFERENCES incidents(incident_id) ON DELETE CASCADE,
  action_type   TEXT NOT NULL,
  risk_tier     TEXT NOT NULL,
  policy_version TEXT NOT NULL,
  params        TEXT NOT NULL,
  rationale     TEXT NOT NULL DEFAULT '',
  status        TEXT NOT NULL,
  approved_by   TEXT,
  approved_at   TEXT,
  external_ref  TEXT,
  result        TEXT NOT NULL DEFAULT '{}',
  created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_actions_status ON actions (status, risk_tier);

CREATE TABLE IF NOT EXISTS dispositions (
  disposition_id TEXT PRIMARY KEY,
  incident_id    TEXT NOT NULL REFERENCES incidents(incident_id) ON DELETE CASCADE,
  verdict        TEXT NOT NULL,
  correct_cause_class TEXT,
  notes          TEXT,
  user_id        TEXT NOT NULL,
  created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
  audit_id   INTEGER PRIMARY KEY AUTOINCREMENT,
  actor      TEXT NOT NULL,
  actor_kind TEXT NOT NULL CHECK (actor_kind IN ('human','agent','service')),
  action     TEXT NOT NULL,
  target     TEXT NOT NULL,
  detail     TEXT NOT NULL,
  at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_steps (
  step_id     INTEGER PRIMARY KEY AUTOINCREMENT,
  incident_id TEXT NOT NULL,
  seq         INTEGER NOT NULL,
  node        TEXT NOT NULL,
  model       TEXT,
  status      TEXT NOT NULL,
  duration_ms INTEGER NOT NULL DEFAULT 0,
  tokens_in   INTEGER NOT NULL DEFAULT 0,
  tokens_out  INTEGER NOT NULL DEFAULT 0,
  usd         REAL NOT NULL DEFAULT 0,
  summary     TEXT NOT NULL DEFAULT '',
  payload     TEXT NOT NULL DEFAULT '{}',
  at          TEXT NOT NULL,
  UNIQUE (incident_id, seq)
);

CREATE TABLE IF NOT EXISTS agent_checkpoints (
  incident_id TEXT PRIMARY KEY,
  node        TEXT NOT NULL,
  state       TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS git_commits (
  sha        TEXT PRIMARY KEY,
  repo       TEXT NOT NULL,
  author     TEXT NOT NULL,
  message    TEXT NOT NULL,
  pr_number  INTEGER,
  files      TEXT NOT NULL,
  diff       TEXT NOT NULL DEFAULT '',
  committed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS model_versions (
  model_ref  TEXT NOT NULL,
  sha        TEXT NOT NULL,
  sql        TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (model_ref, sha)
);

CREATE TABLE IF NOT EXISTS dbt_runs (
  run_id     TEXT PRIMARY KEY,
  model_ref  TEXT NOT NULL,
  status     TEXT NOT NULL,
  rows_affected INTEGER NOT NULL DEFAULT 0,
  duration_ms INTEGER NOT NULL DEFAULT 0,
  tests_passed INTEGER NOT NULL DEFAULT 0,
  tests_failed INTEGER NOT NULL DEFAULT 0,
  started_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS query_log (
  query_id   TEXT PRIMARY KEY,
  asset_id   TEXT NOT NULL,
  operation  TEXT NOT NULL,
  user_name  TEXT NOT NULL,
  rows_produced INTEGER NOT NULL DEFAULT 0,
  query_text TEXT NOT NULL DEFAULT '',
  executed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schema_changes (
  change_id  TEXT PRIMARY KEY,
  asset_id   TEXT NOT NULL,
  column_name TEXT,
  change_kind TEXT NOT NULL,
  before_value TEXT,
  after_value TEXT,
  detected_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS faults (
  fault_id   TEXT PRIMARY KEY,
  fault_type TEXT NOT NULL,
  injected_at TEXT NOT NULL,
  root_asset_id TEXT NOT NULL,
  root_column TEXT,
  mechanism  TEXT NOT NULL,
  cause_class TEXT,
  expected_signals TEXT NOT NULL,
  expected_downstream TEXT NOT NULL,
  commit_sha TEXT,
  pr_number  INTEGER,
  is_decoy   INTEGER NOT NULL DEFAULT 0,
  mutations  TEXT NOT NULL DEFAULT '[]',
  detected_incident_id TEXT
);

-- Small key/value scratch so the CLI can resume a simulation across
-- invocations: the world seed, the cycle cursor, the run origin.
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mutes (
  asset_id   TEXT NOT NULL,
  column_name TEXT NOT NULL DEFAULT '',
  reason     TEXT NOT NULL,
  until      TEXT NOT NULL,
  created_by TEXT NOT NULL,
  PRIMARY KEY (asset_id, column_name)
);

CREATE TABLE IF NOT EXISTS detector_perf (
  detector_id TEXT NOT NULL,
  signal      TEXT NOT NULL,
  cohort      TEXT NOT NULL,
  window_label TEXT NOT NULL,
  true_positives INTEGER NOT NULL DEFAULT 0,
  false_positives INTEGER NOT NULL DEFAULT 0,
  false_negatives INTEGER NOT NULL DEFAULT 0,
  observations INTEGER NOT NULL DEFAULT 0,
  target_fpr  REAL NOT NULL DEFAULT 0,
  observed_fpr REAL NOT NULL DEFAULT 0,
  PRIMARY KEY (detector_id, signal, cohort, window_label)
);

CREATE TABLE IF NOT EXISTS events (
  offset_id  INTEGER PRIMARY KEY AUTOINCREMENT,
  topic      TEXT NOT NULL,
  partition_key TEXT NOT NULL,
  payload    TEXT NOT NULL,
  produced_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_topic ON events (topic, offset_id);
"""


def _dumps(value: Any) -> str:
    return json.dumps(value, default=str, separators=(",", ":"))


def _loads(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


class ControlPlane:
    """Thin repository over the control-plane database."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else settings().db_path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self.migrate()

    # -- connection handling ------------------------------------------------

    @property
    def conn(self) -> sqlite3.Connection:
        existing = getattr(self._local, "conn", None)
        if existing is None:
            existing = sqlite3.connect(str(self.path), timeout=30, isolation_level=None)
            existing.row_factory = sqlite3.Row
            existing.execute("PRAGMA foreign_keys=ON")
            self._local.conn = existing
        return existing

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except Exception:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

    def migrate(self) -> None:
        self.conn.executescript(SCHEMA)

    def reset(self) -> None:
        """Drop every table and rebuild. Used by ``ds reset`` and the tests."""
        cur = self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        names = [r["name"] for r in cur.fetchall()]
        self.conn.execute("PRAGMA foreign_keys=OFF")
        for name in names:
            if name != "sqlite_sequence":
                self.conn.execute('DROP TABLE IF EXISTS "' + name + '"')
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.migrate()

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return list(self.conn.execute(sql, tuple(params)).fetchall())

    def one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, tuple(params)).fetchone()

    def scalar(self, sql: str, params: Iterable[Any] = (), default: Any = 0) -> Any:
        row = self.one(sql, params)
        if row is None or row[0] is None:
            return default
        return row[0]

    def execute(self, sql: str, params: Iterable[Any] = ()) -> None:
        self.conn.execute(sql, tuple(params))

    def executemany(self, sql: str, rows: Iterable[Iterable[Any]]) -> None:
        self.conn.executemany(sql, [tuple(r) for r in rows])

    # -- assets -------------------------------------------------------------

    def upsert_asset(self, asset: Asset, now: datetime) -> None:
        self.execute(
            """
            INSERT INTO assets (asset_id, warehouse, database_name, schema_name, table_name,
                                layer, kind, tier, owner_team, dbt_model_path, repo,
                                is_quarantined, query_count_30d, distinct_consumers_30d,
                                created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(asset_id) DO UPDATE SET
              layer=excluded.layer, kind=excluded.kind, tier=excluded.tier,
              owner_team=excluded.owner_team, dbt_model_path=excluded.dbt_model_path,
              repo=excluded.repo, query_count_30d=excluded.query_count_30d,
              distinct_consumers_30d=excluded.distinct_consumers_30d,
              updated_at=excluded.updated_at
            """,
            (
                asset.asset_id, asset.warehouse, asset.database_name, asset.schema_name,
                asset.table_name, str(asset.layer), str(asset.kind), asset.tier,
                asset.owner_team, asset.dbt_model_path, asset.repo,
                int(asset.is_quarantined), asset.query_count_30d, asset.distinct_consumers_30d,
                iso(now), iso(now),
            ),
        )
        for col in asset.columns:
            self.execute(
                """
                INSERT INTO asset_columns (asset_id, column_name, data_type, is_nullable, ordinal, contains_pii)
                VALUES (?,?,?,?,?,?)
                ON CONFLICT(asset_id, column_name) DO UPDATE SET
                  data_type=excluded.data_type, is_nullable=excluded.is_nullable,
                  ordinal=excluded.ordinal, contains_pii=excluded.contains_pii
                """,
                (asset.asset_id, col.column_name, col.data_type, int(col.is_nullable),
                 col.ordinal, int(col.contains_pii)),
            )

    def get_asset(self, asset_id: str, *, with_columns: bool = True) -> Asset | None:
        row = self.one("SELECT * FROM assets WHERE asset_id = ?", (asset_id,))
        if row is None:
            return None
        return self._asset_from_row(row, with_columns=with_columns)

    def _asset_from_row(self, row: sqlite3.Row, *, with_columns: bool = True) -> Asset:
        columns: list[AssetColumn] = []
        if with_columns:
            for c in self.query(
                "SELECT * FROM asset_columns WHERE asset_id = ? ORDER BY ordinal", (row["asset_id"],)
            ):
                columns.append(
                    AssetColumn(
                        column_name=c["column_name"],
                        data_type=c["data_type"],
                        is_nullable=bool(c["is_nullable"]),
                        ordinal=c["ordinal"],
                        contains_pii=bool(c["contains_pii"]),
                    )
                )
        return Asset(
            asset_id=row["asset_id"],
            warehouse=row["warehouse"],
            database_name=row["database_name"],
            schema_name=row["schema_name"],
            table_name=row["table_name"],
            layer=row["layer"],
            kind=row["kind"],
            tier=row["tier"],
            owner_team=row["owner_team"],
            dbt_model_path=row["dbt_model_path"],
            repo=row["repo"],
            is_quarantined=bool(row["is_quarantined"]),
            query_count_30d=row["query_count_30d"],
            distinct_consumers_30d=row["distinct_consumers_30d"],
            columns=columns,
        )

    def list_assets(self, *, layer: str | None = None, tier: int | None = None,
                    search: str | None = None, limit: int = 500) -> list[Asset]:
        clauses, params = [], []
        if layer:
            clauses.append("layer = ?")
            params.append(layer)
        if tier:
            clauses.append("tier = ?")
            params.append(tier)
        if search:
            clauses.append("asset_id LIKE ?")
            params.append("%" + search + "%")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        rows = self.query("SELECT * FROM assets" + where + " ORDER BY tier, asset_id LIMIT ?", params)
        return [self._asset_from_row(r, with_columns=False) for r in rows]

    # -- lineage ------------------------------------------------------------

    def upsert_edges(self, edges: Iterable[LineageEdge], now: datetime) -> int:
        count = 0
        for e in edges:
            self.execute(
                """
                INSERT INTO lineage_edges (src_asset_id, src_column, dst_asset_id, dst_column,
                    transform_type, transform_expr, source, confidence, model_ref,
                    first_seen_at, last_seen_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(src_asset_id, src_column, dst_asset_id, dst_column, model_ref, source)
                DO UPDATE SET last_seen_at=excluded.last_seen_at,
                              confidence=excluded.confidence,
                              transform_type=excluded.transform_type,
                              transform_expr=excluded.transform_expr
                """,
                (e.src_asset_id, e.src_column, e.dst_asset_id, e.dst_column,
                 str(e.transform_type), e.transform_expr, e.source, e.confidence,
                 e.model_ref, iso(now), iso(now)),
            )
            count += 1
        return count

    def edges_from(self, asset_id: str, column: str | None) -> list[LineageEdge]:
        rows = self.query(
            """
            SELECT * FROM lineage_edges
            WHERE src_asset_id = ? AND (src_column = ? OR src_column IS NULL)
            """,
            (asset_id, column),
        )
        return [self._edge_from_row(r) for r in rows]

    def edges_into(self, asset_id: str, column: str | None) -> list[LineageEdge]:
        rows = self.query(
            """
            SELECT * FROM lineage_edges
            WHERE dst_asset_id = ? AND (dst_column = ? OR dst_column IS NULL OR ? IS NULL)
            """,
            (asset_id, column, column),
        )
        return [self._edge_from_row(r) for r in rows]

    @staticmethod
    def _edge_from_row(row: sqlite3.Row) -> LineageEdge:
        return LineageEdge(
            src_asset_id=row["src_asset_id"],
            src_column=row["src_column"],
            dst_asset_id=row["dst_asset_id"],
            dst_column=row["dst_column"],
            transform_type=row["transform_type"],
            transform_expr=row["transform_expr"],
            source=row["source"],
            confidence=row["confidence"],
            model_ref=row["model_ref"],
        )

    def all_edges(self) -> list[LineageEdge]:
        return [self._edge_from_row(r) for r in self.query("SELECT * FROM lineage_edges")]

    # -- profiles -----------------------------------------------------------

    def insert_profiles(self, profiles: Iterable[ColumnProfile]) -> int:
        rows = []
        for p in profiles:
            rows.append(
                (
                    p.profile_id, p.asset_id, p.column_name, iso(p.window_start), iso(p.window_end),
                    p.row_count, p.sampled_rows, p.freshness_seconds, p.null_count,
                    p.distinct_estimate,
                    p.numeric_sketch, p.string_length_sketch,
                    _dumps(p.categorical_topk) if p.categorical_topk is not None else None,
                    p.new_category_count, p.min_value_hash, p.max_value_hash,
                    p.referential_violation_count, p.schema_hash, int(p.degraded),
                    p.profiler_version, iso(p.computed_at),
                )
            )
        self.executemany(
            """
            INSERT OR REPLACE INTO profiles (profile_id, asset_id, column_name, window_start,
                window_end, row_count, sampled_rows, freshness_seconds, null_count,
                distinct_estimate, numeric_sketch, string_length_sketch, categorical_topk,
                new_category_count, min_value_hash, max_value_hash,
                referential_violation_count, schema_hash, degraded, profiler_version, computed_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            rows,
        )
        return len(rows)

    def profile_series(self, asset_id: str, column: str | None, *, limit: int = 120,
                       before: datetime | None = None) -> list[ColumnProfile]:
        sql = "SELECT * FROM profiles WHERE asset_id = ? AND column_name IS ?"
        params: list[Any] = [asset_id, column]
        if before is not None:
            sql += " AND window_end < ?"
            params.append(iso(before))
        sql += " ORDER BY window_end DESC LIMIT ?"
        params.append(limit)
        rows = self.query(sql, params)
        return [self._profile_from_row(r) for r in reversed(rows)]

    @staticmethod
    def _profile_from_row(row: sqlite3.Row) -> ColumnProfile:
        return ColumnProfile(
            profile_id=row["profile_id"],
            asset_id=row["asset_id"],
            column_name=row["column_name"],
            window_start=utc(row["window_start"]),
            window_end=utc(row["window_end"]),
            row_count=row["row_count"],
            sampled_rows=row["sampled_rows"],
            freshness_seconds=row["freshness_seconds"],
            null_count=row["null_count"],
            distinct_estimate=row["distinct_estimate"],
            numeric_sketch=row["numeric_sketch"],
            string_length_sketch=row["string_length_sketch"],
            categorical_topk=_loads(row["categorical_topk"], None),
            new_category_count=row["new_category_count"],
            min_value_hash=row["min_value_hash"],
            max_value_hash=row["max_value_hash"],
            referential_violation_count=row["referential_violation_count"],
            schema_hash=row["schema_hash"],
            degraded=bool(row["degraded"]),
            profiler_version=row["profiler_version"],
            computed_at=utc(row["computed_at"]),
        )

    def profiled_series_keys(self) -> list[tuple[str, str | None]]:
        rows = self.query("SELECT DISTINCT asset_id, column_name FROM profiles")
        return [(r["asset_id"], r["column_name"]) for r in rows]

    # -- anomalies ----------------------------------------------------------

    def insert_anomalies(self, anomalies: Iterable[Anomaly]) -> int:
        rows = [
            (
                a.anomaly_id, a.profile_id, a.asset_id, a.column_name, str(a.signal),
                a.detector_id, a.detector_version, a.score, a.p_value, a.ci_low, a.ci_high,
                a.observed_value, a.baseline_value, a.consecutive_breaches, int(a.provisional),
                a.incident_id, a.explanation, iso(a.detected_at),
            )
            for a in anomalies
        ]
        self.executemany(
            """
            INSERT OR REPLACE INTO anomalies (anomaly_id, profile_id, asset_id, column_name,
                signal, detector_id, detector_version, score, p_value, ci_low, ci_high,
                observed_value, baseline_value, consecutive_breaches, provisional,
                incident_id, explanation, detected_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            rows,
        )
        return len(rows)

    @staticmethod
    def _anomaly_from_row(row: sqlite3.Row) -> Anomaly:
        return Anomaly(
            anomaly_id=row["anomaly_id"],
            profile_id=row["profile_id"],
            asset_id=row["asset_id"],
            column_name=row["column_name"],
            signal=row["signal"],
            detector_id=row["detector_id"],
            detector_version=row["detector_version"],
            score=row["score"],
            p_value=row["p_value"],
            ci_low=row["ci_low"],
            ci_high=row["ci_high"],
            observed_value=row["observed_value"],
            baseline_value=row["baseline_value"],
            consecutive_breaches=row["consecutive_breaches"],
            provisional=bool(row["provisional"]),
            incident_id=row["incident_id"],
            explanation=row["explanation"],
            detected_at=utc(row["detected_at"]),
        )

    def unassigned_anomalies(self) -> list[Anomaly]:
        rows = self.query(
            "SELECT * FROM anomalies WHERE incident_id IS NULL ORDER BY detected_at"
        )
        return [self._anomaly_from_row(r) for r in rows]

    def anomalies_for_incident(self, incident_id: str) -> list[Anomaly]:
        rows = self.query(
            "SELECT * FROM anomalies WHERE incident_id = ? ORDER BY detected_at", (incident_id,)
        )
        return [self._anomaly_from_row(r) for r in rows]

    def anomalies_for_asset(self, asset_id: str, limit: int = 50) -> list[Anomaly]:
        rows = self.query(
            "SELECT * FROM anomalies WHERE asset_id = ? ORDER BY detected_at DESC LIMIT ?",
            (asset_id, limit),
        )
        return [self._anomaly_from_row(r) for r in rows]

    def assign_anomalies(self, incident_id: str, anomaly_ids: Iterable[str]) -> None:
        self.executemany(
            "UPDATE anomalies SET incident_id = ? WHERE anomaly_id = ?",
            [(incident_id, aid) for aid in anomaly_ids],
        )

    # -- incidents ----------------------------------------------------------

    def upsert_incident(self, inc: Incident) -> None:
        self.execute(
            """
            INSERT INTO incidents (incident_id, status, severity, title, root_asset_id, root_column,
                anomaly_count, occurrence_count, narrative, first_detected_at, rca_started_at,
                rca_completed_at, resolved_at, llm_cost_usd, token_count, trace_id,
                prompt_version, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(incident_id) DO UPDATE SET
              status=excluded.status, severity=excluded.severity, title=excluded.title,
              anomaly_count=excluded.anomaly_count, occurrence_count=excluded.occurrence_count,
              narrative=excluded.narrative, rca_started_at=excluded.rca_started_at,
              rca_completed_at=excluded.rca_completed_at, resolved_at=excluded.resolved_at,
              llm_cost_usd=excluded.llm_cost_usd, token_count=excluded.token_count,
              trace_id=excluded.trace_id, prompt_version=excluded.prompt_version
            """,
            (
                inc.incident_id, str(inc.status), inc.severity, inc.title, inc.root_asset_id,
                inc.root_column, inc.anomaly_count, inc.occurrence_count, inc.narrative,
                iso(inc.first_detected_at),
                iso(inc.rca_started_at) if inc.rca_started_at else None,
                iso(inc.rca_completed_at) if inc.rca_completed_at else None,
                iso(inc.resolved_at) if inc.resolved_at else None,
                inc.llm_cost_usd, inc.token_count, inc.trace_id, inc.prompt_version,
                iso(inc.created_at),
            ),
        )

    def set_incident_status(self, incident_id: str, status: IncidentStatus | str,
                            *, resolved_at: datetime | None = None) -> None:
        if resolved_at is not None:
            self.execute(
                "UPDATE incidents SET status = ?, resolved_at = ? WHERE incident_id = ?",
                (str(status), iso(resolved_at), incident_id),
            )
        else:
            self.execute(
                "UPDATE incidents SET status = ? WHERE incident_id = ?", (str(status), incident_id)
            )

    @staticmethod
    def _incident_from_row(row: sqlite3.Row) -> Incident:
        return Incident(
            incident_id=row["incident_id"],
            status=row["status"],
            severity=row["severity"],
            title=row["title"],
            root_asset_id=row["root_asset_id"],
            root_column=row["root_column"],
            anomaly_count=row["anomaly_count"],
            occurrence_count=row["occurrence_count"],
            narrative=row["narrative"],
            first_detected_at=utc(row["first_detected_at"]),
            rca_started_at=utc(row["rca_started_at"]) if row["rca_started_at"] else None,
            rca_completed_at=utc(row["rca_completed_at"]) if row["rca_completed_at"] else None,
            resolved_at=utc(row["resolved_at"]) if row["resolved_at"] else None,
            llm_cost_usd=row["llm_cost_usd"],
            token_count=row["token_count"],
            trace_id=row["trace_id"],
            prompt_version=row["prompt_version"],
            created_at=utc(row["created_at"]),
        )

    def get_incident(self, incident_id: str) -> Incident | None:
        row = self.one("SELECT * FROM incidents WHERE incident_id = ?", (incident_id,))
        return self._incident_from_row(row) if row else None

    def list_incidents(self, *, status: str | None = None, severity: int | None = None,
                       asset_id: str | None = None, limit: int = 100) -> list[Incident]:
        clauses, params = [], []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if severity:
            clauses.append("severity = ?")
            params.append(severity)
        if asset_id:
            clauses.append("root_asset_id = ?")
            params.append(asset_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        rows = self.query(
            "SELECT * FROM incidents" + where + " ORDER BY severity ASC, first_detected_at DESC LIMIT ?",
            params,
        )
        return [self._incident_from_row(r) for r in rows]

    def open_incident_for(self, root_asset_id: str, since: datetime) -> Incident | None:
        row = self.one(
            """
            SELECT * FROM incidents
            WHERE root_asset_id = ? AND first_detected_at >= ?
              AND status NOT IN ('resolved','dismissed')
            ORDER BY first_detected_at DESC LIMIT 1
            """,
            (root_asset_id, iso(since)),
        )
        return self._incident_from_row(row) if row else None

    # -- evidence and hypotheses -------------------------------------------

    def insert_evidence(self, items: Iterable[Evidence]) -> int:
        rows = [
            (e.evidence_id, e.incident_id, str(e.kind), e.ref, e.summary,
             _dumps(e.payload), e.collected_by, iso(e.collected_at))
            for e in items
        ]
        self.executemany(
            """
            INSERT OR REPLACE INTO evidence (evidence_id, incident_id, kind, ref, summary,
                payload, collected_by, collected_at)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            rows,
        )
        return len(rows)

    def evidence_for(self, incident_id: str) -> list[Evidence]:
        rows = self.query(
            "SELECT * FROM evidence WHERE incident_id = ? ORDER BY collected_at", (incident_id,)
        )
        return [
            Evidence(
                evidence_id=r["evidence_id"],
                incident_id=r["incident_id"],
                kind=r["kind"],
                ref=r["ref"],
                summary=r["summary"],
                payload=_loads(r["payload"], {}),
                collected_by=r["collected_by"],
                collected_at=utc(r["collected_at"]),
            )
            for r in rows
        ]

    def replace_hypotheses(self, incident_id: str, hypotheses: Iterable[Hypothesis],
                           now: datetime) -> None:
        self.execute("DELETE FROM hypotheses WHERE incident_id = ?", (incident_id,))
        self.executemany(
            """
            INSERT INTO hypotheses (hypothesis_id, incident_id, rank, cause_class, statement,
                claims, confidence, calibrated_confidence, status, rejection_reason,
                evidence_requests, round_index, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            [
                (
                    h.hypothesis_id, h.incident_id, h.rank, str(h.cause_class), h.statement,
                    _dumps([c.model_dump() for c in h.claims]), h.confidence,
                    h.calibrated_confidence, h.status, h.rejection_reason,
                    _dumps(h.evidence_requests), h.round_index, iso(now),
                )
                for h in hypotheses
            ],
        )

    def hypotheses_for(self, incident_id: str) -> list[Hypothesis]:
        rows = self.query(
            "SELECT * FROM hypotheses WHERE incident_id = ? ORDER BY rank", (incident_id,)
        )
        out = []
        for r in rows:
            out.append(
                Hypothesis(
                    hypothesis_id=r["hypothesis_id"],
                    incident_id=r["incident_id"],
                    rank=r["rank"],
                    cause_class=r["cause_class"],
                    statement=r["statement"],
                    claims=list(_loads(r["claims"], [])),
                    confidence=r["confidence"],
                    calibrated_confidence=r["calibrated_confidence"],
                    status=r["status"],
                    rejection_reason=r["rejection_reason"],
                    evidence_requests=_loads(r["evidence_requests"], []),
                    round_index=r["round_index"],
                )
            )
        return out

    # -- blast radius -------------------------------------------------------

    def replace_blast_radius(self, incident_id: str, entries: Iterable[BlastRadiusEntry]) -> None:
        self.execute("DELETE FROM blast_radius WHERE incident_id = ?", (incident_id,))
        self.executemany(
            """
            INSERT OR REPLACE INTO blast_radius (incident_id, asset_id, column_name, depth,
                asset_kind, criticality, confidence, tier, layer, last_consumed_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            [
                (
                    incident_id, e.asset_id, e.column_name or "", e.depth, str(e.asset_kind),
                    e.criticality, e.confidence, e.tier,
                    str(e.layer) if e.layer else None,
                    iso(e.last_consumed_at) if e.last_consumed_at else None,
                )
                for e in entries
            ],
        )

    def blast_radius_for(self, incident_id: str) -> list[BlastRadiusEntry]:
        rows = self.query(
            """
            SELECT * FROM blast_radius WHERE incident_id = ?
            ORDER BY criticality DESC, depth ASC
            """,
            (incident_id,),
        )
        return [
            BlastRadiusEntry(
                asset_id=r["asset_id"],
                column_name=r["column_name"] or None,
                depth=r["depth"],
                asset_kind=r["asset_kind"],
                criticality=r["criticality"],
                confidence=r["confidence"],
                tier=r["tier"],
                layer=r["layer"],
                last_consumed_at=utc(r["last_consumed_at"]) if r["last_consumed_at"] else None,
            )
            for r in rows
        ]

    # -- actions ------------------------------------------------------------

    def upsert_action(self, action: Action) -> None:
        self.execute(
            """
            INSERT INTO actions (action_id, incident_id, action_type, risk_tier, policy_version,
                params, rationale, status, approved_by, approved_at, external_ref, result, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(action_id) DO UPDATE SET
              status=excluded.status, approved_by=excluded.approved_by,
              approved_at=excluded.approved_at, external_ref=excluded.external_ref,
              result=excluded.result
            """,
            (
                action.action_id, action.incident_id, str(action.action_type),
                str(action.risk_tier), action.policy_version, _dumps(action.params),
                action.rationale, str(action.status), action.approved_by,
                iso(action.approved_at) if action.approved_at else None,
                action.external_ref, _dumps(action.result), iso(action.created_at),
            ),
        )

    @staticmethod
    def _action_from_row(row: sqlite3.Row) -> Action:
        return Action(
            action_id=row["action_id"],
            incident_id=row["incident_id"],
            action_type=row["action_type"],
            risk_tier=row["risk_tier"],
            policy_version=row["policy_version"],
            params=_loads(row["params"], {}),
            rationale=row["rationale"],
            status=row["status"],
            approved_by=row["approved_by"],
            approved_at=utc(row["approved_at"]) if row["approved_at"] else None,
            external_ref=row["external_ref"],
            result=_loads(row["result"], {}),
            created_at=utc(row["created_at"]),
        )

    def get_action(self, action_id: str) -> Action | None:
        row = self.one("SELECT * FROM actions WHERE action_id = ?", (action_id,))
        return self._action_from_row(row) if row else None

    def actions_for(self, incident_id: str) -> list[Action]:
        rows = self.query(
            "SELECT * FROM actions WHERE incident_id = ? ORDER BY created_at", (incident_id,)
        )
        return [self._action_from_row(r) for r in rows]

    def list_actions(self, *, status: str | None = None, risk_tier: str | None = None,
                     limit: int = 100) -> list[Action]:
        clauses, params = [], []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if risk_tier:
            clauses.append("risk_tier = ?")
            params.append(risk_tier)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        rows = self.query("SELECT * FROM actions" + where + " ORDER BY created_at DESC LIMIT ?", params)
        return [self._action_from_row(r) for r in rows]

    # -- dispositions and audit --------------------------------------------

    def insert_disposition(self, d: Disposition) -> None:
        self.execute(
            """
            INSERT OR REPLACE INTO dispositions (disposition_id, incident_id, verdict,
                correct_cause_class, notes, user_id, created_at)
            VALUES (?,?,?,?,?,?,?)
            """,
            (d.disposition_id, d.incident_id, str(d.verdict),
             str(d.correct_cause_class) if d.correct_cause_class else None,
             d.notes, d.user_id, iso(d.created_at)),
        )

    def disposition_for(self, incident_id: str) -> Disposition | None:
        row = self.one(
            "SELECT * FROM dispositions WHERE incident_id = ? ORDER BY created_at DESC LIMIT 1",
            (incident_id,),
        )
        if row is None:
            return None
        return Disposition(
            disposition_id=row["disposition_id"],
            incident_id=row["incident_id"],
            verdict=row["verdict"],
            correct_cause_class=row["correct_cause_class"],
            notes=row["notes"],
            user_id=row["user_id"],
            created_at=utc(row["created_at"]),
        )

    def audit(self, actor: str, actor_kind: str, action: str, target: str,
              detail: dict[str, Any], at: datetime) -> None:
        """Append-only. There is no update or delete counterpart, by design."""
        self.execute(
            "INSERT INTO audit_log (actor, actor_kind, action, target, detail, at) VALUES (?,?,?,?,?,?)",
            (actor, actor_kind, action, target, _dumps(detail), iso(at)),
        )

    def audit_tail(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.query("SELECT * FROM audit_log ORDER BY audit_id DESC LIMIT ?", (limit,))
        return [
            {
                "audit_id": r["audit_id"],
                "actor": r["actor"],
                "actor_kind": r["actor_kind"],
                "action": r["action"],
                "target": r["target"],
                "detail": _loads(r["detail"], {}),
                "at": r["at"],
            }
            for r in rows
        ]

    # -- agent trace --------------------------------------------------------

    def record_step(self, incident_id: str, seq: int, node: str, *, model: str | None,
                    status: str, duration_ms: int, tokens_in: int, tokens_out: int,
                    usd: float, summary: str, payload: dict[str, Any], at: datetime) -> None:
        self.execute(
            """
            INSERT OR REPLACE INTO agent_steps (incident_id, seq, node, model, status,
                duration_ms, tokens_in, tokens_out, usd, summary, payload, at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (incident_id, seq, node, model, status, duration_ms, tokens_in, tokens_out,
             usd, summary, _dumps(payload), iso(at)),
        )

    def steps_for(self, incident_id: str) -> list[dict[str, Any]]:
        rows = self.query(
            "SELECT * FROM agent_steps WHERE incident_id = ? ORDER BY seq", (incident_id,)
        )
        return [
            {
                "seq": r["seq"],
                "node": r["node"],
                "model": r["model"],
                "status": r["status"],
                "duration_ms": r["duration_ms"],
                "tokens_in": r["tokens_in"],
                "tokens_out": r["tokens_out"],
                "usd": r["usd"],
                "summary": r["summary"],
                "payload": _loads(r["payload"], {}),
                "at": r["at"],
            }
            for r in rows
        ]

    def save_checkpoint(self, incident_id: str, node: str, state: dict[str, Any],
                        at: datetime) -> None:
        self.execute(
            """
            INSERT INTO agent_checkpoints (incident_id, node, state, updated_at) VALUES (?,?,?,?)
            ON CONFLICT(incident_id) DO UPDATE SET
              node=excluded.node, state=excluded.state, updated_at=excluded.updated_at
            """,
            (incident_id, node, _dumps(state), iso(at)),
        )

    def load_checkpoint(self, incident_id: str) -> dict[str, Any] | None:
        row = self.one("SELECT * FROM agent_checkpoints WHERE incident_id = ?", (incident_id,))
        if row is None:
            return None
        return {"node": row["node"], "state": _loads(row["state"], {}), "updated_at": row["updated_at"]}

    # -- events (Kafka stand-in) -------------------------------------------

    def publish(self, topic: str, key: str, payload: dict[str, Any], at: datetime) -> int:
        cur = self.conn.execute(
            "INSERT INTO events (topic, partition_key, payload, produced_at) VALUES (?,?,?,?)",
            (topic, key, _dumps(payload), iso(at)),
        )
        return int(cur.lastrowid or 0)

    def consume(self, topic: str, *, after: int = 0, limit: int = 1000) -> list[dict[str, Any]]:
        rows = self.query(
            "SELECT * FROM events WHERE topic = ? AND offset_id > ? ORDER BY offset_id LIMIT ?",
            (topic, after, limit),
        )
        return [
            {
                "offset": r["offset_id"],
                "topic": r["topic"],
                "key": r["partition_key"],
                "payload": _loads(r["payload"], {}),
                "at": r["produced_at"],
            }
            for r in rows
        ]

    # -- meta ---------------------------------------------------------------

    def set_meta(self, key: str, value: Any) -> None:
        self.execute(
            "INSERT INTO meta (key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, _dumps(value)),
        )

    def get_meta(self, key: str, default: Any = None) -> Any:
        row = self.one("SELECT value FROM meta WHERE key = ?", (key,))
        return _loads(row["value"], default) if row else default

    def topic_counts(self) -> dict[str, int]:
        rows = self.query("SELECT topic, COUNT(*) AS n FROM events GROUP BY topic ORDER BY topic")
        return {r["topic"]: r["n"] for r in rows}


_DEFAULT: ControlPlane | None = None


def control_plane() -> ControlPlane:
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = ControlPlane()
    return _DEFAULT


def reset_control_plane() -> None:
    global _DEFAULT
    _DEFAULT = None
