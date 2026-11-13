# 03 — Data Model

## 1. Kafka topics

| Topic | Key | Partitions | Retention | Producer | Consumers |
|---|---|---|---|---|---|
| `ds.warehouse.write.v1` | `table_id` | 12 | 7 d | ingestion-gateway | profiler |
| `ds.query.executed.v1` | `warehouse_id` | 6 | 7 d | ingestion-gateway | lineage-builder |
| `ds.job.run.v1` | `job_id` | 6 | 30 d | ingestion-gateway (dbt/Airflow) | profiler, orchestrator (evidence) |
| `ds.git.event.v1` | `repo` | 3 | 90 d | ingestion-gateway (GitHub webhook) | lineage-builder, orchestrator (evidence) |
| `ds.schema.changed.v1` | `table_id` | 6 | 90 d | ingestion-gateway | lineage-builder, detector |
| `ds.profile.computed.v1` | `table_id` | 12 | 7 d | profiler | detector |
| `ds.anomaly.detected.v1` | `table_id` | 12 | 30 d | detector | correlator |
| `ds.incident.created.v1` | `incident_id` | 6 | 90 d | correlator | orchestrator, api |
| `ds.incident.updated.v1` | `incident_id` | 6 | 90 d | orchestrator, executor | api (WebSocket fanout) |
| `ds.action.requested.v1` | `incident_id` | 3 | 90 d | policy-gate | action-executor |
| `ds.action.completed.v1` | `incident_id` | 3 | 90 d | action-executor | verification-worker, api |

Ordering guarantee needed: per `table_id` for profiles/anomalies, per `incident_id` for incident lifecycle. Both satisfied by key-based partitioning.

## 2. Core Avro schemas

`ds.profile.computed.v1`:

```json
{
  "type": "record",
  "name": "ProfileComputed",
  "namespace": "datasentry.v1",
  "fields": [
    {"name": "profile_id", "type": "string", "doc": "sha256(table_id|column|window_end|profiler_version)"},
    {"name": "table_id", "type": "string"},
    {"name": "column_name", "type": ["null", "string"], "default": null,
     "doc": "null => table-level profile (volume, freshness)"},
    {"name": "window_start", "type": {"type": "long", "logicalType": "timestamp-millis"}},
    {"name": "window_end", "type": {"type": "long", "logicalType": "timestamp-millis"}},
    {"name": "row_count", "type": "long"},
    {"name": "freshness_seconds", "type": "long"},
    {"name": "null_count", "type": ["null", "long"], "default": null},
    {"name": "distinct_estimate", "type": ["null", "long"], "default": null, "doc": "HyperLogLog"},
    {"name": "numeric_sketch", "type": ["null", "bytes"], "default": null, "doc": "serialized t-digest"},
    {"name": "categorical_topk", "type": ["null", {"type": "map", "values": "long"}], "default": null},
    {"name": "new_category_count", "type": ["null", "long"], "default": null},
    {"name": "min_value_hash", "type": ["null", "string"], "default": null},
    {"name": "max_value_hash", "type": ["null", "string"], "default": null},
    {"name": "referential_violation_count", "type": ["null", "long"], "default": null},
    {"name": "degraded", "type": "boolean", "default": false},
    {"name": "profiler_version", "type": "string"},
    {"name": "computed_at", "type": {"type": "long", "logicalType": "timestamp-millis"}}
  ]
}
```

**Note the absence of raw values.** `min_value_hash` / `max_value_hash` are salted hashes used only for change detection. This is ADR-010 made concrete, and it is the answer to "how do you handle PII?"

`ds.anomaly.detected.v1`:

```json
{
  "type": "record",
  "name": "AnomalyDetected",
  "namespace": "datasentry.v1",
  "fields": [
    {"name": "anomaly_id", "type": "string"},
    {"name": "profile_id", "type": "string"},
    {"name": "table_id", "type": "string"},
    {"name": "column_name", "type": ["null", "string"], "default": null},
    {"name": "signal", "type": {"type": "enum", "name": "Signal",
      "symbols": ["VOLUME","FRESHNESS","NULL_RATE","CARDINALITY","NUMERIC_DRIFT",
                  "CATEGORICAL_DRIFT","NEW_CATEGORY","REFERENTIAL","SCHEMA","MULTIVARIATE"]}},
    {"name": "detector_id", "type": "string"},
    {"name": "detector_version", "type": "string"},
    {"name": "score", "type": "double"},
    {"name": "p_value", "type": ["null", "double"], "default": null},
    {"name": "conformal_interval_low", "type": ["null", "double"], "default": null},
    {"name": "conformal_interval_high", "type": ["null", "double"], "default": null},
    {"name": "observed_value", "type": "double"},
    {"name": "baseline_value", "type": "double"},
    {"name": "consecutive_breaches", "type": "int"},
    {"name": "detected_at", "type": {"type": "long", "logicalType": "timestamp-millis"}}
  ]
}
```

