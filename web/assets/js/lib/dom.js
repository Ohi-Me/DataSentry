/* Tiny DOM helpers.
 *
 * No framework. The console is seven screens over a JSON API, and a build
 * step would cost more than it saves — this way `ds serve` is the whole
 * product with nothing to install.
 *
 * `h` builds elements from a tag string with optional #id and .class parts,
 * so markup reads close to the HTML it produces. Text goes in as text nodes,
 * never as innerHTML, which means API strings can never become markup.
 */

export function h(spec, props = {}, ...children) {
  const [tagPart, ...classParts] = String(spec).split(".");
  const [tag, id] = tagPart.split("#");
  const el = document.createElement(tag || "div");
  if (id) el.id = id;
  if (classParts.length) el.className = classParts.join(" ");

  for (const [key, value] of Object.entries(props || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") el.className = [el.className, value].filter(Boolean).join(" ");
    else if (key === "html") el.innerHTML = value;      // only for our own SVG strings
    else if (key === "text") el.textContent = value;
    else if (key === "style" && typeof value === "object") Object.assign(el.style, value);
    else if (key.startsWith("on") && typeof value === "function")
      el.addEventListener(key.slice(2).toLowerCase(), value);
    else if (key === "dataset") Object.assign(el.dataset, value);
    else el.setAttribute(key, value === true ? "" : String(value));
  }

  append(el, children);
  return el;
}

export function append(parent, children) {
  for (const child of children.flat(4)) {
    if (child === null || child === undefined || child === false) continue;
    parent.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return parent;
}

export function svg(tag, props = {}, ...children) {
  const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key.startsWith("on") && typeof value === "function")
      el.addEventListener(key.slice(2).toLowerCase(), value);
    else el.setAttribute(key, String(value));
  }
  for (const child of children.flat(4)) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

export function icon(path, size = 16) {
  return h("span", {
    class: "icon",
    html:
      '<svg viewBox="0 0 24 24" width="' + size + '" height="' + size +
      '" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" ' +
      'stroke-linejoin="round" aria-hidden="true">' + path + "</svg>",
  });
}

export const ICONS = {
  check: '<path d="M4 12.5l5 5L20 6.5"/>',
  cross: '<path d="M6 6l12 12M18 6L6 18"/>',
  alert: '<path d="M12 8v5M12 17v.01"/><path d="M10.3 3.9L2.5 17.4A1.9 1.9 0 004.2 20h15.6a1.9 1.9 0 001.7-2.6L13.7 3.9a1.9 1.9 0 00-3.4 0z"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  git: '<circle cx="6" cy="6" r="2.5"/><circle cx="6" cy="18" r="2.5"/><circle cx="18" cy="12" r="2.5"/><path d="M6 8.5v7M8.5 6H14a2 2 0 012 2v1.5"/>',
  db: '<ellipse cx="12" cy="6" rx="8" ry="3"/><path d="M4 6v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6"/>',
  flow: '<circle cx="5" cy="6" r="2"/><circle cx="5" cy="18" r="2"/><circle cx="19" cy="12" r="2"/><path d="M7 6.8L17 11M7 17.2L17 13"/>',
  spark: '<path d="M3 13h4l2.5-6 4 12L16 13h5"/>',
  shield: '<path d="M12 3l8 3.5v5c0 5-3.4 9.4-8 10.5-4.6-1.1-8-5.5-8-10.5v-5L12 3z"/>',
  inbox: '<path d="M3 13h4l2 4h6l2-4h4M3 13l3-7h12l3 7v5a2 2 0 01-2 2H5a2 2 0 01-2-2v-5z"/>',
  play: '<path d="M7 4l12 8-12 8V4z"/>',
  external: '<path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 01-1 1H5a1 1 0 01-1-1V7a1 1 0 011-1h5"/>',
  quiet: '<circle cx="12" cy="12" r="9"/><path d="M8 12.5l2.5 2.5L16 9.5"/>',
};

export function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
  return node;
}

export function mount(node, ...children) {
  clear(node);
  append(node, children);
  return node;
}
