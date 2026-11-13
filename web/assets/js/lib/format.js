/* Formatting and shared display atoms.
 *
 * Severity is rendered here once, and it always pairs a glyph and a label
 * with the colour. Encoding severity by colour alone fails for the ~8% of
 * men with a colour vision deficiency, and an on-call console is the last
 * place to get that wrong.
 */

import { h } from "./dom.js";

export function num(value, digits = 0) {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return Number(value).toLocaleString(undefined, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

export function pct(value, digits = 1) {
  if (value === null || value === undefined) return "—";
  return (Number(value) * 100).toFixed(digits) + "%";
}

export function usd(value) {
  if (value === null || value === undefined) return "—";
  const n = Number(value);
  if (n === 0) return "$0.0000";
  if (n < 0.0001) return "<$0.0001";
  return "$" + n.toFixed(4);
}

export function ms(value) {
  if (value === null || value === undefined) return "—";
  const n = Number(value);
  if (n < 1000) return Math.round(n) + " ms";
  if (n < 60000) return (n / 1000).toFixed(1) + " s";
  return (n / 60000).toFixed(1) + " min";
}

export function seconds(value) {
  if (value === null || value === undefined) return "—";
  const n = Number(value);
  // Sub-second is the common case with the deterministic reasoner, and
  // rendering a 21 ms run as "0.0 s" reads as a broken measurement rather
  // than a fast one.
  if (n < 1) return Math.round(n * 1000) + " ms";
  if (n < 90) return n.toFixed(1) + " s";
  return (n / 60).toFixed(1) + " min";
}

export function bytes(value) {
  const n = Number(value || 0);
  if (n < 1024) return Math.round(n) + " B";
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
  return (n / 1048576).toFixed(1) + " MB";
}

export function when(iso) {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return String(iso);
  return date.toLocaleString(undefined, {
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
  });
}

export function clock(iso) {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}

export function ago(iso) {
  if (!iso) return "—";
  const delta = (Date.now() - new Date(iso).getTime()) / 1000;
  if (!Number.isFinite(delta)) return "—";
  const abs = Math.abs(delta);
  if (abs < 90) return "just now";
  if (abs < 5400) return Math.round(abs / 60) + " min ago";
  if (abs < 172800) return Math.round(abs / 3600) + " h ago";
  return Math.round(abs / 86400) + " d ago";
}

const SEVERITY_LABEL = { 1: "critical", 2: "high", 3: "moderate", 4: "low" };
const SEVERITY_GLYPH = { 1: "!!", 2: "!", 3: "~", 4: "·" };

export function severity(level) {
  const n = Number(level) || 4;
  return h("span", { class: "sev sev-" + n, title: "severity " + n + " — " + SEVERITY_LABEL[n] },
    h("span.sev-glyph", { text: SEVERITY_GLYPH[n] }),
    h("span", { text: "S" + n + " " + SEVERITY_LABEL[n] }),
  );
}

const STATUS_TONE = {
  detected: "info",
  rca_running: "info",
  rca_complete: "ghost",
  awaiting_approval: "warn",
  remediating: "warn",
  verifying: "info",
  resolved: "good",
  escalated: "bad",
  dismissed: "ghost",
  budget_exceeded: "bad",
};

export function status(value) {
  const tone = STATUS_TONE[value] || "ghost";
  return h("span", { class: "pill pill--" + tone, text: String(value).replace(/_/g, " ") });
}

const RISK_TONE = { low: "good", medium: "warn", high: "bad" };

export function risk(tier) {
  return h("span", {
    class: "pill pill--" + (RISK_TONE[tier] || "ghost"),
    text: String(tier).toUpperCase() + " RISK",
  });
}

export function pill(text, tone = "ghost") {
  return h("span", { class: "pill pill--" + tone, text: String(text) });
}

export function meter(value, { max = 1, tone = "" } = {}) {
  const width = Math.max(0, Math.min(1, Number(value) / max)) * 100;
  return h("span", { class: "meter " + tone }, h("i", { style: { width: width + "%" } }));
}

/** Tone for a measured value against a target, respecting direction. */
export function gradeAgainst(value, target, { lowerIsBetter = false } = {}) {
  if (value === null || value === undefined) return "";
  const good = lowerIsBetter ? value <= target : value >= target;
  if (good) return "good";
  const near = lowerIsBetter ? value <= target * 1.5 : value >= target * 0.8;
  return near ? "warn" : "bad";
}

export function shortId(value) {
  return String(value || "").slice(0, 8);
}

export function titleCase(value) {
  return String(value || "").replace(/[_-]/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}
