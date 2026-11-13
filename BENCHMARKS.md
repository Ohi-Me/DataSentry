# Benchmarks

Appended to, never overwritten. A single final number says nothing; watching a
metric move across runs is what says whether a change was an improvement or a
coincidence.

`ds eval` appends a row automatically. The narrative entries below are the ones
worth reading — each records what changed and what it did to the numbers.

---

## Detection primitives

Measured directly by the test suite rather than by the end-to-end harness, so
these are properties of the algorithms rather than of any particular world.

| Property | Measured | Target |
|---|---|---|
| Conformal observed FPR, Gaussian noise, α=0.01 | **0.0104** | ≤ 0.02 |
| Conformal observed FPR, lognormal(0, 2.5), α=0.01 | **≤ 0.03** | distribution-free |
| t-digest p50 error, n=20,000 | **0.01%** | — |
| t-digest p99 error, n=20,000 | **0.02%** | — |
| HyperLogLog relative error, p=14, n=50,000 | **0.71%** | ~0.8% claimed |
| Column-map coverage, 42-model fixture DAG | **98.2%** | ≥ 90% |
| Models parsed without fallback | **42 / 42** | — |
| Blast radius, depth 8 | **0.2–0.4 ms** | ≤ 300 ms p95 |
| Profile size, per column per cycle | **~670 bytes** | ≤ 3 KB |
| Profiler throughput | **~10 ms per table-cycle** | — |

---

## End-to-end, against ground truth

One world of 60 tables, 12 days of history, 6 faults and 2 decoys. Ground truth
is recorded by the fault harness at injection time and never touched by the
detection or reasoning path.

| run | recall | precision | decoy FPR | root asset | root cause | halluc. | blast | cost |
|---|---|---|---|---|---|---|---|---|
| 2026-08-29 baseline | 0.50 | 0.14 | 0.50 | — | 0.00 | 0.000 | 1.00 | $0.0000 |
| + benign dismissal, decoy scoring | 0.50 | 0.50 | 0.00 | 1.00 | 0.00 | 0.000 | 1.00 | $0.0000 |
| + evidence before hypotheses | 0.67 | 0.31 | 0.00 | 1.00 | 1.00 | 0.000 | 0.50 | $0.0000 |
| + verifier signature relaxation | 0.67 | 0.36 | 0.00 | 1.00 | 1.00 | 0.000 | 0.67 | $0.0000 |
| + seasonal backfitting, prior cap | 0.67 | 0.21 | 0.00 | 1.00 | 0.50 | 0.000 | 0.61 | $0.0000 |
| + correlator dedup by lineage | 0.67 | 0.40 | 0.00 | 1.00 | 0.67 | 0.000 | 0.61 | $0.0000 |
| **+ column-aware truth, closed loop** | **0.67** | **0.50** | **0.00** | **1.00** | **0.67** | **0.000** | **0.69** | **$0.0000** |
| target | 0.90 | 0.75 | 0.10 | 0.85 | 0.80 | 0.02 | 0.95 | 0.15 |

### What each step did, and why

**+ benign dismissal.** An investigation that concludes `SEASONAL_LEGITIMATE`
now dismisses the incident rather than leaving it in the inbox, and decoy
scoring counts an incident that *survived triage* rather than any statistical
twitch. A backfill really does triple the row count; a detector that cannot see
that is not detecting anything. Decoy FPR 0.50 → 0.00.

**+ evidence before hypotheses.** `Claim.evidence_ids` has `min_length=1`, and
`Hypothesize` was running before any evidence had been collected — so every
claim was dropped and every incident came back undiagnosed. Moving the baseline
evidence set into `Contextualize` took root-cause accuracy from 0.00 to 1.00 in
one change. This was the single largest defect in the build.

**+ verifier signature relaxation.** The verifier was rejecting a correct
`JOIN_SEMANTICS` diagnosis — backed by a structured diff showing INNER became
LEFT — because the volume signal had not *also* fired. Direct mechanism evidence
now outranks a missing corroborating signal.

