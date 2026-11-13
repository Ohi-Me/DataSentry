-- DataSentry control plane, Postgres 16.
--
-- This file is the authority on types and constraints. The local SQLite
-- schema in packages/datasentry_core/store.py mirrors it logically so the
-- system boots without a container runtime, but where they differ, this wins.
--
-- Two things here are load-bearing rather than conventional:
--
--   * audit_log has no UPDATE or DELETE grant. Append-only is enforced by the
--     absence of a privilege, not by an application promise.
--   * Four identities with genuinely different grants. The reasoning identity
--     can read and nothing else, which is what makes a successful prompt
--     injection a cosmetic problem instead of a security one.

BEGIN;

CREATE SCHEMA IF NOT EXISTS datasentry;
SET search_path TO datasentry, public;

-- ─── Assets and lineage ────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS assets (
  asset_id        TEXT PRIMARY KEY,               -- warehouse.db.schema.table
  warehouse       TEXT NOT NULL,
  database_name   TEXT NOT NULL,
  schema_name     TEXT NOT NULL,
  table_name      TEXT NOT NULL,
  layer           TEXT NOT NULL CHECK (layer IN ('bronze','silver','gold')),
  kind            TEXT NOT NULL DEFAULT 'TABLE'
                    CHECK (kind IN ('TABLE','FEATURE_VIEW','DASHBOARD','ML_MODEL','REPORT')),
  tier            SMALLINT NOT NULL DEFAULT 3 CHECK (tier BETWEEN 1 AND 4),
  owner_team      TEXT,
  dbt_model_path  TEXT,
  repo            TEXT,
  is_quarantined  BOOLEAN NOT NULL DEFAULT FALSE,
  query_count_30d INTEGER NOT NULL DEFAULT 0,
  distinct_consumers_30d INTEGER NOT NULL DEFAULT 0,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS asset_columns (
  asset_id     TEXT REFERENCES assets(asset_id) ON DELETE CASCADE,
  column_name  TEXT NOT NULL,
  data_type    TEXT NOT NULL,
  is_nullable  BOOLEAN NOT NULL DEFAULT TRUE,
  ordinal      INT NOT NULL DEFAULT 0,
  contains_pii BOOLEAN NOT NULL DEFAULT FALSE,
  PRIMARY KEY (asset_id, column_name)
);

CREATE TABLE IF NOT EXISTS lineage_edges (
  edge_id        BIGSERIAL PRIMARY KEY,
  src_asset_id   TEXT NOT NULL,
  src_column     TEXT,                            -- NULL = affects the whole asset
  dst_asset_id   TEXT NOT NULL,
  dst_column     TEXT,
  transform_type TEXT NOT NULL DEFAULT 'IDENTITY'
                   CHECK (transform_type IN
                     ('IDENTITY','CAST','AGG','JOIN_KEY','EXPR','WINDOW','UNKNOWN')),
  transform_expr TEXT,
  source         TEXT NOT NULL CHECK (source IN ('declared','observed')),
  confidence     REAL NOT NULL DEFAULT 1.0 CHECK (confidence BETWEEN 0 AND 1),
  model_ref      TEXT,
  first_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (src_asset_id, src_column, dst_asset_id, dst_column, model_ref, source)
);
CREATE INDEX IF NOT EXISTS idx_lineage_src ON lineage_edges (src_asset_id, src_column);
CREATE INDEX IF NOT EXISTS idx_lineage_dst ON lineage_edges (dst_asset_id, dst_column);

-- Materialised transitive closure so the hot path skips the recursion. The
-- recursive CTE remains the builder and the fallback when this is stale.
CREATE TABLE IF NOT EXISTS lineage_closure (
  src_asset_id   TEXT NOT NULL,
  src_column     TEXT,
  dst_asset_id   TEXT NOT NULL,
  dst_column     TEXT,
  depth          SMALLINT NOT NULL,
  min_confidence REAL NOT NULL,
  PRIMARY KEY (src_asset_id, src_column, dst_asset_id, dst_column)
);
CREATE INDEX IF NOT EXISTS idx_closure_src ON lineage_closure (src_asset_id, src_column, depth);

-- ─── Profiling ─────────────────────────────────────────────────────────
--
-- Note the absence of raw values. min_value_hash and max_value_hash are
-- salted digests used only for change detection; the distribution lives in
-- mergeable sketches. This is ADR-010 made concrete and is the answer to
-- "how do you handle PII?".

CREATE TABLE IF NOT EXISTS profiles (
  profile_id     TEXT PRIMARY KEY,                -- sha256(asset|column|window|version)
  asset_id       TEXT NOT NULL,
  column_name    TEXT,                            -- NULL = table-level profile
  window_start   TIMESTAMPTZ NOT NULL,
  window_end     TIMESTAMPTZ NOT NULL,
  row_count      BIGINT NOT NULL DEFAULT 0,
  sampled_rows   INTEGER,                         -- rows actually examined
  freshness_seconds BIGINT NOT NULL DEFAULT 0,
  null_count     BIGINT,
  distinct_estimate BIGINT,
  numeric_sketch TEXT,                            -- serialised t-digest
  string_length_sketch TEXT,
  categorical_topk JSONB,                         -- hashed labels only
  new_category_count BIGINT,
  min_value_hash TEXT,
  max_value_hash TEXT,
  referential_violation_count BIGINT,
  schema_hash    TEXT,
  degraded       BOOLEAN NOT NULL DEFAULT FALSE,
  profiler_version TEXT NOT NULL,
  computed_at    TIMESTAMPTZ NOT NULL
)
PARTITION BY RANGE (window_end);
CREATE INDEX IF NOT EXISTS idx_profiles_series ON profiles (asset_id, column_name, window_end DESC);

-- ─── Detection ─────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS anomalies (
  anomaly_id      TEXT PRIMARY KEY,
  profile_id      TEXT NOT NULL,
  asset_id        TEXT NOT NULL REFERENCES assets(asset_id),
  column_name     TEXT,
  signal          TEXT NOT NULL,
  detector_id     TEXT NOT NULL,
  detector_version TEXT NOT NULL,
  score           DOUBLE PRECISION NOT NULL,
  p_value         DOUBLE PRECISION,
  ci_low          DOUBLE PRECISION,
  ci_high         DOUBLE PRECISION,
  observed_value  DOUBLE PRECISION NOT NULL,
  baseline_value  DOUBLE PRECISION NOT NULL,
  consecutive_breaches INT NOT NULL DEFAULT 1,
  provisional     BOOLEAN NOT NULL DEFAULT FALSE,
  incident_id     UUID,
  explanation     TEXT NOT NULL DEFAULT '',
  detected_at     TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_anomalies_incident ON anomalies (incident_id);
CREATE INDEX IF NOT EXISTS idx_anomalies_asset_time ON anomalies (asset_id, detected_at DESC);

-- ─── Incidents and reasoning ───────────────────────────────────────────

DO $$ BEGIN
  CREATE TYPE incident_status AS ENUM
    ('detected','rca_running','rca_complete','awaiting_approval','remediating',
     'verifying','resolved','escalated','dismissed','budget_exceeded');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS incidents (
  incident_id     UUID PRIMARY KEY,
  status          incident_status NOT NULL DEFAULT 'detected',
  severity        SMALLINT NOT NULL CHECK (severity BETWEEN 1 AND 4),
  title           TEXT NOT NULL,
  root_asset_id   TEXT NOT NULL REFERENCES assets(asset_id),
  root_column     TEXT,
  anomaly_count   INT NOT NULL DEFAULT 0,
  occurrence_count INT NOT NULL DEFAULT 1,
  narrative       TEXT,
  first_detected_at TIMESTAMPTZ NOT NULL,
  rca_started_at  TIMESTAMPTZ,
  rca_completed_at TIMESTAMPTZ,
  resolved_at     TIMESTAMPTZ,
  llm_cost_usd    NUMERIC(10,4) NOT NULL DEFAULT 0,
  token_count     INT NOT NULL DEFAULT 0,
  trace_id        TEXT,
  prompt_version  TEXT,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_incidents_status
  ON incidents (status, severity, first_detected_at DESC);

CREATE TABLE IF NOT EXISTS evidence (
  evidence_id   TEXT PRIMARY KEY,                 -- short and quotable: ev_7f3a
  incident_id   UUID NOT NULL REFERENCES incidents(incident_id) ON DELETE CASCADE,
  kind          TEXT NOT NULL,
  ref           TEXT NOT NULL,
  summary       TEXT NOT NULL,
  payload       JSONB NOT NULL,
  collected_by  TEXT NOT NULL,
  collected_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_evidence_incident ON evidence (incident_id, kind);

CREATE TABLE IF NOT EXISTS hypotheses (
  hypothesis_id  UUID PRIMARY KEY,
  incident_id    UUID NOT NULL REFERENCES incidents(incident_id) ON DELETE CASCADE,
  rank           SMALLINT NOT NULL DEFAULT 0,
  cause_class    TEXT NOT NULL,
  statement      TEXT NOT NULL,
  claims         JSONB NOT NULL,                  -- each claim carries evidence_ids
  confidence     REAL NOT NULL CHECK (confidence BETWEEN 0 AND 1),
  calibrated_confidence REAL,
  status         TEXT NOT NULL CHECK (status IN ('proposed','verified','rejected')),
  rejection_reason TEXT,
  evidence_requests JSONB NOT NULL DEFAULT '[]',
  round_index    SMALLINT NOT NULL DEFAULT 0,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  -- A rejected hypothesis without a reason is not a rejection, it is a shrug.
  CONSTRAINT rejection_needs_reason
    CHECK (status <> 'rejected' OR rejection_reason IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS blast_radius (
  incident_id  UUID NOT NULL REFERENCES incidents(incident_id) ON DELETE CASCADE,
  asset_id     TEXT NOT NULL,
  column_name  TEXT NOT NULL DEFAULT '',
  depth        SMALLINT NOT NULL,
  asset_kind   TEXT NOT NULL,
  criticality  REAL NOT NULL,
  confidence   REAL NOT NULL DEFAULT 1.0,
  tier         SMALLINT NOT NULL DEFAULT 3,
  layer        TEXT,
  last_consumed_at TIMESTAMPTZ,
  PRIMARY KEY (incident_id, asset_id, column_name)
);

-- ─── Actions, policy, disposition ──────────────────────────────────────

CREATE TABLE IF NOT EXISTS actions (
  action_id     UUID PRIMARY KEY,
  incident_id   UUID NOT NULL REFERENCES incidents(incident_id) ON DELETE CASCADE,
  action_type   TEXT NOT NULL CHECK (action_type IN
                  ('OPEN_PR','QUARANTINE_TABLE','PIN_FEATURE_VIEW',
                   'NOTIFY_OWNER','MUTE_ALERT','BACKFILL_REQUEST')),
  risk_tier     TEXT NOT NULL CHECK (risk_tier IN ('low','medium','high')),
  policy_version TEXT NOT NULL,
  params        JSONB NOT NULL,
  rationale     TEXT NOT NULL DEFAULT '',
  status        TEXT NOT NULL CHECK (status IN
                  ('proposed','approved','rejected','executing','succeeded','failed','reverted')),
  approved_by   TEXT,
  approved_at   TIMESTAMPTZ,
  external_ref  TEXT,
  result        JSONB NOT NULL DEFAULT '{}',
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_actions_status ON actions (status, risk_tier);

CREATE TABLE IF NOT EXISTS dispositions (
  disposition_id UUID PRIMARY KEY,
  incident_id    UUID NOT NULL REFERENCES incidents(incident_id) ON DELETE CASCADE,
  verdict        TEXT NOT NULL CHECK (verdict IN
                   ('correct_cause','wrong_cause','false_positive','duplicate','expected')),
  correct_cause_class TEXT,
  notes          TEXT,
  user_id        TEXT NOT NULL,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS audit_log (
  audit_id    BIGSERIAL PRIMARY KEY,
  actor       TEXT NOT NULL,
  actor_kind  TEXT NOT NULL CHECK (actor_kind IN ('human','agent','service')),
  action      TEXT NOT NULL,
  target      TEXT NOT NULL,
  detail      JSONB NOT NULL,
  at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ─── Agent trace and checkpoints ───────────────────────────────────────

CREATE TABLE IF NOT EXISTS agent_steps (
  step_id     BIGSERIAL PRIMARY KEY,
  incident_id UUID NOT NULL,
  seq         INT NOT NULL,
  node        TEXT NOT NULL,
  model       TEXT,
  status      TEXT NOT NULL,
  duration_ms INT NOT NULL DEFAULT 0,
  tokens_in   INT NOT NULL DEFAULT 0,
  tokens_out  INT NOT NULL DEFAULT 0,
  usd         NUMERIC(10,6) NOT NULL DEFAULT 0,
  summary     TEXT NOT NULL DEFAULT '',
  payload     JSONB NOT NULL DEFAULT '{}',
  at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (incident_id, seq)
);

-- Postgres-backed, not in-memory: an orchestrator restart resumes an
-- in-flight incident rather than losing it.
CREATE TABLE IF NOT EXISTS agent_checkpoints (
  incident_id UUID PRIMARY KEY,
  node        TEXT NOT NULL,
  state       JSONB NOT NULL,
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMIT;

-- ─── Identities (docs/07 section 2) ────────────────────────────────────
--
-- Four roles with genuinely different grants. The separation is the security
-- model: the reasoning identity below can SELECT and nothing else, so a
-- perfectly successful prompt injection produces a wrong narrative on a
-- screen rather than a mutated warehouse.

DO $$ BEGIN CREATE ROLE datasentry_reader   NOLOGIN; EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN CREATE ROLE datasentry_api      NOLOGIN; EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN CREATE ROLE datasentry_executor NOLOGIN; EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN CREATE ROLE datasentry_platform NOLOGIN; EXCEPTION WHEN duplicate_object THEN NULL; END $$;

GRANT USAGE ON SCHEMA datasentry TO
  datasentry_reader, datasentry_api, datasentry_executor, datasentry_platform;

-- The reasoning identity: read, and nothing else, anywhere.
GRANT SELECT ON ALL TABLES IN SCHEMA datasentry TO datasentry_reader;

-- The API: CRUD on the control plane, no warehouse access.
GRANT SELECT, INSERT, UPDATE ON incidents, evidence, hypotheses, blast_radius,
  actions, dispositions, agent_steps, agent_checkpoints TO datasentry_api;
GRANT SELECT ON assets, asset_columns, lineage_edges, lineage_closure,
  profiles, anomalies TO datasentry_api;

-- The executor: exactly the writes the six action types require.
GRANT SELECT ON ALL TABLES IN SCHEMA datasentry TO datasentry_executor;
GRANT UPDATE (is_quarantined, updated_at) ON assets TO datasentry_executor;
GRANT UPDATE ON actions TO datasentry_executor;

-- Append-only audit: everyone writes, nobody amends. Enforced by the absence
-- of the privilege rather than by an application promise.
GRANT INSERT ON audit_log TO
  datasentry_reader, datasentry_api, datasentry_executor;
GRANT SELECT ON audit_log TO datasentry_api;
REVOKE UPDATE, DELETE, TRUNCATE ON audit_log FROM
  datasentry_reader, datasentry_api, datasentry_executor;

-- Migrations only, and only from CI.
GRANT ALL ON SCHEMA datasentry TO datasentry_platform;
