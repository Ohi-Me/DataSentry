# 01 — Product Specification

## 1. Problem statement

Enterprise lakehouses run 10k–200k tables. The failures that cost money are not pipeline crashes — those page someone within minutes. The expensive failures are **silent semantic regressions**:

- an upstream team changes a currency column from USD to local currency
- a partner starts sending `NULL` instead of `0`
- a dedup key changes cardinality and rows fan out 3x
- a timezone shifts, moving events across day boundaries
- a `JOIN` changes from `INNER` to `LEFT` in a dbt model
- a late-arriving source silently drops 8% of records

Every pipeline is green. Every dashboard is wrong. Every downstream model is training on poison.

**Detection latency** today is days-to-weeks (discovered by a human noticing an implausible number). **Root-cause latency** is 4–9 engineer-hours per incident, almost all of it spent tracing lineage by hand through SQL, dbt models, and orchestrator DAGs. **Blast radius** is usually never computed — teams fix the table and hope.

## 2. Why now

Three things changed:
1. Lakehouse metadata is finally queryable at scale (Delta transaction logs, Snowflake `ACCESS_HISTORY`/`QUERY_HISTORY`, dbt manifests).
2. Column-level lineage is mechanically extractable with mature SQL parsers.
3. LLMs are good at *hypothesis generation over heterogeneous evidence* — the exact cognitive task a human does badly at 2am — provided you constrain them to cite evidence.

## 3. Personas

| Persona | Goal | Pain today | What DataSentry gives them |
|---|---|---|---|
| **Dana — Data Reliability Engineer** (primary) | Keep the warehouse trustworthy without drowning in alerts | 200 Slack alerts/week, 90% noise, no causation | Correlated incidents with a cited root cause and a proposed fix |
| **Arun — Analytics Engineer** (dbt owner) | Ship model changes safely | Finds out he broke something a week later, from a VP | A PR on his own model, opened minutes after the regression, with the diff and the evidence |
| **Priya — ML Engineer** | Models don't silently degrade | Discovers training data rot after a production metric drops | Feature views auto-pinned to last-known-good; notified with the exact affected features |
| **Marco — Data Governance / Risk** | Provable evidence trail for bad regulatory reports | Reconstructs history manually, badly | Immutable incident record: what broke, when, who was affected, what was done |

## 4. Jobs to be done

1. "Tell me something is wrong *before* a human notices, without telling me 200 times a week."
2. "Tell me **why** it broke, with evidence I can verify in 30 seconds."
3. "Tell me **who is affected**, ranked by how much I should care."
4. "Do the obvious fix for me, and prove it worked."

## 5. Scope

### In scope (v1)
- Snowflake as the monitored warehouse (Delta on Databricks as a secondary source)
- dbt-style SQL transformation DAG as the lineage source
- GitHub as the code source and the remediation channel
- Batch + micro-batch tables; not sub-second streaming tables
- Single tenant, single workspace

### Explicitly out of scope (v1)
- Multi-tenant SaaS, SSO/SCIM, billing
- Non-SQL transformations (Spark DataFrame code, Python UDF logic) — these produce lineage edges marked `inferred=true` from job I/O only
- Auto-fixing business logic beyond template-based reverts and null-guard patches
- Semantic-layer / BI-tool-native integrations beyond a dashboard registry table
- Real customer PII (synthetic data only)

## 6. Non-goals

- **Not** a data catalog. We consume catalog metadata; we don't replace it.
- **Not** a testing framework. dbt tests are complementary; we ingest their results as evidence.
- **Not** an orchestrator. Airflow/dbt Cloud remain the schedulers; we consume their events.
- **Not** an observability dashboard. If the product's value can be summarized as "charts of table health," it has failed.

## 7. User stories with acceptance criteria

### US-1 — Silent regression detected
> As Dana, I want an incident raised within one profiling cycle of a distributional break, so I learn about it before a stakeholder does.

**AC**
- Given a table with ≥14 days of profile history, when the null rate of a column moves outside its conformal interval for 2 consecutive cycles, an anomaly event is emitted within 60s (p95) of the profile write.
- Anomalies on ≥2 columns within the same lineage subtree and a 15-minute window are correlated into a single incident.
- The incident carries a severity derived from criticality × confidence, not from raw z-score.

