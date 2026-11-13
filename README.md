<div align="center">

# DataSentry

**A lakehouse reliability control plane.**
It finds the data regressions that keep every pipeline green and every dashboard wrong,
tells you *why* with evidence you can check in thirty seconds, tells you *who is affected*,
and opens the pull request that fixes it.

`py ds.py demo` — the whole loop, seeded, injected, detected, diagnosed and scored, in under three minutes with nothing installed.
<sub>Measured: 167.6s on a laptop, 60 tables over 12 days — the same world every number in <a href="BENCHMARKS.md">BENCHMARKS.md</a> was measured on.</sub>

</div>

---

## The problem

Enterprise lakehouses run 10k–200k tables. The failures that cost money are not
pipeline crashes — those page someone within minutes. The expensive failures are
**silent semantic regressions**:

- an upstream team changes a currency column from USD to local currency
- a partner starts sending `NULL` instead of `0`
- a dedup key changes and rows fan out 3x
- a `JOIN` quietly goes from `INNER` to `LEFT` in a staging model

Every pipeline is green. Every dashboard is wrong. Every downstream model is
training on poison. Detection today takes days. Root cause takes 4–9 engineer-hours,
almost all of it tracing lineage by hand. Blast radius is usually never computed —
teams fix the table and hope.

**The thesis:** detection is a solved-ish commodity. Root cause, blast radius and
remediation are where the hours go, and that is where column-level lineage plus an
evidence-constrained reasoner creates a step change.

> Monte Carlo tells you the row count is anomalous. DataSentry tells you it is anomalous
> *because* PR #4404 changed a join forty minutes ago, that it has already corrupted two
> feature views and the EMEA revenue dashboard, and here is the PR that fixes it.

---

## Run it

No Docker, no database, no package install, no API key. Python 3.11+ is the
only requirement, and the console has no build step.

**One command, from a fresh clone to a running console:**

```bash
./scripts/dev.sh
```

```powershell
.\scripts\dev.ps1
```

That creates a virtual environment, installs the project, writes a `.env`,
checks the environment, runs the whole loop, and opens the console.

<div align="center">

**Home → `http://localhost:8787`** · **Console → `http://localhost:8787/console`**
API → `/api/v1` · OpenAPI → `/api/docs` · liveness → `/healthz`

</div>

**Or by hand**, which is all the script does:

```bash
py -m pip install -e ".[dev]"   # or: pip install -r requirements.txt
py ds.py doctor                 # python, dependencies, database, provider key, port
py ds.py demo                   # seed, inject, detect, correlate, investigate, act (~3 min)
py ds.py serve --open           # console + API on one port
```

**Or in Docker**, if you would rather not install Python:

```bash
docker compose up --build       # http://localhost:8787
```

`py ds.py doctor` is the first thing to run when something looks wrong. It
checks the Python version, the dependencies, whether the control plane has
been seeded, which reasoner will answer, whether the configured model still
exists in the provider's catalogue, and whether the port is free — and it
prints the command that fixes each one it does not like.

Drive the loop a stage at a time:

```bash
py ds.py seed --fresh          # 200 tables, a dbt-shaped DAG, 21 days of history
                               # (`ds demo` builds 60 over 12 days -- the benchmarked
                               #  world; `ds demo --tables 200` for the full one)
py ds.py inject JOIN_SEMANTICS --advance 18
py ds.py detect --cycles 18
py ds.py correlate
py ds.py rca --verbose
py ds.py execute
py ds.py verify
py ds.py eval                  # score against ground truth; non-zero on regression
py ds.py recalibrate           # turn human dispositions into detector tuning
```

### Reasoning is free by default

DataSentry runs the whole loop with no credentials at all, on a deterministic
reasoner that is a peer rather than a mock. To have a model do the reasoning,
set **one** environment variable — both of these have a free tier and neither
asks for a card:

```bash
# https://console.groq.com/keys
GROQ_API_KEY=gsk_...

# https://aistudio.google.com/apikey
GEMINI_API_KEY=...
```