**+ seasonal backfitting, prior cap.** Two genuine statistical bugs, found by
unit tests rather than by the harness: multi-seasonal decomposition had a term
that cancelled itself out, and the beta prior could carry twenty thousand
pseudo-observations from ninety real ones. Both fixes are correct and both
*hurt* the end-to-end numbers, because a better-calibrated detector fires on
more real-but-minor movements. Precision 0.36 → 0.21. Recorded here rather than
quietly reverted.

**+ correlator dedup by lineage.** The regression above was mostly the
correlator: each cycle elected a different root as more of the DAG lit up, so
one propagating fault became four incidents each titled after a symptom.
Deduplicating by lineage proximity — and re-rooting upstream when the true cause
emerges later — recovered precision 0.21 → 0.40, and excluding joint-profile
anomalies from founding an incident took it to 0.50.

**+ column-aware ground truth.** Blast-radius recall was scoring a *column-level*
answer against a *table-level* truth: a null flood in `amount` was marked as
missing every downstream table that does not read `amount`. Grading the precise
answer against the coarse one is grading against the thing we deliberately did
not build (ADR-001). Truth is now column-aware, 0.61 → 0.69.

**+ the closed loop.** An approved corrective PR now actually clears the
injected fault, so verification resolves rather than escalating every
remediation it ever approved.

---

## Still below target

Four gates are not met, and the reasons are specific rather than "needs tuning".

**Detection recall 0.67 (target 0.90).** Cadence tiering profiles cold tables
every eighth cycle, so a fault on a tier-3 bronze table needs sixteen cycles
before a two-consecutive-breach rule can fire. That is the design working as
intended — compute scales with cadence, not table count — but it means a fault
injected on a cold table inside an 18-cycle window may legitimately not be
detectable yet. The honest fix is to score recall with a per-tier detection
deadline rather than a single window.

**Incident precision 0.50 (target 0.75).** The weakest number. The remaining
false positives are small clusters on freshness and cardinality whose
minimum-effect thresholds are hand-set per signal in `config.py` rather than
derived from each cohort's observed score spread — the last piece of per-signal
tuning, and exactly what conformal calibration was supposed to eliminate.

**Root-cause accuracy 0.67 (target 0.80).** Correct on every fault that lands a
commit — the semantic diff makes that case near-certain. The misses are
source-side faults (`UNIT_CHANGE`, `NULL_FLOOD`) where the deterministic
reasoner has no code change to point at and picks the more general
`SOURCE_SCHEMA_DRIFT` over the specific class. This is precisely the gap a
model should close, and measuring it is the point of shipping more than one
reasoner: `py ds.py eval --provider groq` scores the same worlds against a
named reasoner and writes the provider name into the results file, so the
comparison is a command rather than an environment edit. Groq and Gemini both
have a free tier, so there is no longer a cost argument for leaving this
unmeasured.

**Blast-radius recall 0.69 (target 0.95).** Now measuring the right thing, but
the remaining gap is real: aggregate columns renamed by a downstream model
(`total_amount` from `amount`) are traced by the resolver but not always by the
incident's chosen traversal targets.

---

## Multi-seed, which is the number that counts

Everything in the table above is one world. Six faults per world is a small
denominator, and one fault landing differently moves a metric by 0.17 — so
single-run figures are useful for spotting a break and useless for judging an
improvement.

Three independent worlds of 60 tables, `ds eval --seeds 3 --tables 60`, after
the recalibration loop and the per-signal cohort split:

| gate | mean | range | target | |
|---|---|---|---|---|
| Detection recall | 0.556 | 0.50–0.67 | ≥ 0.90 | below |
| Incident precision | 0.523 | 0.38–0.75 | ≥ 0.75 | below |
| Decoy false-positive rate | 0.000 | 0.00–0.00 | ≤ 0.10 | **pass** |
| Root-asset accuracy | 1.000 | 1.00–1.00 | ≥ 0.85 | **pass** |
| Root-cause accuracy | 0.611 | 0.50–0.67 | ≥ 0.80 | below |
| Hallucination rate | 0.000 | 0.00–0.00 | ≤ 0.02 | **pass** |
| Blast-radius recall | 0.729 | 0.44–1.00 | ≥ 0.95 | below |
| Median cost per incident | 0.000 | 0.00–0.00 | ≤ 0.15 | **pass** |

