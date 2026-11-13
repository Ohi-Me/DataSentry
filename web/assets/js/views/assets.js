/* Asset browser and asset detail.
 *
 * The detail page answers three questions in order: is this healthy, what has
 * it been doing, and what depends on it. Profile history with its anomaly
 * markers first, because that is the one people came for.
 */

import { h, mount } from "../lib/dom.js";
import { firstRun, emptyBlock } from "../lib/empty.js";
import { timeSeries, sparkline } from "../lib/charts.js";
import { num, pct, pill, when, ago, severity, status } from "../lib/format.js";

const view = document.getElementById("view");

const LAYER_TONE = { bronze: "ghost", silver: "info", gold: "good" };

export async function renderAssets(ctx) {
  ctx.crumbs({ label: "Assets" });
  mount(view, h("div.card", {}, h("div.card-body.card-body--flush", {},
    Array.from({ length: 10 }, () => h("div.skeleton.skeleton-row")))));

  const query = sessionStorage.getItem("ds.search") || "";
  const data = await ctx.api.get(
    "/assets?limit=400" + (query ? "&search=" + encodeURIComponent(query) : ""), { fresh: true });

  // Nothing at all, and no search narrowing it: this is an unseeded control
  // plane, not an empty result. A bare table header here is the single most
  // confusing thing a fresh deployment can show.
  if (!data.items.length && !query) {
    mount(view,
      h("div.page-head", {}, h("h1", {}, "Assets")),
      h("div.card", {}, h("div.card-body", {},
        firstRun({ what: "No assets are registered." }))),
    );
    return;
  }

  let layer = sessionStorage.getItem("ds.assets.layer") || "all";

  const draw = () => {
    sessionStorage.setItem("ds.assets.layer", layer);
    const items = data.items
      .filter((a) => layer === "all" || a.layer === layer)
      .sort((a, b) => a.tier - b.tier || b.criticality - a.criticality);

    const rows = [];
    const body = h("tbody", {}, items.map((asset) => {
      const row = h("tr", { onclick: () => ctx.go("/assets/" + encodeURIComponent(asset.asset_id)) },
        h("td", {}, h("div.stack-sm", { style: { gap: "1px" } },
          h("strong", { style: { fontSize: "var(--t-sm)" }, text: asset.name }),
          h("span.faint.mono", { style: { fontSize: "var(--t-xs)" }, text: asset.asset_id }))),
        h("td", {}, pill(asset.layer, LAYER_TONE[asset.layer] || "ghost")),
        h("td", {}, pill("tier " + asset.tier, asset.tier === 1 ? "bad" : "ghost")),
        h("td", {}, h("span.mono.faint", { style: { fontSize: "var(--t-xs)" },
                                           text: String(asset.kind).toLowerCase() })),
        h("td", {}, h("span.dim", { style: { fontSize: "var(--t-xs)" },
                                    text: asset.owner_team || "unassigned" })),
        h("td.right", {}, h("div.row", { style: { justifyContent: "flex-end" } },
          h("span.mono.num.faint", { style: { fontSize: "var(--t-xs)" },
                                     text: asset.criticality.toFixed(2) }),
          h("span.meter", { style: { width: "48px" } },
            h("i", { style: { width: asset.criticality * 100 + "%" } })))),
        h("td.right", {}, asset.is_quarantined ? pill("quarantined", "bad") : h("span")),
      );
      rows.push(row);
      return row;
    }));

    ctx.bindList(rows, {
      onOpen: (i) => ctx.go("/assets/" + encodeURIComponent(items[i].asset_id)),
    });

    mount(view,
      h("div.page-head", {},
        h("h1", {}, "Assets"),
        h("p.lede", { text: "Criticality is a transparent heuristic in v1 — tier, query "
          + "volume, downstream kind and distinct consumers — and it says so. A model "
          + "fitted on forty labels would be theatre." }),
      ),
      h("div.filters", {},
        h("div.seg", {},
          ["all", "bronze", "silver", "gold"].map((value) =>
            h("button", { class: value === layer ? "is-active" : "",
                          onclick: () => { layer = value; draw(); } },
              value + " " + (value === "all" ? data.items.length
                : data.items.filter((a) => a.layer === value).length))),
        ),
        query ? pill('search "' + query + '"', "info") : null,
        query ? h("button.btn.btn-sm", {
          onclick: () => { sessionStorage.removeItem("ds.search"); renderAssets(ctx); },
        }, "Clear") : null,
      ),
      h("div.card", {}, h("div.card-body.card-body--flush", {},
        h("div.table-wrap", {},
          h("table.table", {},
            h("thead", {}, h("tr", {},
              h("th", { text: "Asset" }), h("th", { text: "Layer" }), h("th", { text: "Tier" }),
              h("th", { text: "Kind" }), h("th", { text: "Owner" }),
              h("th.right", { text: "Criticality" }), h("th.right", { text: "" }))),
            body)),
          items.length ? null : emptyBlock(
            query ? "Nothing matches that search" : "No assets in this layer",
            query
              ? "Clear the search, or try part of a table name such as \"orders\"."
              : "Switch to another layer, or to All."),
        )),
    );
  };

  draw();
}

