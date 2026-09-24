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
    disabled: false,
    setAttribute(k, v) {
      if (throwOnceOnSetAttr) { throwOnceOnSetAttr = false; throw new Error("boom"); }
      this.attrs[k] = String(v);
    },
    getAttribute(k) { return this.attrs[k]; },
    appendChild(c) { this.children.push(c); return c; },
    addEventListener() {}, removeEventListener() {},
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

global.window = {
  __LIB_VIEW__: {
    maps: {
      "5F": { href: "data:image/webp;base64,AAA", w: 1515, h: 1051, fw: 6062, fh: 4205 },
      "6F": { href: "data:image/webp;base64,BBB", w: 1883, h: 1011, fw: 7534, fh: 4044 },
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
  documentElement: { style: {}, scrollHeight: 100 },
  addEventListener: (t, fn) => { if (t === "DOMContentLoaded") domReady = fn; },
};

const driver = `
;(async () => {
  domReady();
  await sleep(50);
  const init = sent.find((m) => m.method === "ui/initialize");
  assert(init, "no initialize");
  assert(init.params.appCapabilities.tools !== undefined, "spec appCapabilities");
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
  assert(svg.attrs.viewBox === "0 0 1515 1051", "viewBox 5F");
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

  View._t.switchFloor("6F");
  await sleep(20);
  assert(svg.attrs.viewBox === "0 0 1883 1011", "viewBox 6F");

  const p = View._t.project(10, 20, 0, { S: 2, SQ: 1, SH: 0 });
  assert(p[0] === 20 && p[1] === 40, "projection degenerate: " + p);

  View._t.switchFloor("5F");
  View._t.setMode("3d");
  await sleep(20);
  assert(View._t.get().mode === "3d", "mode 3d");
  assert(findAll(svg, (n) => n.tag === "polygon").length >= 4, "slab sides");
  assert(findAll(svg, (n) => n.tag === "text")
    .some((t) => t.textContent.includes("上到 6F")), "connector label");
  View._t.setTilt(1.0);
  View._t.setTilt(0.45);
  View._t.setMode("2d");
  assert(View._t.get().mode === "2d", "mode 2d");

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
  const line = findAll(svg, (n) => n.tag === "line")[0];
  assert(line, "connector line present");
  assert(line.attrs.x1 === eA[0].toFixed(1) && line.attrs.y1 === eA[1].toFixed(1) &&
         line.attrs.x2 === eB[0].toFixed(1) && line.attrs.y2 === eB[1].toFixed(1),
    "connector endpoints: " + line.attrs.x1 + "," + line.attrs.y1 +
    " -> " + line.attrs.x2 + "," + line.attrs.y2);
  const lbl = findAll(svg, (n) => n.tag === "text").map((t) => t.textContent);
  assert(lbl.some((t) => t.includes("下到 5F")), "down label: " + lbl);
  View._t.setMode("2d");

  // forced render throw -> automatic 2D fallback (flag is one-shot:
  // render3D eats it, the fallback render2D runs clean)
  throwOnceOnSetAttr = true;
  View._t.setMode("3d"); // re-render with mode already 3d
  await sleep(20);
  assert(View._t.get().mode === "2d", "throw must fall back to 2d");

  // single-leg route: 3D refused, no crash
  onMsg({ data: { jsonrpc: "2.0", method: "ui/notifications/tool-result",
    params: { structuredContent: { legs: [{ floor: "5F", polyline: [[1, 2], [3, 4]],
      steps: [], map_file: "5f_base.jpg" }], warnings: [] } } } });
  await sleep(20);
  View._t.setMode("3d");
  assert(View._t.get().mode === "2d", "1-leg must refuse 3d");

  // garbage -> JSON mode; cancelled -> status+bar; empty -> error bar
  onMsg({ data: { jsonrpc: "2.0", method: "ui/notifications/tool-result",
    params: { structuredContent: { foo: 1 } } } });
  await sleep(20);
  onMsg({ data: { jsonrpc: "2.0", method: "ui/notifications/tool-cancelled",
    params: { reason: "user stopped" } } });
  await sleep(20);
  assert(el("status").textContent.includes("cancelled"), "cancel status");
  onMsg({ data: { jsonrpc: "2.0", method: "ui/notifications/tool-result",
    params: { content: [] } } });
  await sleep(20);
  assert(el("errorbar").style.display === "block", "empty must error-bar");
  console.log("VIEWER TEST OK");
})().catch((e) => { console.error("VIEWER TEST FAILED:", e.message); process.exit(1); });
`;

eval("const FIXTURE = " + JSON.stringify(fixture) + ";\n" + src + driver);
