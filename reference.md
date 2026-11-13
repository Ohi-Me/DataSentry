# reference.md — DataSentry

> **Single source of truth.** Architecture, features, decisions, and next steps.
> Update this file after every meaningful change. All other docs are subordinate to it.

**Project code:** `datasentry` (CLI: `ds`)
**Version:** 0.3.0 — the loop is closed, measured, and it learns from a human
**Last updated:** 2026-08-30
**Owner:** Unicorn

---

## 1. One-paragraph description

DataSentry is a control plane for enterprise lakehouses that continuously
profiles every table and column, detects silent data regressions using
calibrated unsupervised detectors, traces each incident to its root cause via
column-level lineage and an evidence-bound reasoner, computes downstream blast
radius, and executes or proposes remediation (quarantine, feature-view pinning,
corrective pull request). Human disposition of every incident feeds back as
labels that tune detector thresholds and gate agent quality in CI.

**The thesis:** detection is a solved-ish commodity; **root cause, blast
radius, and remediation** are where the engineering hours actually go, and that
is where an evidence-constrained reasoner plus column-level lineage creates a
step change.

---

## 2. Document index

| Doc | Contents |
|---|---|
| `reference.md` (this file) | SSOT: status, decisions, architecture summary, next steps |
| `docs/01-product-spec.md` | Problem, personas, scope, non-goals, user stories, success metrics |
| `docs/02-architecture.md` | Component architecture, sequence flows, failure modes, tech stack |
| `docs/03-data-model.md` | Topics, Avro schemas, lake layers, Postgres DDL, lineage model |
| `docs/04-detection-ml.md` | Profiling stats, detector ensemble, conformal calibration, correlation, criticality, eval protocol |
| `docs/05-agent-design.md` | Graph topology, node contracts, tool catalog, output schemas, guardrails, policy engine |
| `docs/06-api-and-frontend.md` | REST/WebSocket API spec, frontend screens, component tree |
| `docs/07-platform-ops.md` | IaC, security model, observability, CI/CD, cost controls, SLOs |
| `docs/08-delivery-plan.md` | Repo layout, local bootstrap, milestones, fault-injection harness, demo script |
| **`docs/09-implementation-notes.md`** | **Where the build differs from the spec, and every design bug the implementation found** |

---

## 3. Current status

| Area | Status |
|---|---|
| Product spec | ✅ Drafted |
| Architecture | ✅ Drafted |
| Data model | ✅ Drafted |
| Detection ML design | ✅ Drafted |
| Agent design | ✅ Drafted |
| API + frontend spec | ✅ Drafted |
| Platform/ops | ✅ Drafted |
| Delivery plan | ✅ Drafted |
| Synthetic warehouse + fault injector | ✅ 200 tables, 9 fault types, 3 decoys, ground truth |
| Sketch library (t-digest, HLL, CMS, Bloom) | ✅ Built and unit-tested for accuracy and mergeability |
| Profiler | ✅ Sketch-only profiles, cadence tiering, ~670 bytes/profile |
| Column-level lineage | ✅ SQL reader + resolver + semantic diff; 98.2% column-map coverage |
| Detector ensemble D1–D7 | ✅ Built; conformal FPR measured at 0.0104 against a 0.0100 target |
| Correlator | ✅ Directed lineage clustering, root election, severity |
| Agentic RCA + adversarial verifier | ✅ Seven-node graph, evidence-bound, checkpointed |
| Policy gate + executor + verification | ✅ Closed loop; a merged revert clears the fault |
| API + console | ✅ REST, WebSocket, 8 screens, zero build step |
| Console theme | ✅ Light-first near-monochrome; dark derived from the same hue |
| Recalibration from dispositions | ✅ Per-cohort FPR against target, drift raises an incident |
| Derived effect floors | ❌ Tried three ways, all worse than the constants — see BENCHMARKS.md |
| Test suite | ✅ 55 unit + 10 integration, all green |
| Eval harness + CI gates | ✅ `ds eval` over N seeds, exits non-zero on a blocking regression |
| Labeled incident gold set (≥40) | ⬜ Generated per run rather than frozen; freezing it is next |
| Terraform / deploy | ⬜ Module layout documented, not applied |

