"""Single-file MCP App view builder (plan Step 5-1).

build_view_html() returns one self-contained HTML document: the
downsampled base maps (src/static/*_small.webp, built by
scripts/build_static.py) as data URIs + viewer.js + CSS, zero external
links. Served as the ui://nav/library-view.html MCP resource.
"""

import base64
import json
import os

# template.py lives one level deeper (src/view/) than library.py, hence 3x dirname.
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
VIEW_DIR = os.path.join(BASE_DIR, 'src', 'view')
STATIC_DIR = os.path.join(BASE_DIR, 'src', 'static')

from view.plan_outline import PLAN_OUTLINE  # noqa: E402

# (floor, small-webp file, full-res base file). Full-res sizes are read
# from disk at build time -- never hardcode them (a re-cropped base map
# must not silently desync the route overlay).
MAPS = (
    ("5F", "5f_small.webp", "5f_base.jpg"),
    ("6F", "6f_small.webp", "6f_base.jpg"),
)

CSS = """
/* ---- design tokens ---------------------------------------------------- */
:root{
  color-scheme:light;
  --surface:#ffffff; --surface-2:#f5f6f8;
  --border:#e5e5e5; --text:#171717; --muted:#6b7280;
  --accent:#1e6fff; --danger:#d9262c; --danger-bg:#fdecec;
  --r-sm:6px; --r-md:8px; --r-lg:12px;
  --sp-1:4px; --sp-2:8px; --sp-3:12px; --sp-4:16px;
  --shadow-1:0 1px 2px rgba(0,0,0,.05); --shadow-2:0 6px 18px rgba(0,0,0,.10);
  --fs-1:12px; --fs-2:13px; --fs-3:15px;
}
[data-theme="dark"]{
  color-scheme:dark;
  --surface:#1c1c1e; --surface-2:#2c2c2e;
  --border:rgba(255,255,255,.10); --text:#fafafa; --muted:#98989d;
  --accent:#3d7bff; --danger:#ff6b6b; --danger-bg:#3a1d1f;
  --shadow-1:0 1px 2px rgba(0,0,0,.40); --shadow-2:0 6px 18px rgba(0,0,0,.55);
}
*,*::before,*::after{box-sizing:border-box}
/* ---- height chain ------------------------------------------------------ */
html{height:100%}
body{min-height:100%;margin:0;font-family:system-ui,sans-serif;
  display:flex;flex-direction:column;background:var(--surface);color:var(--text);
  min-width:0;overflow-x:hidden}
#app{flex:1 1 auto;min-width:0;min-height:0;display:flex;flex-direction:column}
/* ---- controls ------------------------------------------------------------ */
button{font:inherit;color:var(--text);background:var(--surface);
  border:1px solid var(--border);border-radius:var(--r-sm);
  padding:2px 9px;cursor:pointer;white-space:nowrap}
button:hover{border-color:var(--muted)}
button:focus-visible{outline:2px solid var(--accent);outline-offset:1px}
button[disabled]{opacity:.5;cursor:default}
#toolbar{display:flex;gap:6px;align-items:center;padding:5px 8px;
  border-bottom:1px solid var(--border);flex:none;flex-wrap:nowrap;
  min-width:0;overflow:hidden;background:var(--surface)}
#floors{display:flex;gap:2px;background:var(--surface-2);
  border:1px solid var(--border);border-radius:var(--r-md);padding:2px;
  flex:none}
#floors button{border:none;background:transparent;color:var(--muted);
  border-radius:calc(var(--r-md) - 2px);padding:2px 8px}
#floors button[aria-pressed="true"]{background:var(--surface);color:var(--text);
  font-weight:600;box-shadow:var(--shadow-1)}
#mode3d{border-radius:999px;padding:3px 10px;color:var(--muted);flex:none}
#mode3d[aria-pressed="true"]{background:var(--accent);border-color:var(--accent);
  color:#fff;font-weight:600}
#reset{margin-left:auto;flex:none;color:var(--muted)}
#reset:hover{color:var(--text)}
/* ---- layout ---------------------------------------------------------------- */
#main{flex:1 1 auto;min-width:0;min-height:0;display:flex;overflow:hidden}
#stage{flex:1 1 auto;min-width:0;min-height:240px;position:relative;
  /* fallback ratio until JS writes the real viewBox ratio (first paint,
     JSON mode, degraded view) */
  height:auto;align-self:flex-start;aspect-ratio:1.9}
#stage svg{width:100%;height:100%;display:block;touch-action:none}
#detail{position:absolute;left:8px;right:8px;bottom:8px;z-index:2;
  max-height:55%;overflow:auto;padding:var(--sp-2);
  background:var(--surface);border:1px solid var(--border);
  border-radius:var(--r-md);box-shadow:var(--shadow-2);
  font-size:var(--fs-2);color:var(--text)}
#detail[hidden]{display:none}
#detail h4{margin:0 0 var(--sp-1);font-size:var(--fs-3)}
#detail ul{margin:var(--sp-1) 0;padding-left:18px}
#detail button{margin-top:var(--sp-2)}
/* ---- alerts ---------------------------------------------------------------- */
#errorbar{display:none;background:var(--danger-bg);border:1px solid var(--danger);
  border-radius:var(--r-md);padding:var(--sp-2) var(--sp-3);
  margin:var(--sp-2) var(--sp-2) 0;font-size:var(--fs-2);color:var(--text)}
#errorbar button{margin-left:var(--sp-2)}
@media (max-width:760px){
  #stage{min-height:240px}
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
        with Image.open(os.path.join(BASE_DIR, "maps", full)) as img:
            fw, fh = img.size
        maps[floor] = {"href": uri, "w": sw, "h": sh, "fw": fw, "fh": fh,
                       "outline": PLAN_OUTLINE[floor]}
    # Wall segments per floor, in full-res pixel coords. The 3D view extrudes
    # them into vertical faces so the model reads as architecture rather than
    # a tilted picture; the flat 2D view ignores them. They come from
    # corridors.json, the same geometry the router treats as impassable.
    walls = {}
    try:
        import library
        for floor in maps:
            walls[floor] = [
                [[float(a[0]), float(a[1])], [float(b[0]), float(b[1])]]
                for a, b in library.CORRIDORS[floor].get("walls", [])
            ]
    except Exception:                      # pragma: no cover - defensive
        walls = {floor: [] for floor in maps}
    manifest = json.dumps({"maps": maps, "walls": walls})
    return f"""<!doctype html>
<html lang="en" data-theme="light">
<head>
<meta charset="utf-8">
<meta name="color-scheme" content="light">
<title>library route view</title>
<style>{CSS}</style>
</head>
<body>
<div id="app">
  <div id="toolbar">
    <div class="floors" id="floors" role="tablist" aria-label="Floor"></div>
    <button id="mode3d" type="button" aria-pressed="false">3D</button>
    <button id="reset" type="button">Reset</button>
  </div>
  <div id="errorbar" role="alert"></div>
  <div id="main">
    <div id="stage">
      <svg id="map" role="group" aria-label="library route map"></svg>
      <aside id="detail" aria-live="polite" hidden></aside>
    </div>
  </div>
</div>
<script>window.__LIB_VIEW__ = {manifest};</script>
<script>{_viewer_js()}</script>
</body>
</html>"""
