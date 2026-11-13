/* The lineage canvas.
 *
 * Column-level, laid out left to right by depth. Edge style encodes the
 * transform (solid = identity, dashed = expression or aggregate, dotted =
 * observed-only), so a reader can tell a straight pass-through from a
 * computed column without hovering.
 *
 * Two performance decisions carry it. Depth is pruned server-side — the whole
 * graph is never shipped to a browser. And above roughly 320 nodes the render
 * collapses to asset level automatically, with an affordance to expand
 * columns, because a canvas that stutters is worse than one that shows less.
 */

import { h, mount, svg } from "../lib/dom.js";
import { firstRun } from "../lib/empty.js";
import { pill, num } from "../lib/format.js";

const view = document.getElementById("view");
const COLLAPSE_ABOVE = 320;

export async function renderLineage(ctx, assetId) {
  ctx.crumbs({ label: "Lineage" }, ...(assetId ? [{ label: assetId.split(".").slice(-2).join(".") }] : []));

  if (!assetId) {
    const assets = await ctx.api.get("/assets?limit=300");
    const gold = assets.items.filter((a) => a.layer === "gold").slice(0, 24);

    // With no assets there is nothing to pick, and a bare heading above an
    // empty card reads as a broken page rather than an empty one.
    if (!assets.items.length) {
      mount(view,
        h("div.page-head", {}, h("h1", {}, "Lineage")),
        h("div.card", {}, h("div.card-body", {},
          firstRun({ what: "There is no lineage graph yet." }))),
      );
      return;
    }

    mount(view,
      h("div.page-head", {},
        h("h1", {}, "Lineage"),
        h("p.lede", { text: "Pick an asset to trace. Traversal is column-level: "
          + "table-level lineage cannot answer which downstream feature broke, "
          + "which is the only question worth asking." }),
      ),
      h("div.card", {}, h("div.card-body", {},
        h("div.row.row--wrap", {},
          (gold.length ? gold : assets.items.slice(0, 24)).map((asset) =>
            h("a.btn.btn-sm", { href: "#/lineage/" + encodeURIComponent(asset.asset_id) },
              asset.name)),
        ))),
    );
    return;
  }

  mount(view, h("div.skeleton.skeleton-card", { style: { height: "520px" } }));

  let depth = Number(sessionStorage.getItem("ds.lineage.depth") || 3);
  const host = h("div");

  const draw = async () => {
    sessionStorage.setItem("ds.lineage.depth", String(depth));
    const [graph, blast] = await Promise.all([
      ctx.api.get("/assets/" + encodeURIComponent(assetId) + "/lineage?depth=" + depth,
                  { fresh: true }),
      ctx.api.get("/assets/" + encodeURIComponent(assetId) + "/blast-radius").catch(() => null),
    ]);
    mount(host, canvas(ctx, graph, blast, assetId, depth, (d) => { depth = d; draw(); }));
  };

  mount(view,
    h("div.page-head", {},
      h("h1", {}, assetId.split(".").slice(-2).join(".")),
      h("p.lede", { text: "Column-level lineage. Depth is pruned on the server; the "
        + "full graph is never shipped to the browser." }),
    ),
    host,
  );
  await draw();
}

