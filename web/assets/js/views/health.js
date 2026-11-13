/* The health page.
 *
 * This is a product surface, not an internal dashboard. A monitoring system
 * that publishes its own false-positive rate against target is making a much
 * stronger claim than one that publishes only what it caught — and if the
 * numbers are bad, they should be visible while they are bad.
 *
 * Five panels: which reasoner is answering, detector calibration, agent
 * quality, pipeline, economics.
 *
 * The reasoner panel comes first because it changes how everything below it
 * reads. The same recall figure means something different when the
 * deterministic baseline produced it than when a model did, and a dashboard
 * should not make you go and check which.
 */

import { h, mount } from "../lib/dom.js";
import { calibrationPlot, barRow } from "../lib/charts.js";
import { num, pct, usd, ms, pill, gradeAgainst } from "../lib/format.js";

const view = document.getElementById("view");

export async function renderHealth(ctx) {
  ctx.crumbs({ label: "Health" });
  mount(view, h("div.grid.grid-2", {},
    Array.from({ length: 4 }, () =>
      h("div.skeleton.skeleton-card", { style: { height: "260px" } }))));

  const [detectors, agent, pipeline, scores, calibration, reasoning] = await Promise.all([
    ctx.api.get("/health/detectors", { fresh: true }),
    ctx.api.get("/health/agent", { fresh: true }),
    ctx.api.get("/health/pipeline", { fresh: true }),
    ctx.api.get("/health/scores", { fresh: true }).catch(() => ({ available: false })),
    ctx.api.get("/health/calibration", { fresh: true }).catch(() => ({ cohorts: [] })),
    ctx.api.get("/health/reasoning", { fresh: true }).catch(() => null),
  ]);

  mount(view,
    h("div.page-head", {},
      h("h1", {}, "Health"),
      h("p.lede", { text:
        "What the system measures about itself. Calibration against target, agent "
        + "quality against ground truth, and what an incident costs — published "
        + "rather than kept internal, because a number nobody can check is not a "
        + "claim." }),
    ),

    reasoning ? reasoningCard(reasoning) : null,

    scores.available ? gatesCard(scores) : null,

    h("div.grid.grid-2", {},
      calibrationCard(detectors),
      agentCard(agent),
    ),
    h("div.grid.grid-2", { style: { marginTop: "var(--s4)" } },
      signalsCard(detectors),
      pipelineCard(pipeline),
    ),

    dispositionCard(calibration, ctx),
  );
}

/* ------------------------------------------------- calibration feedback */

/** Format a rate small enough that two decimal places would read as zero. */
function rate(value, places = 4) {
  if (value === null || value === undefined) return "—";
  return Number(value).toFixed(places);
}

/** Observed false-positive rate against target, measured against humans.
 *
 * The only version of this number worth publishing. Everything else on this
 * page is the system marking its own homework against a fault harness; this
 * one is marked by the analysts who had to read the alerts.
 */
function dispositionCard(calibration, ctx) {
  const cohorts = calibration.cohorts || [];
  const drifted = calibration.drifted || [];

  const body = cohorts.length
    ? h("table.table", {},
        h("thead", {}, h("tr", {},
          h("th", { text: "Cohort" }),
          h("th", { text: "Signal" }),
          h("th.right", { text: "Observed FPR" }),
          h("th.right", { text: "Target" }),
          h("th.right", { text: "Precision" }),
          h("th.right", { text: "Disposed" }),
          h("th", { text: "" }),
        )),
        h("tbody", {}, cohorts.map((row) =>
          h("tr", {},
            h("td.mono", { text: row.cohort }),
            h("td", { text: row.signal }),
            h("td.right.num", { text: rate(row.observed_fpr) }),
            h("td.right.num.dim", { text: rate(row.target_fpr) }),
            h("td.right.num", { text: rate(row.precision, 2) }),
            h("td.right.num.dim", { text: String(row.disposed ?? 0) }),
            h("td", {}, row.drifted
              ? pill("drifted", "bad")
              : pill("in band", "good")),
          ))),
      )
    : h("div.empty", {},
        h("h3", { text: "Nothing measured yet" }),
        h("p", { text:
          "This table fills in when incidents are disposed of. The loop is "
          + "disposition, then label, then threshold — and it starts with a "
          + "human saying whether the alert was worth reading." }),
      );

  return h("div.card", { style: { marginTop: "var(--s4)" } },
    h("div.card-head", {},
      h("div", {},
        h("span.eyebrow", { text: "Measured against human dispositions" }),
        h("h2", { text: "Calibration feedback" }),
      ),
      h("div.row", {},
        drifted.length
          ? pill(drifted.length + " drifted", "bad")
          : pill("no drift", "ghost"),
        h("button.btn.btn-sm", {
          onclick: async (event) => {
            const button = event.currentTarget;
            button.disabled = true;
            button.textContent = "Recalibrating…";
            try {
              const report = await ctx.api.post("/health/recalibrate", {});
              ctx.toast(
                "Recalibration complete",
                report.dispositions + " dispositions · "
                  + report.cohorts.length + " cohorts · "
                  + report.drifted.length + " drifted",
                report.drifted.length ? "warn" : "good",
              );
              renderHealth(ctx);
            } catch (err) {
              ctx.toast("Recalibration failed",
                        String((err && err.message) || err), "bad");
              button.disabled = false;
              button.textContent = "Recalibrate";
            }
          },
        }, "Recalibrate"),
      ),
    ),
    h("div.card-body.card-body--flush", {}, body),
    h("div.card-body", { style: { paddingTop: 0 } },
      h("p.faint", { style: { fontSize: "var(--t-xs)", margin: 0 },
        text: "A cohort drifts when its observed rate passes twice its target "
            + "with enough evidence behind both denominators — dispositions and "
            + "scored cycles. Drift raises an incident through the same inbox as "
            + "a data regression, because a platform problem routed to its own "
            + "channel is one nobody reads." }),
    ),
  );
}