Spread across seeds: recall ±0.08, precision ±0.16, root-cause ±0.08,
blast-radius ±0.23.

**What the spread taught.** A single-seed run immediately before this showed
root-asset accuracy at 0.75 and root-cause at 0.33 — an apparent regression
that prompted a hunt for the cause. Across three seeds root-asset is 1.00 in
every world. The regression was one fault landing on a different asset. The
lesson is cheap to state and was not cheap to learn: at ±0.16, precision
cannot be compared between two single runs at all, and any change justified by
such a comparison is justified by noise.

---

## Negative results

Kept because they are the expensive part. A repository that only records what
worked invites the next person to spend a day rediscovering what did not.

### Deriving the practical-significance floor from the cohort — worse, three ways

The top item on the roadmap was replacing the hand-set per-signal effect
floors in `config.py` with a figure derived from each cohort's own score
distribution. It is the last piece of per-signal tuning in a system whose
central claim is that conformal calibration removes the need for it.

Three formulations, one seed, 60 tables, everything else held constant
(`tools/design/floor_sweep.py`):

| metric | constant | cohort p99.5 | cohort p90 | cohort median |
|---|---|---|---|---|
| detection recall | 0.67 | 0.67 | 0.67 | 0.67 |
| **incident precision** | **0.40** | 0.33 | 0.33 | 0.33 |
| decoy FPR | 0.00 | 0.00 | 0.00 | 0.00 |
| **root-asset accuracy** | **1.00** | 0.75 | 0.75 | 0.75 |
| **root-cause accuracy** | **0.67** | 0.33 | 0.33 | 0.33 |
| blast-radius recall | 0.44 | 0.69 | 0.69 | 0.69 |

The hand-set constants win on three of six and lose on one. More telling: all
three derived variants produced *identical* scores, across quantiles from the
median to the 99.5th percentile. A knob that changes nothing across two orders
of magnitude of its own range is not the binding constraint on anything.

**Why it fails.** A quantile of the cohort's score distribution is not
independent of the conformal threshold, because the threshold is a quantile of
that same distribution. The "second bar" was a second threshold wearing a
different hat, and a second threshold on the same evidence can only ever cost
recall — it cannot add information. The first attempt at p99.5 made that
obvious by being brutal about it (recall 0.67 to 0.50, precision 0.50 to 0.25
in the end-to-end demo); the gentler quantiles just stopped mattering.

A floor that genuinely asks a different question has to be expressed in the
column's own units — how big is this movement — rather than in its rank among
the cohort's scores. That is the next thing to try, and it is a different
piece of work from the one attempted here.

The constants stay. The discomfort is recorded rather than resolved, which is
the honest state: they measure better and they are still the wrong shape.

---

## Method

- Ground truth is a `FaultRecord` written at injection time. Nothing in the
  detection or reasoning path can read it.
- `ds eval` runs several seeds; the spread across worlds is reported alongside
  the mean, because a metric that moves 0.2 between worlds cannot be tuned by
  0.05 and called an improvement.
- Blocking gates — decoy FPR, root-asset accuracy, hallucination rate, cost per
  incident — fail the build. The rest are tracked here.
- Cost reads $0.0000 because the default reasoner is deterministic. The token
  accounting, the price table and the budget enforcement are all live; they have
  nothing to bill.

---

## Automated runs

Appended by `ds eval`. Needs at least 60 tables: below that the fault
reservations collide and the numbers measure the collision, not the system.

<!-- ds-eval-runs -->

| run | seeds | tables | detection recall | incident precision | decoy false-positive rate | root-asset accuracy | root-cause accuracy | hallucination rate | blast-radius recall | median cost per incident |
|---|---|---|---|---|---|---|---|---|---|---|
| 2026-08-30 10:36 | 3 | 60 | 0.556 | 0.523 | 0.000 | 1.000 | 0.611 | 0.000 | 0.729 | 0.000 |
| 2026-08-29 17:02 | 1 | 60 | 0.667 | 0.444 | 0.000 | 1.000 | 0.667 | 0.000 | 0.500 | 0.000 |