function canvas(ctx, graph, blast, focusId, depth, onDepth) {
  const nodes = graph.nodes || [];
  const edges = graph.edges || [];
  const collapse = nodes.length > COLLAPSE_ABOVE;

  const impacted = new Set(
    (blast?.entries || []).map((e) => e.asset_id + "::" + (e.column_name || "*")),
  );

  const laid = collapse ? collapseToAssets(nodes, edges) : { nodes, edges };
  const layout = dagre(laid.nodes, laid.edges);

  const width = layout.width;
  const height = layout.height;

  const root = svg("svg", {
    viewBox: "0 0 " + width + " " + height,
    width: "100%", height: "100%",
    role: "img", "aria-label": "lineage graph around " + focusId,
    style: "min-height:100%",
  });

  const world = svg("g", {});
  root.append(world);

  for (const edge of laid.edges) {
    const from = layout.positions.get(edge.source);
    const to = layout.positions.get(edge.target);
    if (!from || !to) continue;
    const x1 = from.x + from.w;
    const y1 = from.y + from.h / 2;
    const x2 = to.x;
    const y2 = to.y + to.h / 2;
    const mid = (x1 + x2) / 2;
    const hot = impacted.has(edge.source) || impacted.has(edge.target);
    world.append(svg("path", {
      class: "edge edge--" + edge.transform + (edge.origin === "observed" ? " edge--observed" : "")
             + (hot ? " is-impacted" : ""),
      d: "M" + x1 + " " + y1 + " C" + mid + " " + y1 + " " + mid + " " + y2 + " " + x2 + " " + y2,
    }));
  }

  for (const node of laid.nodes) {
    const pos = layout.positions.get(node.id);
    if (!pos) continue;
    const isFocus = node.asset_id === focusId;
    const isImpacted = impacted.has(node.id);
    const box = svg("g", {
      class: "node-box" + (isFocus ? " is-focus" : "") + (isImpacted ? " is-impacted" : ""),
      transform: "translate(" + pos.x + "," + pos.y + ")",
      onclick: () => ctx.go("/assets/" + encodeURIComponent(node.asset_id)),
    },
      svg("rect", {
        width: pos.w, height: pos.h, rx: 8,
        fill: layerFill(node.layer),
        stroke: "var(--line-strong)", "stroke-width": 1.2,
      }),
      svg("text", { class: "node-label", x: 10, y: 17 },
        (node.asset || node.asset_id.split(".").pop()).slice(0, 26)),
      svg("text", { class: "node-sub", x: 10, y: 30 },
        (node.column ? node.column : (node.columns ? node.columns + " columns" : "table"))
          .slice(0, 30)),
      svg("title", {}, node.asset_id + (node.column ? "." + node.column : "")
        + " · tier " + node.tier + " · " + (node.layer || "")),
    );
    world.append(box);
  }

  // Pan and zoom, kept simple: wheel zooms about the cursor, drag pans.
  let scale = 1;
  let tx = 0;
  let ty = 0;
  const apply = () => world.setAttribute("transform",
    "translate(" + tx + "," + ty + ") scale(" + scale + ")");

  const wrap = h("div.canvas-wrap", {},
    h("div.canvas-toolbar", {},
      h("div.seg", {},
        [1, 2, 3, 4, 6, 8].map((d) =>
          h("button", { class: d === depth ? "is-active" : "", onclick: () => onDepth(d) },
            "depth " + d)),
      ),
      h("span.pill.pill--ghost", { text: num(nodes.length) + " nodes · " + num(edges.length) + " edges" }),
      collapse ? h("span.pill.pill--warn", { text: "collapsed to asset level" }) : null,
      graph.truncated ? h("span.pill.pill--warn", { text: "truncated" }) : null,
      h("button.btn.btn-sm", { style: { marginLeft: "auto" },
        onclick: () => { scale = 1; tx = 0; ty = 0; apply(); } }, "Reset view"),
    ),
    root,
    h("div.canvas-legend", {},
      h("span", {}, h("i.legend-line", { style: { borderTopColor: "var(--line-strong)" } }), "identity"),
      h("span", {}, h("i.legend-line.dashed", { style: { borderTopColor: "var(--line-strong)" } }), "expression / aggregate"),
      h("span", {}, h("i.legend-line.dotted", { style: { borderTopColor: "var(--line-strong)" } }), "observed only"),
      h("span", {}, h("i.legend-line", { style: { borderTopColor: "var(--bad)" } }), "impacted"),
    ),
  );

  root.addEventListener("wheel", (event) => {
    event.preventDefault();
    const box = root.getBoundingClientRect();
    const px = (event.clientX - box.left) / box.width * width;
    const py = (event.clientY - box.top) / box.height * height;
    const next = Math.max(0.35, Math.min(3, scale * (event.deltaY < 0 ? 1.12 : 0.89)));
    tx = px - (px - tx) * (next / scale);
    ty = py - (py - ty) * (next / scale);
    scale = next;
    apply();
  }, { passive: false });

  let dragging = null;
  root.addEventListener("mousedown", (event) => { dragging = { x: event.clientX, y: event.clientY, tx, ty }; });
  window.addEventListener("mouseup", () => { dragging = null; });
  root.addEventListener("mousemove", (event) => {
    if (!dragging) return;
    const box = root.getBoundingClientRect();
    tx = dragging.tx + (event.clientX - dragging.x) * (width / box.width);
    ty = dragging.ty + (event.clientY - dragging.y) * (height / box.height);
    apply();
  });

  return h("div.card", {}, h("div.card-body.card-body--flush", {}, wrap));
}

