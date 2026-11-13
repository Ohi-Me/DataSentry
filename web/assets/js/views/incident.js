/* The incident workspace — the screen people actually live in.
 *
 * Four decisions carry this layout:
 *
 * 1. The timeline and the profile chart share one x-axis. Seeing the commit
 *    rule land exactly on the inflection is the moment the incident explains
 *    itself; two separate cards throw that away.
 * 2. Rejected hypotheses are visible, collapsed by default. Showing the
 *    reasoning the system discarded is what makes people trust the reasoning
 *    it kept.
 * 3. Every evidence chip is clickable and opens the raw artefact — the actual
 *    structured diff, the actual profile delta. One click from claim to proof.
 * 4. Cost and latency sit in the header, not buried on a metrics page. If the
 *    number is only shown where nobody looks, it is not a commitment.
 */

import { h, mount, icon, ICONS } from "../lib/dom.js";
import { timeSeries } from "../lib/charts.js";
import {
  severity, status, risk, pill, when, clock, num, pct, usd, ms, seconds, titleCase,
} from "../lib/format.js";

const view = document.getElementById("view");
const sheet = document.getElementById("evidence-sheet");
const sheetBody = document.getElementById("evidence-sheet-body");

export async function renderIncident(ctx, incidentId) {
  ctx.crumbs({ label: "Incidents", href: "/" }, { label: incidentId.slice(0, 8) });
  mount(view, h("div.split", {},
    h("div.skeleton.skeleton-card", { style: { height: "420px" } }),
    h("div.skeleton.skeleton-card", { style: { height: "420px" } })));

  const detail = await ctx.api.get("/incidents/" + incidentId, { fresh: true });
  const incident = detail.incident;
  const evidence = new Map(detail.evidence.map((e) => [e.evidence_id, e]));

  const verified = detail.hypotheses.filter((hy) => hy.status === "verified");
  const rejected = detail.hypotheses.filter((hy) => hy.status === "rejected");
  const top = verified[0] || null;

  const series = await loadSeries(ctx, incident, detail);

  mount(view,
    header(ctx, incident, detail),
    h("div.split", {},
      h("div.stack", {},
        signalCard(incident, detail, series),
        timelineCard(detail),
      ),
      h("div.stack", {},
        causeCard(ctx, top, rejected, evidence, incident),
        blastCard(ctx, detail),
        actionsCard(ctx, detail),
      ),
    ),
  );
}

/* ------------------------------------------------------------------ header */

function header(ctx, incident, detail) {
  const rca = incident.rca_seconds;
  return h("div.incident-head", {},
    h("div", {},
      h("div.row.row--wrap", { style: { marginBottom: "6px" } },
        severity(incident.severity),
        status(incident.status),
        h("span.mono.faint", { style: { fontSize: "var(--t-xs)" },
                               text: "INC-" + incident.short_id }),
        incident.occurrence_count > 1
          ? pill("x" + incident.occurrence_count + " occurrences", "ghost") : null,
      ),
      h("h1", { text: incident.title }),
      h("div.incident-facts", {},
        fact("Root asset", incident.root_asset + (incident.root_column ? "." + incident.root_column : "")),
        fact("Detected", when(incident.first_detected_at)),
        fact("Time to root cause", rca !== null && rca !== undefined ? seconds(rca) : "—"),
        fact("Cost", usd(incident.llm_cost_usd)),
        fact("Anomalies", num(incident.anomaly_count)),
        fact("Blast radius", num(incident.blast_radius)),
      ),
    ),
    h("div.row", {},
      h("a.btn", { href: "#/incidents/" + incident.incident_id + "/replay" },
        icon(ICONS.play, 14), "Agent replay"),
      h("a.btn", { href: "#/lineage/" + encodeURIComponent(incident.root_asset_id) },
        icon(ICONS.flow, 14), "Lineage"),
      h("button.btn.btn-primary", {
        onclick: () => disposeDialog(ctx, incident, detail),
      }, "Dispose"),
    ),
  );
}

function fact(label, value) {
  return h("div", {}, h("b", { text: String(value) }), label);
}

/* ------------------------------------------------------------ signal chart */

async function loadSeries(ctx, incident, detail) {
  try {
    const query = "/assets/" + encodeURIComponent(incident.root_asset_id) + "/profile"
      + (incident.root_column ? "?column=" + encodeURIComponent(incident.root_column) : "");
    return await ctx.api.get(query);
  } catch {
    return null;
  }
}