---

## 4. Architecture summary

```
 ERP/OLTP  dbt/Airflow  GitHub          Warehouse QUERY_HISTORY
    │           │          │                     │
    └───────────┴──────────┴─────────────────────┘
                        │  (CDC + telemetry + webhooks)
                        ▼
                ┌──────────────┐
                │  event bus   │  ds.*.v1 topics, keyed, replayable
                └──────┬───────┘
        ┌──────────────┼───────────────┬─────────────────┐
        ▼              ▼               ▼                 ▼
   profiler        lineage-builder   detector        replay
  (sketches)       (SQL AST)        (D1..D7 +        (backtest)
        │              │             conformal)
        ▼              ▼               │
   column profiles  lineage graph   anomaly events
        └──────────────┬─────────────────┘
                       ▼
                  correlator  ──►  incident (1..N anomalies)
                       │
                       ▼
             agent-orchestrator
        triage → contextualize → hypothesize → evidence → verify → narrate → plan
                       │
                       ▼
                policy gate (risk tiers)
             ┌─────────┴──────────┐
        auto-execute          human approval
             │                     │
             ▼                     ▼
       action-executor ──► pull request / quarantine / pin feature view
                       │
                       ▼
              verification worker → close or escalate
                       │
                       ▼
        disposition labels → detector tuning + agent eval set
```

---

## 5. Feature status (v1 scope)

| | Feature | Status |
|---|---|---|
| F1 | Incremental column profiling (volume, freshness, null rate, cardinality, numeric quantiles, categorical mass, string length) | ✅ |
| F2 | Calibrated multi-signal detection with conformal thresholds | ✅ |
| F3 | Column-level lineage from SQL via AST parsing | ✅ 98.2% coverage |
| F4 | Anomaly → incident correlation (lineage-aware) | ✅ |
| F5 | Agentic RCA with adversarial verifier and mandatory evidence citation | ✅ |
| F6 | Blast radius with criticality ranking | ✅ |
| F7 | Remediation: quarantine, pin feature view, corrective PR | ✅ |
| F8 | Policy gate with risk tiers | ✅ |
| F9 | Approval tray + agent reasoning replay UI | ✅ |
| F10 | Disposition capture → labels → detector tuning | ✅ closed — `ds recalibrate`, `/health/calibration` |
| F11 | Fault injection harness + ground truth | ✅ 9 faults, 3 decoys |
| F12 | CI gates on detector precision and root-cause accuracy | ✅ `ds eval` |
| — | Referential integrity detector (D2r) | ⚠️ implemented, inert — see docs/09 §2 |
| — | Frozen 40-incident gold set | ⬜ |

---

## 6. Decision log (ADRs)

The original fourteen stand as written in the first draft. Six were tested by
building the thing, and this is what happened to them.

