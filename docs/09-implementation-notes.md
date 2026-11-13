# 09 — Implementation Notes

Where the built system differs from the specification, and why. The specs in
`01`–`08` describe the target. This file describes what actually exists, which
is the more useful document when you are reading the code.

Nothing here is a surprise discovered late — every deviation is either a
documented scope cut from `08-delivery-plan.md` §9, or a decision forced by
running the thing and looking at the output.

---

## 1. Substitutions

| Spec | Built | Why |
|---|---|---|
| Kafka + Schema Registry | append-only `events` table with the same topics, keys and payloads | The properties that matter — ordering per key, replay from offset zero, at-least-once with idempotent writes — hold in both. Splitting a stage back out into its own consumer is a deployment change, not a rewrite. |
| Spark Structured Streaming profiler | the same sketch computation driven by a batch iterator | The contract is identical: a batch goes in, sketches come out, no value survives the call. `foreachBatch` is where the loop would live. |
| Postgres 16 control plane | SQLite on the identical logical schema | The DDL in `infra/sql/001_control_plane.sql` is the Postgres authority. The repository layer is raw SQL — no ORM — so the queries in `03-data-model.md` are the queries that run. |
| sqlglot | a purpose-built SQL reader (`datasentry_lineage/sqlparse.py`) | `08-delivery-plan.md` §4 sanctions exactly this cut: support the dialect subset the transformation layer emits, resolve it precisely, and *report parse coverage as a metric*. Measured coverage is on the health page and in `BENCHMARKS.md`. |
| LangGraph | a ~150-line state graph with conditional edges and a Postgres-backed checkpointer | Seven nodes, one back edge, one interrupt point. The semantics needed are exactly the ones written. |
| Next.js 14 console | a zero-build console served by the API | `ds serve` is the entire product on one port with nothing to install. Same screens, same interactions, same keyboard model. |
| MLflow registry | frozen-at-fit models with the alias discipline preserved | The rule that matters — a detector never trains in-process — is enforced in `families.py`, not by the registry. See §3. |

## 2. Cut, and honestly

**Referential integrity (D2r).** Implemented and inert. Computing FK violations
needs the parent key domain, which the sampled local harness does not feed. The
profiler writes `referential_violation_count = NULL` and the detector does not
apply. It is not reporting a number it cannot compute.

**Observed lineage from query history.** The `query_log` table is populated and
the tool reads it, but `ACCESS_HISTORY`-style observed edges are only produced
as the fallback when a model fails to parse. With parse coverage at 98% the
fallback path is rarely exercised, which is worth stating rather than claiming
two lineage sources are in balance.

**Learned criticality.** Heuristic only, exactly as `04-detection-ml.md` §6
says it should be until 300+ dispositions exist. The weights are the ones in
the spec, unchanged.

## 3. Bugs the implementation found in the design

These were found by running the system and looking at the numbers. Each one is
a case where the spec was right in intent and the obvious implementation of it
was wrong.

**A rate detector must score the sample, not the table.** Profiling is sampled
above a cap. Scoring 135 nulls out of a *scaled* 50,000 row count instead of
the 900 rows actually examined claims fifty times more evidence than exists,
and every rate becomes overwhelmingly significant. `ColumnProfile.sampled_rows`
now travels with the estimate and D2 and D4 use it.

**A baseline computed from a contaminated history teaches the detector that
the regression is normal.** Method-of-moments on mean and variance collapsed
three cycles into a fault: the mean rose, the variance exploded, the prior went
diffuse, and a 12% null rate scored 1.6 after correctly scoring 12.0 one cycle
earlier. Median and MAD have a 50% breakdown point and hold.

**A prior cannot be stronger than its data.** Capping the Beta at a fixed
pseudo-count meant thirty cycles of a three-row table produced a posterior
carrying twenty thousand observations, and one null read as decisive. `alpha +
beta` is now capped at the observations actually seen.

**A breaching point must not enter its own calibration set.** Otherwise a
sustained regression walks its own threshold upward — cycle one breaches, its
extreme score joins the set, cycle two clears the bar it just raised — and the
consecutive-breach rule never fires on precisely the faults it exists to catch.
Excluded points bias the set low, so a breach margin (1.15x) supplies the
hysteresis and the calibration report tracks the resulting drift.