/* --------------------------------------------------------------- gates */

const GATES = [
  ["recall", "Detection recall", "injected faults that produced an incident", 0.90, false],
  ["precision", "Incident precision", "incidents that matched a real fault", 0.75, false],
  ["decoy_fpr", "Decoy false-positive rate", "legitimate events that raised an incident", 0.10, true],
  ["root_accuracy", "Root-asset accuracy", "correlator elected the true root", 0.85, false],
  ["cause_accuracy", "Root-cause accuracy", "top verified hypothesis matched ground truth", 0.80, false],
  ["hallucination_rate", "Hallucination rate", "claims citing evidence that does not exist", 0.02, true],
  ["blast_recall", "Blast-radius recall", "true downstream assets found", 0.95, false],
  ["median_cost", "Median cost per incident", "tokens x price", 0.15, true],
];

function gatesCard(scores) {
  return h("div.card", { style: { marginBottom: "var(--s4)" } },
    h("div.card-head", {},
      h("div", {}, h("span.eyebrow", { text: "Measured against ground truth" }),
        h("h2", { text: "CI gates" })),
      h("span.faint", { style: { fontSize: "var(--t-xs)" },
        text: num(scores.faults) + " injected faults, " + num(scores.decoys) + " decoys" }),
    ),
    h("div.card-body", {},
      GATES.map(([key, label, note, target, lower]) => {
        const value = scores[key];
        const tone = gradeAgainst(value, target, { lowerIsBetter: lower });
        const fmt = key === "median_cost" ? usd(value) : Number(value ?? 0).toFixed(2);
        const targetFmt = key === "median_cost" ? usd(target) : target.toFixed(2);
        return h("div.gate", {},
          h("div.gate-name", {}, label, h("small", { text: note })),
          h("span.gate-value", { style: { color: "var(--" + (tone || "text") + ")" }, text: fmt }),
          pill((lower ? "≤ " : "≥ ") + targetFmt,
               tone === "good" ? "good" : tone === "warn" ? "warn" : "bad"),
        );
      }),
      h("p.faint", { style: { fontSize: "var(--t-xs)", marginTop: "var(--s4)", marginBottom: 0 },
        text: "These come from the fault harness, which records ground truth at "
            + "injection time and never touches the detectors. Numbers below target "
            + "are shown at their real value; a gate that only ever reports success "
            + "is not a gate." }),
    ),
  );
}

/* --------------------------------------------------------- calibration */

function calibrationCard(detectors) {
  const points = (detectors.by_tier || []).map((row) => ({
    label: "tier " + row.tier,
    target: row.target_fpr,
    observed: row.observed_rate,
  }));

  return h("div.card.viz", {},
    h("div.card-head", {},
      h("div", {}, h("span.eyebrow", { text: "Detector calibration" }),
        h("h2", { text: "Observed against target FPR" })),
    ),
    h("div.card-body", {},
      calibrationPlot(points),
      h("p.scatter-note", { text:
        "Split conformal sets a per-column threshold from the order statistics of "
        + "its own calibration set, so the marginal false-positive rate is bounded "
        + "by the target without assuming a distribution. Points on the line mean "
        + "the guarantee is holding. Points well above it trigger recalibration — "
        + "they are the only thing on this plot worth looking at." }),
    ),
  );
}