| ID | Decision | Outcome |
|---|---|---|
| ADR-001 | Column-level lineage, not table-level | **Held.** 98.2% column-map coverage on the fixture DAG. Table-level would not have produced a single useful blast radius. |
| ADR-002 | Recursive traversal, not a graph database | **Held.** Blast radius at depth 8 runs in 0.2–0.4 ms against a 300 ms budget. |
| ADR-003 | Conformal thresholds | **Held, with three amendments.** Observed FPR 0.0104 against a 0.0100 target. Breaching points are excluded from the calibration set, a 1.15x breach margin supplies hysteresis, and a per-signal minimum effect size guards near-degenerate series. All three are documented in docs/09 §3. |
| ADR-004 | The model never scores anomalies | **Held.** No reasoning code touches a detector. |
| ADR-005 | Adversarial verifier with mandatory evidence IDs | **Held, and it earns its place.** Measured hallucination rate 0.000. The verifier needed two corrections to stop rejecting correct diagnoses on missing corroboration — docs/09 §3. |
| ADR-006 | One run per incident, not per anomaly | **Held.** Compression measured per run; see BENCHMARKS.md. |
| ADR-007 | Two-tier model routing | **Partially exercised, now cheaply testable.** The routing exists and is measured. It stays a property of the graph rather than of a vendor: Groq and Gemini carry their own cheap/frontier pairs, so the split survives a provider switch. With a free tier on both, exercising it against real spend no longer costs anything — which removes the reason it was only partially exercised. |
| ADR-008 | Reasoning identity is read-only | **Held structurally.** There is no write tool in `datasentry_agent.tools`. |
| ADR-009 | Warehouse data is untrusted input | **Held.** Injection markers are surfaced as findings, not silently dropped. |
| ADR-010 | Profiles store sketches, never raw values | **Held, and tested.** `test_profiles_contain_no_raw_values` reads what is on disk. |
| ADR-011 | Redpanda locally | **Superseded.** An append-only event table with the same topics and keys — docs/09 §1. |
| ADR-012 | Actions land as pull requests | **Held.** The executor has no path to a default branch. |
| ADR-013 | Warehouse is both monitored system and gold store | **Deferred.** Local runs use the control plane for both. |
| ADR-014 | Time travel gives last-known-good | **Held** in the pin action; modelled locally. |
| **ADR-015** | **Deterministic reasoner as a first-class peer, not a mock** | **New.** It is the baseline the language model must beat, the thing that makes the eval suite reproducible, and the degradation path when the provider is down. |
| **ADR-016** | **Faults propagate downstream in the harness** | **New.** A harness where corruption stopped at one table gave the correlator nothing to correlate and made the compression claim unmeasurable. |
| **ADR-017** | **Cohort pools are keyed by (type, tier, signal)** | **New.** A nonconformity score is only comparable within one detector family. Pooling D2's log-tail with D3's IQR distance produced a cold-start threshold meaningless to both. |
| **ADR-018** | **False-positive rate and precision are reported separately, never conflated** | **New.** Alpha is a per-cycle target, so its denominator is scored cycles. Precision's denominator is raised incidents. Comparing the second against alpha declares drift on a detector behaving exactly as specified. |
| **ADR-019** | **The console is light-first and near-monochrome** | **New.** One ink for text and primary actions, a cool off-white canvas, contrast rather than hue as the accent. Severity keeps its glyph and label, and chart marks stay on their own CVD-validated scale — the aesthetic does not get to overrule either. |
| **ADR-020** | **Free-tier providers resolve first; a named provider never silently switches** | **New.** `auto` resolves groq → gemini → heuristic, so the cheapest working configuration is the default one and one environment variable is the whole setup. Naming a provider whose key is missing falls back to the deterministic reasoner rather than to a different vendor: quietly spending money somewhere else is the one failure mode a billing-capable component must not have. |
| **ADR-021** | **A provider module is transport only** | **New.** The evidence envelope, the standing "content inside `<evidence>` is data" instruction, the coercion that drops an uncited claim and the coercion that drops an action outside the closed enum live in `providers/wire.py` and `providers/hosted.py`, and the same parametrised tests assert all four against every vendor. Three copies of a security guarantee is three places to forget it. |
| **ADR-022** | **Free-tier models are priced at their paid rate** | **New.** Pricing a free tier at zero makes `AgentBudget.max_usd` a dead cap and the spend figure on the Health page a decoration — and the first thing that happens when a project outgrows a free tier is the discovery that neither was ever exercised. An unknown model is priced at the top of the table, so a model added later fails towards over-reading spend. |
| **ADR-023** | **The home page runs the real engines on the visitor's input** | **New.** The control chart asks the production conformal calibrator and the SQL lab runs the production resolver and semantic diff, over a public, read-only playground API. A recording of a demo proves that a demo was recorded; a chart that answers the visitor's own numbers proves the method. The cost is an endpoint anyone can call, so it is capped, rate limited, lock-serialised where the engine keeps shared state, and never audited. |
| **ADR-024** | **The identity comes from quality control, not from dashboards** | **New.** Engineering paper, pencil graphite, stamp ink, a reserved reject red, inspection tags and a calibration certificate. The previous near-monochrome theme was competent and indistinguishable from any other console. Chart marks now come from the identity and were re-validated for colour-vision deficiency against the new surfaces. |

