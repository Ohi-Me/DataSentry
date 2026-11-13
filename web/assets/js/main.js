/* Application shell: routing, keyboard, live status, toasts. */

import { api } from "./lib/api.js";
import { h, mount } from "./lib/dom.js";
import { renderInbox } from "./views/inbox.js";
import { renderIncident } from "./views/incident.js";
import { renderReplay } from "./views/replay.js";
import { renderApprovals } from "./views/approvals.js";
import { renderAssets, renderAsset } from "./views/assets.js";
import { renderLineage } from "./views/lineage.js";
import { renderHealth } from "./views/health.js";
import { renderPolicy, renderAudit } from "./views/policy.js";

const view = document.getElementById("view");
const crumbs = document.getElementById("crumbs");
const toasts = document.getElementById("toasts");

// -- routing ---------------------------------------------------------------

const ROUTES = [
  [/^\/$/, "inbox", () => renderInbox(ctx)],
  [/^\/incidents\/([^/]+)\/replay$/, "inbox", (m) => renderReplay(ctx, m[1])],
  [/^\/incidents\/([^/]+)$/, "inbox", (m) => renderIncident(ctx, m[1])],
  [/^\/approvals$/, "approvals", () => renderApprovals(ctx)],
  [/^\/assets\/(.+)$/, "assets", (m) => renderAsset(ctx, decodeURIComponent(m[1]))],
  [/^\/assets$/, "assets", () => renderAssets(ctx)],
  [/^\/lineage\/(.+)$/, "lineage", (m) => renderLineage(ctx, decodeURIComponent(m[1]))],
  [/^\/lineage$/, "lineage", () => renderLineage(ctx, null)],
  [/^\/health$/, "health", () => renderHealth(ctx)],
  [/^\/policy$/, "policy", () => renderPolicy(ctx)],
  [/^\/audit$/, "audit", () => renderAudit(ctx)],
];

const state = { path: "/", selection: 0, rows: [], onKey: null, cleanup: null };

export const ctx = {
  api,
  go(path) { location.hash = "#" + path; },
  crumbs(...parts) {
    mount(crumbs, parts.flatMap((part, i) => {
      const node = part.href
        ? h("a", { href: "#" + part.href, "data-link": "", text: part.label })
        : h("strong", { text: part.label });
      return i ? [h("span.sep", { text: "/" }), node] : [node];
    }));
  },
  toast(title, detail, tone = "good") {
    const node = h("div", { class: "toast toast--" + tone },
      h("div", {}, h("strong", { text: title }), detail ? h("span", { text: detail }) : null));
    toasts.append(node);
    setTimeout(() => {
      node.style.opacity = "0";
      setTimeout(() => node.remove(), 220);
    }, 4200);
  },
  // A list registers its rows so j/k and Enter work identically everywhere.
  bindList(rows, { onOpen, onKey } = {}) {
    state.rows = rows;
    state.selection = -1;
    state.onKey = { onOpen, onKey };
  },
  select(index) {
    if (!state.rows.length) return;
    state.selection = Math.max(0, Math.min(state.rows.length - 1, index));
    state.rows.forEach((row, i) => row.classList.toggle("is-selected", i === state.selection));
    state.rows[state.selection].scrollIntoView({ block: "nearest" });
  },
  selectedIndex: () => state.selection,
  onCleanup(fn) { state.cleanup = fn; },
};

async function route() {
  const path = (location.hash || "#/").slice(1) || "/";
  state.path = path;
  state.rows = [];
  state.selection = -1;
  state.onKey = null;
  if (state.cleanup) { try { state.cleanup(); } catch { /* ignore */ } state.cleanup = null; }

  for (const [pattern, nav, render] of ROUTES) {
    const match = pattern.exec(path);
    if (!match) continue;
    document.querySelectorAll(".nav a").forEach((a) =>
      a.classList.toggle("is-active", a.dataset.route === nav));
    view.scrollTop = 0;
    try {
      await render(match);
    } catch (error) {
      console.error(error);
      mount(view, h("div.card", {}, h("div.card-body", {},
        h("div.empty", {},
          h("h3", { text: "That view could not load" }),
          h("p", { text: String(error && error.message ? error.message : error) }),
          h("button.btn", { onclick: () => route(), text: "Retry" }),
        ))));
    }
    return;
  }
  ctx.go("/");
}

window.addEventListener("hashchange", route);

// -- badges ----------------------------------------------------------------

async function refreshBadges() {
  try {
    const [incidents, actions] = await Promise.all([
      api.get("/incidents?limit=200", { fresh: true }),
      api.get("/actions?status=proposed", { fresh: true }),
    ]);
    const open = incidents.items.filter(
      (i) => !["resolved", "dismissed"].includes(i.status)).length;
    setBadge("incidents", open);
    setBadge("approvals", actions.items.length);
  } catch { /* the badge is not worth an error state */ }
}