function signalCard(incident, detail, series) {
  if (!series || !series.points || series.points.length < 3) {
    return h("div.card", {}, h("div.card-body", {},
      h("p.muted", { text: "No profile history is available for this series." })));
  }

  // Which measure to plot: the one the strongest anomaly actually moved.
  const lead = [...detail.anomalies].sort((a, b) => b.score - a.score)[0];
  const field = {
    NULL_RATE: "null_rate", CARDINALITY: "distinct_ratio", VOLUME: "rows",
    FRESHNESS: "freshness_s", NUMERIC_DRIFT: "p50", STRING_LENGTH: "p50",
  }[lead?.signal] || "rows";

  const label = {
    null_rate: "null rate", distinct_ratio: "distinct ratio", rows: "row count",
    freshness_s: "freshness (s)", p50: "median value",
  }[field];

  const points = series.points
    .filter((p) => p[field] !== null && p[field] !== undefined)
    .map((p) => ({ at: p.at, value: Number(p[field]) }));

  const anomalies = detail.anomalies
    .filter((a) => a.column === incident.root_column)
    .map((a) => ({ at: a.at, label: a.signal + " · " + a.explanation }));

  const markers = detail.timeline
    .filter((t) => t.kind === "commit" || t.kind === "sql_diff")
    .map((t) => ({ at: t.at, label: t.label }));

  const format = field === "null_rate" || field === "distinct_ratio"
    ? (v) => (v * 100).toFixed(2) + "%"
    : undefined;

  return h("div.card.viz", {},
    h("div.card-head", {},
      h("div", {},
        h("span.eyebrow", { text: "Signal" }),
        h("h2", { text: label + " · " + (incident.root_column || incident.root_asset) }),
      ),
      lead ? h("span.mono.faint", { style: { fontSize: "var(--t-xs)" },
        text: "detector " + lead.detector + " · score " + lead.score.toFixed(2)
              + (lead.threshold ? " vs threshold " + Number(lead.threshold).toFixed(2) : "") }) : null,
    ),
    h("div.card-body", {},
      timeSeries(points, { anomalies, markers, label, format, height: 210 }),
      lead ? h("p.muted", { style: { marginTop: "var(--s3)", marginBottom: 0,
                                     fontSize: "var(--t-sm)" },
                            text: lead.explanation }) : null,
    ),
  );
}

/* --------------------------------------------------------------- timeline */

function timelineCard(detail) {
  const events = detail.timeline || [];
  if (!events.length) return null;
  return h("div.card", {},
    h("div.card-head", {},
      h("div", {}, h("span.eyebrow", { text: "Timeline" }),
        h("h2", { text: "What happened, in order" })),
    ),
    h("div.card-body", {},
      h("div.timeline-track", {},
        events.map((event) =>
          h("div.tl-item", { dataset: { kind: event.kind } },
            h("div.row", { style: { gap: "10px", alignItems: "baseline" } },
              h("span.tl-time", { text: clock(event.at) }),
              h("span.tl-label", { text: event.label }),
            ),
            event.detail ? h("div.tl-detail", { text: event.detail }) : null,
          )),
      ),
    ),
  );
}

/* ------------------------------------------------------------- root cause */

function causeCard(ctx, top, rejected, evidence, incident) {
  if (!top) {
    return h("div.card", {}, h("div.card-body", {},
      h("div.empty", { style: { padding: "var(--s7) var(--s4)" } },
        h("h3", { text: "No cause was established" }),
        h("p", { text: rejected.length
          ? "Every hypothesis was rejected. The rejections are below — that is a "
            + "result, not a gap."
          : "The reasoning loop has not run on this incident yet." }),
        rejected.length ? rejectedList(rejected) : null,
      )));
  }

  return h("div.card", {},
    h("div.card-head", {},
      h("div", {}, h("span.eyebrow", { text: "Root cause" }),
        h("h2", { text: "Verified against cited evidence" })),
      h("span.pill.pill--good", { text: "confidence " + top.confidence.toFixed(2) }),
    ),
    h("div.card-body", {},
      h("div.cause", {},
        h("div.cause-head", {},
          h("span.cause-class", { text: top.cause_class }),
          top.calibrated_confidence !== null && top.calibrated_confidence !== undefined
            ? pill("calibrated " + Number(top.calibrated_confidence).toFixed(2), "ghost")
            : null,
        ),
        h("p.cause-statement", { text: top.statement, style: { margin: 0 } }),
      ),

      incident.narrative
        ? h("p.muted", { style: { marginTop: "var(--s4)", whiteSpace: "pre-line",
                                  fontSize: "var(--t-sm)" },
                         text: incident.narrative })
        : null,

      h("div", { style: { marginTop: "var(--s4)" } },
        h("span.eyebrow", { text: "Claims and their evidence" }),
        top.claims.map((claim) => claimRow(ctx, claim, evidence)),
      ),

      rejected.length ? rejectedList(rejected) : null,
    ),
  );
}