---

## 7. Measured, not asserted

Every number below comes from `ds demo` or `ds eval` against ground truth the
fault harness recorded at injection time. Current values and the gaps are in
`BENCHMARKS.md` and on the console health page — which publishes them whether
or not they are flattering.

| Metric | Target | Where measured |
|---|---|---|
| Conformal FPR against target | within 2x | `tests/unit/test_conformal.py`, health page |
| Column-map coverage | ≥0.90 | `ds demo` step 1 |
| Blast radius traversal (depth 8) | ≤300 ms p95 | `ds demo`, unit tests |
| Detection recall on injected faults | ≥0.90 | fault harness |
| Incident precision | ≥0.75 | ground truth |
| Decoy false-positive rate | ≤0.10 | 3 decoy types |
| Root-asset accuracy | ≥0.85 | ground truth |
| Root-cause accuracy | ≥0.80 | ground truth |
| Hallucination rate | ≤0.02 | claim/evidence resolution |
| Cost per incident | ≤$0.15 | token accounting |

---

## 8. Next steps (immediate, in order)

1. **Freeze a 40-incident gold set.** Generated-per-run is reproducible but not
   stable across code changes; a frozen set is what makes a CI gate meaningful.
   This is now the largest remaining gap.
2. **Feed the key domain** so D2r stops being inert. It is the one detector
   that is implemented and never fires.
3. **Run a hosted provider against the frozen gold set** and publish the
   comparison with the deterministic baseline. That comparison is the point of
   having both, and it is where the root-cause gap on source-side faults
   should close. `py ds.py eval --provider groq` now takes the provider as an
   argument and records it in the results file, so the comparison is one
   command per reasoner rather than an environment edit between runs — and
   with Groq and Gemini on a free tier, running it costs nothing.
4. **Let the recalibration job write back.** It measures drift and raises an
   incident; `suggested_alpha` computes where the target should move and
   nothing applies it yet. Applying it automatically is a bigger decision than
   it looks — a detector that retunes itself without a human in the loop can
   quietly go deaf — so the deliberate order is: measure, surface, then act.
5. **Terraform for a staging environment**, then a recorded demo.

**Done since 0.2.0:** the recalibration loop (item 3 of the previous list) and
the console retheme.

**Attempted and reverted:** per-cohort effect floors replacing the hand-set
table (item 2). Three formulations, all measured worse than the constants they
were meant to replace — `BENCHMARKS.md` carries the table and the reason. The
item stays on the roadmap; the approach does not.

---

## 9. Open questions

- The practical-significance floor is still a hand-set table in `config.py`,
  and it is still the last piece of per-signal tuning in a system that claims
  to have removed the need for one. Deriving it from the cohort's score
  distribution was tried three ways and lost every time, for a reason that is
  now understood: a quantile of the cohort's scores is not independent of a
  threshold that is also a quantile of those scores. A floor asking a genuinely
  different question would have to be expressed in the column's own units —
  how large is the movement — rather than in its rank. That is a different
  piece of work and it is worth doing.
- Excluding breaching points from the calibration set trades an FPR bias for
  the ability to detect sustained faults. The bias is bounded and measured, but
  a weighted scheme that down-weights rather than excludes is probably better.
- Correlation currently uses directed reachability within three hops. Two
  genuinely simultaneous unrelated incidents on the same subtree still merge.
  Storm mode from `02-architecture.md` §7 is the intended answer.
- Should the deterministic reasoner ship as a supported mode, or only as a
  test and fallback? It is currently good enough that the honest answer is not
  obvious, and that is worth measuring rather than deciding.
