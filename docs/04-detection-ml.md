# 04 — Detection & ML Design

## 1. Position

Detection is a **statistics problem**, not an LLM problem (ADR-004). The bar is not "can we find anomalies" — trivially yes. The bar is **finding real regressions at a calibrated false-positive rate across 200k columns with zero per-column configuration**. That is the hard part and the defensible part.

## 2. Profiling

### 2.1 Signals computed per cycle

**Table level**
| Signal | Computation | Notes |
|---|---|---|
| `row_count` | count in window | Both absolute and delta vs. prior window |
| `freshness_seconds` | `now - max(updated_at)` | Falls back to Delta commit time / Snowflake `LAST_ALTERED` |
| `write_duration` | from job telemetry | Correlates infra failures |
| `schema_hash` | hash of ordered (name, type, nullable) | Instant schema-change detection |

**Column level**
| Signal | Computation | Sketch |
|---|---|---|
| `null_rate` | nulls / rows | exact |
| `distinct_estimate` | approx distinct | HyperLogLog (p=14, ~0.8% error) |
| `numeric distribution` | quantiles + mean/std | t-digest (compression 100) |
| `categorical mass` | top-k frequencies | Count-Min Sketch + top-k heap (k=50) |
| `new_category_count` | categories unseen in baseline | Bloom filter against baseline vocabulary |
| `string length distribution` | quantiles of `len()` | t-digest — catches truncation and format changes |
| `referential_violation_count` | FK misses vs. declared/inferred keys | exact, sampled above 10M rows |

Sketches are chosen so that **profiles are mergeable and bounded in size** (~3 KB/column/cycle). Mergeability matters: it lets you compute a 30-day baseline by merging 30 daily sketches instead of rescanning 30 days of data. That property is why sketches and not raw histograms.

### 2.2 Cadence tiers

| Tier | Criterion | Cadence |
|---|---|---|
| Hot | tier-1 assets or >100 writes/day | 5 min |
| Warm | tier-2/3 with daily writes | 1 h |
| Cold | infrequent writes | daily |

Tiering is what makes 200k columns affordable. Compute cost scales with cadence, not table count.

### 2.3 Incremental computation

Spark Structured Streaming with `foreachBatch`; per batch:
1. Read new rows (Delta CDF / Snowflake stream).
2. Compute per-partition sketches.
3. Merge with the in-flight window state (`mapGroupsWithState`, watermark 2×cadence).
4. On window close, serialize and emit `profile.computed`.

Late data beyond the watermark produces a corrective profile with `degraded=true`; the detector down-weights degraded profiles rather than alerting on them.

## 3. Detector ensemble

One detector per signal family, all producing a comparable **nonconformity score** so they can share a calibration framework.

### 3.1 D1 — Volume & freshness (seasonal residual)

MSTL decomposition (daily + weekly seasonality) on the last 60 cycles → forecast residual → nonconformity = `|residual| / MAD(residuals)`.

Chosen over Prophet/ARIMA because it is fast, has no per-series fitting cost worth speaking of, is robust to outliers, and handles the multiple seasonalities that warehouse loads actually have. Be ready to defend this: "I benchmarked MSTL residuals against Prophet and a seasonal-naive baseline on the injected fault set; MSTL matched Prophet's recall at 1/40th the compute."

### 3.2 D2 — Rate signals (null rate, referential violations)

Beta-Binomial model per column: baseline `Beta(α, β)` fitted from the trailing window, with the current cycle's `(nulls, rows)` scored by posterior predictive tail probability. Handles the small-sample problem correctly (a 3-row table with 1 null is not an incident).

### 3.3 D3 — Numeric drift

Two-sample comparison between the current t-digest and the merged baseline t-digest:
- **Wasserstein-1 distance** normalized by baseline IQR (primary — scale-aware, catches unit changes cleanly)
- **Jensen–Shannon divergence** on quantile-binned mass (secondary — catches shape changes with the same mean)

A currency change from USD to INR moves W1 by ~83× IQR. This is the fault type where a well-chosen metric is dramatically better than a z-score on the mean.

### 3.4 D4 — Categorical drift

- Chi-square on top-k mass vs. baseline (with continuity handling for sparse categories)
- New-category rate via Bloom filter
- Total variation distance on the top-k distribution

### 3.5 D5 — Cardinality

Ratio of `distinct_estimate` to `row_count` modeled as a bounded rate; a dedup break shows as a sharp drop in the distinctness ratio with stable row count — a signature no single-signal detector catches.

### 3.6 D6 — Multivariate

Isolation Forest over the standardized joint profile vector (all signals for a column, plus table-level context). Catches "everything is individually fine but the combination has never occurred." Trained per asset-tier cohort, not per column, to avoid 200k models.

### 3.7 D7 — Schema

Deterministic: type change, nullability change, column drop/add, ordering change. Not statistical; fires immediately at severity 2. Included in the ensemble so it flows through the same incident machinery.

## 4. Conformal calibration (the centrepiece)

**Problem:** a raw score threshold gives an unknown, per-column-varying false-positive rate. With 200k columns at even a 0.1% FPR you get 200 alerts per cycle. Unusable.

**Approach:** split conformal prediction per (column, signal).

1. Maintain a rolling calibration set of the last `n=200` nonconformity scores for that column/signal, excluding cycles labeled as incidents.
2. For a target FPR `α`, the threshold is the `⌈(n+1)(1-α)⌉`-th order statistic of the calibration scores.
3. A new score exceeding that threshold is an anomaly; its calibrated p-value is `(1 + #{s_i ≥ s_new}) / (n + 1)`.

**Guarantee:** under exchangeability, the marginal FPR is ≤ α *without distributional assumptions*. This is why the technique is right here — you cannot assume normality across 200k heterogeneous columns.