function claimRow(ctx, claim, evidence) {
  const supported = claim.supported !== false;
  return h("div", { class: "claim " + (supported ? "is-supported" : "is-unsupported") },
    h("div.claim-text", {},
      h("span", { html:
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" ' +
        'stroke-linecap="round" stroke-linejoin="round" width="15" height="15">' +
        (supported ? '<path d="M4 12.5l5 5L20 6.5"/>' : '<path d="M6 6l12 12M18 6L6 18"/>') +
        "</svg>" }),
      h("span", { text: claim.text }),
    ),
    h("div.chips", {},
      claim.evidence_ids.map((id) => {
        const item = evidence.get(id);
        return h("button", {
          class: "chip", dataset: { kind: item ? item.kind : "MISSING" },
          title: item ? item.summary : "this evidence id does not resolve",
          onclick: () => openEvidence(item, id),
        }, h("i"), id);
      }),
      claim.support_note
        ? h("span.faint", { style: { fontSize: "var(--t-xs)" }, text: claim.support_note })
        : null,
    ),
  );
}

function rejectedList(rejected) {
  return h("details.disclosure.rejected-list", {},
    h("summary", { text: "Rejected hypotheses (" + rejected.length + ")" }),
    h("p.faint", { style: { fontSize: "var(--t-xs)", marginBottom: "var(--s3)" },
      text: "Kept on purpose. A diagnosis you can see the system argue itself out of "
          + "is worth more than one that arrives alone." }),
    rejected.map((hy) =>
      h("div.rejected-item", {},
        h("div.row.row--between", {},
          h("span.mono", { style: { fontSize: "var(--t-sm)" }, text: hy.cause_class }),
          h("span.faint", { style: { fontSize: "var(--t-xs)" },
                            text: "round " + (hy.round + 1) }),
        ),
        h("div.muted", { style: { fontSize: "var(--t-xs)", marginTop: "4px" },
                         text: hy.statement }),
        h("div.why", { text: hy.rejection_reason || "no reason recorded" }),
      )),
  );
}

/* ----------------------------------------------------------- blast radius */

function blastCard(ctx, detail) {
  const entries = detail.blast_radius || [];
  if (!entries.length) return null;
  const shown = entries.slice(0, 8);
  return h("div.card", {},
    h("div.card-head", {},
      h("div", {}, h("span.eyebrow", { text: "Blast radius" }),
        h("h2", { text: entries.length + " downstream columns" })),
      h("span.faint", { style: { fontSize: "var(--t-xs)" }, text: "ranked by criticality" }),
    ),
    h("div.card-body", {},
      shown.map((entry) =>
        h("div.blast-item", {},
          entry.tier === 1
            ? h("span", { html: '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" '
              + 'stroke="var(--bad)" stroke-width="2.2" stroke-linecap="round">'
              + '<path d="M12 8v5M12 17v.01"/><circle cx="12" cy="12" r="9"/></svg>' })
            : h("span", { style: { width: "13px" } }),
          h("a.mono", { href: "#/assets/" + encodeURIComponent(entry.asset_id),
                        style: { fontSize: "var(--t-xs)" },
                        text: entry.asset + (entry.column ? "." + entry.column : "") }),
          h("span.pill.pill--ghost", { text: String(entry.kind).toLowerCase().replace(/_/g, " ") }),
          h("span.crit", {},
            h("span.faint.mono", { style: { fontSize: "var(--t-xs)" },
                                   text: entry.criticality.toFixed(2) }),
            h("span.meter", { style: { width: "52px" } },
              h("i", { style: { width: entry.criticality * 100 + "%" } })),
          ),
        )),
      entries.length > shown.length
        ? h("p.faint", { style: { marginTop: "var(--s3)", marginBottom: 0,
                                  fontSize: "var(--t-xs)" },
                         text: (entries.length - shown.length) + " more" })
        : null,
    ),
  );
}

/* ---------------------------------------------------------------- actions */