function signalsCard(detectors) {
  const rows = (detectors.by_signal || []).map((row) => ({
    label: row.signal.toLowerCase().replace(/_/g, " "),
    value: row.emitted,
    fp: row.false_positives,
  }));
  return h("div.card.viz", {},
    h("div.card-head", {},
      h("div", {}, h("span.eyebrow", { text: "Detection volume" }),
        h("h2", { text: "Anomalies by signal" })),
      h("span.faint", { style: { fontSize: "var(--t-xs)" },
        text: num(detectors.anomalies_emitted) + " of " + num(detectors.profiles_scored)
              + " profiles" }),
    ),
    h("div.card-body", {},
      barRow(rows, { format: (v) => num(v), critical: (i) => i.fp > 0 }),
      h("div.divider"),
      h("div.stack-sm", {},
        (detectors.detectors || []).slice(0, 8).map((d) =>
          h("div.row.row--between", { style: { fontSize: "var(--t-xs)" } },
            h("span.mono.dim", { text: d.detector_id + " · " + d.signal.toLowerCase() }),
            h("span.mono.faint", { text: d.version }),
          )),
      ),
    ),
  );
}

/* --------------------------------------------------------------- agent */

function agentCard(agent) {
  const split = agent.model_split || {};
  const halluc = agent.hallucination_rate ?? 0;
  return h("div.card.viz", {},
    h("div.card-head", {},
      h("div", {}, h("span.eyebrow", { text: "Agent quality" }),
        h("h2", { text: "Reasoning, measured" })),
    ),
    h("div.card-body", {},
      h("div.grid.grid-2", { style: { marginBottom: "var(--s4)" } },
        stat("Hallucination rate", pct(halluc, 2),
             "target under 2% · " + num(agent.claims_checked) + " claims checked",
             halluc <= 0.02 ? "good" : "bad"),
        stat("Cost per incident", usd(agent.cost_per_incident_usd),
             "target under " + usd(agent.cost_target_usd),
             agent.cost_per_incident_usd <= agent.cost_target_usd ? "good" : "warn"),
        stat("Cheap-tier share", pct(split.cheap_ratio || 0, 0),
             "target 60% or better",
             (split.cheap_ratio || 0) >= 0.6 ? "good" : "warn"),
        stat("Rejection rate", pct(agent.hypotheses?.rejection_rate || 0, 0),
             "an adversarial verifier should reject often", ""),
      ),
      barRow([
        { label: "verified", value: agent.hypotheses?.verified || 0 },
        { label: "rejected", value: agent.hypotheses?.rejected || 0 },
      ], { format: (v) => num(v) }),
      h("div.divider"),
      h("div.stack-sm", {},
        (agent.prompt_versions || []).map((version) =>
          h("div.row.row--between", { style: { fontSize: "var(--t-xs)" } },
            h("span.mono.dim", { text: version.version }),
            h("span.mono.faint", { text: num(version.runs) + " runs · " + usd(version.avg_cost_usd) }),
          )),
        h("div.row.row--between", { style: { fontSize: "var(--t-xs)" } },
          h("span.dim", { text: "latency p50 / p95" }),
          h("span.mono.faint", { text: ms(agent.latency_ms?.p50) + " / " + ms(agent.latency_ms?.p95) }),
        ),
      ),
      h("p.faint", { style: { fontSize: "var(--t-xs)", marginTop: "var(--s3)", marginBottom: 0 },
        text: "The prompt version is a digest of the prompt files and is recorded on "
            + "every incident, so a quality change can be traced to the prompt that "
            + "caused it." }),
    ),
  );
}

/* ------------------------------------------------------------ pipeline */

function pipelineCard(pipeline) {
  return h("div.card.viz", {},
    h("div.card-head", {},
      h("div", {}, h("span.eyebrow", { text: "Pipeline" }),
        h("h2", { text: "Coverage and throughput" })),
      pipeline.websocket_clients
        ? pill(pipeline.websocket_clients + " live client(s)", "good") : null,
    ),
    h("div.card-body", {},
      h("div.grid.grid-2", { style: { marginBottom: "var(--s4)" } },
        stat("Profiling coverage", pct(pipeline.profiling_coverage, 1),
             num(pipeline.series_profiled) + " series",
             pipeline.profiling_coverage >= 0.5 ? "good" : "warn"),
        stat("Lineage parse coverage", pct(pipeline.lineage_parse_coverage, 1),
             num(pipeline.lineage_edges) + " column edges",
             pipeline.lineage_parse_coverage >= 0.9 ? "good" : "warn"),
      ),
      barRow(Object.entries(pipeline.topics || {}).map(([topic, n]) => ({
        label: topic.replace(/^ds\./, "").replace(/\.v1$/, ""),
        value: n,
      })), { format: (v) => num(v) }),
      h("p.faint", { style: { fontSize: "var(--t-xs)", marginTop: "var(--s3)", marginBottom: 0 },
        text: "Parse coverage is reported rather than assumed. SQL the resolver "
            + "cannot map falls back to observed table-level edges and shows up as "
            + "a gap here — honest partial coverage beats a claim nobody checked." }),
    ),
  );
}

