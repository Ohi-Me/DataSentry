/* The landing page.
 *
 * Every verdict on this page comes from the server. The chart does not
 * decide whether a reading is out of control -- it asks the production
 * conformal calibrator, over the playground API, and draws what it is told.
 * The SQL lab does not guess at lineage -- it runs the production resolver
 * and semantic diff. The certificate is read from this instance's health
 * endpoints. The page's only job is to make those answers legible.
 */

const API = "/api/v1";
const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
const $ = (id) => document.getElementById(id);
const fmt = new Intl.NumberFormat("en", { maximumFractionDigits: 0 });
const fmt1 = new Intl.NumberFormat("en", { maximumFractionDigits: 1, minimumFractionDigits: 1 });

async function post(path, body) {
  const response = await fetch(API + path, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = Array.isArray(data.detail)
      ? data.detail.map((d) => d.msg).join("; ")
      : data.detail;
    throw new Error(detail || "The server answered " + response.status + ".");
  }
  return data;
}

async function get(path) {
  const response = await fetch(API + path, { headers: { accept: "application/json" } });
  if (!response.ok) throw new Error("The server answered " + response.status + ".");
  return response.json();
}

function esc(text) {
  return String(text ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function compact(value) {
  const abs = Math.abs(value);
  if (abs >= 1e6) return fmt1.format(value / 1e6) + "M";
  if (abs >= 1e3) return fmt1.format(value / 1e3) + "k";
  return fmt1.format(value);
}

/* ------------------------------------------------------------------ theme */

$("theme").addEventListener("click", () => {
  const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem("ds.theme", next); } catch { /* private mode */ }
});

/* ================================================================== CHART */

const ALPHAS = [0.002, 0.005, 0.01, 0.02, 0.05];

/** A deterministic example series: revenue per three-hour cycle.
 *
 * AR(1) noise around a level, so it wanders the way a real metric does
 * rather than looking like white noise. Seeded, so every visitor sees the
 * same chart and a screenshot matches what anyone else sees.
 */
function exampleSeries(n = 96, seed = 20260829) {
  let s = seed >>> 0;
  const rand = () => {
    s = (s + 0x6d2b79f5) >>> 0;
    let t = s;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
  const gauss = () => Math.sqrt(-2 * Math.log(rand() || 1e-9)) * Math.cos(2 * Math.PI * rand());
  const out = [];
  let e = 0;
  for (let i = 0; i < n; i++) {
    e = 0.55 * e + gauss() * 900;
    out.push(Math.round(48200 + e));
  }
  return out;
}

const chart = {
  svg: $("chart"),
  tag: $("tag"),
  history: exampleSeries(),
  candidate: 48900,
  alpha: 0.02,
  result: null,
  domain: null,
  dragging: false,
  scripted: true,
  inflight: false,
  pending: false,
  failed: false,
};

function geometry() {
  const rect = chart.svg.getBoundingClientRect();
  const W = Math.max(280, rect.width);
  const H = Math.max(200, rect.height);
  const wide = W >= 640;
  return { W, H, wide, l: wide ? 62 : 44, r: wide ? 236 : 22, t: 18, b: 30 };
}

/** The value range the chart shows. Recomputed only when nothing is being
 * dragged: a scale that moved under the pointer would make the reading
 * slide away from the finger. */
function computeDomain(extra = []) {
  const r = chart.result;
  const values = [...chart.history, chart.candidate, ...extra];
  if (r && r.upper != null) values.push(r.upper, r.lower);
  let lo = Math.min(...values);
  let hi = Math.max(...values);
  const pad = (hi - lo) * 0.12 || Math.abs(hi) * 0.05 || 1;
  chart.domain = [lo - pad, hi + pad];
}

function yOf(v, g) {
  const [lo, hi] = chart.domain;
  return g.t + ((hi - v) / (hi - lo)) * (g.H - g.t - g.b);
}
function vOf(y, g) {
  const [lo, hi] = chart.domain;
  return hi - ((y - g.t) / (g.H - g.t - g.b)) * (hi - lo);
}
function xOf(i, g) {
  const n = chart.history.length + 1;
  return g.l + (i / (n - 1)) * (g.W - g.l - g.r);
}

function niceTicks(lo, hi, count = 5) {
  const span = hi - lo;
  const step0 = span / count;
  const mag = 10 ** Math.floor(Math.log10(step0));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= step0) || step0;
  const ticks = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi; v += step) ticks.push(v);
  return ticks;
}

/** The static layer: grid, limits, band, history. Redrawn when the history,
 * the limits or the domain change -- never during a drag. */
