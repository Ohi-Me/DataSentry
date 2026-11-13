/* Policy and audit.
 *
 * The policy is rendered from the server's own description rather than
 * restated in the UI. A policy page that is a hand-maintained copy of the
 * rules is a page that will eventually be wrong at the worst moment.
 */

import { h, mount } from "../lib/dom.js";
import { pill, when, num, titleCase } from "../lib/format.js";

const view = document.getElementById("view");

export async function renderPolicy(ctx) {
  ctx.crumbs({ label: "Policy" });
  mount(view, h("div.skeleton.skeleton-card", { style: { height: "360px" } }));

  const policy = await ctx.api.get("/health/policy", { fresh: true });

  mount(view,
    h("div.page-head", {},
      h("h1", {}, "Policy"),
      h("p.lede", { text:
        "Declarative and versioned. Every action evaluation is logged with the "
        + "policy version, so the answer to why something executed without "
        + "approval six months ago is a row rather than a recollection." }),
    ),

    h("div.grid.grid-2", {},
      h("div.card", {},
        h("div.card-head", {},
          h("div", {}, h("span.eyebrow", { text: "Risk tiers" }),
            h("h2", { text: "Who can approve what" })),
          pill(policy.version, "ghost"),
        ),
        h("div.card-body.stack-sm", {},
          Object.entries(policy.approvers || {}).map(([tier, roles]) =>
            h("div.row.row--between", {},
              h("span", {}, tierPill(tier)),
              h("span.mono.dim", { style: { fontSize: "var(--t-xs)" }, text: roles.join(", ") }),
            )),
          h("div.divider"),
          h("div.row.row--between", { style: { fontSize: "var(--t-sm)" } },
            h("span.dim", { text: "Bulk approve allowed for" }),
            h("span.row", {}, (policy.bulk_approve || []).map((t) => tierPill(t))),
          ),
          h("div.row.row--between", { style: { fontSize: "var(--t-sm)" } },
            h("span.dim", { text: "Risk thresholds" }),
            h("span.mono.faint", { style: { fontSize: "var(--t-xs)" },
              text: "high >= " + policy.thresholds.high + " · medium >= " + policy.thresholds.medium }),
          ),
        ),
      ),

      h("div.card", {},
        h("div.card-head", {}, h("div", {},
          h("span.eyebrow", { text: "Reversibility" }),
          h("h2", { text: "How expensive is the undo" }))),
        h("div.card-body.stack-sm", {},
          Object.entries(policy.reversibility || {})
            .sort((a, b) => b[1] - a[1])
            .map(([action, score]) =>
              h("div.row.row--between", { style: { fontSize: "var(--t-sm)" } },
                h("span.mono", { style: { fontSize: "var(--t-xs)" }, text: action }),
                h("div.row", {},
                  h("span.faint.mono", { style: { fontSize: "var(--t-xs)" },
                                         text: score.toFixed(2) }),
                  h("span.meter", { style: { width: "70px" } },
                    h("i", { style: { width: score * 100 + "%" } })),
                ),
              )),
        ),
      ),
    ),

    h("div.card", { style: { marginTop: "var(--s4)" } },
      h("div.card-head", {}, h("div", {},
        h("span.eyebrow", { text: "Invariants" }),
        h("h2", { text: "What cannot happen" }))),
      h("div.card-body.stack-sm", {},
        (policy.invariants || []).map((line) =>
          h("div.row", { style: { alignItems: "flex-start", gap: "var(--s3)" } },
            h("span", { html:
              '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="var(--good)" '
              + 'stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" '
              + 'style="margin-top:3px"><path d="M4 12.5l5 5L20 6.5"/></svg>' }),
            h("span", { style: { fontSize: "var(--t-sm)" }, text: line }),
          )),
      ),
    ),
  );
}

function tierPill(tier) {
  return pill(String(tier).toUpperCase(),
              tier === "high" ? "bad" : tier === "medium" ? "warn" : "good");
}

/* ----------------------------------------------------------------- audit */

export async function renderAudit(ctx) {
  ctx.crumbs({ label: "Audit" });
  mount(view, h("div.skeleton.skeleton-card", { style: { height: "420px" } }));

  const data = await ctx.api.get("/audit?limit=200", { fresh: true });

  mount(view,
    h("div.page-head", {},
      h("h1", {}, "Audit log"),
      h("p.lede", { text:
        "Append-only. There is no update or delete path in the store module, so "
        + "the guarantee is structural rather than procedural. Every mutating API "
        + "call is written here by middleware, not by each handler, so the next "
        + "endpoint someone adds cannot forget." }),
    ),
    h("div.card", {},
      h("div.card-head", {},
        h("div", {}, h("span.eyebrow", { text: "Most recent first" }),
          h("h2", { text: num(data.items.length) + " entries" })),
      ),
      h("div.card-body", {},
        data.items.length
          ? data.items.map((item) =>
              h("div.audit-row", {},
                h("span.faint", { text: when(item.at) }),
                h("span", {},
                  pill(item.actor_kind,
                       item.actor_kind === "human" ? "info"
                       : item.actor_kind === "agent" ? "violet" : "ghost")),
                h("div", { style: { minWidth: 0 } },
                  h("div.truncate", {},
                    h("span.dim", { text: item.actor + "  " }),
                    h("span", { text: item.action }),
                  ),
                  h("div.faint.truncate", { text: item.target }),
                ),
              ))
          : h("p.muted", { text: "Nothing recorded yet." }),
      ),
    ),
  );
}