Schema Registry compatibility mode: `BACKWARD`. All new fields must have defaults.

## 3. Delta lake layers

```
s3://datasentry-lake/
  bronze/
    warehouse_writes/      partition: dt
    query_history/         partition: dt
    job_runs/              partition: dt
    git_events/            partition: dt
  silver/
    column_profiles/       partition: dt, table_id_bucket   ← the workhorse table
    table_profiles/        partition: dt
    schema_snapshots/      partition: dt
  gold/
    detector_training/     partition: dt
    incident_features/     partition: dt
```

`silver.column_profiles` is the single most important table. Design notes:
- Partition by `dt` and a 64-way bucket of `table_id` to avoid small-file explosion at 200k columns × 24 cycles/day.
- `OPTIMIZE ... ZORDER BY (table_id, column_name)` nightly.
- Retain 400 days (needed for year-over-year seasonality baselines).
- Delta time travel is the mechanism for "last known good" snapshots used by quarantine/pin actions (ADR-014).

## 4. Postgres control plane (DDL)

```sql
-- ─── Assets and lineage ────────────────────────────────────────────────
CREATE TABLE assets (
  asset_id        TEXT PRIMARY KEY,               -- warehouse.db.schema.table
  warehouse       TEXT NOT NULL,
  database_name   TEXT NOT NULL,
  schema_name     TEXT NOT NULL,
  table_name      TEXT NOT NULL,
  layer           TEXT CHECK (layer IN ('bronze','silver','gold')),
  tier            SMALLINT NOT NULL DEFAULT 3,    -- 1 = business critical
  owner_team      TEXT,
  dbt_model_path  TEXT,
  repo            TEXT,
  is_quarantined  BOOLEAN NOT NULL DEFAULT FALSE,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE asset_columns (
  asset_id     TEXT REFERENCES assets(asset_id) ON DELETE CASCADE,
  column_name  TEXT NOT NULL,
  data_type    TEXT NOT NULL,
  is_nullable  BOOLEAN NOT NULL,
  ordinal      INT NOT NULL,
  contains_pii BOOLEAN NOT NULL DEFAULT FALSE,
  PRIMARY KEY (asset_id, column_name)
);

CREATE TABLE lineage_edges (
  edge_id        BIGSERIAL PRIMARY KEY,
  src_asset_id   TEXT NOT NULL,
  src_column     TEXT,                            -- NULL = table-level edge
  dst_asset_id   TEXT NOT NULL,
  dst_column     TEXT,
  transform_type TEXT NOT NULL,                   -- IDENTITY|CAST|AGG|JOIN_KEY|EXPR|WINDOW|UNKNOWN
  transform_expr TEXT,
  source         TEXT NOT NULL CHECK (source IN ('declared','observed')),
  confidence     REAL NOT NULL DEFAULT 1.0,
  model_ref      TEXT,                            -- dbt model / job id
  first_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (src_asset_id, src_column, dst_asset_id, dst_column, model_ref, source)
);
CREATE INDEX idx_lineage_src ON lineage_edges (src_asset_id, src_column);
CREATE INDEX idx_lineage_dst ON lineage_edges (dst_asset_id, dst_column);

-- Materialized transitive closure, refreshed incrementally on DDL/lineage change
CREATE TABLE lineage_closure (
  src_asset_id TEXT NOT NULL, src_column TEXT,
  dst_asset_id TEXT NOT NULL, dst_column TEXT,
  depth        SMALLINT NOT NULL,
  min_confidence REAL NOT NULL,
  PRIMARY KEY (src_asset_id, src_column, dst_asset_id, dst_column)
);
CREATE INDEX idx_closure_src ON lineage_closure (src_asset_id, src_column, depth);

-- ─── Detection ─────────────────────────────────────────────────────────
CREATE TABLE anomalies (
  anomaly_id      TEXT PRIMARY KEY,
  asset_id        TEXT NOT NULL REFERENCES assets(asset_id),
  column_name     TEXT,
  signal          TEXT NOT NULL,
  detector_id     TEXT NOT NULL,
  detector_version TEXT NOT NULL,
  score           DOUBLE PRECISION NOT NULL,
  observed_value  DOUBLE PRECISION NOT NULL,
  baseline_value  DOUBLE PRECISION NOT NULL,
  ci_low          DOUBLE PRECISION,
  ci_high         DOUBLE PRECISION,
  consecutive_breaches INT NOT NULL DEFAULT 1,
  incident_id     UUID,
  detected_at     TIMESTAMPTZ NOT NULL
);
CREATE INDEX idx_anomalies_incident ON anomalies (incident_id);
CREATE INDEX idx_anomalies_asset_time ON anomalies (asset_id, detected_at DESC);

-- ─── Incidents and reasoning ───────────────────────────────────────────
CREATE TYPE incident_status AS ENUM
  ('detected','rca_running','rca_complete','awaiting_approval','remediating',
   'verifying','resolved','escalated','dismissed','budget_exceeded');

CREATE TABLE incidents (
  incident_id     UUID PRIMARY KEY,
  status          incident_status NOT NULL DEFAULT 'detected',
  severity        SMALLINT NOT NULL,             -- 1 (highest) .. 4
  title           TEXT NOT NULL,
  root_asset_id   TEXT NOT NULL REFERENCES assets(asset_id),
  anomaly_count   INT NOT NULL DEFAULT 0,
  occurrence_count INT NOT NULL DEFAULT 1,
  first_detected_at TIMESTAMPTZ NOT NULL,
  rca_started_at  TIMESTAMPTZ,
  rca_completed_at TIMESTAMPTZ,
  resolved_at     TIMESTAMPTZ,
  llm_cost_usd    NUMERIC(10,4) DEFAULT 0,
  token_count     INT DEFAULT 0,
  trace_id        TEXT,                           -- OTel + LangSmith correlation
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_incidents_status ON incidents (status, severity, first_detected_at DESC);

CREATE TABLE evidence (
  evidence_id   UUID PRIMARY KEY,
  incident_id   UUID NOT NULL REFERENCES incidents(incident_id) ON DELETE CASCADE,
  kind          TEXT NOT NULL,   -- GIT_COMMIT|SQL_DIFF|PROFILE_DELTA|QUERY_LOG|
                                 -- DBT_RUN|SCHEMA_CHANGE|LINEAGE_PATH|PAST_INCIDENT
  ref           TEXT NOT NULL,   -- commit sha, query id, profile_id, ...
  summary       TEXT NOT NULL,   -- short human-readable line
  payload       JSONB NOT NULL,  -- structured detail, rendered in the UI
  collected_by  TEXT NOT NULL,   -- tool name
  collected_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_evidence_incident ON evidence (incident_id, kind);

CREATE TABLE hypotheses (
  hypothesis_id  UUID PRIMARY KEY,
  incident_id    UUID NOT NULL REFERENCES incidents(incident_id) ON DELETE CASCADE,
  rank           SMALLINT NOT NULL,
  cause_class    TEXT NOT NULL,   -- UPSTREAM_CODE_CHANGE|SOURCE_SCHEMA_DRIFT|
                                  -- LATE_DATA|VOLUME_SHIFT|JOIN_SEMANTICS|
                                  -- UNIT_CHANGE|DEDUP_BREAK|INFRA_FAILURE|SEASONAL_LEGIT
  statement      TEXT NOT NULL,
  confidence     REAL NOT NULL,
  status         TEXT NOT NULL CHECK (status IN ('proposed','verified','rejected')),
  rejection_reason TEXT,
  evidence_ids   UUID[] NOT NULL DEFAULT '{}',
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE blast_radius (
  incident_id  UUID NOT NULL REFERENCES incidents(incident_id) ON DELETE CASCADE,
  asset_id     TEXT NOT NULL,
  column_name  TEXT,
  depth        SMALLINT NOT NULL,
  asset_kind   TEXT NOT NULL,   -- TABLE|FEATURE_VIEW|DASHBOARD|ML_MODEL|REPORT
  criticality  REAL NOT NULL,
  last_consumed_at TIMESTAMPTZ,
  PRIMARY KEY (incident_id, asset_id, column_name)
);

-- ─── Actions, policy, disposition ──────────────────────────────────────
CREATE TABLE actions (
  action_id     UUID PRIMARY KEY,
  incident_id   UUID NOT NULL REFERENCES incidents(incident_id) ON DELETE CASCADE,
  action_type   TEXT NOT NULL,  -- QUARANTINE_TABLE|PIN_FEATURE_VIEW|OPEN_PR|
                                -- NOTIFY_OWNER|MUTE_ALERT|BACKFILL_REQUEST
  risk_tier     TEXT NOT NULL CHECK (risk_tier IN ('low','medium','high')),
  policy_version TEXT NOT NULL,
  params        JSONB NOT NULL,
  status        TEXT NOT NULL,  -- proposed|approved|rejected|executing|succeeded|failed|reverted
  approved_by   TEXT,
  approved_at   TIMESTAMPTZ,
  external_ref  TEXT,           -- PR URL, snapshot id
  result        JSONB,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE dispositions (
  disposition_id UUID PRIMARY KEY,
  incident_id    UUID NOT NULL REFERENCES incidents(incident_id) ON DELETE CASCADE,
  verdict        TEXT NOT NULL CHECK (verdict IN
                   ('correct_cause','wrong_cause','false_positive','duplicate','expected')),
  correct_cause_class TEXT,
  notes          TEXT,
  user_id        TEXT NOT NULL,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE audit_log (
  audit_id    BIGSERIAL PRIMARY KEY,
  actor       TEXT NOT NULL,      -- user id or service identity
  actor_kind  TEXT NOT NULL CHECK (actor_kind IN ('human','agent','service')),
  action      TEXT NOT NULL,
  target      TEXT NOT NULL,
  detail      JSONB NOT NULL,
  at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

`audit_log` is append-only: revoke `UPDATE`/`DELETE` from the application role. This is a five-minute change that makes the security story real.

## 5. Snowflake gold layer

```sql
CREATE SCHEMA DATASENTRY_GOLD;

