"""Real-Chromium checks for the MCP App view.

Run after build_static.py + build_preview.py:

    PYTHONPATH=src python dev/browser_test.py

The harness intentionally uses a real browser: the stub DOM cannot verify
layout, CSS theme inheritance, visible SVG geometry, wheel behavior, or the
actual tools/call payload.
"""

import base64
import functools
import http.server
import io
import json
import os
import sys
import threading
from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright

import library

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / "review_out" / "browser"
ARTIFACTS.mkdir(parents=True, exist_ok=True)


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def _start_server():
    handler = functools.partial(QuietHandler, directory=str(ROOT / "dev"))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/preview.html"


def _tiny_webp():
    buf = io.BytesIO()
    Image.new("RGB", (1, 1), (255, 255, 255)).save(buf, format="WEBP")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _host_html(preview_url, route, tiny_webp):
    host = r"""<!doctype html>
<html><head><meta charset="utf-8"><style>
html,body{margin:0;padding:0;background:#fff;font-family:sans-serif}
#view{display:block;width:402px;height:720px;border:0}
</style></head><body>
<iframe id="view" src="__PREVIEW__"></iframe>
<script>
const ROUTE = __ROUTE__;
const TINY_WEBP = "__TINY_WEBP__";
const frame = document.getElementById("view");
window.__inits = 0;
window.__initErrors = [];
window.__calls = [];
window.__ready = false;
window.__dropInit = false;
window.__hostTheme = "light";
window.__route = ROUTE;
window.getCalls = () => window.__calls.slice();
window.setHostTheme = (theme) => {
  window.__hostTheme = theme;
  frame.contentWindow.postMessage({jsonrpc:"2.0",
    method:"ui/notifications/host-context-changed",
    params:{theme: theme}}, "*");
};
window.setFrameWidth = (width) => {
  frame.style.width = width + "px";
};
window.pushEmpty = () => {
  frame.contentWindow.postMessage({jsonrpc:"2.0",
    method:"ui/notifications/tool-result",
    params:{content:[]}}, "*");
};
window.addEventListener("message", (ev) => {
  if (ev.source !== frame.contentWindow) return;
  const m = ev.data;
  if (!m || m.jsonrpc !== "2.0") return;
  if (m.method === "ui/initialize") {
    window.__inits += 1;
    if (window.__dropInit) return;
    // Mirror the REAL host's MCP Apps schema for ui/initialize params
    // (ext-apps app-bridge): appInfo {name,version} and appCapabilities are
    // required objects. The harness used to accept anything, which is exactly
    // how the missing-appInfo defect reached the real Ask host and failed
    // there; now it fails here first.
    const p = m.params || {};
    const ai = p.appInfo;
    const bad = [];
    if (!ai || typeof ai !== "object") bad.push("appInfo");
    else {
      if (typeof ai.name !== "string" || !ai.name) bad.push("appInfo.name");
      if (typeof ai.version !== "string" || !ai.version) bad.push("appInfo.version");
    }
    if (!p.appCapabilities || typeof p.appCapabilities !== "object") bad.push("appCapabilities");
    if (typeof p.protocolVersion !== "string") bad.push("protocolVersion");
    if (bad.length) {
      window.__initErrors.push("invalid ui/initialize params: " + bad.join(", "));
      ev.source.postMessage({jsonrpc:"2.0", id:m.id,
        error:{code:-32602, message:"Invalid params: " + bad.join(", ")}}, "*");
      return;
    }
    ev.source.postMessage({jsonrpc:"2.0", id:m.id,
      result:{protocolVersion:"2026-01-26",
        hostContext:{theme:window.__hostTheme,
          containerDimensions:{maxHeight:720}}}}, "*");
    return;
  }
  if (m.method === "ui/notifications/initialized") {
    ev.source.postMessage({jsonrpc:"2.0",
      method:"ui/notifications/tool-input",
      params:{arguments:{view_mode:"3d"}}}, "*");
    ev.source.postMessage({jsonrpc:"2.0",
      method:"ui/notifications/tool-result",
      params:{structuredContent:window.__route}}, "*");
    window.__ready = true;
    return;
  }
  if (m.method === "tools/call") {
    const name = m.params && m.params.name;
    const args = (m.params && m.params.arguments) || {};
    window.__calls.push({name, arguments:args});
    let result = {content:[{type:"text", text:"ok"}]};
    if (name === "get_location_detail") {
      result = {structuredContent:{id:args.place_id, name:"Test place",
        floor:"5F", point:[4065,1270], call_number:null,
        other_floors:[]}};
    } else if (name === "get_map_view") {
      result = {content:[{type:"image", mimeType:"image/webp",
        data:TINY_WEBP}]};
    }
    ev.source.postMessage({jsonrpc:"2.0", id:m.id, result}, "*");
  }
});
</script></body></html>"""
    return (host.replace("__PREVIEW__", preview_url)
                .replace("__ROUTE__", json.dumps(route))
                .replace("__TINY_WEBP__", tiny_webp))


