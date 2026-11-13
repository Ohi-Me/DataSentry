/* Agent replay.
 *
 * A vertical stepper of node transitions with a scrubber. Each card shows the
 * model tier, the tokens, the duration and the parsed output rendered as a
 * typed object — not raw JSON, because raw JSON is a debugging artefact and
 * this is the screen that has to make the reasoning legible to somebody who
 * did not write it.
 *
 * Worth building early for a reason that has nothing to do with the demo: it
 * is the best debugging tool in the project. Watching Verify reject a
 * hypothesis you thought was obviously right is how prompt bugs get found.
 */

import { h, mount, icon, ICONS } from "../lib/dom.js";
import { barRow } from "../lib/charts.js";
import { num, ms, usd, pill, when, titleCase } from "../lib/format.js";

const view = document.getElementById("view");

const NODE_NOTE = {
  Triage: "Is this real, new and worth investigating? Cheap tier — one classification call.",
  Contextualize: "Assembling the dossier and the baseline evidence set. No model involved.",
  Hypothesize: "Frontier tier. Propose 3–5 candidate causes from the dossier.",
  GatherEvidence: "Run the read-only tools the hypotheses asked for. No model involved.",
  Verify: "Adversarial. Default position is that each hypothesis is wrong.",
  Narrate: "Write the story from verified claims only.",
  PlanActions: "Cheap tier. Map the verified cause onto the closed action enum.",
  Dismiss: "Duplicate, muted, or a known-expected event.",
  Inconclusive: "Nothing survived verification. Recorded as such rather than guessed at.",
};

export async function renderReplay(ctx, incidentId) {
  ctx.crumbs(
    { label: "Incidents", href: "/" },
    { label: incidentId.slice(0, 8), href: "/incidents/" + incidentId },
    { label: "Replay" },
  );
  mount(view, h("div.skeleton.skeleton-card", { style: { height: "480px" } }));

  const [trace, detail] = await Promise.all([
    ctx.api.get("/incidents/" + incidentId + "/trace", { fresh: true }),
    ctx.api.get("/incidents/" + incidentId),
  ]);

  const steps = trace.nodes || [];
  let cursor = steps.length;

  const stepsHost = h("div");
  const draw = () => mount(stepsHost, steps.map((step, i) => stepCard(step, i < cursor, i === cursor - 1)));

  mount(view,
    h("div.page-head", {},
      h("h1", {}, "Agent replay"),
      h("p.lede", { text:
        "Every node transition, with what it cost and what it decided. Rejections "
        + "are shown in full — the discarded reasoning is the part that makes the "
        + "kept reasoning checkable." }),
    ),

    h("div.kpis", {},
      metric("Nodes", steps.length, "transitions in this run"),
      metric("Wall clock", ms(trace.totals.duration_ms), "sum of node durations"),
      metric("Cost", usd(trace.totals.usd), "tokens x price"),
      metric("Cheap / frontier", trace.totals.cheap_calls + " / " + trace.totals.frontier_calls,
             "target is 60% cheap or better"),
      metric("Prompt version", trace.prompt_version || "—", "recorded per run"),
    ),

    h("div.card", { style: { marginBottom: "var(--s4)" } },
      h("div.card-body", {},
        h("div.scrubber", {},
          h("button.btn.btn-sm", { onclick: () => { cursor = 0; draw(); } }, "Reset"),
          h("input", {
            type: "range", min: "0", max: String(steps.length), value: String(cursor),
            "aria-label": "Replay position",
            oninput: (event) => { cursor = Number(event.target.value); draw(); },
          }),
          h("button.btn.btn-sm.btn-primary", {
            onclick: (event) => play(event.target, steps.length, (i) => { cursor = i; draw(); }),
          }, icon(ICONS.play, 13), "Play"),
        ),
      ),
    ),

    h("div.replay", {},
      h("div.card", {}, h("div.card-body", {}, stepsHost)),
      h("div.stack", {},
        budgetCard(trace, detail),
        guardrailCard(detail),
      ),
    ),
  );

  draw();
}

function play(button, total, onTick) {
  let i = 0;
  button.disabled = true;
  const timer = setInterval(() => {
    i += 1;
    onTick(i);
    if (i >= total) { clearInterval(timer); button.disabled = false; }
  }, 420);
}

function metric(label, value, note) {
  return h("div.kpi.kpi--flat", {},
    h("div.stat", {},
      h("span.stat-label", { text: label }),
      h("span.stat-value", { style: { fontSize: "var(--t-lg)" }, text: String(value) }),
      h("span.stat-note", { text: note }),
    ));
}

function stepCard(step, revealed, active) {
  const node = h("div", {
    class: "step " + (step.status === "ok" ? "is-ok" : "is-error") + (active ? " is-active" : ""),
    style: { opacity: revealed ? 1 : 0.28 },
  },
    h("div.step-head", {},
      h("span.step-node", { text: step.node }),
      step.model ? pill(step.model + " tier", step.model === "cheap" ? "ghost" : "violet") : null,
      h("span.step-meta", { text: ms(step.duration_ms) }),
      step.tokens_in || step.tokens_out
        ? h("span.step-meta", { text: num(step.tokens_in) + " in / " + num(step.tokens_out) + " out" })
        : h("span.step-meta", {
            // A node with a model tier but no tokens ran on the deterministic
            // reasoner. Labelling that "no model call" would hide which
            // reasoner produced the result, which is the one thing a reader
            // of this screen most needs to know.
            text: step.model ? "deterministic reasoner" : "no model call",
            title: step.model
              ? "This node has a model tier but consumed no tokens, so it ran on "
                + "the deterministic reasoner -- the baseline the language model "
                + "is graded against, and the fallback when no API key is set."
              : "This node never calls a model.",
          }),
      step.usd ? h("span.step-meta", { text: usd(step.usd) }) : null,
    ),
    h("div.step-summary", { text: step.summary }),
    NODE_NOTE[step.node]
      ? h("div.faint", { style: { fontSize: "var(--t-xs)", marginTop: "3px" },
                         text: NODE_NOTE[step.node] })
      : null,
    revealed ? payload(step) : null,
  );
  return node;
}

