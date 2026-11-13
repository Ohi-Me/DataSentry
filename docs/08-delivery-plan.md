# 08 — Delivery Plan

## 1. Build order rationale

Build in the order that **de-risks the unprovable claims first**. The three things that could sink this project are: (a) the fault harness isn't realistic enough to prove anything, (b) column-level lineage doesn't work on real SQL, (c) the agent hallucinates root causes. Everything else is ordinary engineering.

So: harness → lineage → detection → agent → actions → UI → deploy. **Do not build the UI early.** It is the most fun and the least risky part, and building it first is how projects die at 70%.

## 2. Repository layout

```
datasentry/
├── reference.md                     ← SSOT, update after every change
├── docs/                            ← this documentation set
├── Makefile                         ← dev/up/down/test/eval/demo
├── docker-compose.yml
├── pyproject.toml                   ← uv workspace
├── packages/                        ← shared libraries
│   ├── datasentry_core/                    schemas (Pydantic), config, otel, errors
│   ├── datasentry_detectors/               D1..D7 + conformal calibration
│   ├── datasentry_lineage/                 sqlglot parsing, graph ops, closure
│   └── datasentry_kafka/                   Avro serde, producer/consumer wrappers
├── services/
│   ├── ingestion_gateway/
│   ├── profiler/                    PySpark structured streaming
│   ├── lineage_builder/
│   ├── detector/
│   ├── correlator/
│   ├── orchestrator/
│   │   ├── graph.py                 LangGraph definition
│   │   ├── nodes/
│   │   ├── tools/
│   │   └── prompts/                 versioned prompt files
│   ├── policy_gate/
│   ├── action_executor/
│   ├── verification/
│   └── api/
├── web/                             Next.js 14
├── tools/
│   ├── faultgen/                    synthetic warehouse + fault injection
│   ├── goldset/                     labeled incident builder
│   └── replay/                      Kafka/Delta replay harness
├── infra/                           Terraform
├── evals/
│   ├── gold_incidents/              40 labeled incidents (versioned JSON)
│   ├── fixtures/                    cached tool outputs for deterministic evals
│   └── evaluators/
└── .github/workflows/
    ├── ci.yml  detector-eval.yml  agent-eval.yml  deploy.yml  chaos-nightly.yml
```

## 3. Local bootstrap

```yaml
# docker-compose.yml (essentials)
services:
  redpanda:     # Kafka API + Schema Registry, single binary
  postgres:     # control plane
  redis:
  minio:        # S3-compatible for Delta
  mlflow:       # tracking + registry
  spark:        # local Spark for the profiler
```

```makefile
dev:        ## bring up local stack + migrations + seed warehouse
	docker compose up -d
	uv run alembic upgrade head
	uv run python -m tools.faultgen seed --tables 200 --days 30

fault:      ## inject a fault: make fault TYPE=JOIN_SEMANTICS
	uv run python -m tools.faultgen inject --type $(TYPE)

test:       ## unit + integration
	uv run pytest -q

eval-detector:
	uv run python -m evals.detector --window 14d

eval-agent:
	uv run python -m evals.agent --goldset evals/gold_incidents

demo-up / demo-down:   ## terraform apply/destroy the demo env
```

**Definition of "week 1 done":** `make dev && make fault TYPE=NULL_FLOOD` produces a row in `anomalies`. Nothing else matters until that works.

## 4. Milestones

### Week 1 — Foundation & fault harness
- Monorepo, uv workspace, docker-compose, Alembic migrations, OTel bootstrap
- `tools/faultgen`: 200-table synthetic warehouse with a dbt-style DAG, 30 days of seasonal history, 9 fault types + 3 decoys, deterministic seeds
- Kafka topics + Avro schemas registered
- **Exit:** faults are injectable and reproducible from a seed; the DAG is realistic (multi-hop, joins, aggregations, not a star)

### Week 2 — Profiler
- Spark Structured Streaming job, sketch computation (t-digest, HLL, CMS), windowing with watermarks
- Delta silver `column_profiles` with partitioning and OPTIMIZE
- Cadence tiering
- **Exit:** 5k columns profiled continuously; p95 profile lag <30s; profile size ≤3KB/column/cycle

### Week 3 — Lineage
- sqlglot-based column-level extraction: CTEs, subqueries, `SELECT *` expansion against a catalog snapshot, window functions, unions
- Observed lineage from query history
- Postgres graph + incremental closure
- **Exit:** ≥90% column-mapping accuracy on a 40-model fixture DAG with hand-labeled ground truth; blast radius query <300ms p95 at depth 8

*Week 3 is the highest-risk week. If the parser is going badly by day 3, scope down: support the SQL dialect subset your generator emits, mark everything else `inferred`, and report parse coverage as a metric. Honest partial coverage beats a fake claim.*

### Week 4 — Detection
- D1–D7 implemented in `datasentry_detectors`
- Conformal calibration with weighted, per-cohort thresholds and cold start
- MLflow registration, alias-based loading
- **Exit:** recall ≥0.90, decoy FPR ≤0.10, observed FPR within 2× target on the fault set

### Week 5 — Correlation & incidents
- Lineage-aware clustering, root election, severity, dedup, mutes
- Blast radius + heuristic criticality
- Incident lifecycle state machine in Postgres
- **Exit:** ≥20× alert compression; root asset accuracy ≥0.85

### Week 6 — Agent (part 1)
- LangGraph skeleton, Postgres checkpointer, budget enforcement
- Tool catalog with read-only identity; `get_sql_semantic_diff` implemented properly
- `Triage`, `Contextualize`, `Hypothesize`, `GatherEvidence`
- **Exit:** hypotheses generated with evidence requests; every tool call traced in LangSmith