function drawStatic(animate = false) {
  // A full redraw replaces the reading's element. If it had keyboard focus,
  // hand focus to its replacement, or arrow-key users lose their place.
  const hadFocus = chart.svg.contains(document.activeElement);
  const g = geometry();
  chart.svg.setAttribute("viewBox", "0 0 " + g.W + " " + g.H);
  if (!chart.domain) computeDomain();
  const r = chart.result;
  const parts = [];

  for (const v of niceTicks(...chart.domain)) {
    const y = yOf(v, g);
    parts.push(`<line class="gridline" x1="${g.l}" x2="${g.W - g.r + 8}" y1="${y}" y2="${y}"/>`);
    parts.push(`<text class="tick" x="${g.l - 10}" y="${y + 4}" text-anchor="end">${compact(v)}</text>`);
  }

  const xEnd = g.W - g.r + 8;
  if (r && r.upper != null) {
    const yu = yOf(r.upper, g);
    const yl = yOf(r.lower, g);
    parts.push(`<rect class="band" x="${g.l}" y="${yu}" width="${xEnd - g.l}" height="${Math.max(0, yl - yu)}"/>`);
    parts.push(`<line class="limit" x1="${g.l}" x2="${xEnd}" y1="${yu}" y2="${yu}"/>`);
    parts.push(`<line class="limit" x1="${g.l}" x2="${xEnd}" y1="${yl}" y2="${yl}"/>`);
    if (g.wide) {
      parts.push(`<text class="limit-label" x="${g.l + 6}" y="${yu - 6}">UCL ${compact(r.upper)}</text>`);
      parts.push(`<text class="limit-label" x="${g.l + 6}" y="${yl + 15}">LCL ${compact(r.lower)}</text>`);
    }
  }
  if (r) {
    const yc = yOf(r.center, g);
    parts.push(`<line class="cl" x1="${g.l}" x2="${xEnd}" y1="${yc}" y2="${yc}"/>`);
  }

  const pts = chart.history.map((v, i) => [xOf(i, g), yOf(v, g)]);
  const d = pts.map((p, i) => (i ? "L" : "M") + p[0].toFixed(1) + " " + p[1].toFixed(1)).join("");
  parts.push(`<path class="series" id="series" d="${d}"/>`);
  for (const [x, y] of pts) parts.push(`<circle class="dot" cx="${x}" cy="${y}" r="2.2"/>`);

  // x-axis: cycles back from now.
  const n = chart.history.length;
  for (const k of [0, Math.floor(n / 2), n]) {
    const label = k === n ? "now" : "−" + ((n - k) * 3) + "h";
    parts.push(`<text class="tick" x="${xOf(k, g)}" y="${g.H - 8}" text-anchor="middle">${label}</text>`);
  }

  parts.push(`<g id="dynamic">
      <path class="lead" id="lead"/>
      <path class="string" id="string"/>
      <g class="reading" id="reading" tabindex="0" role="slider" aria-label="Latest reading">
        <circle class="handle-ring" id="ring" r="9"/>
        <circle class="handle-hit" id="hit" r="24"/>
        <circle class="handle" id="handle" r="8"/>
      </g>
    </g>`);
  chart.svg.innerHTML = parts.join("");
  if (hadFocus) $("reading").focus({ preventScroll: true });

  if (animate && !reduceMotion) {
    const path = $("series");
    const len = path.getTotalLength();
    chart.svg.style.setProperty("--len", len);
    chart.svg.classList.add("draw");
  } else {
    chart.svg.classList.remove("draw");
  }
  drawDynamic();
}

/** The reading under inspection, its lead line, its tag. Updated in place
 * on every pointer move -- the elements persist, so focus does too. */
function drawDynamic() {
  const reading = $("reading");
  if (!reading || !chart.domain) return;
  const g = geometry();
  const n = chart.history.length;
  const x0 = xOf(n - 1, g);
  const y0 = yOf(chart.history[n - 1], g);
  const x = xOf(n, g);
  const y = yOf(chart.candidate, g);
  const r = chart.result;
  const out = !!(r && r.breached);

  $("lead").setAttribute("d", `M${x0} ${y0} L${x} ${y}`);
  $("lead").classList.toggle("lead--out", out);
  for (const id of ["ring", "hit", "handle"]) {
    $(id).setAttribute("cx", x);
    $(id).setAttribute("cy", y);
  }
  $("handle").setAttribute("r", chart.dragging ? 10 : 8);
  $("handle").classList.toggle("is-out", out);
  $("ring").classList.toggle("is-out", out);

  reading.setAttribute("aria-valuemin", Math.round(chart.domain[0]));
  reading.setAttribute("aria-valuemax", Math.round(chart.domain[1]));
  reading.setAttribute("aria-valuenow", Math.round(chart.candidate));
  reading.setAttribute("aria-valuetext", fmt.format(chart.candidate) +
    (r && r.calibrated ? (out ? ", out of control" : ", in control") : ""));

  const tagH = chart.tag.offsetHeight || 84;
  const tagLeft = x + 30;
  const tagTop = Math.min(Math.max(y - tagH / 2, 0), g.H - tagH - 4);
  const holeX = tagLeft + 18;
  const holeY = tagTop + tagH / 2;
  if (g.wide && r) {
    $("string").setAttribute("d",
      `M${x + 6} ${y} Q ${(x + holeX) / 2} ${Math.max(y, holeY) + 16} ${holeX} ${holeY}`);
    chart.tag.style.left = tagLeft + "px";
    chart.tag.style.top = tagTop + "px";
    chart.tag.classList.remove("is-hidden");
  } else {
    $("string").setAttribute("d", "");
    chart.tag.classList.add("is-hidden");
  }
}