**Refitting a model mid-stream voids the conformal guarantee.** The isolation
forest was refitting as history grew, moving the score distribution underneath
a calibration set built against the previous model. Fit once, freeze — which is
the same rule as loading a registered model by alias.

**A cohort model needs a standardised space to be a cohort at all.** Feeding
raw magnitudes to a shared forest meant a model fitted on a 50,000-row table
marked a 400-row table as bizarre; D6 produced 89 of 106 anomalies, all of them
reporting that tables differ in size. Each series is now mapped into its own
robust z-space first.

**Seasonal periods come from the cadence, not from a constant.** A hard-coded
`(24, 168)` assumes hourly profiling. At a three-hour cadence the daily cycle is
eight points, and decomposing against the wrong period leaves the entire daily
swing in the residual — drowning a real 20% volume shift in noise the detector
was supposed to have removed.

**Multi-seasonal decomposition needs backfitting.** Estimating each periodic
component independently against the same detrended series leaves the daily
cycle inside the weekly estimate and both inside the residual. Each component
is now estimated against the series with the others removed, and the trend is
re-estimated against the deseasonalised series.

**A t-digest interpolates between centroid *centres*.** Anchoring to the
previous centroid's mean is invisible with hundreds of light centroids and
badly wrong with a few heavy ones — which is the shape weighted adds produce.
Caught by a unit test asserting that the repeat-collapsing optimisation does
not change the answer.

**Evidence must exist before a hypothesis can cite it.** `Claim.evidence_ids`
has `min_length=1`, so with `Hypothesize` running before any collection every
claim was dropped and every incident came back undiagnosed. The baseline
evidence set is now collected in `Contextualize`. This is the type system
enforcing the right order of work: theorise-first-then-find-support is the
failure mode the whole design exists to prevent.

**Lookback windows anchor to the incident, not to now.** An investigation
running a day after detection searched the wrong day for the causing commit and
reported, with complete confidence, that no commit touched the lineage path.

**A traversal starting at a table must seed every column.** Starting from
`(asset, NULL)` follows only the handful of edges carrying a NULL source column
— join keys, mostly — and reports a blast radius of three for a table feeding
forty downstream columns. Silent under-reporting of blast radius is the failure
that gets a real incident closed early.

**Absence of a corroborating signal must not outrank direct mechanism
evidence.** The verifier was rejecting a correct `JOIN_SEMANTICS` diagnosis —
backed by a structured diff showing INNER became LEFT — because volume had not
also fired. It now rejects only when *none* of a cause's expected signals fired,
and skips the check entirely when a diff or schema event is cited.

**A benign explanation has to survive the measurement it explains away.**
`SEASONAL_LEGITIMATE` was verifying on a profile delta that showed the series
forty times outside its baseline. A verifier that accepts a claim its own cited
evidence refutes is decoration.

**A merged revert has to actually fix the data.** Without it the PR opens, CI
passes, the incident moves to verifying, and the next cycle is still corrupted
because the generative fault is still running — so verification escalates every
remediation it ever approved. Correct behaviour, reporting on a broken world.

**A cohort pool must be keyed by signal.** Cold-start thresholds borrow
strength from columns of the same type and tier, and the pool was keyed on
exactly that — but a nonconformity score only means something in its own
detector's units. D2 reports `-log10(tail probability)`; D3 reports a distance
in baseline spreads. Pooling them produced a borrowed threshold meaningless to
both. Splitting the pool by signal makes cold start stricter and correct, and
it surfaced a test that had been passing by sitting one cycle under the
calibration floor.

**A false-positive rate and a precision complement are different numbers.**
The recalibration job divided false positives by *disposed incidents*, which
is `1 - precision`, and compared it against alpha, which is a per-*cycle*
rate. That declares drift on a detector behaving exactly as specified: a 30%
false-alarm share among raised incidents can be a 0.06% rate per cycle. The
FPR denominator has to come from detection — how many times the detector
looked — which is why `record_scored_cycles` exists and why the detect stage
now writes it.

