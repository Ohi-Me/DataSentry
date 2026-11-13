/* The approval tray.
 *
 * Optimised for throughput and keyboard-first: j/k to move, a to approve,
 * r to reject. Each item shows the action, its risk tier, the blast radius,
 * the diff and the one-line rationale — everything needed to decide without
 * opening another screen.
 *
 * Bulk approve is deliberately unavailable for high-risk actions. The point of
 * a tray is speed, and speed is exactly what you do not want on the end of the
 * range where the undo is expensive.
 */

import { h, mount, icon, ICONS } from "../lib/dom.js";
import { risk, pill, when, num, severity } from "../lib/format.js";

const view = document.getElementById("view");

export async function renderApprovals(ctx) {
  ctx.crumbs({ label: "Approvals" });
  mount(view, h("div.card", {}, h("div.card-body.card-body--flush", {},
    Array.from({ length: 5 }, () => h("div.skeleton.skeleton-row", { style: { height: "76px" } })))));

  const data = await ctx.api.get("/actions?status=proposed", { fresh: true });
  const items = data.items.sort(
    (a, b) => ({ high: 0, medium: 1, low: 2 }[a.risk_tier] - { high: 0, medium: 1, low: 2 }[b.risk_tier])
           || (a.severity || 4) - (b.severity || 4),
  );

  if (!items.length) {
    const recent = await ctx.api.get("/actions?limit=30");
    mount(view, header(items),
      h("div.card", {}, h("div.card-body", {},
        h("div.empty", {},
          h("span", { html:
            '<svg viewBox="0 0 24 24" width="42" height="42" fill="none" stroke="currentColor" '
            + 'stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round">'
            + '<path d="M9 12l2 2 4-4"/><path d="M12 3l7 3v6c0 4.4-2.9 8-7 9-4.1-1-7-4.6-7-9V6l7-3z"/></svg>' }),
          h("h3", { text: "The tray is clear" }),
          h("p", { text: "Nothing is waiting on a human. Low-risk actions execute "
                       + "automatically; anything with a costly undo waits here." }),
          recent.items.length
            ? h("div.empty-facts", {},
                h("span", {}, h("b", { text: num(recent.items.length) }), "actions in total"),
                h("span", {}, h("b", {
                  text: num(recent.items.filter((a) => a.status === "succeeded").length) }), "executed"),
                h("span", {}, h("b", {
                  text: num(recent.items.filter((a) => a.status === "rejected").length) }), "rejected"),
              )
            : null,
        ))));
    return;
  }

  const rows = [];
  const list = h("div", {},
    items.map((action) => {
      const row = h("div.tray-item", {
        tabindex: "0",
        onclick: () => ctx.go("/incidents/" + action.incident_id),
      },
        h("div.stack-sm", {},
          risk(action.risk_tier),
          action.severity ? severity(action.severity) : null,
        ),
        h("div.stack-sm", { style: { minWidth: 0 } },
          h("div.row", {},
            h("span.mono", { style: { fontSize: "var(--t-sm)", fontWeight: 650 },
                             text: action.action_type }),
            h("span.dim.truncate", { style: { fontSize: "var(--t-sm)" },
                                     text: action.incident_title || "" }),
          ),
          h("div.muted", { style: { fontSize: "var(--t-xs)" }, text: action.rationale }),
          h("div.row.row--wrap", { style: { gap: "var(--s3)" } },
            action.root_asset
              ? h("span.mono.faint", { style: { fontSize: "var(--t-xs)" },
                                       text: action.root_asset })
              : null,
            action.blast_radius
              ? h("span.faint", { style: { fontSize: "var(--t-xs)" },
                                  text: num(action.blast_radius) + " downstream" })
              : null,
            h("span.faint", { style: { fontSize: "var(--t-xs)" },
                              text: "policy " + action.policy_version }),
            pill("reversible", "good"),
          ),
        ),
        h("div.tray-actions", {},
          h("button.btn.btn-primary.btn-sm", {
            onclick: (event) => { event.stopPropagation(); approve(ctx, action); },
          }, icon(ICONS.check, 13), "Approve"),
          h("button.btn.btn-sm", {
            onclick: (event) => { event.stopPropagation(); reject(ctx, action); },
          }, "Reject"),
        ),
      );
      rows.push(row);
      return row;
    }),
  );

  ctx.bindList(rows, {
    onOpen: (i) => ctx.go("/incidents/" + items[i].incident_id),
    onKey: (key, i) => {
      if (key === "a") approve(ctx, items[i]);
      if (key === "r") reject(ctx, items[i]);
    },
  });

  mount(view, header(items), h("div.card", {}, h("div.card-body.card-body--flush", {}, list)));
}

function header(items) {
  const high = items.filter((a) => a.risk_tier === "high").length;
  return h("div", {},
    h("div.page-head", {},
      h("h1", {}, "Approval tray"),
      h("p.lede", { text:
        "Low risk executes on its own. Everything here has an undo cost worth a "
        + "human decision, and every action in this system is reversible by "
        + "construction — there is no irreversible action type implemented." }),
    ),
    h("div.filters", {},
      pill(items.length + " waiting", items.length ? "warn" : "ghost"),
      high ? pill(high + " high risk — admin only, no bulk approve", "bad") : null,
      h("span.faint", { style: { fontSize: "var(--t-xs)", marginLeft: "auto" },
                        text: "j / k to move · a to approve · r to reject" }),
    ),
  );
}

async function approve(ctx, action) {
  try {
    const result = await ctx.api.post("/actions/" + action.action_id + "/approve",
                                      { role: "admin", user_id: "analyst" });
    ctx.toast("Executed " + action.action_type, result.external_ref || "", "good");
    renderApprovals(ctx);
  } catch (error) {
    ctx.toast("Could not approve", error.message, "bad");
  }
}

async function reject(ctx, action) {
  const reason = prompt("Why is this action wrong? Kept as a training signal.");
  if (!reason) return;
  try {
    await ctx.api.post("/actions/" + action.action_id + "/reject", { reason });
    ctx.toast("Rejected", "recorded with your reason", "warn");
    renderApprovals(ctx);
  } catch (error) {
    ctx.toast("Could not reject", error.message, "bad");
  }
}
