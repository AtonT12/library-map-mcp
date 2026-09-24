"""Single-file MCP App view builder (plan Step 5-1).

build_view_html() returns one self-contained HTML document: the
downsampled base maps (src/static/*_small.webp, built by
scripts/build_static.py) as data URIs + viewer.js + CSS, zero external
links. Served as the ui://nav/library-view.html MCP resource.
"""

import base64
import os

# template.py lives one level deeper (src/view/) than library.py, hence 3x dirname.
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
VIEW_DIR = os.path.join(BASE_DIR, 'src', 'view')
STATIC_DIR = os.path.join(BASE_DIR, 'src', 'static')

# (floor, small-webp file, full-res base file). Full-res sizes are read
# from disk at build time -- never hardcode them (a re-cropped base map
# must not silently desync the route overlay).
MAPS = (
    ("5F", "5f_small.webp", "5f_base.jpg"),
    ("6F", "6f_small.webp", "6f_base.jpg"),
)

CSS = """
html,body{height:100%;margin:0}
body{font-family:system-ui,sans-serif;display:flex;flex-direction:column}
#app{flex:1 1 auto;min-height:0;display:flex;flex-direction:column}
#toolbar{display:flex;gap:8px;align-items:center;padding:6px 10px;
  border-bottom:1px solid #888;flex:none;flex-wrap:wrap}
#toolbar .floors{display:flex;gap:4px}
#toolbar button{padding:2px 10px}
#toolbar button[aria-pressed="true"]{font-weight:bold}
#main{flex:1 1 auto;min-height:0;display:flex;overflow:hidden}
#stage{flex:1 1 auto;min-height:0;min-width:0;position:relative}
#stage svg{width:100%;height:100%;display:block;touch-action:none}
#side{width:260px;flex:none;border-left:1px solid #888;padding:8px;
  overflow-y:auto;font-size:13px}
#side h4{margin:6px 0 4px}
#side ol{margin:4px 0;padding-left:20px}
#errorbar{display:none;background:#fde8e8;border:1px solid #c00;
  padding:8px;margin:6px 10px 0}
#status{color:#555;padding:2px 10px;font-size:12px;flex:none}
#tilt{width:120px}
@media (prefers-color-scheme:dark){
  #side{border-color:#555}#toolbar{border-color:#555}
}
""".strip()


def _data_uri(path):
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"{path} missing: run `python scripts/build_static.py` first.")
    with open(path, "rb") as f:
        return "data:image/webp;base64," + base64.b64encode(f.read()).decode()


def _viewer_js():
    path = os.path.join(VIEW_DIR, "viewer.js")
    with open(path, encoding="utf-8") as f:
        return f.read()


def build_view_html():
    import json
    from PIL import Image
    maps = {}
    for floor, small, full in MAPS:
        uri = _data_uri(os.path.join(STATIC_DIR, small))
        with Image.open(os.path.join(STATIC_DIR, small)) as img:
            sw, sh = img.size
        with Image.open(os.path.join(BASE_DIR, full)) as img:
            fw, fh = img.size
        maps[floor] = {"href": uri, "w": sw, "h": sh, "fw": fw, "fh": fh}
    manifest = json.dumps({"maps": maps})
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="color-scheme" content="light dark">
<title>library route view</title>
<style>{CSS}</style>
</head>
<body>
<div id="app">
  <div id="toolbar">
    <div class="floors" id="floors" role="tablist"></div>
    <button id="mode3d" type="button" aria-pressed="false">3D</button>
    <label>tilt <input id="tilt" type="range" min="0.45" max="1" step="0.01" value="0.62"></label>
    <button id="ping" type="button">ping</button>
    <button id="retry" type="button">retry</button>
  </div>
  <div id="errorbar" role="alert"></div>
  <div id="main">
    <div id="stage"><svg id="map" role="img" aria-label="library route map"></svg></div>
    <aside id="side"><div id="status">booting…</div><div id="detail"></div></aside>
  </div>
</div>
<script>window.__LIB_VIEW__ = {manifest};</script>
<script>{_viewer_js()}</script>
</body>
</html>"""