/* ------------------------------------------------------------ reasoning */

const PROVIDER_LABELS = {
  groq: "Groq",
  gemini: "Google Gemini",
  heuristic: "Deterministic baseline",
};

/** Which reasoner is answering, what it costs, and how to change it.
 *
 * When the deterministic reasoner is active this is not an error state and
 * the panel does not pretend it is — the demo and the eval suite both run on
 * it. It is a "you can have better than this for free" state, so it says so
 * and links the two places that hand out a free key.
 */
function reasoningCard(info) {
  const offline = info.provider === "heuristic";
  const label = PROVIDER_LABELS[info.provider] || info.provider;
  const tone = offline ? "warn" : "good";

  return h("div.card", {},
    h("div.card-head", {},
      h("div", {}, h("span.eyebrow", { text: "Reasoning" }),
        h("h2", { text: label })),
      h("div.row", {},
        info.free_tier ? pill("free tier", "good") : pill("billed", "warn"),
        pill(offline ? "deterministic" : "hosted", tone),
      ),
    ),
    h("div.card-body", {},
      h("div.grid.grid-3", {},
        stat("Cheap tier", info.cheap_model, "triage · planning", tone),
        stat("Frontier tier", info.frontier_model, "hypothesise · verify · narrate", tone),
        stat("Budget per incident", "$" + Number(info.budget_usd || 0).toFixed(2),
             "hard cap, terminates the run", "good"),
      ),

      offline
        ? h("div.note", { style: { marginTop: "var(--s4)" } },
            h("p", { text:
              "No provider key is set, so the deterministic reasoner is answering. "
              + "It is not a mock — it is the baseline a model has to beat on the "
              + "gold set, and it is why the eval suite is reproducible. Narratives "
              + "are blunter than a model's." }),
            h("p", { style: { marginBottom: 0 } },
              "Both of these are free and take a minute: ",
              h("a", { href: "https://console.groq.com/keys", target: "_blank",
                       rel: "noreferrer noopener", text: "Groq" }),
              " or ",
              h("a", { href: "https://aistudio.google.com/apikey", target: "_blank",
                       rel: "noreferrer noopener", text: "Google AI Studio" }),
              ". Put the key in ", h("code", { text: ".env" }), " as ",
              h("code", { text: "GROQ_API_KEY" }), ", then ",
              h("code", { text: "py ds.py doctor" }), " to check it.",
            ),
          )
        : h("div.grid.grid-2", { style: { marginTop: "var(--s4)" } },
            rateRow("Cheap", info.rates && info.rates.cheap),
            rateRow("Frontier", info.rates && info.rates.frontier),
          ),

      h("p.faint", { style: { fontSize: "var(--t-xs)", marginTop: "var(--s3)", marginBottom: 0 },
        text: "Resolved from DS_LLM_PROVIDER=" + (info.configured || "auto")
            + (info.keys_present && info.keys_present.length
               ? " · keys present: " + info.keys_present.join(", ")
               : " · no provider keys present")
            + " · prompts " + (info.prompt_version || "unknown") }),
    ),
  );
}

/** Published per-million-token rates, including on a free tier.
 *
 * Free-tier models are priced at their paid rate everywhere in DataSentry.
 * Pricing them at zero would make the budget cap a decoration and the spend
 * figure meaningless, and the first thing that happens when a project
 * outgrows a free tier is that neither was ever exercised.
 */
function rateRow(label, rate) {
  if (!rate) return null;
  return h("div.stat", {},
    h("span.stat-label", { text: label + " · per million tokens" }),
    h("span.stat-value", { style: { fontSize: "var(--t-lg)" },
      text: usd(rate.usd_per_m_input) + " in · " + usd(rate.usd_per_m_output) + " out" }),
    h("span.stat-note", { text: rate.model }),
  );
}

function stat(label, value, note, tone) {
  return h("div", { class: "stat " + (tone ? "stat--" + tone : "") },
    h("span.stat-label", { text: label }),
    h("span.stat-value", { style: { fontSize: "var(--t-xl)" }, text: value }),
    h("span.stat-note", { text: note }),
  );
}