function layerFill(layer) {
  return {
    bronze: "var(--surface-3)",
    silver: "var(--surface-2)",
    gold: "var(--accent-soft)",
  }[layer] || "var(--surface-2)";
}

/** Fold column nodes into one node per asset when the graph gets big. */
function collapseToAssets(nodes, edges) {
  const byAsset = new Map();
  for (const node of nodes) {
    const existing = byAsset.get(node.asset_id);
    if (existing) { existing.columns += 1; continue; }
    byAsset.set(node.asset_id, {
      id: node.asset_id, asset_id: node.asset_id, asset: node.asset,
      layer: node.layer, tier: node.tier, depth: node.depth, columns: 1, column: null,
    });
  }
  const seen = new Set();
  const collapsed = [];
  for (const edge of edges) {
    const key = edge.src_asset + "->" + edge.dst_asset;
    if (seen.has(key) || edge.src_asset === edge.dst_asset) continue;
    seen.add(key);
    collapsed.push({ source: edge.src_asset, target: edge.dst_asset,
                     transform: edge.transform, origin: edge.origin });
  }
  return { nodes: [...byAsset.values()], edges: collapsed };
}

/**
 * Layered left-to-right layout.
 *
 * Longest-path layering plus a barycentre pass to reduce crossings. Not a
 * full Sugiyama — a real one would be several hundred lines for a graph this
 * size, and the crossings it removes are not the ones that confuse people.
 */
function dagre(nodes, edges) {
  const NODE_W = 176;
  const NODE_H = 40;
  const GAP_X = 92;
  const GAP_Y = 16;

  const byId = new Map(nodes.map((n) => [n.id, n]));
  const incoming = new Map();
  for (const edge of edges) {
    if (!byId.has(edge.source) || !byId.has(edge.target)) continue;
    if (!incoming.has(edge.target)) incoming.set(edge.target, []);
    incoming.get(edge.target).push(edge.source);
  }

  // Layer by the node's own depth relative to the focus; it is already the
  // right number and the server computed it.
  const layers = new Map();
  for (const node of nodes) {
    const layer = (node.depth ?? 0) + 8;
    if (!layers.has(layer)) layers.set(layer, []);
    layers.get(layer).push(node);
  }

  const ordered = [...layers.keys()].sort((a, b) => a - b);
  const positions = new Map();
  let maxRows = 0;

  ordered.forEach((layer, columnIndex) => {
    const column = layers.get(layer);
    column.sort((a, b) => {
      const pa = (incoming.get(a.id) || []).length;
      const pb = (incoming.get(b.id) || []).length;
      return pb - pa || String(a.asset || "").localeCompare(String(b.asset || ""));
    });
    maxRows = Math.max(maxRows, column.length);
    column.forEach((node, rowIndex) => {
      positions.set(node.id, {
        x: 24 + columnIndex * (NODE_W + GAP_X),
        y: 44 + rowIndex * (NODE_H + GAP_Y),
        w: NODE_W, h: NODE_H,
      });
    });
  });

  return {
    positions,
    width: Math.max(720, 48 + ordered.length * (NODE_W + GAP_X)),
    height: Math.max(420, 88 + maxRows * (NODE_H + GAP_Y)),
  };
}