**Practical adaptations:**
- **α is set per asset tier**, not globally: tier-1 α=0.01, tier-2 α=0.005, tier-3 α=0.002. Higher-value assets get more sensitivity because the human cost of a false positive there is worth paying.
- **Exchangeability is violated by trends.** Mitigate with weighted conformal (exponential decay on calibration weights, half-life 14 days) and by scoring residuals from D1 rather than raw values.
- **Consecutive-breach requirement:** an anomaly requires `k=2` consecutive cycles (configurable per signal) which reduces FPR to ~α² for independent noise while barely delaying detection.
- **Cold start:** columns with <30 cycles of history use a cohort-level threshold (pooled across columns of the same type and tier) and are flagged `provisional`.

**Verification:** `FCT_DETECTOR_PERF` compares observed FPR to target α weekly. If observed FPR > 2α for a cohort, the recalibration job re-derives thresholds and raises a platform-health incident. **A monitoring system that monitors its own calibration is the strongest single thing in this project.**

## 5. Anomaly → incident correlation

Raw anomalies are not incidents. A schema change on a bronze table fires 30+ anomalies across the DAG.

**Algorithm**
1. Collect anomalies in a sliding 15-minute window.
2. Build a graph where anomalies are nodes; connect two anomalies if their assets are within lineage distance ≤3 **and** their detection times are within 10 minutes.
3. Take connected components.
4. For each component, elect the **root**: the node minimizing (topological depth in the lineage DAG, detection time). Ties break toward the upstream asset.
5. Compute severity: `severity = f(max_downstream_criticality, root_asset_tier, anomaly_count, max_confidence)`.
6. Dedup against open incidents on the same root asset within 24 h → increment `occurrence_count` instead of creating a new incident.

Effect measured on the fault harness: ~20× reduction in alert volume, which is exactly the ADR-006 cost argument.

## 6. Criticality scoring

**v1: transparent heuristic** (and say so — see `reference.md` §10):

```
criticality = 0.40 * tier_weight(asset.tier)
            + 0.25 * normalize(log1p(query_count_30d))
            + 0.20 * downstream_kind_weight(FEATURE_VIEW=1.0, DASHBOARD=0.8, TABLE=0.4, REPORT=0.9)
            + 0.15 * normalize(distinct_consumers_30d)
```

**v1.1: learned model** — LightGBM regression on human severity overrides once ≥300 dispositions exist. Do not train it before then; a model fitted on 40 labels is theatre.

## 7. Evaluation protocol

### 7.1 Fault injection harness (`tools/faultgen`)

Generates a synthetic 200-table warehouse (bronze → silver → gold, realistic dbt-style DAG, seasonal volume, weekday/weekend patterns, occasional legitimate backfills) and injects faults with **ground truth**:

| Fault | Injection | Expected detectors |
|---|---|---|
| `UNIT_CHANGE` | multiply a numeric column by an FX rate | D3 |
| `NULL_FLOOD` | set 15% of a column to NULL | D2 |
| `DEDUP_BREAK` | change a join key so rows fan out 3× | D1, D5 |
| `JOIN_SEMANTICS` | INNER → LEFT in a silver model | D1, D2 |
| `SCHEMA_DRIFT` | upstream renames/retypes a column | D7 |
| `LATE_DATA` | delay 20% of a partition by 6 h | D1 (freshness) |
| `CATEGORY_SHIFT` | new enum value replaces 30% of an existing one | D4 |
| `SILENT_TRUNCATION` | string column truncated to 20 chars | D3 (length), D5 |
| `TIMEZONE_SHIFT` | shift timestamps by +5:30 | D1 (volume by day boundary) |
| **Decoys** | legitimate backfill, marketing campaign spike, planned schema addition | *none should fire* |

Decoys are as important as faults. A harness with no negatives measures nothing.

### 7.2 Metrics

| Metric | Definition | Target |
|---|---|---|
| Detection recall | injected faults producing an incident | ≥0.90 |
| Detection latency | injection → incident created | ≤2 cycles p90 |
| Incident precision | incidents matching a real fault | ≥0.75 |
| Decoy FPR | decoys producing an incident | ≤0.10 |
| Root asset accuracy | correlator elects the true root | ≥0.85 |
| Observed vs. target FPR | calibration drift | within 2× |

### 7.3 Backtesting

Kafka retention + Delta time travel lets you replay any historical window through a candidate detector. The CI gate (see `07-platform-ops.md`) runs a 14-day replay and blocks the build on a >2 point precision regression. This is the "backtest gate" and it is a genuinely strong CI story.

## 8. MLflow usage

| Artifact | Registered as | Promotion gate |
|---|---|---|
| Isolation Forest per tier cohort | `datasentry-multivariate-{tier}` | Recall ≥ champion on fault set |
| Conformal threshold tables | `datasentry-conformal-{signal}` (as an artifact, versioned) | Observed FPR within 2× target |
| Criticality model (v1.1) | `datasentry-criticality` | Spearman ρ ≥ 0.7 vs. human severity |

Detectors load models by **alias** (`@champion`), never by run ID, so rollback is a registry operation and not a deploy.

Each registered version carries a model card: training window, fault-set performance, known blind spots. Write the blind spots honestly — "D3 does not detect drift in columns with <100 distinct values; D4 covers that case" is the kind of sentence that lands well in an interview.

## 9. What this design deliberately does not do

- **No deep learning for detection.** No justification exists at this data shape; a transformer time-series model would be slower, less interpretable, and no more accurate. Being able to say "I chose not to use deep learning here, and here's the benchmark" is stronger than using it.
- **No supervised anomaly classifier.** Labels are far too sparse and biased toward what previous rules caught.
- **No per-column hyperparameter tuning.** The entire point of conformal calibration is that it removes the need.