### Week 7 — Agent (part 2) + gold set
- `Verify` (adversarial), `Narrate`, `PlanActions`
- Evidence ID validation, claim stripping, rejection recording
- Build the 40-incident gold set with human labels
- Wire all 8 evaluators; run the first baseline
- **Exit:** root-cause accuracy ≥0.80, hallucination ≤0.02, cost ≤$0.15/incident. *Record the baseline before tuning — the before/after delta is your best interview story.*

### Week 8 — Actions, policy, verification
- Policy gate with declarative risk tiers, versioned
- Executor: GitHub App (PR only), quarantine via table tag, feature-view pin via Delta time travel
- Verification worker: re-profile, close or escalate
- **Exit:** full closed loop demonstrable end-to-end from CLI

### Week 9 — Frontend
- Incident inbox, incident workspace, agent replay, lineage canvas, approval tray, health page
- WebSocket live updates
- **Exit:** the demo narrative in `01-product-spec.md` §10 runs clean in the browser

### Week 10 — Deploy, harden, document
- Terraform for staging/demo; blue/green deploy; all CI gates enabled
- Nightly chaos job
- README with architecture diagram, metrics table, and a 3-minute demo video
- **Exit:** `terraform apply` from scratch produces a working system; chaos job green 3 nights running

## 5. Weekly discipline

- Update `reference.md` §3 (status) and §6 (decisions) at the end of every week. If a decision changed, add an ADR row — never edit history silently.
- Every week ends with a green `make test`.
- Keep a `BENCHMARKS.md` appending each week's measured numbers. Watching precision go 0.42 → 0.61 → 0.78 → 0.84 across weeks is a far better artifact than a single final number.

## 6. Fault harness specification (build this properly)

```
tools/faultgen/
  schema.py        # 200-table DAG: 60 bronze, 90 silver, 50 gold
  generators.py    # seasonal volume, weekday/weekend, growth trend, noise
  faults/          # one module per fault type, each with:
                   #   inject(conn, seed) -> FaultRecord(ground truth)
                   #   expected_signals: list[Signal]
                   #   expected_root: (asset_id, column)
  decoys/          # legitimate backfill, campaign spike, planned column add
  cli.py           # seed | inject | list | reset
```

Ground truth record:

```python
@dataclass(frozen=True)
class FaultRecord:
    fault_id: str
    fault_type: str
    injected_at: datetime
    root_asset_id: str
    root_column: str | None
    mechanism: str                 # human-readable, used as the eval label
    expected_signals: list[str]
    expected_downstream: list[str] # for blast-radius scoring
    is_decoy: bool = False
```

Every evaluation number in the project traces back to this dataclass. Get it right in week 1.

## 7. Demo script (3 minutes, rehearsed)

| Time | Beat |
|---|---|
| 0:00 | Health page: 5,000 columns profiled, 1 incident in 3 days, observed FPR 0.008 vs target 0.010 |
| 0:20 | Merge the seeded PR changing INNER→LEFT in `stg_orders` |
| 0:35 | Incident appears in the inbox, live via WebSocket |
| 0:45 | Open it: timeline shows the anomaly onset and the commit marker on the same axis |
| 1:00 | Agent replay: three hypotheses, two rejected with explicit reasons, one verified against four cited evidence artifacts. Click an evidence chip → the actual SQL semantic diff |
| 1:45 | Blast radius: 11 columns, 2 feature views, 1 gold dashboard, ranked by criticality |
| 2:00 | Approval tray: PR diff, risk tier `medium`, approve |
| 2:15 | GitHub Actions runs green, merge, re-profile |
| 2:30 | Incident auto-closes with verification evidence |
| 2:45 | Header: 74 seconds to root cause, $0.11. Health page: agent gold-set accuracy 0.84 |

Rehearse it. The last beat — cost and measured accuracy — is what people remember.

## 8. README claims you will be able to defend

Write these only once they are measured, and put the measurement method next to each:

- "Detects 9 classes of silent data regression at a **calibrated** 1% false-positive rate using split conformal prediction — no per-column configuration."
- "Column-level lineage extracted from SQL ASTs; 92% mapping accuracy on a 40-model fixture DAG; blast radius at depth 8 in <300ms."
- "Agentic RCA with an adversarial verifier: 84% top-1 root-cause accuracy, 1.4% hallucination rate on a 40-incident labeled gold set, $0.11 median cost per incident."
- "20× alert compression via lineage-aware correlation."
- "CI gates the build on agent quality — a prompt change that regresses root-cause accuracy cannot merge."
- "Nightly chaos job injects unseen faults into staging and asserts end-to-end detection within SLO."

Every one of those is a sentence an interviewer will stop and ask about. That's the goal.

## 9. Scope-cut ladder (if you fall behind)

Cut in this order — each cut preserves the thesis:

1. Drop the health page's economics panel (keep the metrics, drop the UI)
2. Drop `PIN_FEATURE_VIEW` and `BACKFILL_REQUEST` actions; keep `OPEN_PR`, `QUARANTINE_TABLE`, `NOTIFY_OWNER`
3. Drop the multivariate detector D6 (keep D1–D5, D7)
4. Drop observed lineage from query history; declared lineage only, and report the coverage gap
5. Drop Databricks; run Spark locally and profile from Snowflake directly
6. Drop the lineage canvas's column-level expansion; table-level rendering with a column list panel

**Never cut:** the fault harness, conformal calibration, the verifier node, the gold set, or the CI eval gates. Those are the project. Everything else is packaging.