/* ------------------------------------------------------------------ detail */

export async function renderAsset(ctx, assetId) {
  ctx.crumbs({ label: "Assets", href: "/assets" },
             { label: assetId.split(".").slice(-2).join(".") });
  mount(view, h("div.skeleton.skeleton-card", { style: { height: "420px" } }));

  const asset = await ctx.api.get("/assets/" + encodeURIComponent(assetId), { fresh: true });
  const columns = asset.columns || [];
  let selected = sessionStorage.getItem("ds.asset.column." + assetId)
    || (columns.find((c) => c.type.startsWith("NUMBER")) || columns[0] || {}).name
    || null;

  const chartHost = h("div");

  const drawChart = async () => {
    if (selected) sessionStorage.setItem("ds.asset.column." + assetId, selected);
    mount(chartHost, h("div.skeleton", { style: { height: "200px" } }));
    const series = await ctx.api.get(
      "/assets/" + encodeURIComponent(assetId) + "/profile"
      + (selected ? "?column=" + encodeURIComponent(selected) : ""), { fresh: true });

    const field = selected ? "null_rate" : "rows";
    const points = (series.points || [])
      .filter((p) => p[field] !== null && p[field] !== undefined)
      .map((p) => ({ at: p.at, value: Number(p[field]) }));
    const anomalies = (series.anomalies || []).map((a) => ({ at: a.at, label: a.explanation }));

    mount(chartHost,
      timeSeries(points, {
        label: selected ? "null rate" : "row count",
        anomalies, height: 210,
        format: selected ? (v) => (v * 100).toFixed(2) + "%" : undefined,
      }));
  };

  mount(view,
    h("div.page-head", {},
      h("div.row.row--wrap", { style: { marginBottom: "6px" } },
        pill(asset.layer, LAYER_TONE[asset.layer] || "ghost"),
        pill("tier " + asset.tier, asset.tier === 1 ? "bad" : "ghost"),
        pill(String(asset.kind).toLowerCase().replace(/_/g, " "), "ghost"),
        asset.is_quarantined ? pill("quarantined", "bad") : null,
      ),
      h("h1", {}, asset.name),
      h("p.lede.mono", { style: { fontSize: "var(--t-xs)" }, text: asset.asset_id }),
    ),

    h("div.kpis", {},
      kpi("Criticality", asset.criticality.toFixed(2), "tier, usage, downstream kind"),
      kpi("Queries (30d)", num(asset.query_count_30d), "from query history"),
      kpi("Consumers (30d)", num(asset.distinct_consumers_30d), "distinct readers"),
      kpi("Owner", asset.owner_team || "unassigned", "notified on incidents"),
      kpi("Columns", num(columns.length),
          columns.filter((c) => c.contains_pii).length + " flagged PII"),
    ),

    h("div.split", {},
      h("div.stack", {},
        h("div.card.viz", {},
          h("div.card-head", {},
            h("div", {}, h("span.eyebrow", { text: "Profile history" }),
              h("h2", { text: selected ? selected : "table volume" })),
            h("select", {
              style: { background: "var(--surface-2)", border: "1px solid var(--line)",
                       borderRadius: "var(--r-md)", padding: "4px 8px",
                       fontSize: "var(--t-xs)" },
              onchange: (event) => { selected = event.target.value || null; drawChart(); },
            },
              h("option", { value: "", text: "table level" }),
              columns.map((c) => h("option", {
                value: c.name, selected: c.name === selected ? "" : null, text: c.name })),
            ),
          ),
          h("div.card-body", {}, chartHost),
        ),
        incidentsCard(ctx, asset),
      ),
      h("div.stack", {},
        columnsCard(asset),
        anomaliesCard(asset),
        h("div.card", {}, h("div.card-body", {},
          h("a.btn.btn-block", { href: "#/lineage/" + encodeURIComponent(assetId) },
            "Open in lineage canvas"))),
      ),
    ),
  );

  drawChart();
}