function actionsCard(ctx, detail) {
  const actions = detail.actions || [];
  if (!actions.length) return null;
  return h("div.card", {},
    h("div.card-head", {},
      h("div", {}, h("span.eyebrow", { text: "Remediation" }),
        h("h2", { text: "Proposed actions" })),
    ),
    h("div.card-body", {},
      actions.map((action) =>
        h("div", { class: "action-card" + (action.risk_tier === "high" ? " is-high" : "") },
          h("div.action-head", {},
            h("span.action-type", { text: action.action_type }),
            risk(action.risk_tier),
            action.status === "proposed"
              ? pill("awaiting approval", "warn")
              : pill(action.status, action.status === "succeeded" ? "good" : "ghost"),
          ),
          h("p.muted", { style: { fontSize: "var(--t-sm)", margin: "0 0 var(--s3)" },
                         text: action.rationale }),
          action.diff
            ? h("details.disclosure", {}, h("summary", { text: "View diff" }),
                h("pre.code", {}, diffNodes(action.diff)))
            : null,
          action.external_ref
            ? h("a.mono.dim", { href: action.external_ref.startsWith("http") ? action.external_ref : "#",
                                target: "_blank", rel: "noopener",
                                style: { fontSize: "var(--t-xs)" }, text: action.external_ref })
            : null,
          action.status === "proposed"
            ? h("div.row", { style: { marginTop: "var(--s3)" } },
                h("button.btn.btn-primary.btn-sm", {
                  onclick: () => approve(ctx, action),
                }, "Approve"),
                h("button.btn.btn-sm", {
                  onclick: () => rejectAction(ctx, action),
                }, "Reject"),
                h("span.faint", { style: { fontSize: "var(--t-xs)", marginLeft: "auto" },
                                  text: "policy " + action.policy_version }),
              )
            : null,
        )),
    ),
  );
}

function diffNodes(diff) {
  return String(diff).split("\n").map((line) => {
    const cls = line.startsWith("+") ? "add" : line.startsWith("-") ? "del"
              : line.startsWith("@@") ? "hunk" : "";
    return h("span", { class: cls, text: line + "\n" });
  });
}

async function approve(ctx, action) {
  try {
    const result = await ctx.api.post("/actions/" + action.action_id + "/approve",
                                      { role: "admin", user_id: "analyst" });
    ctx.toast("Executed " + action.action_type, result.external_ref || "", "good");
    location.reload();
  } catch (error) {
    ctx.toast("Could not approve", error.message, "bad");
  }
}

async function rejectAction(ctx, action) {
  const reason = prompt("Why is this action wrong? The reason is kept as a training signal.");
  if (!reason) return;
  try {
    await ctx.api.post("/actions/" + action.action_id + "/reject", { reason });
    ctx.toast("Action rejected", "recorded with your reason", "warn");
    location.reload();
  } catch (error) {
    ctx.toast("Could not reject", error.message, "bad");
  }
}

/* ------------------------------------------------------------- disposition */

function disposeDialog(ctx, incident, detail) {
  const verdicts = [
    ["correct_cause", "Correct cause", "the diagnosis was right"],
    ["wrong_cause", "Wrong cause", "real incident, wrong explanation"],
    ["false_positive", "False positive", "nothing was actually wrong"],
    ["duplicate", "Duplicate", "same as another open incident"],
    ["expected", "Expected", "a known and accepted change"],
  ];
  mount(sheetBody,
    h("h2", { text: "Dispose of INC-" + incident.short_id }),
    h("p.muted", { style: { fontSize: "var(--t-sm)" },
      text: "This is the feedback loop, not a comment box. Your verdict becomes a "
          + "label for conformal recalibration and a row in the agent eval set." }),
    h("div.stack-sm", { style: { marginTop: "var(--s4)" } },
      verdicts.map(([value, label, note]) =>
        h("button.btn.btn-block", {
          style: { justifyContent: "flex-start", height: "auto", padding: "var(--s3) var(--s4)" },
          onclick: async () => {
            try {
              await ctx.api.post("/incidents/" + incident.incident_id + "/disposition",
                                 { verdict: value, user_id: "analyst" });
              sheet.close();
              ctx.toast("Disposition recorded", label, "good");
              location.reload();
            } catch (error) {
              ctx.toast("Could not record", error.message, "bad");
            }
          },
        },
          h("span.stack-sm", { style: { alignItems: "flex-start", gap: "1px" } },
            h("strong", { text: label }),
            h("span.faint", { style: { fontSize: "var(--t-xs)" }, text: note }),
          ),
        )),
    ),
  );
  sheet.showModal();
}

/* ---------------------------------------------------------------- evidence */