function renderVerdict() {
  const r = chart.result;
  const readout = $("readout");
  if (chart.failed) {
    readout.innerHTML = "The inspection service isn’t answering. If you are running this " +
      "locally, start it with <b>py ds.py serve</b> and reload.";
    return;
  }
  if (!r) return;

  const stamp = $("tag-stamp");
  if (!r.calibrated) {
    chart.tag.classList.remove("is-out");
    stamp.textContent = "Calibrating";
    $("tag-reading").textContent = r.readings + " readings";
    $("tag-p").textContent = "needs " + r.needs + " more";
    readout.innerHTML = `Only ${r.readings} readings. The calibrator needs at least ` +
      `${r.readings + r.needs} before it will draw a limit — a young column that raises ` +
      `alarms is worse than one that raises none.`;
    return;
  }

  const out = r.breached;
  chart.tag.classList.toggle("is-out", out);
  chart.tag.style.setProperty("--tag-tilt", out ? "5deg" : "-3deg");
  stamp.textContent = out ? "Out of control" : "In control";
  const pct = ((r.candidate - r.center) / Math.abs(r.center || 1)) * 100;
  $("tag-reading").textContent = fmt.format(r.candidate) + "  (" + (pct >= 0 ? "+" : "") + fmt1.format(pct) + "%)";
  $("tag-p").textContent = "p = " + r.p_value.toFixed(3) + " · " + r.readings + " readings";

  const side = r.direction === "up" ? "above" : "below";
  const verdict = out
    ? `<span class="out">Out of control</span> — this reading would open an incident.`
    : `<span class="in">In control</span> — ordinary variation, no alarm.`;
  let note = "";
  if (r.alpha < r.alpha_floor) {
    note = ` With ${r.readings} readings the finest false-alarm rate anyone can honestly ` +
      `promise is ${fmt1.format(r.alpha_floor * 100)}%, so the limit sits at the most extreme ` +
      `reading seen. More history buys a stricter guarantee.`;
  }
  readout.innerHTML =
    `Reading <b>${fmt.format(r.candidate)}</b> is <b>${fmt1.format(r.candidate_score)}</b> robust ` +
    `spreads ${side} the centre line. At a ${fmt1.format(r.alpha * 100)}% false-alarm target the ` +
    `limit is <b>${fmt1.format(r.threshold_score)}</b> spreads. ${verdict}${note}`;
}

/** One request in flight at a time. While it is out, only the latest
 * position is remembered, so a fast drag costs one request per round trip
 * rather than one per pixel. */
async function inspect() {
  if (chart.inflight) { chart.pending = true; return; }
  chart.inflight = true;
  const firstAnswer = !chart.result;
  try {
    const previous = chart.result;
    chart.result = await post("/playground/detect", {
      history: chart.history, candidate: chart.candidate, alpha: chart.alpha,
    });
    chart.failed = false;
    const limitsMoved = !previous || previous.upper !== chart.result.upper
      || previous.readings !== chart.result.readings;
    if (limitsMoved && !chart.dragging) { computeDomain(chart.scriptTarget ? [chart.scriptTarget] : []); }
    if (limitsMoved) drawStatic(firstAnswer);
    else drawDynamic();
    renderVerdict();
  } catch (error) {
    chart.failed = true;
    renderVerdict();
    console.warn(error);
  } finally {
    chart.inflight = false;
    if (chart.pending) { chart.pending = false; inspect(); }
  }
}

function setCandidate(value) {
  const [lo, hi] = chart.domain;
  chart.candidate = Math.min(hi, Math.max(lo, value));
  drawDynamic();
  inspect();
}

function stopScript() {
  chart.scripted = false;
  chart.scriptTarget = null;
  if (chart.tween) cancelAnimationFrame(chart.tween);
}

/* -- pointer and keyboard --------------------------------------------------- */