Put it in `.env` (copy `.env.example`) and run `py ds.py doctor` to confirm it
was accepted. Resolution order is `groq → gemini → heuristic`, free
tiers first. See [Three reasoners, one interface](#three-reasoners-one-interface).

---

## How it works

```
  warehouse writes ─┐
  query history    ─┤
  dbt / Airflow    ─┼──►  event bus  ──►  profiler ──►  column profiles (sketches only)
  GitHub webhooks  ─┘                     lineage  ──►  column-level graph
                                             │
                                             ▼
                                    detector D1..D7 + conformal calibration
                                             │  anomalies
                                             ▼
                                        correlator          ~1 incident per event
                                             │
                                             ▼
              triage → contextualize → hypothesize → evidence → verify → narrate → plan
                                             │  every claim cites an artefact by id
                                             ▼
                                        policy gate
                                    ┌────────┴────────┐
                              auto-execute      approval tray
                                    └────────┬────────┘
                                             ▼
                            pull request · quarantine · pin feature view
                                             │
                                             ▼
                                     verification worker
                                    close, or escalate — never auto-close on a timer
```

### Statistics detect. The reasoner explains. Deterministic code acts.

No blurring. A language model never scores an anomaly, and the reasoning layer
never writes anything.

---

## The six things worth reading the code for

### 1. Calibrated detection, not tuned thresholds

A raw score threshold gives an unknown, per-column-varying false-positive rate.
At 200k columns and even 0.1%, that is 200 alerts a cycle and the product is dead.

Split conformal prediction sets a per-column threshold from the order statistics
of that column's own calibration set. Under exchangeability the marginal FPR is
bounded by the target, **distribution-free** — which matters because normality
across 200k heterogeneous columns is not merely unproven, it is obviously false.

Measured on 4,000 samples of Gaussian noise, and again on a lognormal that looks
nothing like it:

```
conformal observed FPR 0.0104  against a target of 0.0100
```

Three adaptations, each documented where it lives: a weighted calibration set
with a 14-day half-life, a cohort threshold for cold-start columns (marked
`provisional`), and a `k=2` consecutive-breach rule that takes the effective
rate to roughly `alpha^2` for independent noise.

Seven detector families share that one framework — seasonal residual, a
beta-binomial posterior for rates, Wasserstein-1 over t-digests for numeric
drift, chi-square for categorical mass, distinctness ratio for dedup breaks, an
isolation forest for joint anomalies, and a deterministic schema check.

### 2. Column-level lineage, parsed from real SQL

Table-level lineage cannot answer "which downstream feature broke", so it cannot
answer the question the product exists to answer.

A purpose-built SQL reader resolves nested CTEs, subqueries, every join kind,
`SELECT *` expansion against a catalog snapshot, window functions and set
operations into column-level edges with a transform type. Join keys become
table-level edges, because a join key changes row multiplicity and therefore
affects every output column.

**98.2% column-map coverage** on the fixture DAG. Coverage is *reported*, never
assumed — SQL the resolver cannot handle falls back to observed table-level edges
and shows up as a gap on the health page.

Blast radius at depth 8 runs in **0.2–0.4 ms** against a 300 ms budget, using
recursive traversal over an indexed graph rather than a graph database (ADR-002,
with the revisit trigger written down).

### 3. The semantic diff

Hand a model a raw text diff and you get "the SQL changed". Hand it this:

```json
{"kind": "join_type", "severity": "breaking",
 "before": {"kind": "INNER", "on": ["customer_id"], "model": "raw.customers"},
 "after":  {"kind": "LEFT",  "on": ["customer_id"], "model": "raw.customers"},
 "detail": "join to raw.customers changed from INNER to LEFT — admits unmatched rows with NULL measures"}
```

and you get a correct root cause. Structure the evidence and the reasoning falls
out. Almost all of the diagnostic accuracy in this system is bought here rather
than in a prompt.

### 4. Evidence or it did not happen

Hallucinated root causes are the failure mode that kills trust, so the defences
are layered and none of them is a prompt asking nicely:

| Layer | Mechanism |
|---|---|
| The type system | `Claim.evidence_ids` has `min_length=1`. A claim without evidence **cannot be constructed**. |
| Post-parse validation | Every cited id must resolve against this incident's evidence table. One that does not strips the claim. |
| The adversarial verifier | Default position is rejection. Verification must be earned against evidence that *entails* a claim, not merely co-occurs with it. |
| The closed action enum | Whatever survives still cannot name an action the executor does not implement. |

Measured hallucination rate: **0.000**.

Rejected hypotheses are kept, with their reasons, and shown in the UI. Showing
the reasoning the system discarded is what makes people trust the reasoning it
kept.

### 5. The security model is architectural

The reasoning identity is read-only. Not by instruction — there is no write tool
in `packages/datasentry_agent/tools/`, and `tests/unit/test_security_invariants.py`
runs every tool in the catalog against a store that raises on anything but a
SELECT.

A perfectly successful prompt injection through warehouse content buys an
attacker a wrong narrative on a screen.

Alongside it: no action type in the enum is irreversible (there is no DROP,
DELETE or TRUNCATE, and there never was); quarantine never auto-executes at any
confidence; high-risk actions need an admin and are excluded from bulk approve;
`audit_log` is append-only by the absence of the privilege; and profiles store
mergeable sketches and salted digests, never a raw value — which
`test_profiles_contain_no_raw_values` checks against what is actually on disk.

### 6. The loop closes on a human

Capturing an analyst's verdict and never letting it change anything is
theatre. They click "false positive", the detector fires on the same column
tomorrow, and they stop clicking.

`ds recalibrate` reads dispositions, pushes each verdict down onto the
individual anomalies the incident contained, and tallies per cohort and
signal. What a verdict means to a detector is not what it means to a person:

| Verdict | To the detector |
|---|---|
| `correct_cause` | a real incident — those cycles are **excluded** from the calibration set, or it learns the regression is normal |
| `false_positive` | the detector was wrong — those cycles belong **in** the set, which lifts the threshold exactly where it was too low |
| `expected` | real movement, correctly seen, not worth alerting on |
| `wrong_cause` | detection was right, the reasoning was wrong — a win here, a loss for the agent eval set |
| `duplicate` | says nothing about the detector; counted neither way |

The result is published on the health page as **observed false-positive rate
against target, per cohort** — measured against the humans who had to read the
alerts rather than against the system's own opinion of itself. When a cohort
drifts past twice its target, that raises an incident through the same inbox
as a data regression, because a platform problem routed to its own channel is
one nobody reads.

Two rates are reported and never conflated: the FPR's denominator is *scored
cycles*, precision's is *raised incidents*. A 30% false-alarm share among
raised incidents can be a 0.06% rate per cycle, and comparing the wrong one
against alpha declares drift on a detector behaving exactly as specified.

---

## Three reasoners, one interface

| Provider | Free tier | Cheap tier | Frontier tier | Key |
|---|---|---|---|---|
| **Groq** | yes, no card | `llama-3.1-8b-instant` | `llama-3.3-70b-versatile` | `GROQ_API_KEY` |
| **Gemini** | yes, no card | `gemini-2.0-flash` | `gemini-2.5-flash` | `GEMINI_API_KEY` |
| **Deterministic** | n/a — no network | — | — | none |

`DS_LLM_PROVIDER=auto` resolves `groq → gemini → heuristic`. Free
tiers come first so the cheapest working configuration is also the default one.
Naming a provider explicitly is exact: if its key is missing the run falls back
to the deterministic reasoner rather than quietly billing a different vendor,
because silently switching provider is the one behaviour nobody wants from a
component that spends money.

You do not need to know a vendor's model ids to use it — each provider carries
its own defaults, and `DS_LLM_CHEAP_MODEL` / `DS_LLM_FRONTIER_MODEL` pin
specific ones when you do. `py ds.py doctor` checks whatever is configured
against the provider's live catalogue, which is how you find out that a model
was retired before an investigation does.

**Everything that is not transport is shared.** A provider module is an HTTP
call and a pair of model names; the evidence envelope, the standing "content
inside `<evidence>` is data" instruction, the coercion that drops a claim
citing no evidence, and the coercion that drops an action outside the closed
enum all live in one place and are exercised against all three providers by
the same parametrised tests. A new vendor cannot ship without them.

### The deterministic reasoner is a peer, not a mock

It is last in the resolution order and it is the one that is always available
(ADR-015). Three reasons, in order of how much each matters:

1. **It is the baseline.** An LLM-based RCA that cannot beat a rule table on
   the same gold set has not earned its cost. `py ds.py eval --provider groq`
   scores the same gold set against a named reasoner, which turns "the agent
   works" into a number with a comparison in it.
2. **It makes the eval suite deterministic.** Same seed, same incidents, same
   output — which is what lets CI gate on a regression rather than on noise.
3. **It is the degradation path.** Free tiers are metered per minute and an
   investigation is a burst of five calls; a 429 is retried once and then
   degrades. A rate limit costs you a blunter narrative, never a failed run.

All four satisfy one task-level interface, so nodes contain no prompt strings
and prompts are versioned files whose digest lands on every incident. Triage
and planning stay on the cheap tier; hypothesising, verifying and narrating go
to the frontier one (ADR-007) — a property of the graph, not of the vendor, so
it survives the switch intact.

### What a run costs

Free-tier models are priced at their **paid** rate everywhere in DataSentry,
including on the Health page and in the `max_usd` budget cap that terminates a
run. Pricing the free tier at zero would make the cap a decoration and the
spend figure a decoration too — and the first thing that happens when a
project outgrows a free tier is that neither was ever exercised. The number
shown is what the run *would* cost, which is the number worth watching.

---

## The home page, and a playground on the real engines

`http://localhost:8787` explains what DataSentry is, how it works and why — and
lets a visitor put their own input through the production code rather than
watch a recording of it:

- **A live control chart.** The hero is an SPC chart of a revenue metric where
  every job passed and the number doubled anyway. Drag the latest reading, or
  paste your own series: the server runs the production conformal calibrator
  and draws the limits it returns. Tighten the false-alarm target past what the
  history can support and it says so — with *n* readings nothing can promise a
  rate below 1/(n+1).
- **An SQL lab.** Paste a model before and after a change. The production
  column-level resolver traces every output column through CTEs and joins with
  no catalog, the semantic diff classifies the change, and the lineage figure
  marks which columns it reaches — value changed, or aggregated over different
  rows.
- **A live certificate.** The instance's own scores against injected faults,
  read from its health endpoints on every load, including the rows that are
  below spec.

The playground endpoints (`/api/v1/playground/*`) are public and read-only, so
they are built for untrusted input: every field is size-capped before parsing,
requests are rate limited per client and globally, lineage calls are
serialised because the resolver keeps module-level state, and none of it
reaches the append-only audit log.

## The console

`/console` is the operator's view of the same instance.

| Screen | What it is for |
|---|---|
| **Incident inbox** | Ranked by severity and downstream criticality, never by arrival time |
| **Incident workspace** | Timeline and profile chart on **one shared x-axis**, so the commit marker lands on the inflection point |
| **Agent replay** | Every node transition with its cost, its tokens and its parsed output; a scrubber replays the run |
| **Lineage canvas** | Column-level, pan and zoom, edge style encodes the transform, depth pruned server-side |
| **Approval tray** | Keyboard-first: `j`/`k` to move, `a` to approve, `r` to reject |
| **Health** | Observed vs target FPR, agent quality, pipeline coverage, economics |
| **Policy** | Rendered from the server's own description, never a hand-maintained copy |
| **Audit** | Append-only, written by middleware rather than by each handler |

The identity is taken from the instruments of quality control rather than
from software dashboards: pages drawn on engineering computation paper, charts
drawn in pencil graphite with control limits in rubber-stamp ink, a reserved
reject red that appears only where a reading has actually breached, manila
inspection tags and stamped verdicts. Big Shoulders — a condensed grotesque
from Chicago's civic signage — sets the headings, its stencil cut the wordmark
and stamps, and Atkinson Hyperlegible everything that is read. Night shift is
the same bench under a lamp.

Two rules outrank the aesthetic, and both are checkable. Severity is never
encoded by colour alone — every one carries a glyph and a label. Chart marks
live on their own scale, validated for colour-vision deficiency against the
real surface colour in both themes by `tools/design/palette_check.py`, which
reads the live CSS rather than keeping its own copy of the values.

---

## Measured, not asserted

Every number below comes from the fault harness, which records ground truth at
injection time and never touches the detectors. `ds eval` runs the loop over
several seeds and **exits non-zero** when a blocking gate regresses.

**Detection primitives** — properties of the algorithms, measured by the test suite:

| Property | Measured | Target |
|---|---|---|
| Conformal observed FPR (Gaussian, α=0.01) | **0.0104** | ≤ 0.02 |
| Conformal observed FPR (lognormal, α=0.01) | **≤ 0.03** | distribution-free |
| Column-map coverage, 42-model DAG | **0.982** | ≥ 0.90 |
| Blast radius traversal, depth 8 | **0.2–0.4 ms** | ≤ 300 ms |
| HyperLogLog error, p=14 | **0.71%** | ~0.8% |
| Profile size per column per cycle | **~670 bytes** | ≤ 3 KB |

**End to end**, three independent worlds of 60 tables, 6 faults and 2 decoys
each, scored against ground truth. Mean with the spread across seeds, because
a metric that moves 0.16 between worlds cannot be tuned by 0.05 and called an
improvement:

| Gate | Measured | Spread | Target | |
|---|---|---|---|---|
| Hallucination rate | **0.000** | — | ≤ 0.02 | ✅ |
| Decoy false-positive rate | **0.000** | — | ≤ 0.10 | ✅ |
| Root-asset accuracy | **1.000** | 1.00–1.00 | ≥ 0.85 | ✅ |
| Cost per incident | **$0.0000** | — | ≤ $0.15 | ✅ |
| Root-cause accuracy | **0.611** | ±0.08 | ≥ 0.80 | ✗ |
| Detection recall | **0.556** | ±0.08 | ≥ 0.90 | ✗ |
| Incident precision | **0.523** | ±0.16 | ≥ 0.75 | ✗ |
| Blast-radius recall | **0.729** | ±0.23 | ≥ 0.95 | ✗ |

**Four gates are not met, and they are in the README because a gate that only
ever reports success is not a gate.** Each has a specific cause, not "needs
tuning" — `BENCHMARKS.md` carries the full reasoning and the run-by-run trend:

- **Recall** is limited by cadence tiering. A cold table is profiled every
  eighth cycle, so a two-consecutive-breach rule needs sixteen cycles to fire on
  one. That is the design working; the metric needs a per-tier deadline.
- **Precision** is the weakest number. The remaining false positives are small
  clusters whose minimum-effect thresholds are still hand-set per signal in
  `config.py` instead of derived from each cohort's observed spread — the last
  piece of per-signal tuning, and exactly what conformal calibration was meant
  to eliminate.
- **Root-cause accuracy** is 1.00 on every fault that lands a commit; the
  semantic diff makes that case near-certain. The misses are source-side faults
  where the *deterministic* reasoner has no code change to point at. Closing
  that gap is what the frontier model is for, and measuring it is the point of
  shipping both.
- **Blast-radius recall** now measures the right thing (column-level truth
  against a column-level answer) but still misses aggregate renames — and at
  ±0.23 across seeds it is the noisiest number here, which is itself the
  finding.

The spread column is doing real work. An earlier single-seed run showed
root-asset accuracy at 0.75 and root-cause at 0.33, which looked like a
regression and was noise: across three seeds root-asset is 1.00 everywhere.
Six faults per world is a small denominator, and one fault landing differently
moves a metric by 0.17. Numbers here are reported as a mean over seeds for
that reason, and single-run figures are not quoted as measurements.

`BENCHMARKS.md` also records the two changes that made the numbers *worse* —
correct statistical fixes that a better-calibrated detector paid for in
precision — rather than quietly reverting them.

---

## Repository

```
datasentry/
├── ds.py                      one entry point: doctor, demo, serve, seed, eval, ...
├── scripts/dev.sh, dev.ps1    fresh clone to running console, one command
├── .env.example               every setting, annotated; copy to .env
├── Dockerfile                 one image, console and API on one port
├── docker-compose.yml         `docker compose up` if you would rather not install Python
├── render.yaml, fly.toml      deployment blueprints (Render free plan, Fly with a volume)
├── DEPLOY.md                  what breaks in a deployment, and why
├── reference.md               SSOT — status, decisions, what is next
├── BENCHMARKS.md              appended by `ds eval`, never overwritten
├── docs/                      01–08 the design; 09 what was actually built
├── packages/
│   ├── datasentry_core/       models, store, event bus, clock, redaction, telemetry
│   ├── datasentry_sketches/   t-digest, HyperLogLog, Count-Min, Bloom — all mergeable
│   ├── datasentry_detectors/  D1–D7, conformal calibration, isolation forest
│   ├── datasentry_lineage/    SQL reader, resolver, semantic diff, graph traversal
│   └── datasentry_agent/      state graph, nodes, read-only tools, guardrails, prompts
│       └── providers/         groq · gemini · deterministic, one interface
├── services/                  profiler, correlator, policy gate, executor, verification, api
├── web/                       the console — no build step
├── tools/faultgen/            200-table world, 9 fault types, 3 decoys, ground truth
├── tools/design/              palette validator and the effect-floor sweep
├── evals/                     the harness and its CI gates
├── tests/                     115 tests: unit, integration, security invariants
└── infra/                     Postgres DDL, docker-compose, Terraform layout
```

Neither `datasentry_detectors` nor `datasentry_lineage` has a third-party
dependency. No numpy, no scipy, no sqlglot — the statistics are a few hundred
lines of well-understood numerics written out where whoever has to trust the
output can read them, and the detection path has no compiled dependency and
starts instantly.

---

## Deploying it

One process serves the console, the API and the WebSocket on one port, so there
is no frontend to build and no second service to run. Blueprints are committed
for [Render](render.yaml) (free plan, one click: **New → Blueprint**) and
[Fly](fly.toml), and the [`Dockerfile`](Dockerfile) runs anywhere that runs a
container. Vercel and Netlify are not a fit: they are serverless, with no
long-lived process, no WebSocket and no writable disk.

```bash
docker compose up --build
```

Two things decide whether a deployment actually works, and both are in
[`DEPLOY.md`](DEPLOY.md):

1. **The image ships seeded.** The Docker build runs the demo once and bakes
   a compact snapshot into the image; on boot it is copied into `/data` only
   when `/data` is empty. A free-tier cold start comes up populated in seconds,
   and a persistent disk is seeded once and never overwritten.
2. **Mount a volume at `/data` to keep changes.** The control plane is SQLite
   on disk, and on most PaaS free tiers the filesystem is ephemeral: console
   changes reset to the snapshot on restart. Fly and Compose mount a volume;
   on Render, switch to `starter` and uncomment the `disk` block.

`/healthz` is liveness and touches no database; `/readyz` is readiness and
reports `seeded: false` until you seed. Before it faces anyone but you, read the
security section of `DEPLOY.md`: there is no authentication, and
`DS_PROFILE_SALT` must change or the hashed sample values in a profile are an
encoding rather than a hash.

---

## What this is honest about

- **There is no `prod`.** This is a portfolio system, and
  `docs/02-architecture.md` §9 says so. What is true is that it is built to
  production standards, and that is the defensible claim.
- **Several substitutions.** An append-only event table stands in for Kafka; a
  purpose-built SQL reader for sqlglot; SQLite for Postgres. Each preserves the
  properties that matter and each is listed with its reasoning in
  `docs/09-implementation-notes.md` §1.
- **The referential-integrity detector is inert.** It is implemented and it does
  not fire, because computing FK violations needs a parent key domain the local
  harness does not feed. It reports nothing rather than reporting a number it
  cannot honestly compute.
- **The gold set is generated, not frozen.** Reproducible from a seed, but not
  stable across code changes — which is the next thing to fix before the CI gate
  fully means what it should.

`docs/09-implementation-notes.md` §3 lists fourteen bugs the implementation found
in the design, each one a case where the spec was right in intent and the obvious
implementation of it was quietly wrong. That list is the most useful thing in the
repository.

---

<div align="center">
<sub>Built from an eight-document design spec. The docs describe the target; <code>docs/09</code> describes what exists.</sub>
</div>