CREATE TABLE DATASENTRY_GOLD.FCT_INCIDENTS (
  INCIDENT_ID STRING, SEVERITY NUMBER, STATUS STRING,
  ROOT_ASSET_ID STRING, ROOT_CAUSE_CLASS STRING, ROOT_CAUSE_CONFIDENCE FLOAT,
  ANOMALY_COUNT NUMBER, BLAST_RADIUS_SIZE NUMBER, MAX_DOWNSTREAM_CRITICALITY FLOAT,
  TIME_TO_DETECT_SEC NUMBER, TIME_TO_ROOT_CAUSE_SEC NUMBER, TIME_TO_RESOLVE_SEC NUMBER,
  AUTO_REMEDIATED BOOLEAN, LLM_COST_USD FLOAT,
  DISPOSITION STRING, FIRST_DETECTED_AT TIMESTAMP_NTZ, RESOLVED_AT TIMESTAMP_NTZ
);

CREATE TABLE DATASENTRY_GOLD.FCT_EVIDENCE (...);      -- flattened evidence for audit queries
CREATE TABLE DATASENTRY_GOLD.DIM_ASSETS (...);        -- SCD2 on tier/owner
CREATE TABLE DATASENTRY_GOLD.FCT_DETECTOR_PERF (      -- weekly rollups per detector/signal
  DETECTOR_ID STRING, SIGNAL STRING, WEEK DATE,
  TRUE_POSITIVES NUMBER, FALSE_POSITIVES NUMBER, FALSE_NEGATIVES NUMBER,
  PRECISION FLOAT, RECALL FLOAT, TARGET_FPR FLOAT, OBSERVED_FPR FLOAT
);
```

`FCT_DETECTOR_PERF.OBSERVED_FPR` vs `TARGET_FPR` is the conformal calibration check. If they diverge, the recalibration job runs. Being able to show that table is worth more than any architecture diagram.

## 6. Redis keyspaces

| Key | Type | TTL | Purpose |
|---|---|---|---|
| `ds:dedup:anomaly:{hash}` | string | 24 h | Suppress duplicate anomalies |
| `ds:budget:incident:{id}` | hash | 1 h | Token/dollar spend counters, enforced by the orchestrator |
| `ds:ratelimit:llm` | token bucket | — | Global LLM concurrency cap |
| `ds:mute:{asset_id}` | string | user-set | Active mutes with expiry |
| `ds:closure:{asset}:{col}` | string (JSON) | 10 min | Cached blast-radius traversal |

## 7. Retention

| Store | Retention | Reason |
|---|---|---|
| Kafka | 7–90 d by topic | Replay window for detector backtests |
| Delta silver profiles | 400 d | Year-over-year seasonality baselines |
| Postgres incidents/evidence | 2 y | Operational history |
| Snowflake gold | 7 y | Audit posture |
| S3 agent traces | 1 y | Eval corpus |

## 8. Key query — blast radius

```sql
WITH RECURSIVE downstream AS (
  SELECT e.dst_asset_id, e.dst_column, 1 AS depth, e.confidence AS min_conf
  FROM lineage_edges e
  WHERE e.src_asset_id = :asset_id
    AND (e.src_column = :column OR e.src_column IS NULL)
  UNION ALL
  SELECT e.dst_asset_id, e.dst_column, d.depth + 1, LEAST(d.min_conf, e.confidence)
  FROM lineage_edges e
  JOIN downstream d
    ON e.src_asset_id = d.dst_asset_id
   AND (e.src_column = d.dst_column OR e.src_column IS NULL)
  WHERE d.depth < 8
)
SELECT d.dst_asset_id, d.dst_column, MIN(d.depth) AS depth,
       MAX(d.min_conf) AS confidence, a.tier, a.layer
FROM downstream d
JOIN assets a ON a.asset_id = d.dst_asset_id
GROUP BY 1,2, a.tier, a.layer
ORDER BY a.tier ASC, depth ASC;
```

The `lineage_closure` table exists so the hot path doesn't run this recursion; the recursion is the builder for the closure and the fallback when the closure is stale. Be ready to explain that tradeoff — it's a good interview moment.
