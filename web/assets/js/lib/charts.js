/* Charts.
 *
 * Four forms, each chosen for the job its data does:
 *
 *   timeSeries   change over time, with the conformal band the detector
 *                actually used and the change events on the SAME x-axis
 *   barRow       magnitude across a handful of named categories
 *   calibration  observed vs target FPR — a scatter against the identity line,
 *                where the points off the line are the entire story
 *   sparkline    a shape, not a reading; no axes, no tooltip
 *
 * Colour comes from the validated slots in viz.css and never from the UI
 * chrome tokens. Marks are thin, grid lines recede, and every chart with more
 * than one mark type carries a legend whose entries pair a shape with a word —
 * so identity survives a colourblind reader, a greyscale print and a
 * forced-colors mode.
 *
 * Line and scatter charts ship a crosshair and a tooltip. An SVG chart in a
 * browser is interactive whether or not you planned for it; the only question
 * is whether hovering does something useful.
 */

import { h, svg } from "./dom.js";

const PAD = { top: 14, right: 16, bottom: 26, left: 46 };

function scaleLinear(domain, range) {
  const [d0, d1] = domain;
  const [r0, r1] = range;
  const span = d1 - d0 || 1;
  const fn = (v) => r0 + ((v - d0) / span) * (r1 - r0);
  fn.invert = (p) => d0 + ((p - r0) / (r1 - r0 || 1)) * span;
  return fn;
}

function niceTicks(min, max, count = 4) {
  if (!Number.isFinite(min) || !Number.isFinite(max) || min === max) return [min];
  const raw = (max - min) / count;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) || mag * 10;
  const start = Math.ceil(min / step) * step;
  const out = [];
  for (let v = start; v <= max + step * 0.01; v += step) out.push(Number(v.toFixed(10)));
  return out;
}

function fmtTick(value) {
  const abs = Math.abs(value);
  if (abs === 0) return "0";
  if (abs >= 1e9) return (value / 1e9).toFixed(1) + "B";
  if (abs >= 1e6) return (value / 1e6).toFixed(1) + "M";
  if (abs >= 1e3) return (value / 1e3).toFixed(1) + "k";
  if (abs >= 1) return String(Math.round(value * 100) / 100);
  if (abs >= 0.01) return value.toFixed(3);
  return value.toExponential(1);
}

function legend(entries) {
  return h("div.viz-legend", {},
    entries.map((entry) =>
      h("span", { title: entry.note || "" },
        h("i", {
          class: "swatch-" + (entry.shape || "line"),
          style: entry.shape === "dot"
            ? { background: entry.color }
            : entry.shape === "dash"
              ? { color: entry.color, borderTopColor: entry.color }
              : { background: entry.color },
        }),
        entry.label,
      ),
    ),
  );
}

/* ------------------------------------------------------------ time series */

/**
 * @param points  [{at, value}] ordered ascending
 * @param band    optional [{at, low, high}] — the conformal interval
 * @param anomalies [{at, label}]
 * @param markers [{at, label}] — commits, deploys, schema events
 */