chart.svg.addEventListener("pointerdown", (event) => {
  if (!event.target.closest(".reading")) return;
  stopScript();
  chart.dragging = true;
  chart.svg.setPointerCapture(event.pointerId);
  $("hint").classList.remove("is-pulsing");
  event.preventDefault();
});
chart.svg.addEventListener("pointermove", (event) => {
  if (!chart.dragging) return;
  const rect = chart.svg.getBoundingClientRect();
  setCandidate(vOf(event.clientY - rect.top, geometry()));
});
const endDrag = () => {
  if (!chart.dragging) return;
  chart.dragging = false;
  // Now that nothing is under the finger, let the scale follow the reading,
  // so dragging past the top and letting go makes room to drag further.
  computeDomain([chart.candidate + (chart.candidate - chart.domain[0]) * 0.25]);
  drawStatic();
  renderVerdict();
};
chart.svg.addEventListener("pointerup", endDrag);
chart.svg.addEventListener("pointercancel", endDrag);

function onKey(event) {
  const r = chart.result;
  const step = r ? r.scale * (event.shiftKey ? 3 : 0.5) : 100;
  const moves = {
    ArrowUp: step, ArrowRight: step, ArrowDown: -step, ArrowLeft: -step,
    PageUp: step * 6, PageDown: -step * 6,
  };
  if (event.key in moves) {
    stopScript();
    const next = chart.candidate + moves[event.key];
    if (next > chart.domain[1] || next < chart.domain[0]) {
      computeDomain([next]);
      chart.candidate = next;
      drawStatic();
      inspect();
    } else {
      setCandidate(next);
    }
    event.preventDefault();
  } else if (event.key === "Home" && r) {
    stopScript();
    setCandidate(r.center);
    event.preventDefault();
  }
}
chart.svg.addEventListener("keydown", (event) => {
  if (event.target.closest && event.target.closest(".reading")) onKey(event);
});

/* -- controls --------------------------------------------------------------- */

$("alpha").addEventListener("input", (event) => {
  chart.alpha = ALPHAS[Number(event.target.value)];
  $("alpha-out").textContent = fmt1.format(chart.alpha * 100).replace(/\.0$/, "") + "%";
  inspect();
});

$("own-toggle").addEventListener("click", () => {
  const panel = $("own");
  panel.hidden = !panel.hidden;
  $("own-toggle").setAttribute("aria-expanded", String(!panel.hidden));
  if (!panel.hidden) $("own-series").focus();
});

$("own-apply").addEventListener("click", () => {
  const values = $("own-series").value
    .split(/[\s,;]+/).map((t) => t.trim()).filter(Boolean).map(Number);
  const bad = values.findIndex((v) => !Number.isFinite(v));
  const msg = $("own-msg");
  if (bad >= 0) { msg.textContent = "Item " + (bad + 1) + " isn’t a number."; return; }
  if (values.length < 31) {
    msg.textContent = "That’s " + values.length + " readings. Paste at least 31 — thirty to calibrate, one to inspect.";
    return;
  }
  if (values.length > 401) { msg.textContent = "Paste 401 readings or fewer."; return; }
  msg.textContent = "";
  stopScript();
  chart.history = values.slice(0, -1);
  chart.candidate = values[values.length - 1];
  chart.result = null;
  computeDomain();
  drawStatic();
  inspect();
});

$("own-reset").addEventListener("click", () => {
  stopScript();
  chart.history = exampleSeries();
  chart.candidate = 48900;
  chart.result = null;
  $("own-series").value = "";
  $("own-msg").textContent = "";
  computeDomain();
  drawStatic();
  inspect();
});

new ResizeObserver(() => { if (chart.domain) drawStatic(); }).observe(chart.svg);

/* -- the opening: a regression arrives ---------------------------------------- */

function playRegression() {
  const target = Math.round(chart.history.reduce((a, b) => a + b, 0) / chart.history.length * 2.02);
  chart.scriptTarget = target;
  computeDomain([target]);
  inspect();

  const begin = () => {
    if (!chart.scripted) return;
    if (reduceMotion) { setCandidate(target); finish(); return; }
    const from = chart.candidate;
    const start = performance.now();
    const duration = 950;
    const frame = (now) => {
      if (!chart.scripted) return;
      const t = Math.min(1, (now - start) / duration);
      const eased = 1 - Math.pow(1 - t, 3);
      setCandidate(from + (target - from) * eased);
      if (t < 1) chart.tween = requestAnimationFrame(frame);
      else finish();
    };
    chart.tween = requestAnimationFrame(frame);
  };
  const finish = () => {
    chart.scripted = false;
    $("hint").classList.add("is-pulsing");
  };
  setTimeout(begin, reduceMotion ? 0 : 1700);
}

playRegression();

/* ============================================================ SPECIMENS */

