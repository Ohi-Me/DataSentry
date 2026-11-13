# Terraform

Module layout, not an applied deployment. There is no cloud account behind
this repository, and an unapplied `terraform plan` proves nothing — so this
documents the intended shape rather than pretending to be infrastructure.
`docs/09-implementation-notes.md` section 5 says the same thing.

```
infra/terraform/
  modules/
    network/         VPC, public and private subnets, NAT, VPC endpoints
    ecs-service/     one Fargate service; used seven times, once per deployable
    rds/             Postgres 16, Multi-AZ, KMS at rest
    redis/           ElastiCache for dedup, budgets and rate limits
    kafka/           MSK or a Confluent Cloud PrivateLink attachment
    observability/   Prometheus remote write, Loki, Grafana, OTel collector
    iam/             the four identities in docs/07 section 2, as policy documents
  envs/
    staging/         nightly chaos runs, CI integration tests
    demo/            the recorded walkthrough
```

There is deliberately no `envs/prod/`. This is a portfolio system, and
`docs/02-architecture.md` section 9 says so in as many words. What is true is
that it is built to production standards — IaC, gates, SLOs, a real security
model — and that is the defensible claim.

## What the IAM module encodes

The four identities are the security model, so they are the part of this
layout worth reading. The grants are already written and testable in
`infra/sql/001_control_plane.sql`; the Terraform module is the same separation
expressed for the warehouse and the GitHub App:

| Identity | Can | Cannot |
|---|---|---|
| `datasentry-reader` | SELECT on monitored schemas, read `QUERY_HISTORY`, read-only GitHub | write anything, anywhere |
| `datasentry-executor` | `ALTER TABLE ... SET TAG`, write the pin table, open pull requests | push to a default branch, DROP, DELETE |
| `datasentry-platform` | DDL on DataSentry's own schemas | touch monitored data |
| `datasentry-api` | CRUD on control-plane tables | reach the warehouse at all |

The profiler, the lineage builder and **the reasoning orchestrator** all run
as `datasentry-reader`. That is the whole answer to "how do you make agents
safe?", and it is an architectural answer rather than a prompt-engineering one.