### US-2 — Root cause with evidence
> As Dana, I want the system to name the most likely cause and show me the evidence, so I can confirm in under a minute.

**AC**
- Every incident produces ≥1 and ≤5 ranked hypotheses.
- Every claim in the accepted hypothesis references ≥1 evidence artifact by ID (git commit, query hash, profile delta, dbt run, schema change event).
- Hypotheses whose claims fail verification are stored as `rejected` with the rejection reason and shown in the replay view — rejected reasoning is a feature, not garbage.
- Top-1 root-cause accuracy ≥80% on the 40-incident gold set.

### US-3 — Blast radius
> As Priya, I want to know which of my features and models consumed bad data.

**AC**
- Downstream traversal returns all column-level descendants to depth ≤8 within 300ms p95.
- Each impacted asset carries a criticality score and last-consumed timestamp.
- Feature views whose source columns are impacted are flagged and eligible for auto-pinning.

### US-4 — Remediation
> As Arun, I want a PR I can review, not a ticket telling me to investigate.

**AC**
- For supported fault classes, the executor opens a GitHub PR containing: the diff, the incident narrative, the evidence table, the blast radius list, and the simulated effect.
- The PR triggers the repo's own CI; DataSentry reports pass/fail back onto the incident.
- No action is ever pushed directly to `main`.

### US-5 — Policy and autonomy
> As Dana, I want low-risk actions to happen automatically and high-risk actions to wait for me.

**AC**
- Every action carries a risk tier (`low` / `medium` / `high`) computed from asset tier × action reversibility × blast radius size.
- `low` auto-executes; `medium` and `high` enter the approval tray.
- Policies are declarative and versioned; every evaluation is logged with the policy version.

### US-6 — Verification and learning
> As Dana, I want to know the fix actually worked, and I want the system to get better at this.

**AC**
- After remediation merges, the table is re-profiled; if the anomaly does not clear within 2 cycles, the incident escalates rather than auto-closing.
- Analyst disposition (`correct_cause` / `wrong_cause` / `false_positive` / `duplicate`) is captured with one click.
- Dispositions write to the label store and are consumed by both the conformal recalibration job and the LangSmith eval set.

### US-7 — Alert fatigue control
> As Dana, I want fewer, better alerts.

**AC**
- Incident volume per week is a tracked metric with a target ceiling; exceeding it triggers a recalibration job rather than a louder alert.
- Repeated identical incidents on the same asset within 24h are deduplicated into one incident with an occurrence counter.
- Known-and-accepted anomalies (e.g., a planned backfill) can be muted with an expiry, and the mute is an auditable object.

## 8. Success metrics

See `reference.md` §7 for the target table. Product-level north stars:

- **MTTRC** (mean time to root cause): baseline 4–9 human-hours → target <90 seconds machine + <5 minutes human confirmation.
- **Precision@incident** ≥ 0.75 — the number that determines whether anyone keeps the product turned on.
- **% incidents auto-remediated** — the number that determines whether it's a product or a monitor.

## 9. Competitive positioning (be able to say this in one breath)

> "Monte Carlo tells you the row count is anomalous. We tell you it's anomalous *because* PR #4471 changed a join 40 minutes ago, that it has already corrupted 3 feature views and the EMEA revenue dashboard, and here's the PR that fixes it."

## 10. Demo narrative (design backwards from this)

1. Show a healthy warehouse.
2. Merge a PR that changes an `INNER JOIN` to `LEFT JOIN` in a silver model.
3. Watch: profile shift → anomalies fire → correlate into one incident.
4. Open the incident: timeline shows the anomaly onset overlaid with the commit.
5. Agent replay: three hypotheses, two rejected with reasons, one verified with four cited evidence artifacts.
6. Blast radius: 11 downstream columns, 2 feature views, 1 gold dashboard.
7. Approval tray: PR diff reverting the join, risk tier `medium`, one click to approve.
8. CI runs green, merge, re-profile, incident auto-closes with verification evidence.
9. Metrics page: this incident cost $0.11 and took 74 seconds to root-cause.

Step 9 is what separates this from a demo video. Build the cost and latency accounting early.