function sparkline(svg) {
  const kind = svg.dataset.spark;
  const W = 200;
  const H = 56;
  const base = (i) => 28 + Math.sin(i * 1.7) * 3 + Math.cos(i * 0.9) * 2;
  const line = (pts, cls) => `<path class="${cls}" d="${pts.map((p, i) => (i ? "L" : "M") + p[0] + " " + p[1]).join("")}"/>`;
  const parts = [`<line class="s-lim" x1="0" x2="${W}" y1="18" y2="18"/>`,
                 `<line class="s-lim" x1="0" x2="${W}" y1="38" y2="38"/>`];
  if (kind === "nulls") {
    for (let i = 0; i < 20; i++) {
      const bad = i >= 14;
      const h = bad ? 20 + (i % 3) * 3 : 3 + (i % 4);
      parts.push(`<rect class="s-bar${bad ? " s-bar--bad" : ""}" x="${i * 10 + 1}" y="${H - 4 - h}" width="7" height="${h}"/>`);
    }
    parts.splice(0, 2, `<line class="s-lim" x1="0" x2="${W}" y1="${H - 14}" y2="${H - 14}"/>`);
  } else {
    const ok = [];
    const bad = [];
    for (let i = 0; i <= 20; i++) {
      const x = i * 10;
      const broken = i >= 14;
      let y = base(i);
      if (broken && kind === "step-up") y = 6 + Math.sin(i) * 2;
      if (broken && kind === "drop") y = 52 + Math.sin(i) * 1;
      if (i <= 14) ok.push([x, i === 14 ? base(i) : y]);
      if (i >= 13) bad.push([x, i === 13 ? base(i) : y]);
    }
    parts.push(line(ok, "s-line"), line(bad, "s-bad"));
  }
  svg.innerHTML = parts.join("");
}
document.querySelectorAll("[data-spark]").forEach(sparkline);

/* ============================================================== STATIONS */

const STATIONS = [
  {
    name: "Profile",
    gist: "summarise every column, every cycle",
    body: "Each cycle, every column is reduced to mergeable sketches — t-digest quantiles, " +
      "HyperLogLog distinct counts, Count-Min top values, null and row counts. The sketches " +
      "merge across partitions and across time, so a profile of a billion rows costs the " +
      "same few kilobytes as a profile of a thousand.",
    fig: "0 raw values",
    note: "Profiles hold sketches and salted hashes only. A test reads what is actually on disk to prove it.",
    where: "packages/datasentry_sketches · services/profiler",
  },
  {
    name: "Detect",
    gist: "score each column against its own history",
    body: "Seven detector families score every column: volume, freshness, null rate, " +
      "distribution drift, new categories, distinctness and schema. A conformal calibrator " +
      "turns each score into a threshold with a stated false-alarm rate, learned from that " +
      "column’s own history — the chart above is this station.",
    fig: "1 limit per column",
    note: "No hand-tuned thresholds. The false-alarm rate is a setting, and the observed rate is published against it.",
    where: "packages/datasentry_detectors",
  },
  {
    name: "Correlate",
    gist: "one root, one incident",
    body: "A broken join moves dozens of columns at once. Anomalies that arrive together and " +
      "sit close in the lineage graph become one incident, and the most upstream asset is " +
      "elected its root — so a single regression pages once, not fifty times.",
    fig: "1 page",
    note: "Per root cause, not per column. Decoy changes that look alarming but are legitimate are part of the test harness.",
    where: "services/correlator · packages/datasentry_lineage",
  },
  {
    name: "Diagnose",
    gist: "explain it, citing evidence",
    body: "A reasoner reads the profiles, the semantic diff of recent SQL and the lineage, and " +
      "proposes causes. Every claim has to cite evidence by id. An adversarial verifier then " +
      "tries to reject each hypothesis, and anything it cannot support is dropped rather " +
      "than softened.",
    fig: "0 uncited claims",
    note: "Runs on Groq or Gemini on their free tiers, or on a deterministic reasoner with no network at all.",
    where: "packages/datasentry_agent",
  },
  {
    name: "Act",
    gist: "policy decides, not the model",
    body: "The reasoner proposes; a policy gate decides. Low-risk, reversible actions — opening " +
      "a pull request that reverts one clause, notifying an owner — run on their own. " +
      "Anything riskier waits for a person in the approval tray.",
    fig: "0 ways to DROP",
    note: "There is no DELETE, DROP or TRUNCATE to approve. They were never implemented, which is stronger than forbidding them.",
    where: "services/policy_gate · services/action_executor",
  },
  {
    name: "Verify",
    gist: "close only when the data is back",
    body: "After a fix, the affected columns are profiled again. An incident closes when the " +
      "readings are back inside their limits, and is escalated if they are not. Nothing " +
      "closes because time passed.",
    fig: "0 closed on a timer",
    note: "Every human disposition feeds the recalibration job, so the detectors learn which alarms were worth raising.",
    where: "services/verification · services/detector/recalibration.py",
  },
];

