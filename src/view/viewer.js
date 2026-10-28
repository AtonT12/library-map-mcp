"use strict";
/* Library route view: handshake (5-0) + 2D rendering V3-V9 (5-1) +
 * oblique 3D (5-2) + error fallbacks L3 (Step 6).
 *
 * Runs in two shells: the dev shell (dev/view.html, JSON-text mode) and the
 * shipped single-file view (template.py, full mode with #map svg).
 * Every host-side field is optional-read; any failure lands on the error
 * bar with an exit, never a blank iframe.
 *
 * Wire shapes frozen in dev/HOST_CONTRACT.md.
 */

const View = (() => {
  const HANDSHAKE_TIMEOUT_MS = 3000;
  const TILE_DEBOUNCE_MS = 800;
  const TILE_LRU_MAX = 9;
  const TILE_ZOOM_FRACTION = 0.35; // viewBox narrower than this -> fetch tile
  const SVG_NS = "http://www.w3.org/2000/svg";

  let reqId = 0;
  const pending = new Map();
  let hostContext = {};
  let lastSource = "none";

  // Route state ---------------------------------------------------------------
  let route = null;      // parsed {legs, origin, destination, planId, warnings}
  let floor = null;      // current floor tab
  let vbMemo = {};       // floor -> {x,y,w,h} viewBox memory
  // The 3D model is the default view: a flat plan cannot show a route that
  // changes floors, and the extruded model reads as the building itself.
  // A caller can still ask for the flat view (the model passes
  // view_mode="2d" when the user asks for a plain floor plan).
  let mode = "3d";       // "2d" | "3d"
  let requestedMode = null;   // explicit mode from the tool arguments
  let tiltSQ = 0.62;
  let tiles = new Map(); // LRU: key -> dataUri
  let tileTimer = null;
  let lastTileKey = null;

  const $ = (id) => document.getElementById(id);
  const fullMode = () => !!$("map");
  const manifest = () => (window.__LIB_VIEW__ && window.__LIB_VIEW__.maps) || {};

  function setStatus(text) {
    const el = $("status");
    if (el) el.textContent = text;
  }

  function showError(stage, detail, exits) {
    const bar = $("errorbar");
    if (!bar) return;
    bar.style.display = "block";
    bar.textContent = "";
    const span = document.createElement("span");
    span.textContent = `View error [${stage}]: ${detail}. `;
    bar.appendChild(span);
    (exits || []).forEach((x) => {
      const b = document.createElement("button");
      b.type = "button";
      b.textContent = x.label;
      b.addEventListener("click", x.fn);
      bar.appendChild(b);
    });
  }

  function hideError() {
    const bar = $("errorbar");
    if (bar) { bar.style.display = "none"; bar.textContent = ""; }
  }

  function esc(s) {
    return String(s === undefined || s === null ? "" : s);
  }

  // -- JSON-RPC over postMessage ---------------------------------------------
  function post(msg) {
    window.parent.postMessage(msg, "*");
  }

  function call(method, params, timeoutMs = HANDSHAKE_TIMEOUT_MS) {
    return new Promise((resolve, reject) => {
      const id = ++reqId;
      const timer = setTimeout(() => {
        if (pending.delete(id)) {
          reject(new Error(`request '${method}' timed out after ${timeoutMs}ms`));
        }
      }, timeoutMs);
      pending.set(id, {
        resolve: (v) => { clearTimeout(timer); resolve(v); },
        reject: (e) => { clearTimeout(timer); reject(e); },
      });
      post({ jsonrpc: "2.0", id, method, params });
    });
  }

  function note(method, params) {
    post({ jsonrpc: "2.0", method, params });
  }

  async function sendMessage(text) {
    // Exit path L3: backfill the chat. Spec request ui/message with
    // {role, content}; the host MAY require consent (rejection -> status).
    try {
      await call("ui/message", {
        role: "user",
        content: { type: "text", text },
      });
      setStatus("Sent to chat.");
    } catch (e) {
      setStatus(`Chat backfill refused: ${e && e.message ? e.message : e}`);
    }
  }

  // -- Host state --------------------------------------------------------------
  function applyTheme() {
    const theme = hostContext && hostContext.theme;
    const de = document.documentElement;
    de.style.colorScheme = theme === "dark" ? "dark" : "light";
    // Dual-path theming: an explicit host theme always wins; the system
    // preference only applies while no explicit theme was set.
    if (theme === "dark" || theme === "light") de.dataset.theme = theme;
    else delete de.dataset.theme;
  }

  function maxHeight() {
    const cd = hostContext && hostContext.containerDimensions;
    return (cd && cd.maxHeight) || null;
  }

  function applyHeight() {
    // V8: honor the host's maxHeight cap when present; no-op otherwise
    // (and in shells without #app, e.g. dev/view.html).
    const app = $("app");
    if (!app) return;
    const mh = maxHeight();
    if (mh) app.style.maxHeight = `${mh}px`;
    else app.style.removeProperty("max-height");
  }

  function renderContract() {
    const el = $("contract");
    if (el) {
      el.textContent = JSON.stringify(
        { hostContext, lastSource, mode, floor }, null, 2);
    }
  }

  function renderData(obj) {
    const el = $("data");
    if (el) el.textContent = JSON.stringify(obj, null, 2);
    reportSize();
  }

  let lastSentSize = null;   // {w, h} of the last size-changed notification

  function reportSize() {
    // Measure like the SDK does: temporarily size the root element to
    // max-content so the reading reflects CONTENT, not the iframe's own
    // height. With html,body{height:100%} a scrollHeight reading is the
    // viewport height -- a self-confirming loop that pinned the frame at
    // whatever the host already showed.
    const de = document.documentElement;
    if (!de || !de.style || typeof de.getBoundingClientRect !== "function") return;
    const app = $("app");
    const prevDocHeight = de.style.height;
    const prevAppMax = app ? app.style.maxHeight : "";
    try {
      de.style.height = "max-content";
      if (app) app.style.removeProperty("max-height");
      const r = de.getBoundingClientRect();
      const w = Math.round(r.width || de.scrollWidth || window.innerWidth || 0);
      const h = Math.round(r.height || 0);
      // Dedupe on the exact (width, height) pair: the host clamps on its side,
      // so re-sending an unchanged value is pure protocol noise.
      if (lastSentSize && lastSentSize.w === w && lastSentSize.h === h) return;
      lastSentSize = { w, h };
      note("ui/notifications/size-changed", { width: w, height: h });
    } finally {
      de.style.height = prevDocHeight;
      if (app) app.style.maxHeight = prevAppMax;
    }
  }

  function setStageAspect(w, h) {
    // Single funnel for the stage box ratio: 3D passes the projected
    // viewBox, 2D passes the floor map's viewBox, the degraded view 4/3.
    const stage = $("stage");
    if (!stage || !w || !h) return;
    const f = (n) => Math.round(n * 100) / 100;
    stage.style.aspectRatio = `${f(w)} / ${f(h)}`;
  }

  // Refit on WIDTH changes (the content is width-driven, via aspect-ratio);
  // height-only changes (host clamping the frame) must NOT refit, otherwise
  // grow -> report -> clamp -> refit would oscillate.
  let fitRO = null;
  function setupResizeObserver() {
    if (fitRO || typeof ResizeObserver === "undefined") return;
    let lastW = null;
    fitRO = new ResizeObserver(() => {
      const stage = $("stage");
      const w = (stage && stage.clientWidth) || (window.innerWidth || 0);
      if (lastW !== null && w === lastW) {
        // Height-only change: keep the reported size honest, stop there.
        reportSize();
        return;
      }
      lastW = w;
      if (!route) return;
      try {
        if (mode === "3d") render3D();
        else render2D();
      } catch { /* keep the current view */ }
    });
    fitRO.observe(document.documentElement);
    const stage = $("stage");
    if (stage) fitRO.observe(stage);
  }

  // -- V2: tool-result parsing ---------------------------------------------------
  function parseRouteData(params) {
    const p = params || {};
    if (p.structuredContent !== undefined && p.structuredContent !== null) {
      return { kind: "structured", data: p.structuredContent };
    }
    const text = (p.content || [])
      .filter((b) => b && b.type === "text" && typeof b.text === "string")
      .map((b) => b.text)
      .join("\n");
    if (text) {
      try {
        return { kind: "text-json", data: JSON.parse(text), raw: text };
      } catch {
        return { kind: "text-raw", raw: text };
      }
    }
    return { kind: "empty" };
  }

  function asRoute(sc) {
    if (!sc || !Array.isArray(sc.legs) || sc.legs.length === 0) return null;
    // A view_mode in the result is the authoritative presentation hint: the
    // host always delivers the result, whereas tool-input may be absent.
    const vm = sc.view_mode ? String(sc.view_mode).toLowerCase() : null;
    if (vm === "2d" || vm === "3d") {
      requestedMode = vm;
      mode = vm;
    }
    return {
      legs: sc.legs,
      origin: sc.origin || null,
      destination: sc.destination || null,
      planId: sc.plan_id || sc.planId || null,
      warnings: Array.isArray(sc.warnings) ? sc.warnings : [],
    };
  }

  function onToolResult(params) {
    hideError();
    clearTiles();
    const parsed = parseRouteData(params);
    if (parsed.kind === "empty") {
      lastSource = "empty";
      showError("data", "no route data received", [
        { label: "Retry", fn: () => window.location.reload() },
      ]);
      renderData({ error: "no route data received" });
      renderContract();
      return;
    }
    if (parsed.kind === "text-raw") {
      lastSource = "text-raw";
      showError("data", "could not parse structuredContent or text; raw text shown below", [
        { label: "Send to chat", fn: () => sendMessage(parsed.raw) },
      ]);
      renderData({ raw: parsed.raw });
      renderContract();
      return;
    }
    lastSource = parsed.kind === "structured" ? "structuredContent" : "text-fallback";
    const r = asRoute(parsed.data);
    if (!r) {
      // Structured but not a route (e.g. status echo): JSON mode.
      renderData(parsed.data);
      renderContract();
      setStatus("tool-result rendered (JSON).");
      return;
    }
    route = r;
    if (!route.legs.some((l) => l.floor === floor)) {
      floor = route.legs[0].floor;
    }
    try {
      if (fullMode()) renderRoute();
      else renderData(parsed.data);
    } catch (e) {
      showError("draw", e && e.message ? e.message : String(e), [
        { label: "Retry", fn: () => { try { renderRoute(); hideError(); } catch (e2) { /* keep bar */ } } },
        { label: "Fall back to image", fn: () => attemptDegraded("draw failed") },
      ]);
    }
    renderContract();
    setStatus("tool-result rendered.");
  }

  function onToolCancelled(params) {
    const reason = (params && params.reason) || "unknown reason";
    setStatus(`tool cancelled: ${reason}`);
    showError("cancelled", `Tool execution was cancelled (${reason})`, [
      { label: "Retry", fn: () => window.location.reload() },
    ]);
  }

  function onToolInput(params) {
    // The host forwards the tool arguments before the result. A
    // view_mode argument lets the caller pick the presentation: "2d" for a
    // flat floor plan, "3d" (default) for the extruded model.
    const args = (params && params.arguments) || null;
    if (args && typeof args === "object" && args.view_mode) {
      const m = String(args.view_mode).toLowerCase();
      if (m === "2d" || m === "3d") {
        requestedMode = m;
        mode = m;
      }
    }
    setStatus("tool-input received.");
  }

  // -- Map geometry (2D) -----------------------------------------------------------
  function mapInfo(f) {
    return manifest()[f] || null;
  }

  // full-res -> small-map px
  function kmap(f) {
    const m = mapInfo(f);
    if (!m) return null;
    return { kx: m.w / m.fw, ky: m.h / m.fh };
  }

  function legByFloor(f) {
    return (route && route.legs.find((l) => l.floor === f)) || null;
  }

  function svgEl(tag, attrs, parent) {
    const n = document.createElementNS(SVG_NS, tag);
    if (attrs) for (const k of Object.keys(attrs)) n.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(n);
    return n;
  }

  function pathD(pts, k) {
    return pts.map((p, i) =>
      `${i === 0 ? "M" : "L"}${(p[0] * k.kx).toFixed(1)},${(p[1] * k.ky).toFixed(1)}`).join("");
  }

  function stationCircle(g, x, y, fill, title, placeId) {
    const c = svgEl("circle", { cx: x.toFixed(1), cy: y.toFixed(1), r: 15, fill, stroke: "white", "stroke-width": 4 }, g);
    if (title) {
      const t = svgEl("title", null, c);
      t.textContent = title;
    }
    if (placeId) {
      c.setAttribute("data-pid", placeId);
      c.style.cursor = "pointer";
      c.addEventListener("click", (ev) => { ev.stopPropagation(); showDetail(placeId); });
    }
    return c;
  }

  function currentVB(f) {
    const m = mapInfo(f);
    const w = m ? m.w : 800;
    const h = m ? m.h : 600;
    if (!vbMemo[f]) vbMemo[f] = { x: 0, y: 0, w, h };
    return vbMemo[f];
  }

  function render2D() {
    const svg = $("map");
    const leg = legByFloor(floor);
    const m = mapInfo(floor);
    if (!leg || !m) {
      showError("data", `no map/leg for floor ${esc(floor)}`);
      return;
    }
    const k = kmap(floor);
    const vb = currentVB(floor);
    svg.textContent = "";
    svg.setAttribute("viewBox", `${vb.x} ${vb.y} ${vb.w} ${vb.h}`);
    setStageAspect(vb.w, vb.h);
    svgEl("image", { href: m.href, x: 0, y: 0, width: m.w, height: m.h }, svg);
    const g = svgEl("g", { id: "route" }, svg);
    const poly = Array.isArray(leg.polyline) ? leg.polyline : [];
    if (poly.length >= 2) {
      svgEl("path", { d: pathD(poly, k), fill: "none", stroke: "white", "stroke-width": 9, "stroke-linejoin": "round" }, g);
      svgEl("path", { d: pathD(poly, k), fill: "none", stroke: "#1E6FFF", "stroke-width": 5, "stroke-linejoin": "round" }, g);
    }
    // Stations: leg endpoints; transit junctions get their own mark.
    const steps = Array.isArray(leg.steps) ? leg.steps : [];
    const first = poly[0], last = poly[poly.length - 1];
    if (first) {
      const s0 = steps[0] || {};
      stationCircle(g, first[0] * k.kx, first[1] * k.ky, "green",
        s0.label || "start", s0.place_id || null);
    }
    if (last && poly.length > 1) {
      const sN = steps[steps.length - 1] || {};
      stationCircle(g, last[0] * k.kx, last[1] * k.ky, "red",
        sN.label || "end", sN.place_id || null);
      const core = svgEl("circle", { cx: (last[0] * k.kx).toFixed(1), cy: (last[1] * k.ky).toFixed(1), r: 4, fill: "white" }, g);
      void core;
    }
    (steps.filter((s) => s.kind === "transit")).forEach((s) => {
      if (!s.point) return;
      stationCircle(g, s.point[0] * k.kx, s.point[1] * k.ky, "#f90",
        s.label || "transit", null);
    });
    const tiles = svgEl("g", { id: "tiles" }, svg);
    void tiles;
    renderTabs();
    renderSide();
    maybeTile();
    reportSize();
  }

  // -- V4: pan / zoom ------------------------------------------------------------
  function svgPoint(svg, evt) {
    const r = svg.getBoundingClientRect ? svg.getBoundingClientRect() : { left: 0, top: 0, width: 800, height: 600 };
    const vb = currentVB(floor);
    return {
      x: vb.x + ((evt.clientX - r.left) / (r.width || 1)) * vb.w,
      y: vb.y + ((evt.clientY - r.top) / (r.height || 1)) * vb.h,
    };
  }

  function bindPanZoom() {
    const svg = $("map");
    if (!svg || svg.__bound) return;
    svg.__bound = true;
    let drag = null;
    svg.addEventListener("pointerdown", (ev) => {
      drag = { x: ev.clientX, y: ev.clientY, vb: { ...currentVB(floor) } };
      if (svg.setPointerCapture) { try { svg.setPointerCapture(ev.pointerId); } catch { /* noop */ } }
    });
    svg.addEventListener("pointermove", (ev) => {
      if (!drag) return;
      const r = svg.getBoundingClientRect ? svg.getBoundingClientRect() : { width: 800, height: 600 };
      const vb = currentVB(floor);
      vb.x = drag.vb.x - ((ev.clientX - drag.x) / (r.width || 1)) * drag.vb.w;
      vb.y = drag.vb.y - ((ev.clientY - drag.y) / (r.height || 1)) * drag.vb.h;
      applyVB();
    });
    const end = () => { if (drag) { drag = null; maybeTile(); } };
    svg.addEventListener("pointerup", end);
    svg.addEventListener("pointercancel", end);
    svg.addEventListener("wheel", (ev) => {
      if (ev.preventDefault) ev.preventDefault();
      const vb = currentVB(floor);
      const f = ev.deltaY > 0 ? 1.2 : 1 / 1.2;
      const p = svgPoint(svg, ev);
      const nw = Math.min(Math.max(vb.w * f, 40), (mapInfo(floor) || { w: 800 }).w);
      const nh = nw * (vb.h / vb.w);
      vb.x = p.x - ((p.x - vb.x) / vb.w) * nw;
      vb.y = p.y - ((p.y - vb.y) / vb.h) * nh;
      vb.w = nw; vb.h = nh;
      applyVB();
      maybeTile();
    }, { passive: false });
  }

  function applyVB() {
    const svg = $("map");
    if (!svg) return;
    const vb = currentVB(floor);
    svg.setAttribute("viewBox", `${vb.x} ${vb.y} ${vb.w} ${vb.h}`);
  }

  // -- V7: HD tiles --------------------------------------------------------------
  function tileKey(f, cx, cy) {
    return `${f}|${Math.round(cx / 50)}|${Math.round(cy / 50)}|${route ? route.planId : ""}`;
  }

  function maybeTile() {
    if (mode !== "2d" || !route) return;
    const m = mapInfo(floor);
    if (!m) return;
    const vb = currentVB(floor);
    if (vb.w >= m.w * TILE_ZOOM_FRACTION) { hideTiles(); return; }
    if (tileTimer) clearTimeout(tileTimer);
    tileTimer = setTimeout(fetchTile, TILE_DEBOUNCE_MS);
  }

  async function fetchTile() {
    const m = mapInfo(floor);
    if (!m) return;
    const vb = currentVB(floor);
    const cx = vb.x + vb.w / 2, cy = vb.y + vb.h / 2;
    // small-px -> full-res using the manifest scale
    const k = kmap(floor);
    const fc = [cx / k.kx, cy / k.ky];
    const key = tileKey(floor, fc[0], fc[1]);
    if (tiles.has(key)) { showTile(key); return; }
    try {
      // Spec: the bridge reuses MCP verbs; App->Host tool calls are
      // plain tools/call (there is no ui/call-server-tool).
      const res = await call("tools/call", {
        name: "get_map_view",
        arguments: { floor, center: fc, width: 1600, height: 1200, plan_id: route ? route.planId : null },
      }, 15000);
      const img = ((res && res.content) || []).find(
        (b) => b && b.type === "image" && typeof b.data === "string");
      if (!img) return;
      const mime = img.mimeType || "image/webp";
      const uri = `data:${mime};base64,${img.data}`;
      tiles.set(key, { uri, floor, cx: fc[0], cy: fc[1] });
      while (tiles.size > TILE_LRU_MAX) {
        tiles.delete(tiles.keys().next().value);
      }
      showTile(key);
    } catch {
      /* tile miss: stay on the inline base map */
    }
  }

  // Server-side crop origin (must match views.render_view clamp).
  function cropOrigin(full, center, box) {
    const b = Math.min(box, full);
    return Math.min(Math.max(center - b / 2, 0), full - b);
  }

  function showTile(key) {
    const t = tiles.get(key);
    const svg = $("map");
    const g = svg && svg.querySelector ? svg.querySelector("#tiles") : null;
    if (!t || !svg || !g) return;
    lastTileKey = key;
    const m = mapInfo(t.floor);
    const k = kmap(t.floor);
    // tile box in full-res px, same 1600x1200 request as fetchTile
    const ox = cropOrigin(m.fw, t.cx, 1600);
    const oy = cropOrigin(m.fh, t.cy, 1200);
    const tw = Math.min(1600, m.fw), th = Math.min(1200, m.fh);
    g.textContent = "";
    const im = svgEl("image", {
      href: t.uri, x: (ox * k.kx).toFixed(1), y: (oy * k.ky).toFixed(1),
      width: (tw * k.kx).toFixed(1), height: (th * k.ky).toFixed(1),
    }, g);
    im.addEventListener("error", () => {
      // Host CSP may block data: URIs -> retry once as a blob URL.
      try {
        fetch(t.uri).then((r) => r.blob()).then((b) => {
          im.setAttribute("href", URL.createObjectURL(b));
        }).catch(() => { /* keep base map */ });
      } catch { /* keep base map */ }
    });
  }

  function hideTiles() {
    lastTileKey = null;
    const svg = $("map");
    const g = svg && svg.querySelector ? svg.querySelector("#tiles") : null;
    if (g) g.textContent = "";
  }

  function clearTiles() {
    if (tileTimer) { clearTimeout(tileTimer); tileTimer = null; }
    tiles = new Map();
    hideTiles();
  }

  // -- V5/V6/sidebar ---------------------------------------------------------------
  function renderTabs() {
    const bar = $("floors");
    if (!bar || !route) return;
    bar.textContent = "";
    route.legs.forEach((l) => {
      const b = document.createElement("button");
      b.type = "button";
      b.textContent = l.floor;
      b.setAttribute("role", "tab");
      b.setAttribute("aria-pressed", l.floor === floor ? "true" : "false");
      b.addEventListener("click", () => switchFloor(l.floor));
      bar.appendChild(b);
    });
    const b3 = $("mode3d");
    if (b3) {
      // 3D works for single-floor routes too (a lone floor plate with
      // extruded walls); only the cross-floor connector is missing there.
      const cross = route.legs.length >= 2;
      b3.disabled = false;
      b3.textContent = cross && mode === "2d" ? "3D · cross-floor" : "3D";
      b3.setAttribute("aria-pressed", mode === "3d" ? "true" : "false");
    }
  }

  function renderSide() {
    const d = $("detail");
    if (!d || !route) return;
    const o = route.origin, dst = route.destination;
    let html = "";
    if (o && dst) {
      html += `<h4>${esc(o.name)} → ${esc(dst.name)}</h4>`;
    }
    const leg = legByFloor(floor);
    if (leg && Array.isArray(leg.steps) && leg.steps.length) {
      html += "<ol>" + leg.steps.map((s) => `<li>${esc(s.instruction || s.label || "")}</li>`).join("") + "</ol>";
    }
    if (route.warnings.length) {
      html += "<h4>Warnings</h4><ul>" +
        route.warnings.map((w) => `<li>${esc(w)}</li>`).join("") + "</ul>";
    }
    d.innerHTML = html;
  }

  async function showDetail(placeId) {
    const d = $("detail");
    setStatus(`loading ${placeId}…`);
    try {
      const res = await call("tools/call", {
        name: "get_location_detail", arguments: { id: placeId },
      });
      const sc = res && res.structuredContent;
      if (res && res.isError) {
        const t = ((res && res.content) || [])
          .filter((b) => b && b.type === "text" && typeof b.text === "string")
          .map((b) => b.text).join("\n");
        setStatus(`detail failed: ${t || "unknown error"}`);
        return;
      }
      if (d) {
        if (sc && sc.name) {
          d.innerHTML = `<h4>${esc(sc.name)}</h4>` +
            `<div>Floor ${esc(sc.floor || "")} · at (${esc((sc.point || []).join(", "))})</div>` +
            (sc.call_number ? `<div>Call number ${esc(sc.call_number)}</div>` : "") +
            ((sc.other_floors || []).length ? `<div>Also on ${esc(sc.other_floors.join(", "))}</div>` : "");
        } else {
          const t = ((res && res.content) || []).filter((b) => b && b.type === "text").map((b) => b.text).join("\n");
          d.innerHTML = `<h4>${esc(placeId)}</h4><pre>${esc(t)}</pre>`;
        }
      }
      setStatus("detail loaded.");
    } catch (e) {
      setStatus(`detail failed: ${e && e.message ? e.message : e}`);
    }
  }

  function switchFloor(f) {
    if (!route || !route.legs.some((l) => l.floor === f)) return;
    floor = f;
    hideTiles();
    try {
      if (mode === "3d") render3D();
      else render2D();
    } catch (e) {
      showError("draw", e && e.message ? e.message : String(e));
    }
    renderContract();
    reportSize();
  }

  function renderRoute() {
    hideError();
    if (!route) return;
    bindPanZoom();
    if (mode === "3d") {
      try {
        render3D();
        reportSize();
        return;
      } catch (e) {
        mode = "2d";
        setStatus("3D rendering failed; fell back to the flat 2D view.");
      }
    }
    render2D();
    reportSize();
  }

  // -- 3D oblique view (5-2) ---------------------------------------------------------
  // project(x, y, z) = [S*(x + SH*y), S*SQ*y - z], z in screen px.
  function projParams() {
    const SQ = tiltSQ;
    const SH = (1 - SQ) * 0.18;
    const svg = $("map");
    const vw = (svg && svg.clientWidth) || 800;
    const vh = (svg && svg.clientHeight) || 600;

    // Collect the plan outline of every leg (not the full base maps: the
    // maps carry a legend / title / compass band below the plan, 55% of
    // 6F, and fitting the full image stretched the slab and pushed the
    // route off-centre).
    const rectsOf = (floorName) => {
      const m = mapInfo(floorName);
      if (!m) return [];
      return outlineOf(floorName) || [[0, 0, m.fw, m.fh]];
    };
    const legs = route.legs || [];
    const floors = [];
    legs.forEach((l) => {
      if (floors.indexOf(l.floor) < 0) floors.push(l.floor);
    });

    // Two-pass fit: the floor separation depends on the scale and the scale
    // depends on the separation. Guess, then refine against the real
    // projected bounding box so the model fills the viewport.
    let S = 1, z6 = 0, UH = 1;
    for (let pass = 0; pass < 3; pass++) {
      const pts = [];
      floors.forEach((f) => {
        const z = f === "6F" ? z6 : 0;
        rectsOf(f).forEach((r) => {
          [[r[0], r[1]], [r[2], r[1]], [r[2], r[3]], [r[0], r[3]]].forEach(([x, y]) => {
            pts.push([S * (x + SH * y), S * SQ * y - z]);
          });
        });
      });
      let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
      pts.forEach((p) => {
        if (p[0] < minX) minX = p[0];
        if (p[0] > maxX) maxX = p[0];
        if (p[1] < minY) minY = p[1];
        if (p[1] > maxY) maxY = p[1];
      });
      const bw = Math.max(1, maxX - minX);
      const bh = Math.max(1, maxY - minY);
      // Scale so the projected model fits both axes with a small margin.
      const next = Math.min((vw * 0.96) / bw, (vh * 0.96) / bh);
      if (Math.abs(next - S) < 1e-6) { S = next; break; }
      S = next;
      // Floor separation: one storey above the projected height of the
      // tallest slab, so the upper floor is never hidden behind the lower
      // one.
      UH = Math.max.apply(null, floors.map((f) => {
        const rs = rectsOf(f);
        return rs.length ? Math.max.apply(null, rs.map((r) => r[3])) : 1;
      }));
      z6 = 0.34 * UH * S;
    }
    return {
      S, SQ, SH, UW: 1, UH,
      z6,                        // 6F floor height above 5F
      thick: 0.010 * UH * S,    // slab thickness
      lift: 0.018 * UH * S,     // route lift above the slab
      wallH: 0.030 * UH * S,    // extruded wall height
      vw, vh,
    };
  }

  // Plan outline of a floor: list of [x0,y0,x1,y1] in full-res px.
  function outlineOf(floorName) {
    const m = mapInfo(floorName);
    if (!m || !Array.isArray(m.outline) || !m.outline.length) return null;
    return m.outline;
  }

  // Wall segments of a floor in full-res px, from the manifest. Same
  // geometry the router treats as impassable.
  function wallsOf(floorName) {
    const all = (window.__LIB_VIEW__ && window.__LIB_VIEW__.walls) || null;
    if (!all) return [];
    const w = all[floorName];
    return Array.isArray(w) ? w : [];
  }

  function project(x, y, z, P) {
    return [P.S * (x + P.SH * y), P.S * P.SQ * y - z];
  }

  // Screen-space bounding box of the projected model, with a margin.
  function projBounds(P, legs) {
    let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
    const grow = (x, y) => {
      if (x < minX) minX = x;
      if (x > maxX) maxX = x;
      if (y < minY) minY = y;
      if (y > maxY) maxY = y;
    };
    legs.forEach((l) => {
      const z = l.floor === "6F" ? P.z6 : 0;
      const rects = outlineOf(l.floor) || [];
      rects.forEach((r) => {
        [[r[0], r[1]], [r[2], r[1]], [r[2], r[3]], [r[0], r[3]]].forEach(([x, y]) => {
          const p = project(x, y, z, P);
          grow(p[0], p[1]);
          // The slab has thickness below it and the walls rise above it.
          grow(p[0], p[1] - (P.wallH || 0));
          grow(p[0], p[1] + (P.thick || 0));
        });
      });
    });
    if (!isFinite(minX)) return { x: 0, y: 0, w: 100, h: 100 };
    const pad = Math.max(12, (maxX - minX) * 0.03);
    return {
      x: minX - pad, y: minY - pad,
      w: (maxX - minX) + pad * 2, h: (maxY - minY) + pad * 2,
    };
  }

  function poly3(points, z, P) {
    return points.map((p) => project(p[0], p[1], z, P)
      .map((v) => v.toFixed(1)).join(",")).join(" ");
  }

  function ptsStr(pts) {
    return pts.map((p) => `${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(" ");
  }

  // The slab is the plan outline: one or more rectangles, each drawn as a
  // top face plus extruded side faces, so the model reads as a floor plate
  // rather than a rectangle of picture. 5F is L-shaped, hence the list.
  function slabPolys(rects, z, P) {
    const tops = [];
    const sides = [];
    const t = P.thick;
    rects.forEach((r) => {
      const c = [[r[0], r[1]], [r[2], r[1]], [r[2], r[3]], [r[0], r[3]]]
        .map(([x, y]) => project(x, y, z, P));
      tops.push(c);
      // Only the south and east edges get a visible side face (the ones
      // facing the camera); drawing all four doubles the polygon count and
      // hides the floor behind its own back walls.
      sides.push([c[1], c[2], [c[2][0], c[2][1] + t], [c[1][0], c[1][1] + t]]);
      sides.push([c[2], c[3], [c[3][0], c[3][1] + t], [c[2][0], c[2][1] + t]]);
    });
    const shadow = tops.map((c) => c.map(([x, y]) => [x + 10, y + 12]));
    return { tops, sides, shadow };
  }

  function render3D() {
    const svg = $("map");
    if (!svg) return;
    const legs = route.legs;
    const P = projParams();
    const cur = legByFloor(floor) || legs[0];
    const others = legs.filter((l) => l !== cur);
    const zOf = (l) => (l.floor === "6F" ? P.z6 : 0);

    // Screen-space viewBox covering union + lift.
    // Screen-space viewBox covering the projected model plus the lift.
    const vb = projBounds(P, legs);
    svg.textContent = "";
    svg.setAttribute("viewBox", `${vb.x.toFixed(1)} ${vb.y.toFixed(1)} ${vb.w.toFixed(1)} ${vb.h.toFixed(1)}`);
    setStageAspect(vb.w, vb.h);
    // v1 polish: slab gradient + soft shadow filter (single defs, rebuilt
    // with every render since the svg is cleared above).
    const defs = svgEl("defs", null, svg);
    const grad = svgEl("linearGradient", { id: "slab3d", x1: 0, y1: 0, x2: 0, y2: 1 }, defs);
    svgEl("stop", { offset: 0, "stop-color": "#f4f6f8" }, grad);
    svgEl("stop", { offset: 1, "stop-color": "#d7dde3" }, grad);
    const filt = svgEl("filter", { id: "soft3d", x: "-40%", y: "-40%", width: "180%", height: "180%" }, defs);
    svgEl("feGaussianBlur", { stdDeviation: 8 }, filt);
    const wgrad = svgEl("linearGradient", { id: "wall3d", x1: 0, y1: 0, x2: 0, y2: 1 }, defs);
    svgEl("stop", { offset: 0, "stop-color": "#8d7b9e", "stop-opacity": 0.85 }, wgrad);
    svgEl("stop", { offset: 1, "stop-color": "#5d4f6b", "stop-opacity": 0.6 }, wgrad);

    const drawFloor = (leg, current) => {
      const m = mapInfo(leg.floor);
      if (!m) return;
      const z = zOf(leg);
      const g = svgEl("g", { opacity: current ? 1 : 0.72 }, svg);
      const rects = outlineOf(leg.floor) || [[0, 0, m.fw, m.fh]];
      const slab = slabPolys(rects, z, P);

      // Contact shadow under the plate.
      slab.shadow.forEach((sh) => {
        svgEl("polygon", { points: ptsStr(sh), fill: "#000", opacity: 0.15, filter: "url(#soft3d)" }, g);
      });
      slab.sides.forEach((s) => {
        svgEl("polygon", { points: ptsStr(s), fill: "#8f99a3", opacity: 0.9 }, g);
      });
      slab.tops.forEach((t) => {
        svgEl("polygon", { points: ptsStr(t), fill: "url(#slab3d)", stroke: "#ffffff", "stroke-width": 2 }, g);
      });

      // Base map texture, clipped to the plan outline so the legend band
      // never shows up on the plate. The clip path lives in the IMAGE's own
      // coordinate system (full-res px), because the group that carries it
      // is already transformed into screen space -- clipping in screen
      // space would apply the transform twice and drop the texture.
      const clipId = `clip-${leg.floor}`;
      const clip = svgEl("clipPath", { id: clipId }, defs);
      rects.forEach((r) => {
        svgEl("rect", {
          x: r[0], y: r[1], width: r[2] - r[0], height: r[3] - r[1],
        }, clip);
      });
      const rg = svgEl("g", {
        transform: `matrix(${P.S} 0 ${(P.S * P.SH).toFixed(4)} ${(P.S * P.SQ).toFixed(4)} 0 ${(-z).toFixed(1)})`,
        "clip-path": `url(#${clipId})`,
      }, g);
      svgEl("image", { href: m.href, x: 0, y: 0, width: m.fw, height: m.fh, opacity: 0.93 }, rg);

      // Extruded walls: each segment becomes a vertical face rising from
      // the slab. Faces are drawn far-to-near along y so the nearer wall
      // hides the one behind it, which is what gives the model its depth.
      // They stay translucent on purpose: the floor plan underneath is the
      // information, the extrusion only says "this is a wall".
      const walls = wallsOf(leg.floor);
      const wh = P.wallH || 0;
      const sorted = walls.slice().sort((a, b) => (a[0][1] + a[1][1]) - (b[0][1] + b[1][1]));
      const wg = svgEl("g", null, g);
      sorted.forEach((seg) => {
        const a = project(seg[0][0], seg[0][1], z, P);
        const b = project(seg[1][0], seg[1][1], z, P);
        if (a[0] === b[0] && a[1] === b[1]) return;
        // The face rises upward on screen, i.e. subtract from y.
        svgEl("polygon", {
          points: ptsStr([a, b, [b[0], b[1] - wh], [a[0], a[1] - wh]]),
          fill: "url(#wall3d)", stroke: "#4a3f56", "stroke-width": 1,
          "stroke-opacity": 0.45,
        }, wg);
        // Top cap: a thin bright line that reads as the wall's top edge.
        svgEl("line", {
          x1: a[0].toFixed(1), y1: (a[1] - wh).toFixed(1),
          x2: b[0].toFixed(1), y2: (b[1] - wh).toFixed(1),
          stroke: "#f2e9f8", "stroke-width": 2, "stroke-linecap": "round",
          opacity: 0.85,
        }, wg);
      });

      // Route: floats above the wall tops so it is never occluded.
      const poly = Array.isArray(leg.polyline) ? leg.polyline : [];
      const lift = (P.lift || 0) + (P.wallH || 0) * 1.15;
      if (poly.length >= 2) {
        const flat = poly.map((p) => project(p[0], p[1], z, P));
        const sh2 = flat.map(([x, y]) => [x + 9, y + 11]);
        svgEl("polyline", { points: ptsStr(sh2), fill: "none", stroke: "#000", opacity: 0.3, "stroke-width": 10, "stroke-linejoin": "round", filter: "url(#soft3d)" }, g);
        const up = poly.map((p) => project(p[0], p[1], z + lift, P));
        svgEl("polyline", { points: ptsStr(up), fill: "none", stroke: "white", "stroke-width": 10, "stroke-linejoin": "round" }, g);
        svgEl("polyline", { points: ptsStr(up), fill: "none", stroke: "#1E6FFF", "stroke-width": 5.5, "stroke-linejoin": "round" }, g);
      }
      const steps = Array.isArray(leg.steps) ? leg.steps : [];
      const lift2 = lift;
      const dot = (p, fill, title) => {
        if (!p) return;
        const [sx, sy] = project(p[0], p[1], z + lift2, P);
        const c = svgEl("circle", { cx: sx.toFixed(1), cy: sy.toFixed(1), r: 11, fill, stroke: "white", "stroke-width": 3.5 }, g);
        if (title) {
          const t = svgEl("title", null, c);
          t.textContent = title;
        }
      };
      if (poly[0]) dot(poly[0], "#22a355", (steps[0] || {}).label || "start");
      if (poly[poly.length - 1]) dot(poly[poly.length - 1], "#d33", (steps[steps.length - 1] || {}).label || "end");
      // Floor plate (v1): dark chip with the floor name. The chip sits above
      // the middle of the slab so it never falls outside the view box
      // (the projected corners do, for the far floor).
      const mid = slab.tops.map((c) => [
        (c[0][0] + c[1][0] + c[2][0] + c[3][0]) / 4,
        (c[0][1] + c[1][1] + c[2][1] + c[3][1]) / 4,
      ])[0];
      const chipX = mid[0] - 32;
      const chipY = mid[1] - 62;
      svgEl("rect", { x: chipX.toFixed(1), y: chipY.toFixed(1), width: 64, height: 36, rx: 7, fill: "#111", opacity: 0.85 }, g);
      const plate = svgEl("text", { x: (chipX + 32).toFixed(1), y: (chipY + 27).toFixed(1), "font-size": 24, "font-weight": "bold", "text-anchor": "middle", fill: "#fff" }, g);
      plate.textContent = leg.floor;
    };

    // Far-to-near: non-current floors first (6F is visually above: paint it
    // after 5F when 5F is current, and vice versa).
    const ordered = floor === "6F" ? [...others, cur] : [...others.reverse(), cur];
    ordered.forEach((l) => drawFloor(l, l === cur));

    // Vertical connector between the two transit points. Transit anchors
    // come from the legs' transit steps (direction-aware); the polyline-
    // end heuristic below is only a fallback for step-less data.
    if (legs.length >= 2) {
      const t = (route.legs[0].transit) || (route.legs[1].transit) || null;
      const down = !!(t && t.direction === "down");
      const tp = (leg, useEnd) => {
        const st = ((leg && leg.steps) || []).find(
          (s) => s && s.kind === "transit" && Array.isArray(s.point));
        if (st) return st.point;
        const p = (leg && leg.polyline) || [];
        return useEnd ? p[p.length - 1] : p[0];
      };
      const pA = tp(legs[0], !down);
      const pB = tp(legs[1], down);
      if (pA && pB) {
        const qA = project(pA[0], pA[1], zOf(legs[0]), P);
        const qB = project(pB[0], pB[1], zOf(legs[1]), P);
        const cg = svgEl("g", null, svg);
        svgEl("line", {
          x1: qA[0].toFixed(1), y1: qA[1].toFixed(1),
          x2: qB[0].toFixed(1), y2: qB[1].toFixed(1),
          stroke: "#c00", "stroke-width": 7, "stroke-linecap": "round",
        }, cg);
        [qA, qB].forEach((q) => {
          svgEl("circle", { cx: q[0].toFixed(1), cy: q[1].toFixed(1), r: 9, fill: "#c00", stroke: "white", "stroke-width": 3 }, cg);
        });
        const label = t ? (t.direction === "down"
          ? `↓ Down to ${t.to_floor}` : `↑ Up to ${t.to_floor}`) : "Change floors";
        // Rounded pill: white ground, red text. The background width is a
        // pure per-character estimate -- getBBox()/getComputedTextLength()
        // are off limits because the headless test DOM does not implement
        // them and a throw here would break the whole 3D pipeline.
        const FS = 26, PADX = 12;
        const estCh = (ch) => (
          ch === " " ? 0.32 :
          /[0-9]/.test(ch) ? 0.58 :
          ch === "↑" || ch === "↓" ? 0.55 :
          /^[a-z]$/.test(ch) ? 0.52 : 0.66);
        let tw = 0;
        for (const ch of label) tw += estCh(ch) * FS;
        const pw = tw + PADX * 2, ph = FS * 1.5;
        const mx = (qA[0] + qB[0]) / 2 + 14, my = (qA[1] + qB[1]) / 2;
        svgEl("rect", {
          x: (mx - pw / 2).toFixed(1), y: (my - ph / 2).toFixed(1),
          width: pw.toFixed(1), height: ph.toFixed(1), rx: (ph / 2).toFixed(1),
          fill: "#fff", stroke: "#d33", "stroke-width": 1.5,
        }, cg);
        const tx = svgEl("text", {
          x: mx.toFixed(1), y: (my + FS * 0.35).toFixed(1),
          "font-size": FS, fill: "#c00", "font-weight": "bold",
          "text-anchor": "middle",
        }, cg);
        tx.textContent = label;
      }
    }
    renderTabs();
    renderSide();
    reportSize();
  }

  function setMode(next) {
    mode = next;
    hideTiles();
    try {
      renderRoute();
    } catch (e) {
      showError("draw", e && e.message ? e.message : String(e));
    }
    renderContract();
    reportSize();
  }

  // -- L3 degraded view ------------------------------------------------------------
  async function attemptDegraded(reason) {
    // Image + steps without the SVG pipeline. Works even when render2D/3D
    // throws: plain <img> plus the human steps.
    const stage = $("stage");
    try {
      let leg = route && route.legs[0];
      const f = (leg && leg.floor) || "5F";
      const poly = (leg && leg.polyline) || null;
      const steps = (leg && leg.steps) || [];
      let uri = null;
      try {
        const res = await call("tools/call", {
          name: "get_map_view",
          arguments: { floor: f, route: poly, width: 1600, height: 1200,
                       plan_id: route ? route.planId : null },
        }, 15000);
        const img = ((res && res.content) || []).find(
          (b) => b && b.type === "image" && typeof b.data === "string");
        if (img) uri = `data:${img.mimeType || "image/webp"};base64,${img.data}`;
      } catch {
        uri = null;
      }
      if (stage) {
        stage.textContent = "";
        // The degraded image is a fixed 1600x1200 crop from get_map_view.
        setStageAspect(4, 3);
        if (uri) {
          const im = document.createElement("img");
          im.src = uri;
          im.alt = `degraded route map (${reason})`;
          im.style.width = "100%";
          stage.appendChild(im);
        }
        const d = $("detail");
        if (d) {
          d.innerHTML = `<h4>Steps (degraded view: ${esc(reason)})</h4><ol>` +
            steps.map((s) => `<li>${esc(s.instruction || s.label || "")}</li>`).join("") +
            "</ol>";
        }
        reportSize();
      }
      showError("degraded", reason, [
        { label: "Send to chat", fn: () => sendMessage(`Route view degraded: ${reason}`) },
        { label: "Retry full view", fn: () => { try { renderRoute(); hideError(); } catch { /* keep */ } } },
      ]);
    } catch (e) {
      showError("degraded", e && e.message ? e.message : String(e));
    }
  }

  // -- Inbound dispatch --------------------------------------------------------
  function onMessage(ev) {
    const m = ev.data;
    if (!m || m.jsonrpc !== "2.0") return;
    if (m.id !== undefined &&
        (m.result !== undefined || m.error !== undefined)) {
      const p = pending.get(m.id);
      if (!p) return;
      pending.delete(m.id);
      if (m.error) p.reject(new Error((m.error && m.error.message) || "rpc error"));
      else p.resolve(m.result);
      return;
    }
    if (m.method === "ui/notifications/tool-result") onToolResult(m.params);
    else if (m.method === "ui/notifications/tool-cancelled") onToolCancelled(m.params);
    else if (m.method === "ui/notifications/tool-input") onToolInput(m.params);
    else if (m.method === "ui/notifications/host-context-changed") {
      // Spec: params IS Partial<HostContext> (no wrapper) -> merge.
      const patch = (m.params && typeof m.params === "object") ? m.params : {};
      hostContext = { ...hostContext, ...patch };
      applyTheme();
      applyHeight();
      renderContract();
    }
  }

  // -- Boot ------------------------------------------------------------------------
  async function boot() {
    setStatus("handshake…");
    try {
      // protocolVersion cites the spec's McpUiInitializeResult example
      // (extension Stable 2026-01-26); appCapabilities fields are
      // spec-literal (experimental / tools / availableDisplayModes).
      const res = await call("ui/initialize", {
        protocolVersion: "2026-01-26",
        appCapabilities: { tools: {}, availableDisplayModes: ["inline"] },
      });
      hostContext = (res && res.hostContext) || {};
    } catch (e) {
      hostContext = {};
      showError("handshake", `Could not reach the host; please ask again (${e && e.message ? e.message : e})`, [
        { label: "Retry", fn: () => boot() },
      ]);
    }
    applyTheme();
    applyHeight();
    try {
      note("ui/notifications/initialized", {});
    } catch (e) {
      showError("handshake", e && e.message ? e.message : String(e));
    }
    renderContract();
    renderData({ hint: "waiting for ui/notifications/tool-result…" });
    setStatus("waiting for tool-result…");
    setupResizeObserver();
    reportSize();
  }

  async function ping() {
    setStatus("ping…");
    try {
      const res = await call("tools/call", {
        name: "get_library_status",
        arguments: {},
      });
      lastSource = "tools/call";
      if (fullMode() && route) {
        setStatus("pong received.");
      } else {
        renderData(res && res.structuredContent !== undefined
          ? res.structuredContent
          : res);
        setStatus("pong received.");
      }
    } catch (e) {
      showError("tools/call", e && e.message ? e.message : String(e));
      setStatus("ping failed (see error bar).");
    }
    renderContract();
  }

  return {
    boot, ping, onMessage,
    // Test hooks (no production use).
    _t: {
      get: () => ({ route, floor, mode, vbMemo, tiles: [...tiles.keys()] }),
      project, projParams, parseRouteData, asRoute, switchFloor, setMode,
      attemptDegraded, cropOrigin, pathD, reportSize,
      setTilt: (v) => {
        if (!(v >= 0.45 && v <= 1.0)) return;
        tiltSQ = v;
        if (mode === "3d" && route && fullMode()) {
          try {
            render3D();
          } catch {
            mode = "2d";
            setStatus("3D rendering failed; fell back to the flat 2D view.");
            render2D();
          }
        }
      },
    },
  };
})();

window.addEventListener("message", View.onMessage);
document.addEventListener("DOMContentLoaded", () => View.boot());
document.addEventListener("click", (ev) => {
  if (!ev.target) return;
  if (ev.target.id === "ping") View.ping();
  if (ev.target.id === "retry") View.boot();
  if (ev.target.id === "mode3d") View._t.setMode(mode3dNext());
  function mode3dNext() {
    const b = document.getElementById("mode3d");
    return (b && b.getAttribute("aria-pressed") === "true") ? "2d" : "3d";
  }
});
document.addEventListener("input", (ev) => {
  if (ev.target && ev.target.id === "tilt") {
    // tiltSQ lives inside the closure; route through the hook.
    View._t.setTilt(parseFloat(ev.target.value));
  }
});
