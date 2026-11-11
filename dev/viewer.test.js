/* Committed Step 7 view test: stub SVG DOM, drive viewer.js with the real
 * RoutePlan fixture. No browser, no dependencies. Run from repo root:
 *   node dev/viewer.test.js
 */
const fs = require("fs");
const path = require("path");
const assert = require("assert");

const ROOT = path.join(__dirname, "..");
const src = fs.readFileSync(path.join(ROOT, "src", "view", "viewer.js"), "utf8");
const fixture = JSON.parse(fs.readFileSync(
  path.join(ROOT, "dev", "fixtures", "route_n607.json"), "utf8"));

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function find(root, pred) {
  if (pred(root)) return root;
  for (const c of root.children || []) {
    const hit = find(c, pred);
    if (hit) return hit;
  }
  return null;
}
function findAll(root, pred, acc = []) {
  if (pred(root)) acc.push(root);
  for (const c of root.children || []) findAll(c, pred, acc);
  return acc;
}
let throwOnceOnSetAttr = false;
function mkEl(tag) {
  return {
    tag, attrs: {}, children: [], _text: "", style: { removeProperty() {} },
    disabled: false, listeners: {},
    setAttribute(k, v) {
      if (throwOnceOnSetAttr) { throwOnceOnSetAttr = false; throw new Error("boom"); }
      this.attrs[k] = String(v);
    },
    getAttribute(k) { return this.attrs[k]; },
    appendChild(c) { this.children.push(c); return c; },
    addEventListener(t, fn) { (this.listeners[t] || (this.listeners[t] = [])).push(fn); },
    removeEventListener(t, fn) {
      this.listeners[t] = (this.listeners[t] || []).filter((f) => f !== fn);
    },
    dispatch(t, ev) {
      (this.listeners[t] || []).forEach((fn) => fn(ev));
    },
    querySelector(sel) {
      if (sel[0] === "#") return find(this, (n) => n.attrs && n.attrs.id === sel.slice(1));
      return find(this, (n) => n.tag === sel);
    },
    set textContent(v) { this._text = String(v); this.children = []; },
    get textContent() { return this._text; },
    set innerHTML(v) { this.children = []; },
    get innerHTML() { return ""; },
  };
}
const svg = mkEl("svg");
svg.clientWidth = 800; svg.clientHeight = 600;
svg.getBoundingClientRect = () => ({ left: 0, top: 0, width: 800, height: 600 });
svg.setPointerCapture = () => {};

const elements = { map: svg };
const el = (id) => (elements[id] || (elements[id] = mkEl("div")));
const sent = [];
let onMsg = null;
let domReady = null;

// Measured content size while the root is forced to max-content. Tests
// mutate it to prove changed sizes re-notify and stable ones do not.
const docRect = { width: 800, height: 520 };

global.window = {
  innerWidth: 800,
  __LIB_VIEW__: {
    maps: {
      "5F": { href: "data:image/webp;base64,AAA", w: 1515, h: 1051, fw: 6062, fh: 4205,
              outline: [[0, 0, 6062, 1900], [0, 1900, 1400, 4205]] },
      "6F": { href: "data:image/webp;base64,BBB", w: 1883, h: 1011, fw: 7534, fh: 4044,
              outline: [[0, 0, 7534, 1760]] },
    },
    walls: {
      "5F": [[[100, 100], [500, 100]], [[500, 100], [500, 400]]],
      "6F": [[[200, 200], [900, 200]], [[900, 200], [900, 500]]],
    },
  },
  parent: { postMessage: (m) => sent.push(m) },
  addEventListener: (t, fn) => { if (t === "message") onMsg = fn; },
  location: { reload: () => {} },
};
global.document = {
  getElementById: el,
  createElement: (t) => mkEl(t),
  createElementNS: (ns, t) => mkEl(t),
  documentElement: {
    style: {},
    scrollHeight: 100,
    scrollWidth: 800,
    dataset: {},
    // Mirrors the SDK measurement trick: while height is forced to
    // max-content the reading is the CONTENT box; at rest it is the
    // iframe viewport.
    getBoundingClientRect() {
      return this.style.height === "max-content"
        ? { left: 0, top: 0, width: docRect.width, height: docRect.height }
        : { left: 0, top: 0, width: 800, height: 180 };
    },
  },
  addEventListener: (t, fn) => { if (t === "DOMContentLoaded") domReady = fn; },
};