function renderStations() {
  const list = $("stations");
  list.innerHTML = STATIONS.map((s, i) => `
    <li class="station" role="presentation">
      <button type="button" role="tab" id="st-${i}" aria-controls="station-panel"
              aria-selected="${i === 0}" tabindex="${i === 0 ? 0 : -1}" data-i="${i}">
        <span class="node">${String(i + 1).padStart(2, "0")}</span>
        <span><span class="name">${esc(s.name)}</span><br><span class="gist">${esc(s.gist)}</span></span>
      </button>
    </li>`).join("");
  const tabs = [...list.querySelectorAll("[role=tab]")];
  const select = (i, focus) => {
    tabs.forEach((t, k) => {
      t.setAttribute("aria-selected", String(k === i));
      t.tabIndex = k === i ? 0 : -1;
    });
    if (focus) tabs[i].focus();
    const s = STATIONS[i];
    const panel = $("station-panel");
    panel.setAttribute("aria-labelledby", "st-" + i);
    panel.innerHTML = `
      <div>
        <h3>${esc(s.name)}</h3>
        <p>${esc(s.body)}</p>
        <p class="where">${esc(s.where)}</p>
      </div>
      <div class="guarantee">
        <p class="label">What it guarantees</p>
        <p class="fig">${esc(s.fig)}</p>
        <p class="fig-note">${esc(s.note)}</p>
      </div>`;
  };
  tabs.forEach((tab, i) => {
    tab.addEventListener("click", () => select(i, false));
    tab.addEventListener("keydown", (e) => {
      const keys = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 };
      if (e.key in keys) {
        select((i + keys[e.key] + tabs.length) % tabs.length, true);
        e.preventDefault();
      } else if (e.key === "Home") { select(0, true); e.preventDefault(); }
      else if (e.key === "End") { select(tabs.length - 1, true); e.preventDefault(); }
    });
  });
  select(0, false);
}
renderStations();

/* =================================================================== LAB */

const lab = { examples: [], active: null };

async function loadExamples() {
  try {
    const data = await get("/playground/examples");
    lab.examples = data.sql;
    $("presets").innerHTML = lab.examples.map((e) =>
      `<button class="preset" type="button" aria-pressed="false" data-id="${esc(e.id)}">${esc(e.label)}</button>`,
    ).join("");
    $("presets").querySelectorAll(".preset").forEach((b) =>
      b.addEventListener("click", () => choose(b.dataset.id, true)));
    choose(lab.examples[0].id, false);
    runTrace();
  } catch {
    $("lab-msg").textContent = "The examples didn’t load. You can still paste your own SQL.";
  }
}

function choose(id, run) {
  const example = lab.examples.find((e) => e.id === id);
  if (!example) return;
  lab.active = id;
  $("sql-before").value = example.before;
  $("sql-after").value = example.after;
  $("story").textContent = example.story;
  $("presets").querySelectorAll(".preset").forEach((b) =>
    b.setAttribute("aria-pressed", String(b.dataset.id === id)));
  if (run) runTrace();
}

["sql-before", "sql-after"].forEach((id) => $(id).addEventListener("input", () => {
  if (!lab.active) return;
  lab.active = null;
  $("story").textContent = "Your own SQL. Nothing you paste is stored.";
  $("presets").querySelectorAll(".preset").forEach((b) => b.setAttribute("aria-pressed", "false"));
}));

$("inspect").addEventListener("click", runTrace);

async function runTrace() {
  const before = $("sql-before").value;
  const after = $("sql-after").value;
  const msg = $("lab-msg");
  msg.classList.remove("is-error");
  if (!before.trim()) {
    msg.textContent = "Paste the model as it was into “Before”.";
    msg.classList.add("is-error");
    return;
  }
  const button = $("inspect");
  button.disabled = true;
  msg.textContent = "Inspecting…";
  try {
    const result = await post("/playground/trace", { before, after, model: "fct_revenue" });
    renderReport(result);
    msg.textContent = "";
  } catch (error) {
    msg.textContent = error.message;
    msg.classList.add("is-error");
  } finally {
    button.disabled = false;
  }
}