function payload(step) {
  const data = step.payload || {};

  if (data.hypotheses) {
    return h("div.step-payload.stack-sm", {},
      data.hypotheses.map((hy) =>
        h("div", { style: { padding: "var(--s3)", background: "var(--surface-2)",
                            border: "1px solid var(--line)", borderRadius: "var(--r-md)" } },
          h("div.row.row--between", {},
            h("span.mono", { style: { fontSize: "var(--t-xs)" }, text: hy.cause_class }),
            h("span.faint.mono", { style: { fontSize: "var(--t-xs)" },
                                   text: hy.confidence.toFixed(2) }),
          ),
          h("div.muted", { style: { fontSize: "var(--t-xs)", marginTop: "4px" },
                           text: hy.statement }),
          hy.evidence_requests && hy.evidence_requests.length
            ? h("div.faint.mono", { style: { fontSize: "var(--t-xs)", marginTop: "4px" },
                                    text: "wants: " + hy.evidence_requests.join(", ") })
            : null,
        )),
    );
  }

  if (data.evidence) {
    return h("div.step-payload", {},
      h("div.chips", { style: { paddingLeft: 0 } },
        data.evidence.map((e) =>
          h("span.chip", { dataset: { kind: e.kind }, title: e.summary }, h("i"), e.id))),
    );
  }

  if (data.results) {
    return h("div.step-payload.stack-sm", {},
      data.results.map((result) =>
        h("div.row", { style: { gap: "var(--s2)", alignItems: "flex-start" } },
          pill(result.status, result.status === "verified" ? "good" : "bad"),
          h("div", {},
            h("span.mono", { style: { fontSize: "var(--t-xs)" }, text: result.cause_class }),
            result.rejection_reason
              ? h("div.faint", { style: { fontSize: "var(--t-xs)" },
                                 text: result.rejection_reason })
              : h("div.faint", { style: { fontSize: "var(--t-xs)" },
                                 text: "cites " + (result.cited || []).join(", ") }),
          ),
        )),
    );
  }

  if (data.actions) {
    return h("div.step-payload.stack-sm", {},
      data.actions.map((action) =>
        h("div.row", {},
          h("span.mono", { style: { fontSize: "var(--t-xs)" }, text: action.action_type }),
          h("span.faint", { style: { fontSize: "var(--t-xs)" }, text: action.rationale }),
        )),
    );
  }

  if (data.narrative) {
    return h("p.muted", { style: { fontSize: "var(--t-sm)", whiteSpace: "pre-line",
                                   marginTop: "var(--s3)", marginBottom: 0 },
                          text: data.narrative });
  }

  return null;
}

function budgetCard(trace, detail) {
  const totals = trace.totals;
  const calls = totals.cheap_calls + totals.frontier_calls;
  return h("div.card.viz", {},
    h("div.card-head", {},
      h("div", {}, h("span.eyebrow", { text: "Economics" }),
        h("h2", { text: "What this run cost" })),
    ),
    h("div.card-body", {},
      barRow([
        { label: "cheap tier calls", value: totals.cheap_calls },
        { label: "frontier tier calls", value: totals.frontier_calls },
        { label: "tool calls", value: detail.cost ? detail.cost.tool_calls : 0 },
      ], { format: (v) => String(v) }),
      h("div.divider"),
      h("div.stack-sm", {},
        row("Tokens in", num(totals.tokens_in)),
        row("Tokens out", num(totals.tokens_out)),
        row("Cost", usd(totals.usd)),
        row("Cheap ratio", calls ? ((totals.cheap_calls / calls) * 100).toFixed(0) + "%" : "—"),
      ),
      h("p.faint", { style: { fontSize: "var(--t-xs)", marginTop: "var(--s3)", marginBottom: 0 },
        text: "Two of the seven nodes use no model at all. Every node made "
            + "deterministic is one that is cheaper, faster and testable." }),
    ),
  );
}

function guardrailCard(detail) {
  const hypotheses = detail.hypotheses || [];
  const claims = hypotheses.flatMap((hy) => hy.claims || []);
  const unsupported = claims.filter((c) => c.supported === false).length;
  return h("div.card", {},
    h("div.card-head", {},
      h("div", {}, h("span.eyebrow", { text: "Guardrails" }),
        h("h2", { text: "What was enforced" })),
    ),
    h("div.card-body.stack-sm", {},
      row("Claims made", String(claims.length)),
      row("Claims without support", String(unsupported)),
      row("Hypotheses rejected",
          String(hypotheses.filter((hy) => hy.status === "rejected").length)),
      h("div.divider"),
      h("p.faint", { style: { fontSize: "var(--t-xs)", margin: 0 },
        text: "Claim.evidence_ids has min_length=1, so a claim with no evidence "
            + "cannot be constructed. Every cited id is resolved against this "
            + "incident's evidence table before the verifier sees it; one that "
            + "does not resolve strips the claim." }),
    ),
  );
}

function row(label, value) {
  return h("div.row.row--between", { style: { fontSize: "var(--t-sm)" } },
    h("span.dim", { text: label }),
    h("span.mono.num", { text: value }),
  );
}
