# 07 — Platform, Security, Observability, CI/CD

## 1. Cloud architecture (AWS)

```
                    ┌─────────────── Route53 + ACM ───────────────┐
                    │                                             │
             ┌──────▼──────┐                             ┌────────▼────────┐
             │ CloudFront  │  (Next.js static/SSR)       │       ALB       │
             └─────────────┘                             └────────┬────────┘
                                                                  │
  ┌───────────────────────────── VPC ──────────────────────────────────────┐
  │  public subnets: ALB, NAT                                              │
  │  ┌── private app subnets ──────────────────────────────────────────┐   │
  │  │  ECS Fargate: api · orchestrator · executor · verification      │   │
  │  │              · ingestion-gateway · detector · correlator        │   │
  │  └─────────────────────────────────────────────────────────────────┘   │
  │  ┌── private data subnets ─────────────────────────────────────────┐   │
  │  │  RDS Postgres 16 (Multi-AZ)   ElastiCache Redis                 │   │
  │  └─────────────────────────────────────────────────────────────────┘   │
  │  VPC endpoints: S3, Secrets Manager, ECR, CloudWatch                   │
  └────────────────────────────────────────────────────────────────────────┘
        │                    │                      │
   Confluent Cloud      Databricks              Snowflake
   (PrivateLink)        (Spark jobs)            (PrivateLink)
```

**Terraform layout**

```
infra/
  modules/{network,ecs-service,rds,redis,kafka,observability,iam}/
  envs/
    local/      # docker-compose only
    staging/
    demo/
```

Everything is IaC. No console clicks. `terraform plan` runs on every PR and posts the diff as a comment.

## 2. Identity model (the security centrepiece)

Four distinct identities, and the separation is the point:

| Identity | Permissions | Used by |
|---|---|---|
| `datasentry-reader` | `SELECT` on monitored schemas, `SELECT` on `QUERY_HISTORY`/`ACCESS_HISTORY`, read-only GitHub | profiler, lineage-builder, **agent-orchestrator** |
| `datasentry-executor` | `ALTER TABLE ... SET TAG` (quarantine), write to feature-view pin table, GitHub App with `pull_requests:write` only | action-executor |
| `datasentry-platform` | Full DDL on DataSentry's own schemas | migrations only, via CI |
| `datasentry-api` | CRUD on control-plane tables, no warehouse access | api |

**The agent literally cannot write anything.** If a prompt injection succeeded perfectly, the worst outcome is a wrong narrative in the UI — not a mutated warehouse. This is the answer to "how do you make agents safe?" and it's an architectural answer, not a prompt-engineering answer.

`datasentry-executor` additionally:
- can never push to `main` (branch protection enforced in Terraform, not just settings)
- can never `DROP` or `DELETE` anything
- every action is reversible by construction; irreversible action types are not implemented

## 3. Security controls

| Control | Implementation |
|---|---|
| Secrets | AWS Secrets Manager, rotated; no secrets in env files or images; local dev uses `.env.local` gitignored with dummy values |
| Encryption | TLS everywhere; KMS at rest on RDS/S3/Redis; Snowflake/Databricks via PrivateLink |
| PII | Profiles store sketches and salted hashes only (ADR-010); `asset_columns.contains_pii` drives extra redaction in evidence payloads |
| Prompt injection | Statistics-only prompts; `<evidence trust="untrusted-data">` delimiters; read-only agent identity; closed action enum |
| Supply chain | Pinned dependencies (`uv.lock`, `pnpm-lock.yaml`), Dependabot, `pip-audit` + `npm audit` in CI, SBOM generated per release |
| Container | Distroless base, non-root user, read-only root filesystem, Trivy scan gate |
| Audit | Append-only `audit_log`; `UPDATE`/`DELETE` revoked from the app role; exported nightly to Snowflake |
| Rate limits | Per-user API limits; global LLM concurrency cap; GitHub API budget guard |
| Data residency | Single region; LLM calls can be routed to a same-region endpoint; a `self_hosted_model` config flag exists for the "we can't send data to a third party" conversation |

**Threat model — write this down and be able to recite it:**
1. *Malicious data producer* embeds instructions in a column value or SQL comment → mitigated by statistics-only prompting + read-only agent.
2. *Compromised agent output* proposes a destructive action → mitigated by the closed action enum + policy gate + reversibility requirement.
3. *Over-privileged service* → mitigated by four-identity separation + least-privilege IAM.
4. *Insider approves a bad action* → mitigated by audit log + high-risk actions requiring `admin` + no bulk approve for high risk.
5. *Cost attack* (someone triggers 10k incidents) → mitigated by budget caps + global rate limit + storm mode.

## 4. Observability

### 4.1 Tracing

One OTel trace per incident, spanning ingestion → profiling → detection → correlation → RCA → action → verification. `trace_id` is propagated through Kafka headers (this is the part people skip; do it — cross-async-boundary tracing is a strong signal).

`incidents.trace_id` links the UI to both Grafana and LangSmith.

### 4.2 Metrics (Prometheus)

**Pipeline:** consumer lag per topic, profile throughput, profiling coverage %, lineage parse coverage %, closure staleness.
**Detection:** anomalies/min by signal, observed FPR vs. target by cohort, incidents/hour, correlation compression ratio.
**Agent:** runs/min, p50/p95 duration, node failure rate, tool call counts, budget-exceeded rate, cache hit rate, cost/incident.
**Action:** proposed/approved/rejected counts, PR merge rate, verification pass rate, revert rate.
**Product:** MTTRC, precision@incident, % auto-remediated, alert volume/week.