function renderReport(result) {
  const report = $("report");
  report.hidden = false;
  const lineage = result.after || result.before;
  const diff = result.diff;
  const stamp = $("report-stamp");

  if (lineage.error) {
    stamp.className = "stamp stamp--notable";
    stamp.textContent = "Unreadable";
    $("report-headline").textContent = "The resolver couldn’t read this SQL: " + lineage.error;
    $("changes").innerHTML = "<li><span></span><span>DataSentry’s reader handles SELECT statements with CTEs, joins, subqueries and aggregates. Check the statement starts with SELECT or WITH.</span></li>";
    $("lineage").innerHTML = "";
    $("coverage").textContent = "";
    return;
  }

  if (!diff) {
    stamp.className = "stamp stamp--clean";
    stamp.textContent = "Traced";
    $("report-headline").textContent = "No second version to compare. Here is the column lineage of the model as written.";
    $("changes").innerHTML = "";
  } else if (diff.error) {
    stamp.className = "stamp stamp--notable";
    stamp.textContent = "Unreadable";
    $("report-headline").textContent = diff.error;
    $("changes").innerHTML = "";
  } else {
    const breaking = diff.breaking_count > 0;
    const any = diff.changes.length > 0;
    stamp.className = "stamp " + (breaking ? "" : any ? "stamp--notable" : "stamp--clean");
    stamp.textContent = breaking ? "Breaking" : any ? "Notable" : "No change";
    $("report-headline").textContent = any
      ? diff.headline.charAt(0).toUpperCase() + diff.headline.slice(1) + "."
      : "The two versions compute the same thing. Formatting and aliases don’t count as change.";
    $("changes").innerHTML = diff.changes.map((c) =>
      `<li><span class="sev sev--${esc(c.severity)}">${esc(c.severity)}</span><span>${esc(c.detail)}</span></li>`,
    ).join("");
  }

  const pct = Math.round((lineage.coverage || 0) * 100);
  $("coverage").textContent = pct + "% of columns traced";
  drawLineage(lineage, result.impact || []);
}

/** Source tables on the left, the model's output columns on the right, an
 * edge for every column that feeds another. Edges into a column the change
 * reaches are drawn hot, so the blast radius of an edit is visible at a
 * glance -- which is the question a reviewer is actually asking. */
function drawLineage(lineage, impact) {
  const svg = $("lineage");
  const status = Object.fromEntries(impact.map((i) => [i.column.toLowerCase(), i]));
  const removed = impact.filter((i) => i.status === "removed").map((i) => ({
    name: i.column, transform: "REMOVED", sources: [], removed: true,
  }));
  const outputs = [...lineage.columns, ...removed];

  // Every (table, column) that feeds an output, grouped by table.
  const tables = new Map();
  for (const col of outputs) {
    for (const src of col.sources) {
      const dot = src.lastIndexOf(".");
      const table = src.slice(0, dot);
      const column = src.slice(dot + 1);
      if (!tables.has(table)) tables.set(table, []);
      if (!tables.get(table).includes(column)) tables.get(table).push(column);
    }
  }
  for (const t of lineage.tables) if (!tables.has(t)) tables.set(t, []);

  const W = 640;
  const rowH = 22;
  const leftW = 210;
  const outW = 190;
  const outX = W - outW;
  const pad = 10;

  const srcPos = new Map();
  let y = 6;
  const tableParts = [];
  for (const [table, cols] of tables) {
    const h = 30 + Math.max(1, cols.length) * rowH;
    tableParts.push(`<rect class="tbl" x="0" y="${y}" width="${leftW}" height="${h}" rx="4"/>`);
    tableParts.push(`<text class="tbl-name" x="${pad}" y="${y + 20}">${esc(table)}</text>`);
    cols.forEach((c, k) => {
      const cy = y + 30 + k * rowH + 12;
      srcPos.set(table + "." + c, cy);
      tableParts.push(`<text class="src-col" x="${pad + 8}" y="${cy + 4}">${esc(c)}</text>`);
    });
    if (!cols.length) tableParts.push(`<text class="src-col" x="${pad + 8}" y="${y + 46}">join only</text>`);
    y += h + 14;
  }
  const leftH = y;

  const outH = outputs.length * (rowH + 12) + 40;
  const H = Math.max(leftH, outH) + 4;
  const outTop = Math.max(6, (H - outputs.length * (rowH + 12)) / 2);
  const outParts = [];
  const edges = [];

  outputs.forEach((col, k) => {
    const oy = outTop + k * (rowH + 12);
    const state = status[col.name.toLowerCase()] || { status: col.removed ? "removed" : "unchanged" };
    outParts.push(`<g class="is-${esc(state.status)}"><title>${esc(state.reason || "")}</title>` +
      `<rect class="out" x="${outX}" y="${oy}" width="${outW}" height="${rowH + 4}" rx="3"/>` +
      `<text class="out-name" x="${outX + 10}" y="${oy + 17}">${esc(col.name)}</text>` +
      `<text class="out-kind" x="${outX + outW - 8}" y="${oy + 17}" text-anchor="end">${esc(String(col.transform).toLowerCase())}</text></g>`);
    const ty = oy + (rowH + 4) / 2;
    const hot = state.status === "changed" || state.status === "rowset";
    const rows = state.status === "rows";
    for (const src of col.sources) {
      const sy = srcPos.get(src);
      if (sy == null) continue;
      const x1 = leftW;
      const x2 = outX;
      const mx = (x1 + x2) / 2;
      edges.push(`<path class="edge${hot ? " edge--hot" : rows ? " edge--rows" : ""}" d="M${x1} ${sy} C ${mx} ${sy}, ${mx} ${ty}, ${x2} ${ty}"/>`);
    }
  });

  // Hot edges last, so they sit on top of the quiet ones.
  edges.sort((a, b) => (a.includes("--hot") ? 1 : 0) - (b.includes("--hot") ? 1 : 0));
  const modelX = (leftW + outX) / 2;
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.innerHTML = edges.join("") + tableParts.join("") + outParts.join("") +
    `<rect class="model" x="${modelX - 56}" y="${H / 2 - 13}" width="112" height="26" rx="3"/>` +
    `<text class="model-name" x="${modelX}" y="${H / 2 + 5}" text-anchor="middle">fct_revenue</text>`;
}

