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
  let mode = "2d";       // "2d" | "3d"
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
    document.documentElement.style.colorScheme =
      theme === "dark" ? "dark" : "light";
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

  function reportSize() {
    const de = document.documentElement || {};
    const w = de.scrollWidth || (window.innerWidth || 0);
    const h = de.scrollHeight || 0;
    // Spec shape: {width, height}.
    note("ui/notifications/size-changed", { width: w, height: h });
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
      showError("data", "未收到路线数据", [
        { label: "重试", fn: () => window.location.reload() },
      ]);
      renderData({ error: "no route data received" });
      renderContract();
      return;
    }
    if (parsed.kind === "text-raw") {
      lastSource = "text-raw";
      showError("data", "structuredContent 与 text 都解析失败，已显示原始 text", [
        { label: "反馈到对话", fn: () => sendMessage(parsed.raw) },
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
        { label: "重试", fn: () => { try { renderRoute(); hideError(); } catch (e2) { /* keep bar */ } } },
        { label: "降级为图片", fn: () => attemptDegraded("draw failed") },
      ]);
    }
    renderContract();
    setStatus("tool-result rendered.");
  }

  function onToolCancelled(params) {
    const reason = (params && params.reason) || "unknown reason";
    setStatus(`tool cancelled: ${reason}`);
    showError("cancelled", `Tool execution was cancelled (${reason})`, [
      { label: "重试", fn: () => window.location.reload() },
    ]);
  }

  function onToolInput(params) {
    setStatus("tool-input received (arguments logged to console).");
    // eslint-disable-next-line no-console
    console.log("[view] tool-input", params);
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
      const cross = route.legs.length >= 2;
      b3.disabled = !cross;
      b3.textContent = cross && mode === "2d" ? "3D · 跨层" : "3D";
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
      html += "<h4>注意</h4><ul>" +
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
            `<div>楼层 ${esc(sc.floor || "")} · 坐标 (${esc((sc.point || []).join(", "))})</div>` +
            (sc.call_number ? `<div>索书号 ${esc(sc.call_number)}</div>` : "") +
            ((sc.other_floors || []).length ? `<div>亦在 ${esc(sc.other_floors.join("、"))}</div>` : "");
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
  }

  function renderRoute() {
    hideError();
    if (!route) return;
    bindPanZoom();
    if (mode === "3d") {
      if (route.legs.length < 2) {
        mode = "2d";
        setStatus("single-floor route: 3D unavailable, showing 2D.");
      } else {
        try {
          render3D();
          return;
        } catch (e) {
          mode = "2d";
          setStatus("3D 渲染异常，已回落 2D 平铺。");
        }
      }
    }
    render2D();
  }

  // -- 3D oblique view (5-2) ---------------------------------------------------------
  // project(x, y, z) = [S*(x + SH*y), S*SQ*y - z], z in screen px.
  function projParams() {
    const SQ = tiltSQ;
    const SH = (1 - SQ) * 0.18;
    let UW = 0, UH = 0;
    route.legs.forEach((l) => {
      const m = mapInfo(l.floor);
      if (m) { UW = Math.max(UW, m.fw); UH = Math.max(UH, m.fh); }
    });
    const svg = $("map");
    const vw = (svg && svg.clientWidth) || 800;
    const vh = (svg && svg.clientHeight) || 600;
    // Fit the union box at SQ tilt plus the 6F lift.
    const S = Math.min(vw / (UW * (1 + SH) + 1), (vh * 0.92) / (UH * SQ + 0.06 * UH + 1));
    return { S, SQ, SH, UW, UH, z6: 0.06 * UH * S, thick: 0.012 * UH * S, vw, vh };
  }

  function project(x, y, z, P) {
    return [P.S * (x + P.SH * y), P.S * P.SQ * y - z];
  }

  function poly3(points, z, P) {
    return points.map((p) => project(p[0], p[1], z, P)
      .map((v) => v.toFixed(1)).join(",")).join(" ");
  }

  function slabPolys(fw, fh, z, P) {
    const c = [[0, 0], [fw, 0], [fw, fh], [0, fh]].map(([x, y]) => project(x, y, z, P));
    const t = P.thick;
    const sides = [
      [c[0], c[1], [c[1][0], c[1][1] + t], [c[0][0], c[0][1] + t]],
      [c[1], c[2], [c[2][0], c[2][1] + t], [c[1][0], c[1][1] + t]],
      [c[2], c[3], [c[3][0], c[3][1] + t], [c[2][0], c[2][1] + t]],
      [c[3], c[0], [c[0][0], c[0][1] + t], [c[3][0], c[3][1] + t]],
    ];
    const shadow = c.map(([x, y]) => [x + 10, y + 12]);
    return { top: c, sides, shadow };
  }

  function ptsStr(pts) {
    return pts.map((p) => `${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(" ");
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
    const x0 = -20, y0 = -(P.z6 + P.thick + 40);
    const x1 = P.S * (P.UW * (1 + P.SH)) + 20;
    const y1 = P.S * P.SQ * P.UH + 20;
    svg.textContent = "";
    svg.setAttribute("viewBox", `${x0.toFixed(1)} ${y0.toFixed(1)} ${(x1 - x0).toFixed(1)} ${(y1 - y0).toFixed(1)}`);

    const drawFloor = (leg, current) => {
      const m = mapInfo(leg.floor);
      if (!m) return;
      const z = zOf(leg);
      const g = svgEl("g", { opacity: current ? 1 : 0.45 }, svg);
      const slab = slabPolys(m.fw, m.fh, z, P);
      svgEl("polygon", { points: ptsStr(slab.shadow), fill: "#000", opacity: 0.15 }, g);
      slab.sides.forEach((s) => {
        svgEl("polygon", { points: ptsStr(s), fill: "#9aa3ad", opacity: 0.85 }, g);
      });
      const rg = svgEl("g", {
        transform: `matrix(${P.S} 0 ${(P.S * P.SH).toFixed(4)} ${(P.S * P.SQ).toFixed(4)} 0 ${(-z).toFixed(1)})`,
      }, g);
      svgEl("image", { href: m.href, x: 0, y: 0, width: m.fw, height: m.fh }, rg);
      const poly = Array.isArray(leg.polyline) ? leg.polyline : [];
      if (poly.length >= 2) {
        svgEl("polyline", { points: poly3(poly, z, P), fill: "none", stroke: "white", "stroke-width": 7, "stroke-linejoin": "round" }, g);
        svgEl("polyline", { points: poly3(poly, z, P), fill: "none", stroke: "#1E6FFF", "stroke-width": 4, "stroke-linejoin": "round" }, g);
      }
      const steps = Array.isArray(leg.steps) ? leg.steps : [];
      const dot = (p, fill, title) => {
        if (!p) return;
        const [sx, sy] = project(p[0], p[1], z, P);
        const c = svgEl("circle", { cx: sx.toFixed(1), cy: sy.toFixed(1), r: 9, fill, stroke: "white", "stroke-width": 3 }, g);
        if (title) {
          const t = svgEl("title", null, c);
          t.textContent = title;
        }
      };
      if (poly[0]) dot(poly[0], "green", (steps[0] || {}).label || "start");
      if (poly[poly.length - 1]) dot(poly[poly.length - 1], "red", (steps[steps.length - 1] || {}).label || "end");
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
          stroke: "#c00", "stroke-width": 5,
        }, cg);
        const label = t ? (t.direction === "down"
          ? `↓ 下到 ${t.to_floor}` : `↑ 上到 ${t.to_floor}`) : "换层";
        const tx = svgEl("text", {
          x: ((qA[0] + qB[0]) / 2 + 8).toFixed(1),
          y: ((qA[1] + qB[1]) / 2).toFixed(1),
          "font-size": 22, fill: "#c00",
        }, cg);
        tx.textContent = label;
      }
    }
    renderTabs();
    renderSide();
  }

  function setMode(next) {
    if (next === "3d" && (!route || route.legs.length < 2)) {
      setStatus("single-floor route: 3D unavailable.");
      return;
    }
    mode = next;
    hideTiles();
    try {
      renderRoute();
    } catch (e) {
      showError("draw", e && e.message ? e.message : String(e));
    }
    renderContract();
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
        if (uri) {
          const im = document.createElement("img");
          im.src = uri;
          im.alt = `degraded route map (${reason})`;
          im.style.width = "100%";
          stage.appendChild(im);
        }
        const d = $("detail");
        if (d) {
          d.innerHTML = `<h4>步骤（降级视图：${esc(reason)}）</h4><ol>` +
            steps.map((s) => `<li>${esc(s.instruction || s.label || "")}</li>`).join("") +
            "</ol>";
        }
      }
      showError("degraded", reason, [
        { label: "反馈到对话", fn: () => sendMessage(`路线视图降级：${reason}`) },
        { label: "重试完整视图", fn: () => { try { renderRoute(); hideError(); } catch { /* keep */ } } },
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
      showError("handshake", `未能连接到 host，请重新发起提问 (${e && e.message ? e.message : e})`, [
        { label: "重试", fn: () => boot() },
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
      attemptDegraded, cropOrigin, pathD,
      setTilt: (v) => {
        if (!(v >= 0.45 && v <= 1.0)) return;
        tiltSQ = v;
        if (mode === "3d" && route && fullMode()) {
          try {
            render3D();
          } catch {
            mode = "2d";
            setStatus("3D 渲染异常，已回落 2D 平铺。");
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