function kpi(label, value, note) {
  return h("div.kpi.kpi--flat", {},
    h("div.stat", {},
      h("span.stat-label", { text: label }),
      h("span.stat-value", { style: { fontSize: "var(--t-lg)" }, text: String(value) }),
      h("span.stat-note", { text: note })));
}

function columnsCard(asset) {
  return h("div.card", {},
    h("div.card-head", {}, h("div", {},
      h("span.eyebrow", { text: "Schema" }), h("h2", { text: "Columns" }))),
    h("div.card-body.stack-sm", {},
      (asset.columns || []).map((column) =>
        h("div.row.row--between", { style: { fontSize: "var(--t-sm)" } },
          h("span.mono", { text: column.name }),
          h("div.row", {},
            column.contains_pii ? pill("PII", "warn") : null,
            h("span.faint.mono", { style: { fontSize: "var(--t-xs)" }, text: column.type }),
          ),
        )),
      h("p.faint", { style: { fontSize: "var(--t-xs)", marginTop: "var(--s3)", marginBottom: 0 },
        text: "A column flagged PII gets extra redaction in evidence payloads. "
            + "Profiles never store raw values for any column — only mergeable "
            + "sketches and salted hashes." }),
    ),
  );
}

function anomaliesCard(asset) {
  const items = asset.recent_anomalies || [];
  if (!items.length) return null;
  return h("div.card", {},
    h("div.card-head", {}, h("div", {},
      h("span.eyebrow", { text: "Recent" }),
      h("h2", { text: items.length + " anomalies" }))),
    h("div.card-body.stack-sm", {},
      items.slice(0, 10).map((anomaly) =>
        h("div", { style: { fontSize: "var(--t-xs)" } },
          h("div.row.row--between", {},
            h("span.mono", { text: anomaly.signal.toLowerCase().replace(/_/g, " ")
                                   + (anomaly.column ? " · " + anomaly.column : "") }),
            h("span.faint", { text: ago(anomaly.at) }),
          ),
          h("div.faint", { text: "score " + anomaly.score.toFixed(2) }),
        )),
    ),
  );
}

function incidentsCard(ctx, asset) {
  const items = asset.incidents || [];
  if (!items.length) return null;
  return h("div.card", {},
    h("div.card-head", {}, h("div", {},
      h("span.eyebrow", { text: "History" }), h("h2", { text: "Incidents on this asset" }))),
    h("div.card-body.card-body--flush", {},
      h("table.table", {}, h("tbody", {},
        items.map((incident) =>
          h("tr", { onclick: () => ctx.go("/incidents/" + incident.incident_id) },
            h("td", {}, severity(incident.severity)),
            h("td", { text: incident.title }),
            h("td", {}, status(incident.status)),
            h("td.right.faint", { style: { fontSize: "var(--t-xs)" },
                                  text: ago(incident.first_detected_at) }),
          )),
      ))),
  );
}
