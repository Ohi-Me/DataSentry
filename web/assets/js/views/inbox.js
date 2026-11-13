/* Incident inbox.
 *
 * Sorted by severity and criticality, not by time. A list ordered by recency
 * puts the newest trivial anomaly above the tier-1 revenue table that broke
 * forty minutes ago, which is precisely backwards for the person reading it.
 */

import { h, mount, icon, ICONS } from "../lib/dom.js";
import { firstRun, isUnseeded } from "../lib/empty.js";
import { severity, status, when, ago, num, usd, seconds, pill } from "../lib/format.js";

const view = document.getElementById("view");

const FILTERS = [
  { key: "open", label: "Open", test: (i) => !["resolved", "dismissed"].includes(i.status) },
  { key: "approval", label: "Needs approval", test: (i) => i.awaiting_approval > 0 },
  { key: "resolved", label: "Resolved", test: (i) => i.status === "resolved" },
  { key: "dismissed", label: "Dismissed", test: (i) => i.status === "dismissed" },
  { key: "all", label: "All", test: () => true },
];

export async function renderInbox(ctx) {
  ctx.crumbs({ label: "Incidents" });
  mount(view, skeleton());

  const [data, pipeline] = await Promise.all([
    ctx.api.get("/incidents?limit=200", { fresh: true }),
    ctx.api.get("/health/pipeline").catch(() => null),
  ]);

  const active = sessionStorage.getItem("ds.inbox.filter") || "open";
  const items = [...data.items].sort(
    (a, b) => a.severity - b.severity
          || (b.max_criticality || 0) - (a.max_criticality || 0)
          || new Date(b.first_detected_at) - new Date(a.first_detected_at),
  );

  const draw = (filterKey) => {
    sessionStorage.setItem("ds.inbox.filter", filterKey);
    const filter = FILTERS.find((f) => f.key === filterKey) || FILTERS[0];
    const shown = items.filter(filter.test);

    const body = shown.length
      ? table(ctx, shown)
      : emptyState(pipeline, items.length);

    mount(view,
      h("div.page-head", {},
        h("h1", {}, "Incidents"),
        h("p.lede", { text:
          "Ranked by severity and downstream criticality, not by arrival time. " +
          "Every incident here is a correlated cluster of anomalies with one elected root." }),
      ),
      kpis(items, pipeline),
      h("div.filters", {},
        h("div.seg", {},
          FILTERS.map((f) =>
            h("button", {
              class: f.key === filterKey ? "is-active" : "",
              onclick: () => draw(f.key),
            }, f.label + " " + items.filter(f.test).length)),
        ),
        h("span.faint", { style: { fontSize: "var(--t-xs)", marginLeft: "auto" },
                          text: "j / k to move · Enter to open" }),
      ),
      h("div.card", {}, h("div.card-body.card-body--flush", {}, body)),
    );
  };

  draw(active);
}

function kpis(items, pipeline) {
  const open = items.filter((i) => !["resolved", "dismissed"].includes(i.status));
  const sev1 = open.filter((i) => i.severity === 1).length;
  const approvals = items.reduce((n, i) => n + (i.awaiting_approval || 0), 0);
  const withCause = items.filter((i) => i.cause_class);
  const rcaTimes = items.map((i) => i.rca_seconds).filter((v) => v !== null && v !== undefined);
  const median = rcaTimes.length
    ? rcaTimes.slice().sort((a, b) => a - b)[Math.floor(rcaTimes.length / 2)] : null;
  const cost = items.reduce((n, i) => n + (i.llm_cost_usd || 0), 0);

  return h("div.kpis", {},
    kpi("Open", open.length, sev1 ? sev1 + " at severity 1" : "none critical",
        sev1 ? "bad" : "flat"),
    kpi("Awaiting approval", approvals, approvals ? "in the tray" : "tray is clear",
        approvals ? "warn" : "flat"),
    kpi("Root cause found", withCause.length + " / " + items.length,
        withCause.length ? "each cites its evidence" : "no diagnoses yet", "good"),
    kpi("Median time to cause", median !== null ? seconds(median) : "—",
        "target under 90 s", "good"),
    kpi("Reasoning spend", usd(cost), "across every investigation", "flat"),
  );
}