export function openEvidence(item, id) {
  if (!item) {
    mount(sheetBody,
      h("h2", { text: "Evidence not found" }),
      h("p.muted", { text: "The id " + id + " does not resolve to a stored artefact. "
        + "A claim citing it would have been stripped before verification — this is "
        + "the hallucination tripwire, visible." }));
    sheet.showModal();
    return;
  }

  mount(sheetBody,
    h("div.row.row--between", {},
      h("h2", { text: titleCase(item.kind) }),
      h("span.mono.faint", { text: item.evidence_id }),
    ),
    h("p.muted", { style: { fontSize: "var(--t-sm)" }, text: item.summary }),
    h("div.row.row--wrap", { style: { margin: "var(--s3) 0" } },
      pill("collected by " + item.collected_by, "ghost"),
      pill(when(item.collected_at), "ghost"),
      item.ref ? pill("ref " + String(item.ref).slice(0, 20), "ghost") : null,
    ),
    payloadView(item),
  );
  sheet.showModal();
}

function payloadView(item) {
  const payload = item.payload || {};

  if (item.kind === "SQL_DIFF") {
    return h("div", {},
      h("h3", { style: { margin: "var(--s4) 0 var(--s2)" }, text: "Semantic changes" }),
      h("div.stack-sm", {},
        (payload.changes || []).map((change) =>
          h("div", { style: { padding: "var(--s3)", borderRadius: "var(--r-md)",
                              background: "var(--surface-2)",
                              border: "1px solid var(--line)" } },
            h("div.row", {},
              pill(change.kind, change.severity === "breaking" ? "bad"
                : change.severity === "notable" ? "warn" : "ghost"),
              h("span", { style: { fontSize: "var(--t-sm)" }, text: change.detail }),
            ),
            change.before || change.after
              ? h("pre.code", { style: { marginTop: "var(--s2)" },
                  text: "before: " + JSON.stringify(change.before) + "\n"
                      + "after:  " + JSON.stringify(change.after) })
              : null,
          )),
      ),
      payload.sql_before && payload.sql_after
        ? h("details.disclosure", { style: { marginTop: "var(--s4)" } },
            h("summary", { text: "Full SQL, before and after" }),
            h("div.grid.grid-2", {},
              h("div", {}, h("span.eyebrow", { text: "before" }),
                h("pre.code", { text: payload.sql_before })),
              h("div", {}, h("span.eyebrow", { text: "after" }),
                h("pre.code", { text: payload.sql_after })),
            ))
        : null,
    );
  }

  if (item.kind === "PROFILE_DELTA" && payload.points) {
    const field = payload.changed && payload.changed[0]
      ? String(payload.changed[0]).split(" ")[0] : "rows";
    const points = payload.points
      .filter((p) => p[field] !== null && p[field] !== undefined)
      .map((p) => ({ at: p.at, value: Number(p[field]) }));
    return h("div.viz", {},
      h("h3", { style: { margin: "var(--s4) 0 var(--s2)" }, text: field }),
      points.length > 2 ? timeSeries(points, { label: field, height: 170 })
                        : h("p.muted", { text: "Not enough points to plot." }),
      h("pre.code", { style: { marginTop: "var(--s3)" },
        text: JSON.stringify({ latest: payload.latest, baseline: payload.baseline,
                               changed: payload.changed }, null, 2) }),
    );
  }

  if (item.kind === "GIT_COMMIT" && payload.commits) {
    return h("div.stack-sm", { style: { marginTop: "var(--s4)" } },
      payload.commits.map((commit) =>
        h("div", { style: { padding: "var(--s3)", borderRadius: "var(--r-md)",
                            background: commit.touches_lineage
                              ? "var(--accent-soft)" : "var(--surface-2)",
                            border: "1px solid var(--line)" } },
          h("div.row.row--between", {},
            h("span.mono", { style: { fontSize: "var(--t-xs)" },
                             text: String(commit.sha).slice(0, 10)
                                   + (commit.pr_number ? "  PR #" + commit.pr_number : "") }),
            commit.touches_lineage
              ? pill("on the lineage path", "good") : pill("elsewhere", "ghost"),
          ),
          h("div", { style: { fontSize: "var(--t-sm)", marginTop: "4px" },
                     text: String(commit.message || "").split("\n")[0] }),
          h("div.faint.mono", { style: { fontSize: "var(--t-xs)", marginTop: "2px" },
                                text: (commit.files || []).join(", ") }),
        )),
    );
  }

  return h("pre.code", { style: { marginTop: "var(--s4)" },
                         text: JSON.stringify(payload, null, 2) });
}