def _wait_frame(page, preview_url):
    page.wait_for_function("window.__ready === true", timeout=8000)
    bad = page.evaluate("window.__initErrors || []")
    if bad:
        raise AssertionError("ui/initialize failed host-schema validation: "
                             + "; ".join(bad))
    for frame in page.frames:
        if frame.url.startswith(preview_url):
            frame.wait_for_selector("#map")
            return frame
    raise AssertionError("preview iframe did not appear")


def _settle(frame):
    frame.evaluate("""() => new Promise((resolve) => {
      requestAnimationFrame(() => requestAnimationFrame(resolve));
    })""")


def _viewbox(frame):
    return frame.evaluate("""() => {
      const s = document.querySelector("#map");
      const v = s.viewBox.baseVal;
      return {x:v.x, y:v.y, w:v.width, h:v.height,
              raw:s.getAttribute("viewBox")};
    }""")


def _bbox(frame, selector="#route"):
    return frame.evaluate("""(sel) => {
      const el = document.querySelector(sel);
      if (!el) return null;
      const b = el.getBBox();
      return {x:b.x, y:b.y, w:b.width, h:b.height};
    }""", selector)


def _visible_bbox(frame):
    return frame.evaluate("""() => {
      const nodes = document.querySelectorAll(
        "#map polygon, #map polyline, #map circle, #map text, #map line");
      let x0=Infinity, y0=Infinity, x1=-Infinity, y1=-Infinity;
      nodes.forEach((n) => {
        const b=n.getBBox();
        if (!b || !isFinite(b.x) || !isFinite(b.y)) return;
        x0=Math.min(x0,b.x); y0=Math.min(y0,b.y);
        x1=Math.max(x1,b.x+b.width); y1=Math.max(y1,b.y+b.height);
      });
      if (!isFinite(x0)) return null;
      return {x:x0, y:y0, w:x1-x0, h:y1-y0};
    }""")


def _contains(outer, inner, tol=0.5):
    return (inner["x"] >= outer["x"] - tol and
            inner["y"] >= outer["y"] - tol and
            inner["x"] + inner["w"] <= outer["x"] + outer["w"] + tol and
            inner["y"] + inner["h"] <= outer["y"] + outer["h"] + tol)


def _screenshot(frame, name):
    path = ARTIFACTS / name
    frame.locator("#stage").screenshot(path=str(path))
    return path


def _blue_pixels(path):
    with Image.open(path) as img:
        img = img.convert("RGB")
        return sum(1 for r, g, b in img.getdata()
                   if b >= 180 and b > r + 60 and b > g + 20)


def _assert_layout(frame):
    data = frame.evaluate("""() => {
      const ids = ["floors", "mode3d", "reset"];
      const rects = ids.map((id) => {
        const r = document.getElementById(id).getBoundingClientRect();
        return {id, left:r.left, right:r.right, top:r.top, bottom:r.bottom};
      });
      const de = document.documentElement;
      const stage = document.getElementById("stage").getBoundingClientRect();
      return {clientWidth:de.clientWidth, scrollWidth:de.scrollWidth,
              rects, stage:{w:stage.width, h:stage.height}};
    }""")
    assert data["scrollWidth"] <= data["clientWidth"] + 1, data
    assert data["stage"]["w"] > 300 and data["stage"]["h"] >= 180, data
    first = data["rects"][0]
    first_center = (first["top"] + first["bottom"]) / 2
    for r in data["rects"]:
        assert r["left"] >= 0 and r["right"] <= data["clientWidth"] + 0.5, data
        center = (r["top"] + r["bottom"]) / 2
        assert abs(center - first_center) < 1.5, data
    for i, a in enumerate(data["rects"]):
        for b in data["rects"][i + 1:]:
            assert a["right"] <= b["left"] + 0.5 or b["right"] <= a["left"] + 0.5, data


def _dispatch_wheel(frame, delta_y):
    return frame.evaluate("""(dy) => {
      const s = document.querySelector("#map");
      const r = s.getBoundingClientRect();
      const ev = new WheelEvent("wheel", {
        deltaY: dy, clientX: r.left + r.width / 2,
        clientY: r.top + r.height / 2, bubbles: true, cancelable: true,
      });
      return s.dispatchEvent(ev);
    }""", delta_y)