function setBadge(name, value) {
  const node = document.querySelector('[data-badge="' + name + '"]');
  if (node) node.textContent = value > 0 ? String(value) : "";
}

// -- live ------------------------------------------------------------------

const liveNode = document.getElementById("live-status");

api.on((message) => {
  if (message.type === "connection") {
    const { status } = message.payload;
    liveNode.classList.toggle("is-live", status === "live");
    liveNode.classList.toggle("is-down", status === "down");
    liveNode.querySelector(".live-text").textContent =
      status === "live" ? "live" : status === "down" ? "offline" : "connecting";
    return;
  }
  if (message.type === "incident.created") {
    ctx.toast("New incident", message.payload.root_asset_id || "", "warn");
  }
  if (message.type === "action.completed") {
    ctx.toast("Action " + (message.payload.status || "completed"),
              message.payload.external_ref || "", "good");
  }
  refreshBadges();
  // Only re-render if the current screen is showing the thing that changed.
  if (state.path === "/" || state.path === "/approvals") route();
});

// -- keyboard --------------------------------------------------------------

let chord = null;

document.addEventListener("keydown", (event) => {
  const tag = (event.target.tagName || "").toLowerCase();
  const typing = tag === "input" || tag === "textarea" || event.target.isContentEditable;

  if (event.key === "Escape") {
    document.querySelectorAll("dialog[open]").forEach((d) => d.close());
    if (typing) event.target.blur();
    return;
  }
  if (typing) return;

  if (chord === "g") {
    chord = null;
    const target = { i: "/", a: "/approvals", h: "/health", l: "/lineage",
                     s: "/assets", p: "/policy" }[event.key];
    if (target) { event.preventDefault(); ctx.go(target); }
    return;
  }
  if (event.key === "g") { chord = "g"; setTimeout(() => (chord = null), 900); return; }

  if (event.key === "/") {
    event.preventDefault();
    document.getElementById("global-search").focus();
    return;
  }
  if (event.key === "?") {
    event.preventDefault();
    document.getElementById("help-sheet").showModal();
    return;
  }

  if (event.key === "j" || event.key === "ArrowDown") {
    if (!state.rows.length) return;
    event.preventDefault();
    ctx.select(state.selection + 1);
    return;
  }
  if (event.key === "k" || event.key === "ArrowUp") {
    if (!state.rows.length) return;
    event.preventDefault();
    ctx.select(state.selection <= 0 ? 0 : state.selection - 1);
    return;
  }
  if (event.key === "Enter" && state.onKey?.onOpen && state.selection >= 0) {
    event.preventDefault();
    state.onKey.onOpen(state.selection);
    return;
  }
  if (state.onKey?.onKey && state.selection >= 0) {
    state.onKey.onKey(event.key, state.selection, event);
  }
});

// -- search ----------------------------------------------------------------

const search = document.getElementById("global-search");
let searchTimer = null;

search.addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(async () => {
    const query = search.value.trim();
    if (query.length < 2) return;
    ctx.go("/assets?q=" + encodeURIComponent(query));
    sessionStorage.setItem("ds.search", query);
    route();
  }, 240);
});

search.addEventListener("keydown", (event) => {
  if (event.key === "Enter") { event.preventDefault(); search.blur(); }
});

// -- theme -----------------------------------------------------------------

const THEME_KEY = "ds.theme";

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  try { localStorage.setItem(THEME_KEY, theme); } catch { /* private mode */ }
}

try {
  const stored = localStorage.getItem(THEME_KEY);
  if (stored) {
    applyTheme(stored);
  } else if (window.matchMedia("(prefers-color-scheme: dark)").matches) {
    // No stored choice: follow the operating system. The theme is light-first,
    // but somebody whose machine is in dark mode at 2am did not ask for a
    // white screen.
    document.documentElement.dataset.theme = "dark";
  }
} catch { /* private mode: keep the light default */ }

document.getElementById("theme-toggle").addEventListener("click", () => {
  applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
  route();  // charts read their palette from CSS variables at build time
});

document.getElementById("help-btn").addEventListener("click", () =>
  document.getElementById("help-sheet").showModal());

for (const sheet of document.querySelectorAll("dialog.sheet")) {
  sheet.addEventListener("click", (event) => {
    if (event.target === sheet) sheet.close();
  });
}

// -- boot ------------------------------------------------------------------

api.connect();
route();
refreshBadges();
setInterval(refreshBadges, 30000);
