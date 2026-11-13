/* Empty states, shared.
 *
 * One distinction runs through this file and it is the reason it exists:
 * "there is nothing to show" and "nothing has ever run" are different
 * answers, and showing the same words for both is how a fresh clone and a
 * fresh deployment come to look exactly like a healthy, quiet system.
 *
 * `firstRun` is the second case. It is the first screen a new reader lands
 * on, so it is the one screen here whose job is to be read rather than
 * scanned, and it ends with the command that fixes it.
 */

import { h, icon, ICONS } from "./dom.js";

/** Has anything ever been written to this control plane?
 *
 * Derived from the pipeline health payload rather than from a row count on
 * the page being viewed, so every view agrees about which state it is in.
 */
export function isUnseeded(pipeline) {
  if (!pipeline) return false;
  return !pipeline.series_profiled && !pipeline.profiles && !pipeline.lineage_edges;
}

/** The database exists and is empty. Say what to type. */
export function firstRun({ what = "The control plane is empty." } = {}) {
  return h("div.empty.empty--first-run", {},
    icon(ICONS.db, 42),
    h("h3", { text: "No warehouse yet" }),
    h("p", { text: what
      + " DataSentry ships with a synthetic lakehouse and a fault harness, so "
      + "there is nothing to connect and nothing to configure — one command "
      + "builds the world, injects regressions, and runs detection, "
      + "correlation, root-cause analysis and remediation end to end." }),
    h("div.first-run-steps", {},
      step("py ds.py demo", "build the world and run the whole loop (a few minutes)"),
      step("py ds.py doctor", "check Python, dependencies, provider keys and the port"),
    ),
    h("p.faint", { style: { marginTop: "var(--s5)", fontSize: "var(--t-xs)" }, text:
      "This page reconnects on its own — leave it open and incidents arrive "
      + "over the live socket as they are raised." }),
  );
}

function step(command, explanation) {
  return h("div.first-run-step", {},
    h("code", { text: command }),
    h("span", { text: explanation }),
  );
}

/** The ordinary case: a filter matched nothing, or this slice is genuinely quiet. */
export function emptyBlock(title, detail, { glyph = "quiet", children = [] } = {}) {
  return h("div.empty", {},
    icon(ICONS[glyph] || ICONS.quiet, 42),
    h("h3", { text: title }),
    detail ? h("p", { text: detail }) : null,
    ...children,
  );
}