def _assert_preview_fresh():
    preview = ROOT / "dev" / "preview.html"
    sources = [ROOT / "src" / "view" / "viewer.js",
               ROOT / "src" / "view" / "template.py"]
    if not preview.exists():
        raise AssertionError(
            "dev/preview.html missing: run python scripts/build_preview.py")
    newest = max(p.stat().st_mtime for p in sources)
    if preview.stat().st_mtime < newest:
        raise AssertionError(
            "dev/preview.html is stale: run python scripts/build_preview.py")


def main():
    _assert_preview_fresh()
    route = library.get_route("N607").model_dump()
    preview_url = ""
    server = None
    try:
        server, preview_url = _start_server()
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            context = browser.new_context(
                viewport={"width": 500, "height": 760},
                device_scale_factor=1, color_scheme="dark",
                reduced_motion="reduce")
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
            page.on("console", lambda m: errors.append(
                f"console: {m.text}") if m.type == "error" else None)
            page.set_content(_host_html(
                preview_url, route, _tiny_webp()))
            frame = _wait_frame(page, preview_url)
            _settle(frame)
            frame.wait_for_function(
                "document.querySelectorAll('#map polyline').length >= 2",
                timeout=8000)
            _settle(frame)

            # 1. 3D is visible in a real layout.
            vb3d = _viewbox(frame)
            bbox3d = _visible_bbox(frame)
            assert bbox3d and bbox3d["w"] > 0 and bbox3d["h"] > 0, bbox3d
            assert _contains(vb3d, bbox3d, tol=2), (vb3d, bbox3d)
            path3d = _screenshot(frame, "402-3d-light.png")
            assert _blue_pixels(path3d) > 20, "3D screenshot has no blue route"
            assert frame.evaluate("""() => {
              const s = document.querySelector("#map");
              return s.viewBox.baseVal.width > 0 &&
                s.viewBox.baseVal.height > 0;
            }""")
            assert frame.evaluate("document.documentElement.dataset.theme") == "light"

            # 2. 3D must not let map gestures rewrite the projected viewBox.
            before = frame.evaluate(
                "document.querySelector('#map').getAttribute('viewBox')")
            _dispatch_wheel(frame, -300)
            frame.evaluate("""() => {
              const s = document.querySelector("#map");
              s.dispatchEvent(new PointerEvent("pointerdown", {
                pointerId:1, clientX:100, clientY:100, bubbles:true}));
              s.dispatchEvent(new PointerEvent("pointermove", {
                pointerId:1, clientX:20, clientY:20, bubbles:true}));
            }""")
            after = frame.evaluate(
                "document.querySelector('#map').getAttribute('viewBox')")
            assert before == after, (before, after)

            # 3. Flat 5F frame is route-focused, not the legend band.
            frame.locator("#mode3d").click()
            frame.wait_for_function(
                "document.querySelector('#mode3d').textContent.trim() === '2D'")
            _settle(frame)
            vb5 = _viewbox(frame)
            route5 = _bbox(frame)
            assert _contains(vb5, route5, tol=2), (vb5, route5)
            assert vb5["w"] * vb5["h"] < 1515 * 1051, vb5
            path5 = _screenshot(frame, "402-2d-5f.png")
            assert _blue_pixels(path5) > 20, "5F screenshot has no blue route"

            # 4. 6F framing excludes the ~55% legend band.
            frame.locator("#floors button").nth(1).click()
            frame.wait_for_function(
                "document.querySelector('#floors button[aria-pressed=\"true\"]').textContent === '6F'")
            _settle(frame)
            vb6 = _viewbox(frame)
            route6 = _bbox(frame)
            assert _contains(vb6, route6, tol=2), (vb6, route6)
            assert vb6["h"] < 1011 * 0.55, vb6
            path6 = _screenshot(frame, "402-2d-6f.png")
            assert _blue_pixels(path6) > 20, "6F screenshot has no blue route"

            # 5. 2D wheel zooms, and Reset restores the requested 3D frame.
            width_before = vb6["w"]
            _dispatch_wheel(frame, -300)
            frame.wait_for_function(
                "document.querySelector('#map').viewBox.baseVal.width < " +
                str(width_before - 0.5))
            frame.locator("#reset").click()
            _settle(frame)
            assert frame.evaluate(
                "document.querySelector('#mode3d').textContent.trim() === '3D'")
            assert frame.evaluate(
                "document.querySelector('#floors button[aria-pressed=\"true\"]').textContent === '5F'")
            assert _viewbox(frame)["raw"] == vb3d["raw"], (
                vb3d["raw"], _viewbox(frame)["raw"])

            # 6. Marker detail uses the advertised place_id argument.
            frame.locator("#mode3d").click()
            _settle(frame)
            marker = frame.locator("circle[data-pid]").first
            assert marker.count() == 1, "no clickable marker"
            marker.click()
            page.wait_for_function(
                "window.__calls.some((c) => c.name === 'get_location_detail')",
                timeout=5000)
            calls = page.evaluate("window.getCalls()")
            detail = next(c for c in calls if c["name"] == "get_location_detail")
            assert list(detail["arguments"]) == ["place_id"], detail
            assert detail["arguments"]["place_id"] == (
                route["origin"]["id"] if route.get("origin") else None), detail
            assert frame.evaluate("document.querySelector('#detail').hidden") is False

            # 7. Host light wins over OS dark, and a pushed update is applied.
            assert frame.evaluate(
                "getComputedStyle(document.body).backgroundColor") == "rgb(255, 255, 255)"
            page.evaluate("window.setHostTheme('dark')")
            frame.wait_for_function(
                "document.documentElement.dataset.theme === 'dark'")
            assert frame.evaluate(
                "getComputedStyle(document.body).backgroundColor") == "rgb(28, 28, 30)"
            page.evaluate("window.setHostTheme('light')")
            frame.wait_for_function(
                "document.documentElement.dataset.theme === 'light'")
            assert page.evaluate("window.__inits") == 1, "theme update rebooted app"

            # 8. 402px layout and the production-only UI surface.
            _assert_layout(frame)
            assert frame.evaluate("""() => ["ping","tilt","retry","status"]
              .every((id) => document.getElementById(id) === null)""")
            assert frame.evaluate(
                "document.querySelectorAll('#detail ol').length === 0")
            text = frame.evaluate("document.body.innerText")
            assert "Follow the route" not in text
            assert "green marker" not in text

            # 9. Narrow, breakpoint, and wide layouts must all remain usable.
            for width in (360, 402, 760, 1100):
                page.evaluate("window.setFrameWidth(%d)" % width)
                _settle(frame)
                _assert_layout(frame)
            page.evaluate("window.setFrameWidth(402)")
            _settle(frame)

            # 10. Deep zoom requests a tile; it stays below the route layer.
            # The detail test left the view in 2D mode.
            for _ in range(12):
                _dispatch_wheel(frame, -500)
            frame.wait_for_selector("#tiles image", timeout=5000)
            order = frame.evaluate("""() => {
              const svg = document.querySelector("#map");
              return Array.from(svg.children).map((n) => n.id || n.tagName);
            }""")
            assert order.index("tiles") < order.index("route"), order

            # 11. Empty tool result must produce an actionable error, not a
            # blank map. The error bar offers Reset.
            page.evaluate("window.pushEmpty()")
            frame.wait_for_function(
                "document.querySelector('#errorbar').style.display === 'block'")
            assert "Reset" in frame.locator("#errorbar").inner_text()

            # 12. Missing host theme defaults to light even with OS dark.
            page3 = context.new_page()
            page3.set_content(_host_html(
                preview_url, route, _tiny_webp()).replace(
                    'window.__hostTheme = "light";',
                    'window.__hostTheme = null;'))
            frame3 = page3.locator("#view").element_handle().content_frame()
            if frame3 is None:
                raise AssertionError("missing-theme iframe did not load")
            page3.wait_for_function("window.__ready === true", timeout=8000)
            assert frame3.evaluate("document.documentElement.dataset.theme") == "light"
            assert frame3.evaluate(
                "getComputedStyle(document.body).backgroundColor") == "rgb(255, 255, 255)"
            page3.close()

            # 13. Handshake failure gets a Reconnect action, never a fake
            # map-control Retry.
            page2 = context.new_page()
            page2.set_content(_host_html(
                preview_url, route, _tiny_webp()).replace(
                    "window.__dropInit = false;",
                    "window.__dropInit = true;"))
            frame2 = page2.locator("#view").element_handle().content_frame()
            if frame2 is None:
                raise AssertionError("handshake test iframe did not load")
            frame2.wait_for_function(
                "document.querySelector('#errorbar').innerText.includes('Reconnect')",
                timeout=7000)
            assert "Retry" not in frame2.locator("#errorbar").inner_text()
            page2.close()

            assert not errors, errors
            browser.close()
    finally:
        if server:
            server.shutdown()
            server.server_close()
    print("BROWSER TEST OK")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"BROWSER TEST FAILED: {e}", file=sys.stderr)
        raise