loadExamples();

/* =========================================================== CERTIFICATE */

const CERT_ROWS = [
  { key: "hallucination_rate", what: "Uncited claims", why: "diagnoses that asserted something no evidence supports", lower: true, fmt: (v) => v.toFixed(3) },
  { key: "decoy_fpr", what: "False alarms on legitimate change", why: "decoys that raised an incident", lower: true, fmt: (v) => v.toFixed(2) },
  { key: "root_accuracy", what: "Root asset found", why: "the correlator elected the table that actually broke", fmt: (v) => v.toFixed(2) },
  { key: "cause_accuracy", what: "Root cause found", why: "the diagnosis named the fault that was injected", fmt: (v) => v.toFixed(2) },
  { key: "recall", what: "Faults detected", why: "injected faults that produced an incident", fmt: (v) => v.toFixed(2) },
  { key: "precision", what: "Incidents that were real", why: "raised incidents that matched an injected fault", fmt: (v) => v.toFixed(2) },
  { key: "blast_recall", what: "Affected assets found", why: "downstream assets the blast radius correctly listed", fmt: (v) => v.toFixed(2) },
  { key: "median_cost", what: "Reasoning cost per incident", why: "median model spend, priced at paid rates", lower: true, fmt: (v) => "$" + v.toFixed(4) },
];

async function loadCertificate() {
  const rows = $("cert-rows");
  try {
    const [scores, pipeline] = await Promise.all([
      get("/health/scores"),
      get("/health/pipeline").catch(() => null),
    ]);
    $("cert-issued").textContent = new Date().toLocaleString("en", {
      dateStyle: "medium", timeStyle: "short",
    });
    if (!scores.available) {
      $("cert-sample").textContent = "none yet";
      rows.innerHTML = `<tr><td colspan="4">This instance hasn’t been measured yet. Run <span class="mono">py ds.py demo</span> on the server and reload — it injects faults with known answers and scores itself against them.</td></tr>`;
      return;
    }
    $("cert-sample").textContent = `${scores.faults} injected faults, ${scores.decoys} decoys, ${scores.incidents} incidents`;
    const out = CERT_ROWS.filter((r) => r.key in scores).map((r) => {
      const value = scores[r.key];
      const target = scores.targets[r.key];
      const pass = r.lower ? value <= target : value >= target;
      return `<tr>
        <td><span class="what">${esc(r.what)}</span><span class="why">${esc(r.why)}</span></td>
        <td class="num">${r.lower ? "≤" : "≥"} ${esc(r.fmt(target))}</td>
        <td class="num">${esc(r.fmt(value))}</td>
        <td><span class="verdict verdict--${pass ? "pass" : "fail"}">${pass ? "Within spec" : "Below spec"}</span></td>
      </tr>`;
    });
    if (pipeline) {
      out.push(`<tr>
        <td><span class="what">Columns with traced lineage</span><span class="why">output columns the SQL resolver mapped to a source</span></td>
        <td class="num">≥ 90%</td>
        <td class="num">${Math.round(pipeline.lineage_parse_coverage * 100)}%</td>
        <td><span class="verdict verdict--${pipeline.lineage_parse_coverage >= 0.9 ? "pass" : "fail"}">${pipeline.lineage_parse_coverage >= 0.9 ? "Within spec" : "Below spec"}</span></td>
      </tr>`);
    }
    rows.innerHTML = out.join("");
  } catch {
    rows.innerHTML = `<tr><td colspan="4">The health endpoints didn’t answer. The certificate is read live, so it needs the server running.</td></tr>`;
  }
}
loadCertificate();

/* ================================================================== COPY */

$("copy").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  const text = $(button.dataset.copy).innerText;
  try {
    await navigator.clipboard.writeText(text);
    button.textContent = "Copied";
  } catch {
    button.textContent = "Select and copy";
  }
  setTimeout(() => { button.textContent = "Copy"; }, 1800);
});