**A floor drawn from the same distribution as the threshold is not a second
opinion.** The practical-significance floor was set at the cohort's 99.5th
percentile score, which sounds independent and is not: the conformal threshold
is a high quantile of that same distribution, so the floor was a second
threshold at a stricter quantile. A second threshold on the same evidence only
ever costs recall, and it did — 0.67 to 0.50, with precision 0.50 to 0.25.
`tools/design/floor_sweep.py` measures the alternatives rather than arguing
about them.

**A validated palette is only validated against the surface it was checked
on.** Restyling moved the dark surface from `#0b1020` to `#151c20` and made
light the default theme. Every dark pair still cleared, but the light-mode
change marker sat at 2.82:1 against white — tolerable while light was
secondary, not once it was the default. `tools/design/palette_check.py` now
reads the live CSS rather than keeping its own copy of the values, because a
checker that duplicates what it checks is measuring a palette nobody ships.

**A provider's error codes are part of its API, and they disagree.** `health()`
treated 401 and 403 as "key rejected" and everything else as a transport
problem, which is right for Groq. Google answers an invalid key
with **400 INVALID_ARGUMENT**, so a typo'd `GEMINI_API_KEY` produced "models
endpoint returned 400" and sent the reader looking for a network fault.
`ds doctor` exists to answer exactly this question, so getting it wrong was
worse than not having the check. The reason string is now read out of the
response body. Found by probing all three live endpoints with a deliberately
invalid key, which is a thirty-second test that no amount of unit mocking
would have replaced -- a mock returns the status code you assumed.

**A free tier priced at zero disables the budget it was meant to exercise.**
`AgentBudget.max_usd` terminates a run that spends too much, and the Health
page publishes reasoning spend. Pricing Groq and Gemini at their real cost of
zero would have made both of those permanently inert, so the first real bill
would arrive in an environment where neither had ever fired. Free-tier models
are priced at their published paid rate, and an unknown model is priced at the
top of the table rather than at an average, so a model added later fails
towards over-reading spend rather than under-reading it.

**A resolver that is correct is not the same as a resolver that is safe to
share.** `resolve_model` keeps two module-level side tables and clears them at
the start of every call. That is fine for a batch job and wrong for a web
endpoint: FastAPI runs sync handlers in a threadpool, so two visitors tracing
SQL at once would clear each other's state mid-resolve and get transforms
classified against the wrong relation. The playground serialises trace calls
behind a lock, and `test_concurrent_traces_do_not_corrupt_each_other` runs
thirty-two resolves across eight threads and requires every answer to equal a
lone request's. The better fix is to make the side tables per-call; the lock
is the smallest change that makes the public endpoint correct today.

**The SQL reader is lenient about reserved words.** Sized for lineage rather
than execution, it reads `select from where (((` as a projection from a table
named `where` instead of raising. Genuinely non-SQL input still gets a clear
`ParseError`. Harmless for lineage — it cannot invent an edge that is not in
the text — but worth knowing before trusting it as a validator.

## 4. Harness realism fixes

Three changes to the fault generator, each because the harness was measuring
itself rather than the system:

- **Faults propagate downstream.** Corruption that stopped at one table gave
  the correlator nothing to correlate, so 20x compression was unmeasurable.
  Mutations now flow to descendants, attenuated by distance — which also makes
  root election a real problem rather than a formality.
- **Faults reserve their connected neighbourhood.** Two faults whose subtrees
  meet produce one entangled incident. Realistic, and it makes both faults
  unscoreable.
- **Generated models select their parents' real columns.** Projecting
  `null as x` for everything the parent lacked produced a DAG that was
  structurally connected and carried almost no column-level lineage. Column-map
  coverage went from 92.2% to 98.2% when the fixture started looking like a
  dbt model.

## 5. Not built

- Terraform beyond a documented module layout. There is no cloud account to
  apply it against, and an unapplied `terraform plan` proves nothing.
- OIDC and role mapping. The API enforces roles on approvals; there is no
  identity provider behind them.
- LangSmith. Traces are recorded locally through `datasentry_core.telemetry`
  with the same shape.
- The nightly chaos job. `ds eval` is the same thing run on demand.