function kpi(label, value, note, tone) {
  return h("div", { class: "kpi kpi--" + tone },
    h("div.stat", {},
      h("span.stat-label", { text: label }),
      h("span.stat-value", { text: String(value) }),
      h("span.stat-note", { text: note }),
    ),
  );
}

function table(ctx, items) {
  const rows = [];
  const body = h("tbody", {},
    items.map((item) => {
      const row = h("tr.inbox-row", {
        tabindex: "0",
        onclick: () => ctx.go("/incidents/" + item.incident_id),
      },
        h("td", {}, severity(item.severity)),
        h("td", {},
          h("div.inbox-title", {},
            h("strong", { text: item.title }),
            h("div.inbox-meta", {},
              h("span.mono", { text: item.root_asset }),
              item.root_column ? h("span.mono", { text: "." + item.root_column }) : null,
              h("span", {}, icon(ICONS.alert, 11), num(item.anomaly_count) + " anomalies"),
              item.blast_radius
                ? h("span", {}, icon(ICONS.flow, 11), num(item.blast_radius) + " downstream")
                : null,
              item.occurrence_count > 1
                ? h("span", { text: "x" + item.occurrence_count + " occurrences" }) : null,
            ),
          ),
        ),
        h("td", {}, item.cause_class
          ? h("div.stack-sm", {},
              h("span.mono", { style: { fontSize: "var(--t-xs)" }, text: item.cause_class }),
              h("span.faint", { style: { fontSize: "var(--t-xs)" },
                                text: "confidence " + (item.confidence ?? 0).toFixed(2) }))
          : h("span.faint", { style: { fontSize: "var(--t-xs)" }, text: "not diagnosed" })),
        h("td", {}, status(item.status)),
        h("td.right", {}, item.awaiting_approval
          ? pill(item.awaiting_approval + " to approve", "warn") : h("span")),
        h("td.right.mono.faint", { style: { fontSize: "var(--t-xs)" },
                                   title: when(item.first_detected_at),
                                   text: ago(item.first_detected_at) }),
      );
      rows.push(row);
      return row;
    }),
  );

  ctx.bindList(rows, {
    onOpen: (i) => ctx.go("/incidents/" + items[i].incident_id),
  });

  return h("div.table-wrap", {},
    h("table.table", {},
      h("thead", {}, h("tr", {},
        h("th", { text: "Severity" }),
        h("th", { text: "Incident" }),
        h("th", { text: "Root cause" }),
        h("th", { text: "Status" }),
        h("th.right", { text: "" }),
        h("th.right", { text: "Detected" }),
      )),
      body,
    ),
  );
}

function emptyState(pipeline, total) {
  if (!total && isUnseeded(pipeline)) return firstRun();
  // An empty state that teaches. "No incidents" alone leaves the reader unsure
  // whether the system is healthy or broken; the numbers settle it.
  return h("div.empty", {},
    h("span", { html:
      '<svg viewBox="0 0 24 24" width="42" height="42" fill="none" stroke="currentColor" ' +
      'stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round">' +
      '<circle cx="12" cy="12" r="9"/><path d="M8 12.5l2.5 2.5L16 9.5"/></svg>' }),
    h("h3", { text: total ? "Nothing matches this filter" : "The warehouse is quiet" }),
    h("p", { text: total
      ? "Try another filter, or widen to All."
      : "No correlated regressions. Detection is running — the numbers below are what it has covered." }),
    pipeline ? h("div.empty-facts", {},
      h("span", {}, h("b", { text: num(pipeline.series_profiled) }), "series profiled"),
      h("span", {}, h("b", { text: num(pipeline.profiles) }), "profiles written"),
      h("span", {}, h("b", { text: num(pipeline.lineage_edges) }), "lineage edges"),
      h("span", {}, h("b", { text: num(pipeline.anomalies) }), "anomalies scored"),
    ) : null,
  );
}

function skeleton() {
  return h("div", {},
    h("div.kpis", {}, Array.from({ length: 5 }, () => h("div.skeleton.skeleton-card"))),
    h("div.card", {}, h("div.card-body.card-body--flush", {},
      Array.from({ length: 8 }, () => h("div.skeleton.skeleton-row")))),
  );
}