export function timeSeries(points, {
  height = 190,
  band = null,
  anomalies = [],
  markers = [],
  label = "",
  unit = "",
  format = fmtTick,
} = {}) {
  const frame = h("div.viz-frame");
  if (!points || points.length < 2) {
    frame.append(h("p.muted", { text: "Not enough history to plot." }));
    return frame;
  }

  const width = 720;
  const innerW = width - PAD.left - PAD.right;
  const innerH = height - PAD.top - PAD.bottom;

  const times = points.map((p) => new Date(p.at).getTime());
  const values = points.map((p) => Number(p.value));
  const lows = band ? band.map((b) => Number(b.low)) : [];
  const highs = band ? band.map((b) => Number(b.high)) : [];

  const yMin = Math.min(...values, ...(lows.length ? lows : values));
  const yMax = Math.max(...values, ...(highs.length ? highs : values));
  const padY = (yMax - yMin) * 0.14 || Math.abs(yMax) * 0.14 || 1;

  const x = scaleLinear([Math.min(...times), Math.max(...times)], [PAD.left, PAD.left + innerW]);
  const y = scaleLinear([yMin - padY, yMax + padY], [PAD.top + innerH, PAD.top]);

  const root = svg("svg", {
    class: "chart", viewBox: "0 0 " + width + " " + height,
    preserveAspectRatio: "none", role: "img",
    "aria-label": (label || "series") + " over time with its conformal interval",
  });
  root.style.height = height + "px";

  // Grid and y axis. Recessive by design — the data is the loud part.
  for (const tick of niceTicks(yMin - padY, yMax + padY, 4)) {
    const py = y(tick);
    root.append(svg("line", { class: "grid-line", x1: PAD.left, x2: PAD.left + innerW, y1: py, y2: py }));
    root.append(svg("text", { class: "tick-text", x: PAD.left - 7, y: py + 3, "text-anchor": "end" },
      format(tick)));
  }
  root.append(svg("line", { class: "axis-line", x1: PAD.left, x2: PAD.left + innerW,
                            y1: PAD.top + innerH, y2: PAD.top + innerH }));

  // The band the detector actually used, drawn behind the line.
  if (band && band.length > 1) {
    const upper = band.map((b) => x(new Date(b.at).getTime()) + "," + y(Number(b.high)));
    const lower = band.slice().reverse().map((b) => x(new Date(b.at).getTime()) + "," + y(Number(b.low)));
    root.append(svg("polygon", { class: "series-band", points: upper.concat(lower).join(" ") }));
  }

  const line = points.map((p, i) => (i ? "L" : "M") + x(times[i]) + " " + y(values[i])).join(" ");
  root.append(svg("path", { class: "series-line", d: line }));

  // Change markers share this x-axis on purpose. Seeing the commit rule land
  // on the inflection is the moment the incident explains itself, and putting
  // it in a separate card is the surest way to lose that.
  for (const marker of markers.slice(0, 6)) {
    const px = x(new Date(marker.at).getTime());
    if (!Number.isFinite(px)) continue;
    root.append(svg("line", { class: "marker-rule", x1: px, x2: px, y1: PAD.top, y2: PAD.top + innerH }));
    root.append(svg("text", {
      class: "marker-label", x: px + 4, y: PAD.top + 9,
      "text-anchor": px > PAD.left + innerW - 90 ? "end" : "start",
    }, marker.label));
  }

  for (const anomaly of anomalies) {
    const px = x(new Date(anomaly.at).getTime());
    const nearest = points.reduce((best, p, i) =>
      Math.abs(times[i] - new Date(anomaly.at).getTime()) <
      Math.abs(times[best] - new Date(anomaly.at).getTime()) ? i : best, 0);
    if (!Number.isFinite(px)) continue;
    root.append(svg("circle", {
      class: "anomaly-mark", cx: px, cy: y(values[nearest]), r: 4.5,
    }, svg("title", {}, anomaly.label || "anomaly")));
  }

  // x ticks: first, middle, last. More would collide at this width.
  for (const i of [0, Math.floor(points.length / 2), points.length - 1]) {
    root.append(svg("text", {
      class: "tick-text", x: x(times[i]), y: height - 8,
      "text-anchor": i === 0 ? "start" : i === points.length - 1 ? "end" : "middle",
    }, new Date(points[i].at).toLocaleDateString(undefined, { month: "short", day: "numeric" })));
  }

  // Hover layer: crosshair plus a tooltip. An SVG chart is interactive whether
  // or not you planned for it.
  const crosshair = svg("line", { class: "crosshair", y1: PAD.top, y2: PAD.top + innerH,
                                  x1: -99, x2: -99 });
  root.append(crosshair);
  const hit = svg("rect", { class: "hit", x: PAD.left, y: PAD.top, width: innerW, height: innerH });
  root.append(hit);

  const tip = h("div.viz-tip");
  frame.append(root, tip);

  const move = (event) => {
    const box = root.getBoundingClientRect();
    const px = ((event.clientX - box.left) / box.width) * width;
    const t = x.invert(px);
    let best = 0;
    for (let i = 1; i < times.length; i += 1) {
      if (Math.abs(times[i] - t) < Math.abs(times[best] - t)) best = i;
    }
    crosshair.setAttribute("x1", x(times[best]));
    crosshair.setAttribute("x2", x(times[best]));
    tip.classList.add("is-on");
    tip.replaceChildren(
      h("b", { text: new Date(points[best].at).toLocaleString(undefined,
        { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) }),
      h("dl", {},
        h("dt", { text: label || "value" }),
        h("dd", { text: format(values[best]) + (unit ? " " + unit : "") }),
        band && band[best]
          ? [h("dt", { text: "interval" }),
             h("dd", { text: format(Number(band[best].low)) + " – " + format(Number(band[best].high)) })]
          : null,
      ),
    );
    const left = (x(times[best]) / width) * box.width;
    tip.style.left = Math.min(Math.max(left - 60, 0), box.width - 150) + "px";
    tip.style.top = "6px";
  };

  hit.addEventListener("mousemove", move);
  hit.addEventListener("mouseleave", () => {
    tip.classList.remove("is-on");
    crosshair.setAttribute("x1", -99);
    crosshair.setAttribute("x2", -99);
  });

  const entries = [{ label: label || "measured", color: "var(--viz-series)", shape: "line" }];
  if (band) entries.push({ label: "conformal interval", color: "var(--viz-series-soft)", shape: "line" });
  if (anomalies.length) entries.push({ label: "anomaly", color: "var(--viz-critical)", shape: "dot" });
  if (markers.length) entries.push({ label: "change event", color: "var(--viz-marker)", shape: "dash" });
  frame.append(legend(entries));

  return frame;
}

/* ---------------------------------------------------------------- bar row */

/** Magnitude across named categories. One series, so labels carry identity. */
export function barRow(items, { max = null, format = (v) => String(v), critical = () => false } = {}) {
  if (!items.length) return h("p.muted", { text: "Nothing to show." });
  const top = max ?? Math.max(...items.map((i) => Number(i.value) || 0), 1);
  return h("div.viz", {},
    h("div.stack-sm", {},
      items.map((item) =>
        h("div", { style: { display: "grid", gridTemplateColumns: "minmax(0,150px) 1fr auto",
                            gap: "12px", alignItems: "center" } },
          h("span.truncate.dim", { style: { fontSize: "var(--t-xs)" }, text: item.label,
                                   title: item.label }),
          h("span", { class: "meter" + (critical(item) ? " is-bad" : ""),
                      style: { width: "100%" } },
            h("i", { style: { width: Math.max(2, (Number(item.value) / top) * 100) + "%" } })),
          h("span.mono.num", { style: { fontSize: "var(--t-xs)", minWidth: "52px",
                                        textAlign: "right" },
                               text: format(item.value) }),
        ),
      ),
    ),
  );
}

/* ------------------------------------------------------- calibration plot */

/**
 * Observed against target false-positive rate, with the identity line.
 *
 * Points ON the line mean the conformal guarantee is holding. Points above it
 * are the story, so they are the only thing that changes colour — and they
 * change to a reserved status colour, paired with a label, never colour alone.
 * Identity comes from direct labels rather than a hue per signal: eight hues on
 * one scatter cannot clear the all-pairs colourblind floor, and a legend the
 * reader has to decode is worse than the name sitting next to the dot.
 */
export function calibrationPlot(points, { height = 260 } = {}) {
  const frame = h("div.viz-frame");
  if (!points.length) {
    frame.append(h("p.muted", { text: "No calibration data yet." }));
    return frame;
  }
  const width = 460;
  const innerW = width - PAD.left - PAD.right;
  const innerH = height - PAD.top - PAD.bottom;

  const maxRate = Math.max(
    ...points.map((p) => Math.max(Number(p.observed) || 0, Number(p.target) || 0)),
    0.012,
  ) * 1.25;

  const x = scaleLinear([0, maxRate], [PAD.left, PAD.left + innerW]);
  const y = scaleLinear([0, maxRate], [PAD.top + innerH, PAD.top]);

  const root = svg("svg", {
    class: "chart", viewBox: "0 0 " + width + " " + height, role: "img",
    "aria-label": "observed against target false positive rate per signal",
  });
  root.style.height = height + "px";

  for (const tick of niceTicks(0, maxRate, 4)) {
    root.append(svg("line", { class: "grid-line", x1: PAD.left, x2: PAD.left + innerW,
                              y1: y(tick), y2: y(tick) }));
    root.append(svg("text", { class: "tick-text", x: PAD.left - 7, y: y(tick) + 3,
                              "text-anchor": "end" }, (tick * 100).toFixed(2) + "%"));
    root.append(svg("text", { class: "tick-text", x: x(tick), y: height - 8,
                              "text-anchor": "middle" }, (tick * 100).toFixed(2) + "%"));
  }

  root.append(svg("line", { class: "identity-line", x1: x(0), y1: y(0),
                            x2: x(maxRate), y2: y(maxRate) }));
  root.append(svg("text", { class: "axis-title", x: PAD.left + innerW, y: PAD.top + 10,
                            "text-anchor": "end" }, "observed = target"));
  root.append(svg("text", { class: "axis-title", x: PAD.left, y: height - 2 }, "target FPR"));

  for (const point of points) {
    const px = x(Number(point.target) || 0);
    const py = y(Number(point.observed) || 0);
    const over = (Number(point.observed) || 0) > (Number(point.target) || 0) * 2;
    root.append(svg("circle", {
      class: "dot" + (over ? " dot--over" : ""), cx: px, cy: py, r: 5.5,
    }, svg("title", {}, point.label + ": observed " + ((point.observed || 0) * 100).toFixed(3) +
                        "%, target " + ((point.target || 0) * 100).toFixed(3) + "%")));
    root.append(svg("text", {
      class: "point-label", x: px + 9, y: py + 3,
      "text-anchor": px > PAD.left + innerW - 70 ? "end" : "start",
    }, point.label));
  }

  frame.append(root);
  frame.append(legend([
    { label: "within target", color: "var(--viz-series)", shape: "dot" },
    { label: "above 2x target — recalibration due", color: "var(--viz-critical)", shape: "dot" },
    { label: "observed = target", color: "var(--viz-text-faint)", shape: "dash" },
  ]));
  return frame;
}

/* --------------------------------------------------------------- sparkline */

/** A shape, not a reading. No axes and no tooltip, on purpose. */
export function sparkline(values, { width = 90, height = 22, tone = "" } = {}) {
  if (!values || values.length < 2) return h("span");
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const step = width / (values.length - 1);
  const d = values
    .map((v, i) => (i ? "L" : "M") + (i * step).toFixed(1) + " " +
                   (height - ((v - min) / span) * (height - 3) - 1.5).toFixed(1))
    .join(" ");
  const node = svg("svg", { class: "spark", width, height,
                            viewBox: "0 0 " + width + " " + height, "aria-hidden": "true" },
    svg("path", { class: tone, d }));
  return node;
}

export { fmtTick };