### 4.3 Logging

Structured JSON to Loki. Every log line carries `trace_id`, `incident_id` where applicable, and `service`. No raw warehouse values are ever logged — enforced by a redaction filter with a unit test that asserts it.

### 4.4 SLOs

| SLO | Target | Error budget policy |
|---|---|---|
| Detection lag (write → incident) p95 | ≤ 2.5 min | >2% breach in 7d → freeze feature work on the detection path |
| API availability | 99.5% | Standard |
| RCA completion rate | ≥95% of incidents get a non-`budget_exceeded` result | <90% → raise budgets or optimize prompts |
| Calibration | observed FPR within 2× target | Breach → auto-recalibration job |

### 4.5 Dashboards

Three Grafana boards: **Pipeline health**, **Detection quality**, **Agent economics**. The last one — cost per incident, model tier split, cache hit rate, token distribution — is the one almost nobody builds and the one that reads as senior.

## 5. CI/CD

### 5.1 Pipelines

**`ci.yml`** (every PR)
1. Lint + format (`ruff`, `mypy --strict`, `eslint`, `tsc --noEmit`)
2. Unit tests (`pytest`, `vitest`) — coverage gate 80% on `services/**`
3. Container build + Trivy scan (fail on HIGH/CRITICAL)
4. Integration tests: docker-compose with Redpanda, Postgres, Redis, MinIO; asserts the full loop on 3 injected faults
5. `terraform validate` + `plan` (posted as a comment)

**`detector-eval.yml`** (on `services/detector/**`, `packages/detectors/**`)
- Replays 14 days of profiles from the fixture set through the candidate detector
- Computes recall, precision, decoy FPR, calibration error
- **Fails if precision drops >2pp or recall drops >3pp vs. `main`**
- Posts a delta table as a PR comment

**`agent-eval.yml`** (on `services/orchestrator/**`, including `prompts/**`)
- Runs the 40-incident gold set through LangSmith with cached tool fixtures
- **Fails on any regression** in root-cause accuracy, grounding, hallucination rate, or cost
- Posts per-evaluator deltas

**`deploy.yml`** (on merge to `main`)
- Build → push to ECR → Alembic migrations (forward-only, tested against a restored snapshot) → blue/green ECS deploy → smoke tests → auto-rollback on smoke failure

**`chaos-nightly.yml`** (scheduled, staging)
- Injects 5 random faults into the staging warehouse
- Asserts each is detected, correlated, and root-caused within SLO
- Opens a GitHub issue on failure with the trace links

The nightly chaos job is the strongest single CI artifact here: **the system is continuously proving it still works, end to end, on faults it has never seen.**

### 5.2 Gate summary

| Gate | Blocks merge on |
|---|---|
| Lint/type | Any error |
| Unit coverage | <80% on services |
| Container scan | HIGH/CRITICAL CVE |
| Integration | Any of 3 faults undetected |
| Detector eval | Precision −2pp or recall −3pp |
| Agent eval | Any evaluator regression |
| Terraform | Plan error |

### 5.3 Release hygiene

- Conventional commits → automated changelog
- Semantic versioning on services; prompt versions are independent and recorded per run
- Every release attaches: model cards for detectors, the agent eval report, the SBOM

## 6. Scalability plan

| Dimension | v1 | Path to 10× | Path to 100× |
|---|---|---|---|
| Columns profiled | 5k | 50k: add Spark executors; cadence tiering | 500k: partition profiling by workspace, separate Delta tables per bucket |
| Kafka throughput | 2k msg/s | 20k: repartition to 48, tune batch sizes | 200k: dedicated clusters per topic family |
| Lineage edges | 200k | 2M: closure table + covering indexes | 20M: migrate to a graph store (ADR-002 revisit trigger) |
| Incidents/day | 50 | 500: horizontal orchestrator workers, priority queue | 5k: aggressive semantic caching, tiered routing, storm mode |
| LLM cost | $8/day | Cache + routing keeps this sublinear | Fine-tuned small model for `Hypothesize` on common cause classes |

**The real bottleneck is LLM concurrency and cost, not compute.** Three mitigations, in order of impact:
1. Incident correlation (~20× reduction in agent runs) — ADR-006
2. Two-tier model routing (~4× cost reduction) — ADR-007
3. Semantic caching of hypothesis sets (~10× on recurring incidents)

Know these numbers. "How would you scale this?" is a guaranteed interview question and "add more workers" is a losing answer when the constraint is dollars per decision.

## 7. Cost model (portfolio scale)

| Item | Monthly |
|---|---|
| Confluent Cloud (basic, demo only) | ~$25 |
| Snowflake trial → XS warehouse, auto-suspend 60s | ~$30 |
| Databricks (free/trial tier or local Spark) | $0–20 |
| AWS: Fargate (3 small tasks), RDS t4g.small, Redis t4g.micro, S3 | ~$60 |
| LLM (evals + demo runs) | ~$25 |
| **Total** | **~$140–160/month, only during the demo window** |

Cost controls: everything scales to zero outside demo windows; a `make demo-up` / `make demo-down` pair tears the environment down; local development uses zero cloud spend. Local-first development is not just frugal — it makes the CI integration tests fast and hermetic.