const driver = `
;(async () => {
  domReady();
  await sleep(50);
  const init = sent.find((m) => m.method === "ui/initialize");
  assert(init, "no initialize");
  assert(init.params.appCapabilities.tools !== undefined, "spec appCapabilities");
  // The host schema makes appInfo mandatory; omitting it is rejected outright
  // by the real Ask host (observed 2026-11-11).
  assert(init.params.appInfo && typeof init.params.appInfo.name === "string"
    && init.params.appInfo.name, "appInfo.name");
  assert(typeof init.params.appInfo.version === "string" && init.params.appInfo.version,
    "appInfo.version");
  assert((init.params.appCapabilities.availableDisplayModes || []).includes("inline"),
    "display modes");
  onMsg({ data: { jsonrpc: "2.0", id: init.id,
    result: { hostContext: { theme: "light" } } } });
  await sleep(50);

  onMsg({ data: { jsonrpc: "2.0", method: "ui/notifications/tool-result",
    params: { content: [{ type: "text", text: "route" }],
              structuredContent: FIXTURE } } });
  await sleep(50);
  assert(View._t.get().floor === "5F", "first leg floor");
  // The 3D model is the default presentation, so the route arrives in 3D.
  assert(View._t.get().mode === "3d", "default mode is 3d");
  // 3D draws the floor plate as a polygon and extrudes the walls into
  // vertical faces: at least one wall face per manifest wall of this floor.
  const wallFaces = findAll(svg, (n) => n.tag === "polygon"
    && n.attrs.fill === "url(#wall3d)");
  assert(wallFaces.length >= 2, "extruded wall faces: " + wallFaces.length);
  const wallCaps = findAll(svg, (n) => n.tag === "line"
    && n.attrs.stroke === "#f2e9f8");
  assert(wallCaps.length >= 2, "wall top caps: " + wallCaps.length);
  // The base map texture must be clipped to the plan outline, otherwise the
  // legend band of the base map would show up on the floor plate.
  const clipped = findAll(svg, (n) => n.tag === "g"
    && String(n.attrs["clip-path"] || "").indexOf("#clip-") >= 0);
  assert(clipped.length >= 1, "texture clipped to the plan outline");

  // The flat view fits the current route, not the full base image: both
  // floors carry a legend/title band that must not dominate the first frame.
  View._t.setMode("2d");
  await sleep(20);
  const vb5 = svg.attrs.viewBox.split(" ").map(Number);
  assert(vb5.every(Number.isFinite) && vb5[2] > 0 && vb5[3] > 0,
    "finite 5F viewBox: " + svg.attrs.viewBox);
  assert(vb5[2] < 1515 || vb5[3] < 1051,
    "5F viewBox must crop the legend, got " + svg.attrs.viewBox);
  assert(el("stage").style.aspectRatio ===
    Math.round(vb5[2] * 100) / 100 + " / " + Math.round(vb5[3] * 100) / 100,
    "2D stage aspect: " + el("stage").style.aspectRatio);
  const paths = findAll(svg, (n) => n.tag === "path");
  assert(paths.length >= 2, "casing+core");
  const kx = 1515 / 6062, ky = 1051 / 4205;
  const expect = "M" + (4065 * kx).toFixed(1) + "," + (1270 * ky).toFixed(1);
  assert(paths[0].attrs.d.startsWith(expect), "path start: " + paths[0].attrs.d);
  const fills = findAll(svg, (n) => n.tag === "circle").map((c) => c.attrs.fill);
  assert(fills.includes("green") && fills.includes("red") && fills.includes("#f90"),
    "station marks: " + fills);
  const tabs = (el("floors").children || []).map((b) => b.textContent);
  assert(tabs.join(",") === "5F,6F", "tabs: " + tabs);
  const childIds = svg.children.map((c) => c.attrs && c.attrs.id);
  assert(childIds.indexOf("tiles") < childIds.indexOf("route"),
    "tiles must render below the route: " + childIds.join(","));

  View._t.switchFloor("6F");
  await sleep(20);
  const vb6 = svg.attrs.viewBox.split(" ").map(Number);
  assert(vb6.every(Number.isFinite) && vb6[3] < 1011 * 0.55,
    "6F viewBox must exclude the legend band: " + svg.attrs.viewBox);

  const p = View._t.project(10, 20, 0, { S: 2, SQ: 1, SH: 0 });
  assert(p[0] === 20 && p[1] === 40, "projection degenerate: " + p);

  View._t.switchFloor("5F");
  View._t.setMode("3d");
  await sleep(20);
  assert(View._t.get().mode === "3d", "mode 3d");
  assert(findAll(svg, (n) => n.tag === "polygon").length >= 4, "slab sides");
  assert(findAll(svg, (n) => n.tag === "text")
    .some((t) => t.textContent.includes("Up to 6F")), "connector label");
  const vb3d = svg.attrs.viewBox;
  let prevented = false;
  svg.dispatch("wheel", { deltaY: -100, clientX: 120, clientY: 80,
    preventDefault() { prevented = true; } });
  svg.dispatch("pointerdown", { clientX: 120, clientY: 80, pointerId: 1 });
  svg.dispatch("pointermove", { clientX: 80, clientY: 40 });
  assert(svg.attrs.viewBox === vb3d, "3D wheel/drag must not touch viewBox");
  assert(!prevented, "3D wheel must leave chat scrolling alone");
  View._t.setTilt(1.0);
  View._t.setTilt(0.45);
  View._t.setMode("2d");
  assert(View._t.get().mode === "2d", "mode 2d");
  View._t.switchFloor("6F");
  View.reset();
  await sleep(20);
  assert(View._t.get().mode === "3d", "reset restores requested 3D mode");
  assert(View._t.get().floor === "5F", "reset restores first floor");
  assert(svg.attrs.viewBox === vb3d, "reset restores the initial 3D frame");

  // down-route (6F->5F): connector must span the two transit steps,
  // not the destination/origin endpoints (regression: old code used
  // legs[0] end + legs[1] start, correct only uphill).
  View._t.setTilt(0.62);
  const downLegs = [
    { ...FIXTURE.legs[1], transit: { id: "central-stairs", name: "Central Stairs",
      from_floor: "6F", to_floor: "5F", direction: "down", accessible: false } },
    { ...FIXTURE.legs[0], transit: null },
  ];
  onMsg({ data: { jsonrpc: "2.0", method: "ui/notifications/tool-result",
    params: { structuredContent: { legs: downLegs, origin: FIXTURE.destination,
      destination: FIXTURE.origin, plan_id: "down1", warnings: [] } } } });
  await sleep(20);
  View._t.setMode("3d");
  await sleep(20);
  assert(View._t.get().mode === "3d", "down 3d");
  const P = View._t.projParams();
  const zOf = (f) => (f === "6F" ? P.z6 : 0);
  const tp = (leg) => (leg.steps.find((s) => s.kind === "transit") || {}).point;
  const eA = View._t.project(...tp(downLegs[0]), zOf("6F"), P);
  const eB = View._t.project(...tp(downLegs[1]), zOf("5F"), P);
  // The connector is the red link between the two levels; the walls also
  // draw white top-cap lines, so pick the red one.
  const line = findAll(svg, (n) => n.tag === "line" && n.attrs.stroke === "#c00")[0];
  assert(line, "connector line present");
  assert(line.attrs.x1 === eA[0].toFixed(1) && line.attrs.y1 === eA[1].toFixed(1) &&
         line.attrs.x2 === eB[0].toFixed(1) && line.attrs.y2 === eB[1].toFixed(1),
    "connector endpoints: " + line.attrs.x1 + "," + line.attrs.y1 +
    " -> " + line.attrs.x2 + "," + line.attrs.y2);
  const lbl = findAll(svg, (n) => n.tag === "text").map((t) => t.textContent);
  assert(lbl.some((t) => t.includes("Down to 5F")), "down label: " + lbl);
  View._t.setMode("2d");

  // Size protocol: repeated unchanged measurements send nothing (dedupe on
  // the exact (w,h) pair); a changed measurement re-notifies exactly once.
  const sizeMsgs = () => sent.filter(
    (m) => m.method === "ui/notifications/size-changed");
  const nBefore = sizeMsgs().length;
  View._t.reportSize();
  View._t.reportSize();
  assert(sizeMsgs().length === nBefore, "unchanged size must not re-notify");
  docRect.height += 120;
  View._t.reportSize();
  assert(sizeMsgs().length === nBefore + 1, "changed size must notify once");
  assert(sizeMsgs()[nBefore].params.height === 640, "reported new height");
  docRect.height -= 120;

  // forced render throw -> automatic 2D fallback (flag is one-shot:
  // render3D eats it, the fallback render2D runs clean)
  throwOnceOnSetAttr = true;
  View._t.setMode("3d"); // re-render with mode already 3d
  await sleep(20);
  assert(View._t.get().mode === "2d", "throw must fall back to 2d");

  // single-leg route: 3D still works (one floor plate, no cross-floor
  // connector) -- a lone floor is a valid model, not a degraded case.
  onMsg({ data: { jsonrpc: "2.0", method: "ui/notifications/tool-result",
    params: { structuredContent: { legs: [{ floor: "5F", polyline: [[1, 2], [3, 4]],
      steps: [], map_file: "5f_base.jpg" }], warnings: [] } } } });
  await sleep(20);
  View._t.setMode("3d");
  await sleep(20);
  assert(View._t.get().mode === "3d", "1-leg must keep 3d");
  assert(findAll(svg, (n) => n.tag === "polygon").length >= 1,
    "1-leg still draws a floor plate");

  // garbage -> JSON mode; cancelled -> status+bar; empty -> error bar
  onMsg({ data: { jsonrpc: "2.0", method: "ui/notifications/tool-result",
    params: { structuredContent: { foo: 1 } } } });
  await sleep(20);
  onMsg({ data: { jsonrpc: "2.0", method: "ui/notifications/tool-cancelled",
    params: { reason: "user stopped" } } });
  await sleep(20);
  assert(el("errorbar").style.display === "block", "cancel must error-bar");
  onMsg({ data: { jsonrpc: "2.0", method: "ui/notifications/tool-result",
    params: { content: [] } } });
  await sleep(20);
  assert(el("errorbar").style.display === "block", "empty must error-bar");
  console.log("VIEWER TEST OK");
})().catch((e) => { console.error("VIEWER TEST FAILED:", e.message); process.exit(1); });
`;

eval("const FIXTURE = " + JSON.stringify(fixture) + ";\n" + src + driver);
